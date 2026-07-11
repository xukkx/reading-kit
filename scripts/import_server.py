#!/usr/bin/env python3
"""
import_server.py — local web console for the import_book.py pipeline.

Serves a single-page Chinese UI (embedded HTML, zero external requests) that
wraps scripts/import_book.py: submit an import (正式导入 / 演练 / 仅质检),
watch a strictly-sequential job queue with live logs, and read the quality-gate
report (verified ratio, per-page failures, escalation guidance) as cards.

Per this repo's convention the script never imports its siblings: import_book.py
is driven via subprocess and all state passes through the filesystem
(staging/_jobs/<job_id>/job.json + job.log).

v1.5 adds a human adjudication screen (人工精校): review staging pages that
have transcripts but no verified file (adjudicate/escalated/error), compare
the page image against the model A/B transcripts, pick one or write a final
text. Saving writes staging/<slug>/verified/pg-XXXX.md and appends a
status=verified line to staging/ledger.jsonl in ingest.py's field shape, so
the quality gate (import_book.py) and reflow.py count the page as verified
with zero changes downstream.

Default port: this ROOT's ragPort in ~/.reading-kit/registry.json + 100
(complit 8866 / zhangxianyi 8867); unregistered roots fall back to 8830.

Usage:
  python scripts/import_server.py [--port N] [--root PATH]
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
IMPORT_BOOK = Path(__file__).resolve().parent / "import_book.py"
REGISTRY = Path.home() / ".reading-kit" / "registry.json"
FALLBACK_PORT = 8830
MODES = ("import", "dry-run", "gate-only")
TERMINAL = ("passed", "escalated", "error", "cancelled")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def sanitize_slug(text):
    """Same rule as import_book.py (kept as a duplicate, not an import, per
    repo convention): word chars (incl. CJK) and dashes only."""
    s = re.sub(r"[^\w\-]+", "-", text).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s.lower() or "book"


def registry_entry(root):
    """This root's entry in ~/.reading-kit/registry.json, or None."""
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    for p in data.get("projects", []):
        try:
            if Path(p["path"]).resolve() == root.resolve():
                return p
        except (KeyError, OSError):
            continue
    return None


# ---------- gate-report parsing (from import_book.py's printed lines) ----------

def parse_gate(text):
    """Best-effort parse of import_book.py's [gate] block (plus the final
    'vault note:' line) out of a job log. Returns a dict for job.json's "gate"
    field, or None when no gate lines are present (e.g. dry-run, early error)."""
    gate = {}
    m = re.search(r"\[gate\] QUALITY GATE: (PASS|FAIL)", text)
    if m:
        gate["pass"] = m.group(1) == "PASS"
    m = re.search(r"\[gate\] slug=(\S+) range=(\d+)-(\d+) pages=(\d+)", text)
    if m:
        gate["slug"] = m.group(1)
        gate["first"], gate["last"] = int(m.group(2)), int(m.group(3))
        gate["total"] = int(m.group(4))
    m = re.search(r"\[gate\] verified=(\d+)/(\d+) ratio=([\d.]+) threshold=([\d.]+)", text)
    if m:
        gate["verified"] = int(m.group(1))
        gate.setdefault("total", int(m.group(2)))
        gate["ratio"] = float(m.group(3))
        gate["threshold"] = float(m.group(4))
    m = re.search(r"\[gate\] ledger statuses: verified=(\d+) adjudicate=(\d+) "
                  r"escalated=(\d+) error=(\d+) missing=(\d+)", text)
    if m:
        gate["tally"] = {"verified": int(m.group(1)), "adjudicate": int(m.group(2)),
                         "escalated": int(m.group(3)), "error": int(m.group(4)),
                         "missing": int(m.group(5))}
    failed = []
    # [^\S\n] = horizontal whitespace only: a page line whose note is empty must
    # not swallow the next line (plain \s+ crosses the newline).
    for m in re.finditer(r"^\[gate\][^\S\n]+(pg-\d+)[^\S\n]+(\S+)[^\S\n]+"
                         r"sim=(\S+)(?:[^\S\n]+(.*))?$", text, re.M):
        sim = m.group(3)
        if sim == "-":
            sim = None
        else:
            try:
                sim = float(sim)
            except ValueError:
                pass  # keep the raw string
        failed.append({"page": m.group(1), "status": m.group(2),
                       "similarity": sim, "note": (m.group(4) or "").strip()})
    if failed:
        gate["failed"] = failed
    m = re.search(r"^vault note:\s+(.+)$", text, re.M)
    if m:
        gate["vault_note"] = m.group(1).strip()
    return gate or None


# ---------- human adjudication (人工精校, v1.5) ----------
#
# A page is "pending" when staging/<slug>/transcripts/ has an A and/or B
# transcript for it but staging/<slug>/verified/<page>.md does not exist.
# Saving an adjudication writes the verified file and appends a ledger line in
# ingest.py's field shape, which is exactly what the downstream quality gate
# (import_book.py) and reflow.py consume.

ADJ_LOCK = threading.Lock()
LABEL_RE = re.compile(r"[\w\-]+$")   # pg-0042, img-0001-foo; no dots/slashes


def utc_iso():
    """Same ledger timestamp shape as ingest.py's now()."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_within(path, base):
    """True if resolved `path` equals or lies inside resolved `base`."""
    s, b = str(path), str(base)
    return s == b or s.startswith(b.rstrip("\\/") + os.sep)


def ledger_by_pages_dir(staging):
    """Parse staging/ledger.jsonl once into {resolved pages-dir: {page label:
    last-wins record}}. Slug attribution uses the image-parent-dir rule, the
    same rule import_book.py's slug_ledger uses (kept as a duplicate, not an
    import, per repo convention); lines without a usable image path (e.g.
    free-form manual-correction lines) are skipped, as they are there."""
    out = {}
    ledger = staging / "ledger.jsonl"
    if not ledger.exists():
        return out
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    cache = {}  # dirname string -> resolved dir string ("" = unusable)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        img, page = rec.get("image"), rec.get("page")
        if not img or not page:
            continue
        d = os.path.dirname(str(img))
        parent = cache.get(d)
        if parent is None:
            try:
                parent = str(Path(d).resolve()) if d else ""
            except OSError:
                parent = ""
            cache[d] = parent
        if parent:
            out.setdefault(parent, {})[page] = rec
    return out


def slug_records(staging, slug):
    """Last-wins ledger records for one slug, keyed by page label."""
    return ledger_by_pages_dir(staging).get(
        str((staging / slug / "pages").resolve()), {})


def pending_labels(staging, slug):
    """Sorted page labels with at least one transcript but no verified file."""
    tdir = staging / slug / "transcripts"
    vdir = staging / slug / "verified"
    labels = set()
    if tdir.is_dir():
        for f in tdir.iterdir():
            if f.name.endswith((".a.md", ".b.md")):
                labels.add(f.name[:-5])
    return sorted(l for l in labels if not (vdir / f"{l}.md").exists())


def find_page_image(staging, slug, label, rec):
    """Image file for a page: the ledger record's path when it exists inside
    staging, else the pages/ file with the matching page number (pdftoppm pads
    filenames by the PDF's total page digits, so label pg-0010's file may be
    pg-010.png), else any pages/ file whose stem equals the label."""
    if rec and rec.get("image"):
        try:
            p = Path(rec["image"]).resolve()
            if p.is_file() and is_within(p, staging.resolve()):
                return p
        except OSError:
            pass
    pages = staging / slug / "pages"
    if not pages.is_dir():
        return None
    m = re.fullmatch(r"pg-0*(\d+)", label)
    if m:
        n = int(m.group(1))
        for f in sorted(pages.glob("pg-*.png")):
            m2 = re.fullmatch(r"pg-0*(\d+)", f.stem)
            if m2 and int(m2.group(1)) == n:
                return f.resolve()
    for f in sorted(pages.iterdir()):
        if f.is_file() and f.stem == label:
            return f.resolve()
    return None


def staging_url(staging, path):
    """/staging/... URL for an absolute path inside STAGING."""
    rel = path.relative_to(staging.resolve()).as_posix()
    return "/staging/" + quote(rel)


def adj_summary(staging):
    """GET /api/adjudicate: slugs with pages awaiting human review."""
    out = []
    if not staging.is_dir():
        return out
    ledger = ledger_by_pages_dir(staging)
    for d in sorted(staging.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        pending = pending_labels(staging, d.name)
        if not pending:
            continue
        recs = ledger.get(str((d / "pages").resolve()), {})
        by = {}
        for label in pending:
            rec = recs.get(label)
            st = rec.get("status", "missing") if rec else "missing"
            by[st] = by.get(st, 0) + 1
        out.append({"slug": d.name, "pending": len(pending), "by_status": by})
    return out


def adj_pages(staging, slug):
    """GET /api/adjudicate/<slug>: ordered pending pages, or None -> 404."""
    if not LABEL_RE.fullmatch(slug) or not (staging / slug).is_dir():
        return None
    recs = slug_records(staging, slug)
    tdir = staging / slug / "transcripts"
    pages = []
    for label in pending_labels(staging, slug):
        rec = recs.get(label)
        img = find_page_image(staging, slug, label, rec)
        a_f, b_f = tdir / f"{label}.a.md", tdir / f"{label}.b.md"
        d_f = tdir / f"{label}.diff"
        has_diff = d_f.exists()
        pages.append({
            "page": label,
            "status": rec.get("status", "missing") if rec else "missing",
            "similarity": rec.get("similarity") if rec else None,
            "note_or_error": (rec.get("note") or rec.get("error")) if rec else None,
            "has_diff": has_diff,
            "image_url": staging_url(staging, img) if img else None,
            "a_url": staging_url(staging, a_f.resolve()) if a_f.exists() else None,
            "b_url": staging_url(staging, b_f.resolve()) if b_f.exists() else None,
            "diff_url": staging_url(staging, d_f.resolve()) if has_diff else None,
        })
    return pages


def adj_save(staging, slug, page, body):
    """POST /api/adjudicate/<slug>/<page>: write verified/<page>.md + append a
    verified ledger line (ingest.py field shape). Returns (json, http code)."""
    if not LABEL_RE.fullmatch(slug) or not (staging / slug).is_dir():
        return {"error": f"staging 里没有 slug '{slug}'"}, 404
    if not LABEL_RE.fullmatch(page):
        return {"error": "页码标签不合法"}, 400
    action = body.get("action")
    if action not in ("pick_a", "pick_b", "custom"):
        return {"error": "action 必须是 pick_a / pick_b / custom"}, 400
    text = body.get("text")
    if text is not None and not isinstance(text, str):
        return {"error": "text 必须是字符串"}, 400
    if text is not None and not text.strip():
        text = None                      # blank text = not provided
    if action == "custom" and text is None:
        return {"error": "custom 需要非空 text"}, 400
    sdir = staging / slug
    vfile = sdir / "verified" / f"{page}.md"
    tdir = sdir / "transcripts"
    with ADJ_LOCK:
        if vfile.exists():
            return {"error": f"{page} 已有 verified 文件，无需重复精校"}, 409
        if text is not None:             # explicit text always wins
            content = text
        else:
            src = tdir / f"{page}.{'a' if action == 'pick_a' else 'b'}.md"
            if not src.exists():
                return {"error": f"转录文件不存在：{src.name}"}, 400
            content = src.read_text(encoding="utf-8")
        prior = slug_records(staging, slug).get(page)
        img = find_page_image(staging, slug, page, prior)
        # Mirror ingest.py's record shape; carry provenance from the page's
        # last ledger line when available. The image path is what attributes
        # the line to this slug downstream, so keep the prior one verbatim.
        rec = {"ts": utc_iso(),
               "source": prior.get("source") if prior else None,
               "page": page,
               "image": (prior.get("image") if prior and prior.get("image")
                         else str(img) if img
                         else str(sdir / "pages" / f"{page}.png")),
               "model_a": prior.get("model_a") if prior else None,
               "model_b": prior.get("model_b") if prior else None}
        for k in ("similarity", "containment"):
            if prior and k in prior:
                rec[k] = prior[k]
        rec["status"] = "verified"
        rec["note"] = f"human adjudicated: {action}"
        vfile.parent.mkdir(parents=True, exist_ok=True)
        vfile.write_text(content, encoding="utf-8")
        with open(staging / "ledger.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        remaining = len(pending_labels(staging, slug))
    return {"ok": True, "page": page, "remaining": remaining}, 200


# ---------- job manager (one worker thread, strictly sequential) ----------

class JobManager:
    def __init__(self, root):
        self.root = root
        self.vault = root / "vault"
        self.staging = root / "staging"
        self.input_dir = root / "Input"
        self.jobs_dir = self.staging / "_jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.jobs = {}          # id -> job dict (in-memory source of truth)
        self._procs = {}        # id -> Popen while running
        self.q = queue.Queue()
        self.restored = self._restore()
        threading.Thread(target=self._worker, daemon=True).start()

    # -- persistence --

    def _persist(self, job):
        d = self.jobs_dir / job["id"]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, d / "job.json")

    def _restore(self):
        """Load job history from disk. Jobs a previous server life left in
        queued/running can never finish -> mark them error."""
        n = 0
        if not self.jobs_dir.exists():
            return 0
        for d in sorted(self.jobs_dir.iterdir()):
            jf = d / "job.json"
            if not (d.is_dir() and jf.exists()):
                continue
            try:
                job = json.loads(jf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(job, dict) or "id" not in job:
                continue
            if job.get("state") in ("queued", "running"):
                job["state"] = "error"
                job["note"] = "server restarted"
                job["finished"] = job.get("finished") or now_iso()
                job["pid"] = None
                self._persist(job)
            self.jobs[job["id"]] = job
            n += 1
        return n

    # -- API-facing operations --

    def info(self, project, rag_port):
        pdfs = []
        if self.input_dir.exists():
            for f in sorted(self.input_dir.rglob("*.pdf")):
                pdfs.append(str(f.relative_to(self.root)))
        for f in sorted(self.root.glob("*.pdf")):
            pdfs.append(f.name)
        cols = []
        books = self.vault / "Books"
        if books.exists():
            for e in sorted(books.iterdir()):
                try:
                    if e.is_dir() and any(c.is_dir() for c in e.iterdir()):
                        cols.append(e.name)
                except OSError:
                    continue
        return {"project": project, "root": str(self.root), "ragPort": rag_port,
                "collections": cols, "pdfs": pdfs,
                "imported_pdfs": sorted(self.imported_pdfs()),
                "jobs_dir": str(self.jobs_dir)}

    def imported_pdfs(self):
        """Normalized pdf paths of every PASSED full import — the dropdown
        marks these and create_job refuses them without explicit consent."""
        with self.lock:
            return {(j["params"].get("pdf") or "").replace("\\", "/")
                    for j in self.jobs.values()
                    if j["state"] == "passed"
                    and j["params"].get("mode") == "import"
                    and j["params"].get("pdf")}

    def list_jobs(self):
        with self.lock:
            jobs = [dict(j) for j in self.jobs.values()]
        jobs.sort(key=lambda j: (j.get("created") or "", j["id"]), reverse=True)
        return jobs

    def get_job(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                return None
            out = dict(job)
        log = self.jobs_dir / jid / "job.log"
        tail, size = "", 0
        try:
            if log.exists():
                size = log.stat().st_size
                lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = "\n".join(lines[-200:])
        except OSError:
            pass
        out["log_tail"] = tail
        out["log_size"] = size
        return out

    def create_job(self, body):
        """Validate + enqueue. Returns (job, error_message, http_code)."""
        mode = body.get("mode") or "import"
        if mode not in MODES:
            return None, "mode 必须是 import / dry-run / gate-only", 400

        def txt(k):
            v = body.get(k)
            return v.strip() if isinstance(v, str) and v.strip() else None

        pdf, title = txt("pdf"), txt("title")
        collection, slug = txt("collection"), txt("slug")
        if mode == "gate-only":
            if not slug:
                return None, "gate-only 模式必须提供 slug", 400
        else:
            if not title:
                return None, "title 必填（gate-only 模式除外）", 400
            if not pdf:
                return None, "pdf 必填（gate-only 模式除外）", 400

        def intval(k):
            v = body.get(k)
            if v is None or v == "":
                return None, None
            try:
                return int(v), None
            except (TypeError, ValueError):
                return None, f"{k} 必须是整数"

        first, err = intval("first")
        if err:
            return None, err, 400
        last, err = intval("last")
        if err:
            return None, err, 400
        if first is not None and first < 1:
            return None, "first 必须 >= 1", 400
        if first is not None and last is not None and last < first:
            return None, "last 必须 >= first", 400
        ratio = body.get("min_verified_ratio")
        if ratio is None or ratio == "":
            ratio = None
        else:
            try:
                ratio = float(ratio)
            except (TypeError, ValueError):
                return None, "min_verified_ratio 必须是数字", 400
            if not 0 < ratio <= 1:
                return None, "min_verified_ratio 必须在 (0, 1] 内", 400

        job_slug = slug or sanitize_slug(title)

        # full re-import of a book already on the shelf needs explicit consent
        # (legitimate when folding in adjudicated pages — it resumes, not
        # re-OCRs — but never silently: the job history already confuses
        # people enough without unasked-for reruns)
        if mode == "import" and title and not body.get("confirm"):
            books = self.vault / "Books"
            if books.is_dir():
                for cdir in books.iterdir():
                    note = cdir / title / "正文.md"
                    if note.exists():
                        return None, ("CONFIRM:这本书已在架（"
                                      f"Books/{cdir.name}/{title}/正文.md）。"
                                      "重跑会复用已完成的 OCR/共识阶段、"
                                      "更新正文与索引，不会重复花钱。"), 409
            # same PDF under a DIFFERENT title would silently duplicate the
            # book (and re-spend OCR): the title guard can't see it, job
            # history can
            if pdf and pdf.replace("\\", "/") in self.imported_pdfs():
                for jb in self.jobs.values():
                    if (jb["state"] == "passed" and jb["params"].get("mode") == "import"
                            and (jb["params"].get("pdf") or "").replace("\\", "/")
                            == pdf.replace("\\", "/")):
                        return None, ("CONFIRM:这个 PDF 此前已导入为《"
                                      f"{jb['params'].get('title') or jb['slug']}》"
                                      f"（任务 {jb['id']}）。用新书名重跑会生成"
                                      "一本重复的书并重新花 OCR 的钱。"), 409

        with self.lock:
            for jb in self.jobs.values():
                if jb["slug"] == job_slug and jb["state"] in ("queued", "running"):
                    return None, (f"slug '{job_slug}' 已有任务在排队/运行中"
                                  f"（任务 {jb['id']}）"), 409
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "created": now_iso(),
                   "params": {"pdf": pdf, "title": title, "collection": collection,
                              "slug": slug, "first": first, "last": last,
                              "min_verified_ratio": ratio, "mode": mode},
                   "slug": job_slug, "state": "queued", "exit_code": None,
                   "started": None, "finished": None, "pid": None,
                   "note": None, "gate": None}
            self.jobs[jid] = job
            self._persist(job)
        self.q.put(jid)
        return job, None, 200

    def cancel(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                return {"error": "job not found"}, 404
            if job["state"] not in ("queued", "running"):
                return {"error": f"任务状态为 {job['state']}，无法取消"}, 409
            pid = job.get("pid")
            proc = self._procs.get(jid)
            job["state"] = "cancelled"
            job["finished"] = now_iso()
            job["note"] = "cancelled by user"
            self._persist(job)
        if pid:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True)
            elif proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
        return {"ok": True, "id": jid, "state": "cancelled"}, 200

    # -- worker --

    def _build_cmd(self, job):
        p = job["params"]
        # import_book.py requires --title even for --gate-only; fall back to the
        # slug there (the gate-only path only ever uses the slug).
        title = p.get("title") or job["slug"]
        cmd = [sys.executable, str(IMPORT_BOOK),
               "--title", title, "--slug", job["slug"], "--root", str(self.root)]
        if p.get("pdf"):
            cmd += ["--pdf", p["pdf"]]
        if p.get("collection"):
            cmd += ["--collection", p["collection"]]
        if p.get("first") is not None:
            cmd += ["--first", str(p["first"])]
        if p.get("last") is not None:
            cmd += ["--last", str(p["last"])]
        if p.get("min_verified_ratio") is not None:
            cmd += ["--min-verified-ratio", str(p["min_verified_ratio"])]
        if p["mode"] == "dry-run":
            cmd.append("--dry-run")
        elif p["mode"] == "gate-only":
            cmd.append("--gate-only")
        return cmd

    def _worker(self):
        """Single worker: jobs run strictly one at a time (cloud OCR spend +
        rate control)."""
        while True:
            jid = self.q.get()
            with self.lock:
                job = self.jobs.get(jid)
                if job is None or job["state"] != "queued":
                    continue  # cancelled while queued, or gone
                job["state"] = "running"
                job["started"] = now_iso()
                self._persist(job)
                cmd = self._build_cmd(job)
            log_path = self.jobs_dir / jid / "job.log"
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            rc = None
            try:
                with open(log_path, "ab") as logf:
                    logf.write(("[server] exec: " + " ".join(cmd) + "\n")
                               .encode("utf-8"))
                    logf.flush()
                    proc = subprocess.Popen(cmd, stdout=logf,
                                            stderr=subprocess.STDOUT,
                                            cwd=str(self.root), env=env,
                                            creationflags=flags)
                    with self.lock:
                        job["pid"] = proc.pid
                        self._procs[jid] = proc
                        self._persist(job)
                    rc = proc.wait()
                    logf.write(f"[server] child exited with code {rc}\n"
                               .encode("utf-8"))
            except Exception as e:  # child failed to launch etc.
                try:
                    with open(log_path, "ab") as logf:
                        logf.write(f"[server] failed to run child: {e}\n"
                                   .encode("utf-8"))
                except OSError:
                    pass
            gate = None
            try:
                gate = parse_gate(log_path.read_text(encoding="utf-8",
                                                     errors="replace"))
            except OSError:
                pass
            with self.lock:
                self._procs.pop(jid, None)
                job["exit_code"] = rc
                job["finished"] = now_iso()
                job["gate"] = gate
                job["pid"] = None
                if job["state"] != "cancelled":
                    if rc == 0:
                        job["state"] = "passed"
                    elif rc == 2:
                        job["state"] = "escalated"
                    else:
                        job["state"] = "error"
                        if rc is None:
                            job["note"] = "server failed to launch child (see log)"
                self._persist(job)


# ---------- the console page (single page, inline CSS+JS, no external requests) ----------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>reading-kit 导入控制台</title>
<style>
:root{
  --bg:#14161b; --card:#1d2027; --line:#2c303a; --fg:#e6e8ee; --muted:#9aa1ad;
  --accent:#7aa2f7; --ok:#5fd08a; --warn:#e8b268; --err:#ef7683;
  --chip-q-bg:#343945; --chip-q-fg:#c8ccd4; --chip-r-bg:#1e3a5f; --chip-r-fg:#9cc0ff;
  --chip-p-bg:#1d3b2a; --chip-p-fg:#6fdb98; --chip-e-bg:#4a3016; --chip-e-fg:#f2ae66;
  --chip-x-bg:#462026; --chip-x-fg:#f28b96; --chip-c-bg:#31343c; --chip-c-fg:#8b909a;
  --esc-bg:#2a2118; --input-bg:#14161b;
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#f3f4f7; --card:#ffffff; --line:#dde0e6; --fg:#1f2329; --muted:#5b636e;
    --accent:#2f5fd0; --ok:#1d8a4e; --warn:#a96c14; --err:#c23948;
    --chip-q-bg:#e6e8ee; --chip-q-fg:#4b5260; --chip-r-bg:#dbe7ff; --chip-r-fg:#2a55b8;
    --chip-p-bg:#d8f2e2; --chip-p-fg:#136c3c; --chip-e-bg:#fbe8cd; --chip-e-fg:#8a5410;
    --chip-x-bg:#fadadd; --chip-x-fg:#a52a37; --chip-c-bg:#e8e9ec; --chip-c-fg:#6a7078;
    --esc-bg:#fdf3e3; --input-bg:#f7f8fa;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:20px 14px 60px}
header h1{font-size:22px;margin:0 0 4px}
header .meta{color:var(--muted);font-size:13px;word-break:break-all}
header .meta a{color:var(--accent);text-decoration:none}
header .meta a:hover{text-decoration:underline}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:16px;margin-top:16px}
h2{font-size:16px;margin:0 0 12px}
.modes{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px}
.modes label{cursor:pointer;color:var(--fg)}
.row{margin-bottom:10px}
.row label{display:block;font-size:13px;color:var(--muted);margin-bottom:3px}
input[type=text],input[type=number],select{width:100%;padding:7px 9px;
  border:1px solid var(--line);border-radius:6px;background:var(--input-bg);
  color:var(--fg);font-size:14px}
select{cursor:pointer}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
@media (max-width:520px){.row3{grid-template-columns:1fr}}
.warn{border:1px dashed var(--warn);color:var(--warn);border-radius:8px;
  padding:8px 12px;font-size:13px;margin-bottom:12px}
button{border:1px solid var(--line);background:var(--input-bg);color:var(--fg);
  border-radius:6px;padding:7px 16px;font-size:14px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.primary:hover{opacity:.9;color:#fff}
.msg{margin-left:10px;font-size:13px}
.msg.ok{color:var(--ok)} .msg.err{color:var(--err)}
details.hist{margin:-6px 0 14px 14px}
details.hist summary{cursor:pointer;color:var(--muted);font-size:12px;
padding:2px 0;user-select:none}
details.hist .job{opacity:.75;margin-top:8px}
.job{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;margin-top:12px}
.job-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}
.job-title b{font-size:15px}
.tag{font-size:12px;color:var(--accent);border:1px solid var(--accent);
  border-radius:4px;padding:0 6px;margin-left:6px}
.dim{color:var(--muted);font-size:12px;margin-left:6px;word-break:break-all}
.chip{padding:2px 12px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.st-queued{background:var(--chip-q-bg);color:var(--chip-q-fg)}
.st-running{background:var(--chip-r-bg);color:var(--chip-r-fg)}
.st-passed{background:var(--chip-p-bg);color:var(--chip-p-fg)}
.st-escalated{background:var(--chip-e-bg);color:var(--chip-e-fg)}
.st-error{background:var(--chip-x-bg);color:var(--chip-x-fg)}
.st-cancelled{background:var(--chip-c-bg);color:var(--chip-c-fg)}
.job-sub{color:var(--muted);font-size:12px;margin-top:4px;word-break:break-all}
.job-actions{margin-top:10px;display:flex;gap:8px}
.job-actions button{padding:4px 12px;font-size:13px}
pre.log{margin:10px 0 0;background:#0d0f12;color:#c9d1d9;border:1px solid var(--line);
  border-radius:8px;padding:10px;font:12px/1.5 Consolas,"Courier New",monospace;
  max-height:320px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.ok-line{margin-top:8px;color:var(--ok);font-size:13px;word-break:break-all}
.gate-esc{margin-top:10px;background:var(--esc-bg);border-left:4px solid var(--warn);
  border-radius:6px;padding:10px 12px;font-size:13px}
.gate-esc .gate-line{margin-bottom:4px}
.gate-esc .tally{color:var(--muted);margin-bottom:6px}
.tbl-wrap{overflow-x:auto;margin:6px 0}
.gate-esc table{border-collapse:collapse;font-size:12px;min-width:320px}
.gate-esc th,.gate-esc td{border:1px solid var(--line);padding:3px 8px;text-align:left}
.gate-esc th{color:var(--muted);font-weight:600}
.guide{margin-top:8px;color:var(--warn);font-weight:600}
.empty{color:var(--muted);font-size:13px;margin-top:12px}
/* --- 人工精校 (adjudication) --- */
.wrap.adj-open{max-width:1400px}
.adj-row .job-title .dim{margin-left:8px}
.adj-top{display:flex;align-items:center;gap:14px;margin-top:16px;flex-wrap:wrap}
.adj-progress{font-size:14px}
.adj-main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px;margin-top:12px}
@media (max-width:900px){.adj-main{grid-template-columns:1fr}}
.adj-left{position:sticky;top:10px;align-self:start;text-align:center}
.adj-left img{max-width:100%;max-height:calc(100vh - 40px);object-fit:contain;
  border:1px solid var(--line);border-radius:8px;background:#fff;cursor:zoom-in}
.adj-pane{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;margin-bottom:12px}
.adj-pane.picked{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.adj-pane-head{display:flex;align-items:center;gap:10px;font-size:13px;
  color:var(--muted);flex-wrap:wrap}
.adj-pane-head b{color:var(--fg)}
.adj-pane-head button{padding:2px 10px;font-size:12px}
pre.adj-pre{margin:8px 0 0;background:var(--input-bg);border:1px solid var(--line);
  border-radius:6px;padding:8px 10px;font:12px/1.7 Consolas,"Courier New",monospace;
  max-height:240px;overflow:auto;white-space:pre-wrap;word-break:break-all}
#adjEdit{width:100%;min-height:170px;margin-top:8px;background:var(--input-bg);
  color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;
  font:13px/1.7 Consolas,"Courier New",monospace;resize:vertical}
.adj-actions{margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.adj-keys{color:var(--muted);font-size:12px;margin-top:6px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>reading-kit 导入控制台</h1>
  <div class="meta">项目 <b id="proj">…</b> · <span id="rootLbl"></span>
    · <a id="ragLink" target="_blank" style="display:none"></a></div>
</header>

<div id="mainView">

<section class="card">
  <h2>新书导入</h2>
  <div class="modes">
    <label><input type="radio" name="mode" value="import" checked> 正式导入</label>
    <label><input type="radio" name="mode" value="dry-run"> 演练（dry-run）</label>
    <label><input type="radio" name="mode" value="gate-only"> 仅质检（gate-only）</label>
  </div>
  <div class="row" id="rowPdf">
    <label>PDF 文件（Input/ 与项目根目录下自动发现）</label>
    <select id="pdfSelect"><option value="">—— 选择 PDF ——</option></select>
    <input id="pdfText" type="text" style="margin-top:6px"
      placeholder="或手填完整路径（优先于上面的下拉选择）">
  </div>
  <div class="row" id="rowTitle">
    <label>书名 *</label>
    <input id="titleIn" type="text" placeholder="将成为 vault/Books/ 下的文件夹名">
  </div>
  <div class="row" id="rowSlug" style="display:none">
    <label>Slug *（staging 里已有数据的书）</label>
    <input id="slugIn" type="text" placeholder="staging/&lt;slug&gt;/ 的目录名">
  </div>
  <div class="row">
    <label>合集（collection，可留空 = default）</label>
    <input id="collIn" list="collList" placeholder="如：比较文学">
    <datalist id="collList"></datalist>
  </div>
  <div class="row3">
    <div class="row"><label>起始页</label><input id="firstIn" type="number" min="1"></div>
    <div class="row"><label>结束页</label><input id="lastIn" type="number" min="1"></div>
    <div class="row"><label>校验阈值</label>
      <input id="ratioIn" type="number" step="0.01" min="0.01" max="1" value="0.85"></div>
  </div>
  <div id="warnBox" class="warn">注意：未填起始/结束页时，<b>整本 PDF 会被当作一本书处理，
    多书合订 PDF 请分段导入</b>（每本书一个页码区间，分次提交）。</div>
  <div class="row" style="margin-bottom:0">
    <button class="primary" id="submitBtn">提交任务</button>
    <span id="formMsg" class="msg"></span>
  </div>
</section>

<section>
  <h2 style="margin-top:24px">任务队列</h2>
  <div id="emptyHint" class="empty">暂无任务。任务严格串行执行（控制云端 OCR 花费与限速）。</div>
  <div id="jobs"></div>
</section>

<section>
  <h2 style="margin-top:24px">人工精校</h2>
  <div id="adjEmpty" class="empty">暂无待精校页面。质量门拦下的书（adjudicate / escalated / error 页）会出现在这里。</div>
  <div id="adjList"></div>
</section>

</div><!-- /mainView -->

<div id="adjView" style="display:none">
  <div class="adj-top">
    <button id="adjBack">← 返回</button>
    <div class="adj-progress" id="adjProgress"></div>
  </div>
  <div id="adjBody">
    <div class="adj-main">
      <div class="adj-left">
        <a id="adjImgLink" href="#" target="_blank" title="点击在新标签页查看原图"><img id="adjImg" alt="页面扫描图"></a>
        <div id="adjNoImg" class="empty" style="display:none">（该页无扫描图）</div>
      </div>
      <div class="adj-right">
        <div class="adj-pane" id="paneA">
          <div class="adj-pane-head"><b>模型 A</b>
            <button id="pickABtn">选此版（A）</button></div>
          <pre class="adj-pre" id="adjTextA"></pre>
        </div>
        <div class="adj-pane" id="paneB">
          <div class="adj-pane-head"><b>模型 B</b>
            <button id="pickBBtn">选此版（B）</button></div>
          <pre class="adj-pre" id="adjTextB"></pre>
        </div>
        <div class="adj-pane" id="paneDiff" style="display:none">
          <div class="adj-pane-head"><b>模型分歧 diff</b>
            <button id="diffBtn">显示 diff</button></div>
          <pre class="adj-pre" id="adjTextDiff" style="display:none"></pre>
        </div>
        <div class="adj-pane">
          <div class="adj-pane-head"><b>最终文本</b>
            <span class="dim">选 A/B 后自动填入，可继续编辑；也可直接手写</span></div>
          <textarea id="adjEdit" spellcheck="false"></textarea>
          <div class="adj-actions">
            <button class="primary" id="adjSaveBtn">保存并下一页</button>
            <button id="adjSkipBtn">跳过</button>
            <span id="adjMsg" class="msg"></span>
          </div>
          <div class="adj-keys">快捷键：A / B 选版 · Ctrl+Enter 保存并下一页 · → 跳过</div>
        </div>
      </div>
    </div>
  </div>
  <div id="adjDone" class="card" style="display:none"></div>
</div>

</div>
<script>
const $=id=>document.getElementById(id);
const cards=new Map(), timers={}, expanded=new Set();
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=s=>s?s.replace('T',' '):'';
const ST={queued:['排队中','st-queued'],running:['进行中','st-running'],
  passed:['已通过','st-passed'],escalated:['需人工精校','st-escalated'],
  error:['失败','st-error'],cancelled:['已取消','st-cancelled']};
const MODE={'import':'正式导入','dry-run':'演练','gate-only':'仅质检'};

async function j(u,o){const r=await fetch(u,o);const d=await r.json().catch(()=>({}));
  return {ok:r.ok,status:r.status,d};}

async function loadInfo(){
  try{
    const {d}=await j('/api/info');
    $('proj').textContent=d.project||'?';
    $('rootLbl').textContent=d.root||'';
    if(d.ragPort){const a=$('ragLink');a.href='http://localhost:'+d.ragPort+'/';
      a.textContent='RAG 服务 :'+d.ragPort;a.style.display='';}
    const done=new Set(d.imported_pdfs||[]);
    (d.pdfs||[]).forEach(p=>{const o=document.createElement('option');
      o.value=p;
      o.textContent=done.has(p.replace(/\\/g,'/'))?p+'　✅ 已导入过':p;
      $('pdfSelect').appendChild(o);});
    (d.collections||[]).forEach(c=>{const o=document.createElement('option');
      o.value=c;$('collList').appendChild(o);});
  }catch(e){}
}

function curMode(){return document.querySelector('input[name=mode]:checked').value;}
function modeChanged(){
  const g=curMode()==='gate-only';
  $('rowPdf').style.display=g?'none':'';
  $('rowTitle').style.display=g?'none':'';
  $('rowSlug').style.display=g?'':'none';
  updateWarn();
}
function updateWarn(){
  $('warnBox').style.display=
    (curMode()!=='gate-only' && !$('firstIn').value && !$('lastIn').value)?'':'none';
}

async function submitForm(){
  const mode=curMode(), body={mode};
  if(mode==='gate-only'){
    body.slug=$('slugIn').value.trim();
  }else{
    body.pdf=$('pdfText').value.trim()||$('pdfSelect').value;
    body.title=$('titleIn').value.trim();
    const c=$('collIn').value.trim(); if(c)body.collection=c;
  }
  const f=$('firstIn').value.trim(), l=$('lastIn').value.trim(), r=$('ratioIn').value.trim();
  if(f)body.first=Number(f); if(l)body.last=Number(l);
  if(r)body.min_verified_ratio=Number(r);
  const msg=$('formMsg');
  try{
    let res=await j('/api/import',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!res.ok&&res.d.needs_confirm){
      if(!confirm(res.d.error+'\n\n确定要重跑这本书吗？')){
        msg.textContent='已取消（该书已在架，未重复提交）。';msg.className='msg';return;}
      body.confirm=true;
      res=await j('/api/import',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    }
    if(res.ok){msg.textContent='已加入队列：'+res.d.id;msg.className='msg ok';refreshJobs();}
    else{msg.textContent='错误：'+(res.d.error||('HTTP '+res.status));msg.className='msg err';}
  }catch(e){msg.textContent='请求失败：'+e;msg.className='msg err';}
}

function gateHtml(job){
  const g=job.gate;
  if(job.state==='passed'){
    if(!g)return job.params.mode==='dry-run'
      ?'<div class="ok-line">演练完成：仅打印执行计划，未运行任何阶段。</div>':'';
    let s='<div class="ok-line">已通过：校验比 '
      +(g.ratio!=null?g.ratio.toFixed(4):'?')
      +'（'+(g.verified??'?')+' / '+(g.total??'?')+' 页）';
    if(g.vault_note)s+=' · 笔记：'+esc(g.vault_note);
    return s+'</div>';
  }
  if(job.state==='escalated'){
    let h='<div class="gate-esc">';
    if(g){
      h+='<div class="gate-line"><b>质量门未通过</b>：校验比 '
        +(g.ratio!=null?g.ratio.toFixed(4):'?')+' &lt; 阈值 '+(g.threshold??'?')
        +'（verified '+(g.verified??'?')+' / '+(g.total??'?')+' 页'
        +(g.first!=null?('，范围 '+g.first+'-'+g.last):'')+'）</div>';
      if(g.tally)h+='<div class="tally">已核验 '+(g.tally.verified??0)
        +' · 待裁决 '+(g.tally.adjudicate??0)+' · 已升级 '+(g.tally.escalated??0)
        +' · 出错 '+(g.tally.error??0)+' · 缺失 '+(g.tally.missing??0)+'</div>';
      if(g.failed&&g.failed.length){
        h+='<div class="tbl-wrap"><table><thead><tr><th>页</th><th>状态</th>'
          +'<th>相似度</th><th>备注</th></tr></thead><tbody>';
        g.failed.forEach(f=>{h+='<tr><td>'+esc(f.page)+'</td><td>'+esc(f.status)
          +'</td><td>'+(f.similarity==null?'-':esc(f.similarity))
          +'</td><td>'+esc(f.note||'')+'</td></tr>';});
        h+='</tbody></table></div>';
      }
    }else{
      h+='<div class="gate-line"><b>质量门未通过</b>（未能从日志解析出报告，请展开日志查看）。</div>';
    }
    h+='<div class="guide">该书未通过自动校验，请走「学术精校」流程（见 WORKFLOWS.md），'
      +'已保留 staging 数据可复用。</div></div>';
    return h;
  }
  return '';
}

function makeCard(job){
  const el=document.createElement('div');el.className='job';
  el.innerHTML='<div class="job-head"><div class="job-title"></div>'
    +'<span class="chip"></span></div><div class="job-sub"></div><div class="gate"></div>'
    +'<div class="job-actions"><button class="btn-log">展开日志</button>'
    +'<button class="btn-cancel">取消</button></div>'
    +'<pre class="log" style="display:none"></pre>';
  el.querySelector('.btn-log').onclick=()=>toggleLog(job.id,el);
  el.querySelector('.btn-cancel').onclick=()=>cancelJob(job.id);
  return el;
}

function updateCard(el,job){
  el.querySelector('.job-title').innerHTML='<b>'+esc(job.params.title||job.slug)+'</b>'
    +'<span class="tag">'+(MODE[job.params.mode]||esc(job.params.mode))+'</span>'
    +'<span class="dim">slug='+esc(job.slug)+' · '+esc(job.id)+'</span>';
  const [txt,cls]=ST[job.state]||[job.state,''];
  const chip=el.querySelector('.chip');chip.textContent=txt;chip.className='chip '+cls;
  let sub='创建 '+fmt(job.created);
  if(job.started)sub+=' · 开始 '+fmt(job.started);
  if(job.finished)sub+=' · 结束 '+fmt(job.finished);
  if(job.exit_code!==null&&job.exit_code!==undefined)sub+=' · 退出码 '+job.exit_code;
  if(job.note)sub+=' · '+esc(job.note);
  el.querySelector('.job-sub').innerHTML=sub;
  el.querySelector('.btn-cancel').style.display=
    (job.state==='queued'||job.state==='running')?'':'none';
  el.querySelector('.gate').innerHTML=gateHtml(job);
}

const histOpen=new Set();
function renderJobs(jobs){
  $('emptyHint').style.display=jobs.length?'none':'';
  const box=$('jobs');
  /* one group per book (slug): newest run as the card, older runs collapsed
     under it — a book's gate re-checks and resumes are HISTORY, not duplicates */
  const groups=new Map();
  jobs.forEach(job=>{
    if(!groups.has(job.slug))groups.set(job.slug,[]);
    groups.get(job.slug).push(job);
  });
  box.textContent='';
  groups.forEach((list,slug)=>{
    const latest=list[0];
    let card=cards.get(latest.id);
    if(!card){card=makeCard(latest);cards.set(latest.id,card);}
    updateCard(card,latest);
    box.appendChild(card);
    if(list.length>1){
      const det=document.createElement('details');det.className='hist';
      if(histOpen.has(slug))det.open=true;
      det.addEventListener('toggle',()=>{
        if(det.open)histOpen.add(slug);else histOpen.delete(slug);});
      const sum=document.createElement('summary');
      sum.textContent='本书另有 '+(list.length-1)+' 次历史运行（质检 / 重跑记录，同一本书）';
      det.appendChild(sum);
      list.slice(1).forEach(job=>{
        let c=cards.get(job.id);
        if(!c){c=makeCard(job);cards.set(job.id,c);}
        updateCard(c,job);
        det.appendChild(c);
      });
      box.appendChild(det);
    }
  });
}

async function refreshJobs(){
  try{const {d}=await j('/api/jobs');if(Array.isArray(d))renderJobs(d);}catch(e){}
}

function toggleLog(id,el){
  const pre=el.querySelector('.log'),btn=el.querySelector('.btn-log');
  if(expanded.has(id)){
    expanded.delete(id);pre.style.display='none';btn.textContent='展开日志';
    if(timers[id]){clearInterval(timers[id]);delete timers[id];}
  }else{
    expanded.add(id);pre.style.display='block';btn.textContent='收起日志';
    pollLog(id,el);timers[id]=setInterval(()=>pollLog(id,el),1500);
  }
}
async function pollLog(id,el){
  try{
    const {d}=await j('/api/jobs/'+id);
    const pre=el.querySelector('.log');
    const atBottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<40;
    pre.textContent=d.log_tail||'（暂无日志）';
    if(atBottom)pre.scrollTop=pre.scrollHeight;
    if(['passed','escalated','error','cancelled'].includes(d.state)&&timers[id]){
      clearInterval(timers[id]);delete timers[id];}
  }catch(e){}
}

async function cancelJob(id){
  if(!confirm('确定取消该任务？'))return;
  try{await j('/api/jobs/'+id+'/cancel',{method:'POST'});}catch(e){}
  refreshJobs();
}

/* ---- 人工精校 (adjudication) ---- */
const adj={slug:null,pages:[],idx:0,saved:0,remaining:0,picked:null,
  pickedText:'',texts:{a:'',b:''},diffLoaded:false};
const STZH={adjudicate:'待裁决',escalated:'已升级',error:'出错',
  missing:'无台账',verified:'已核验'};

async function loadAdjSummary(){
  try{const {ok,d}=await j('/api/adjudicate');
    if(ok&&Array.isArray(d))renderAdjSummary(d);}catch(e){}
}
function renderAdjSummary(list){
  $('adjEmpty').style.display=list.length?'none':'';
  const box=$('adjList');box.innerHTML='';
  list.forEach(s=>{
    const parts=Object.keys(s.by_status||{}).sort()
      .map(k=>(STZH[k]||k)+' '+s.by_status[k]);
    const div=document.createElement('div');div.className='job adj-row';
    div.innerHTML='<div class="job-head"><div class="job-title"><b>'+esc(s.slug)
      +'</b><span class="dim">'+s.pending+' 页待精校'
      +(parts.length?'（'+esc(parts.join(' · '))+'）':'')+'</span></div>'
      +'<button class="btn-adj">开始精校</button></div>';
    div.querySelector('.btn-adj').onclick=()=>startAdj(s.slug);
    box.appendChild(div);
  });
}

async function startAdj(slug){
  try{
    const res=await j('/api/adjudicate/'+encodeURIComponent(slug));
    if(!res.ok){alert('加载失败：'+(res.d.error||('HTTP '+res.status)));return;}
    adj.slug=slug;adj.pages=res.d;adj.idx=0;adj.saved=0;adj.remaining=res.d.length;
    $('mainView').style.display='none';$('adjView').style.display='';
    document.querySelector('.wrap').classList.add('adj-open');
    window.scrollTo(0,0);
    showAdjPage();
  }catch(e){alert('加载失败：'+e);}
}
function closeAdj(){
  $('adjView').style.display='none';$('mainView').style.display='';
  document.querySelector('.wrap').classList.remove('adj-open');
  loadAdjSummary();
}

function showAdjPage(){
  const msg=$('adjMsg');msg.textContent='';msg.className='msg';
  if(adj.idx>=adj.pages.length){showAdjDone();return;}
  $('adjBody').style.display='';$('adjDone').style.display='none';
  const p=adj.pages[adj.idx];
  adj.picked=null;adj.pickedText='';adj.diffLoaded=false;adj.texts={a:'',b:''};
  paneMark(null);
  $('adjProgress').innerHTML='第 <b>'+(adj.idx+1)+'</b> / '+adj.pages.length
    +' 页 · <b>'+esc(p.page)+'</b> · 状态 '+esc(STZH[p.status]||p.status)
    +(p.similarity!=null?' · 相似度 '+esc(p.similarity):'')
    +(p.note_or_error?' · <span class="dim">'+esc(p.note_or_error)+'</span>':'');
  if(p.image_url){
    $('adjImg').src=p.image_url;$('adjImgLink').href=p.image_url;
    $('adjImgLink').style.display='';$('adjNoImg').style.display='none';
  }else{$('adjImgLink').style.display='none';$('adjNoImg').style.display='';}
  $('adjEdit').value='';
  loadTranscript('a',p.a_url,$('adjTextA'));
  loadTranscript('b',p.b_url,$('adjTextB'));
  $('paneDiff').style.display=p.has_diff?'':'none';
  const dp=$('adjTextDiff');dp.style.display='none';dp.textContent='';
  $('diffBtn').textContent='显示 diff';
}
async function loadTranscript(which,url,el){
  if(!url){el.textContent='（无该模型转录）';return;}
  el.textContent='加载中…';
  try{const r=await fetch(url);adj.texts[which]=await r.text();
    el.textContent=adj.texts[which];}
  catch(e){el.textContent='（加载失败：'+e+'）';}
}
function paneMark(which){
  $('paneA').classList.toggle('picked',which==='a');
  $('paneB').classList.toggle('picked',which==='b');
}
function pick(which){
  const p=adj.pages[adj.idx];if(!p)return;
  if(!(which==='a'?p.a_url:p.b_url))return;
  adj.picked=(which==='a')?'pick_a':'pick_b';
  adj.pickedText=adj.texts[which];
  $('adjEdit').value=adj.pickedText;
  paneMark(which);
}
async function toggleDiff(){
  const p=adj.pages[adj.idx];if(!p||!p.diff_url)return;
  const pre=$('adjTextDiff');
  if(pre.style.display==='none'){
    if(!adj.diffLoaded){
      try{const r=await fetch(p.diff_url);pre.textContent=await r.text();}
      catch(e){pre.textContent='（diff 加载失败）';}
      adj.diffLoaded=true;
    }
    pre.style.display='';$('diffBtn').textContent='收起 diff';
  }else{pre.style.display='none';$('diffBtn').textContent='显示 diff';}
}
async function adjSave(){
  const p=adj.pages[adj.idx];if(!p)return;
  const val=$('adjEdit').value,msg=$('adjMsg');
  let body;
  if(adj.picked){
    body={action:adj.picked};
    if(val.trim()&&val!==adj.pickedText)body.text=val; // 编辑过的版本优先
  }else if(val.trim()){
    body={action:'custom',text:val};
  }else{
    msg.textContent='请先选 A/B 版本，或在文本框输入内容';msg.className='msg err';
    return;
  }
  try{
    const res=await j('/api/adjudicate/'+encodeURIComponent(adj.slug)+'/'
      +encodeURIComponent(p.page),{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(res.ok){
      adj.saved++;
      if(typeof res.d.remaining==='number')adj.remaining=res.d.remaining;
      adj.idx++;showAdjPage();
    }else if(res.status===409){
      msg.textContent='该页已有 verified 文件，自动跳到下一页';msg.className='msg err';
      adj.idx++;setTimeout(showAdjPage,700);
    }else{
      msg.textContent='错误：'+(res.d.error||('HTTP '+res.status));msg.className='msg err';
    }
  }catch(e){msg.textContent='请求失败：'+e;msg.className='msg err';}
}
function adjSkip(){adj.idx++;showAdjPage();}
function showAdjDone(){
  $('adjBody').style.display='none';
  const done=$('adjDone');done.style.display='';
  done.innerHTML='<h2>本轮精校完成</h2>'
    +'<p>已完成 <b>'+adj.saved+'</b> 页精校，<b>'+esc(adj.slug)+'</b> 剩余 <b>'
    +adj.remaining+'</b> 页待处理。</p>'
    +'<p class="dim" style="color:var(--muted)">建议：回到「新书导入」表单，选「仅质检（gate-only）」、'
    +'slug 填 '+esc(adj.slug)+'，重跑质量门确认达标；达标后再走正式导入的后续步骤。</p>'
    +'<div class="adj-actions"><button class="primary" id="adjGateBtn">去运行仅质检</button>'
    +'<button id="adjDoneBack">返回</button></div>';
  $('adjProgress').textContent=adj.slug+' · 精校完成';
  $('adjGateBtn').onclick=()=>{
    document.querySelector('input[name=mode][value="gate-only"]').checked=true;
    modeChanged();$('slugIn').value=adj.slug;closeAdj();window.scrollTo(0,0);
  };
  $('adjDoneBack').onclick=closeAdj;
}
document.addEventListener('keydown',e=>{
  if($('adjView').style.display==='none')return;
  if($('adjBody').style.display==='none')return; // 完成卡片时不响应
  if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();adjSave();return;}
  const t=e.target;
  if(t&&(t.tagName==='TEXTAREA'||t.tagName==='INPUT'))return;
  if(e.key==='a'||e.key==='A'){e.preventDefault();pick('a');}
  else if(e.key==='b'||e.key==='B'){e.preventDefault();pick('b');}
  else if(e.key==='ArrowRight'){e.preventDefault();adjSkip();}
});
$('adjBack').onclick=closeAdj;
$('pickABtn').onclick=()=>pick('a');
$('pickBBtn').onclick=()=>pick('b');
$('diffBtn').onclick=toggleDiff;
$('adjSaveBtn').onclick=adjSave;
$('adjSkipBtn').onclick=adjSkip;

document.querySelectorAll('input[name=mode]').forEach(r=>r.onchange=modeChanged);
$('firstIn').oninput=updateWarn; $('lastIn').oninput=updateWarn;
$('submitBtn').onclick=submitForm;
loadInfo(); refreshJobs(); setInterval(refreshJobs,2000);
loadAdjSummary(); setInterval(loadAdjSummary,10000);
</script>
</body>
</html>
"""


# ---------- HTTP handler ----------

def make_handler(mgr, project, rag_port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                data = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/info":
                self._json(mgr.info(project, rag_port))
                return
            if path == "/api/jobs":
                self._json(mgr.list_jobs())
                return
            m = re.match(r"^/api/jobs/([0-9a-zA-Z\-]+)$", path)
            if m:
                job = mgr.get_job(m.group(1))
                if job is None:
                    self._json({"error": "job not found"}, 404)
                else:
                    self._json(job)
                return
            if path == "/api/adjudicate":
                self._json(adj_summary(mgr.staging))
                return
            m = re.match(r"^/api/adjudicate/([^/]+)/?$", path)
            if m:
                pages = adj_pages(mgr.staging, unquote(m.group(1)))
                if pages is None:
                    self._json({"error": "slug not found in staging"}, 404)
                else:
                    self._json(pages)
                return
            if path.startswith("/staging/"):
                self._staging_file(unquote(path[len("/staging/"):]))
                return
            self._json({"error": "not found"}, 404)

        def _staging_file(self, rel):
            """Serve one file from under STAGING only. The resolved target must
            stay inside STAGING (traversal -> 404); no directory listings."""
            base = mgr.staging.resolve()
            try:
                target = (base / rel).resolve()
            except (OSError, ValueError):
                self._json({"error": "not found"}, 404)
                return
            if not is_within(target, base) or not target.is_file():
                self._json({"error": "not found"}, 404)
                return
            ext = target.suffix.lower()
            if ext == ".png":
                ctype = "image/png"
            elif ext in (".jpg", ".jpeg"):
                ctype = "image/jpeg"
            elif ext in (".md", ".diff", ".txt", ".json", ".jsonl"):
                ctype = "text/plain; charset=utf-8"
            else:
                ctype = "application/octet-stream"
            try:
                data = target.read_bytes()
            except OSError:
                self._json({"error": "not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            path = urlsplit(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if path == "/api/import":
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._json({"error": "invalid JSON body"}, 400)
                    return
                if not isinstance(body, dict):
                    self._json({"error": "body must be a JSON object"}, 400)
                    return
                job, err, code = mgr.create_job(body)
                if err:
                    if err.startswith("CONFIRM:"):
                        self._json({"error": err[8:], "needs_confirm": True}, code)
                    else:
                        self._json({"error": err}, code)
                else:
                    self._json({"id": job["id"]})
                return
            m = re.match(r"^/api/jobs/([0-9a-zA-Z\-]+)/cancel$", path)
            if m:
                out, code = mgr.cancel(m.group(1))
                self._json(out, code)
                return
            m = re.match(r"^/api/adjudicate/([^/]+)/([^/]+)$", path)
            if m:
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._json({"error": "invalid JSON body"}, 400)
                    return
                if not isinstance(body, dict):
                    self._json({"error": "body must be a JSON object"}, 400)
                    return
                out, code = adj_save(mgr.staging, unquote(m.group(1)),
                                     unquote(m.group(2)), body)
                self._json(out, code)
                return
            self._json({"error": "not found"}, 404)

    return Handler


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(
        description="Local web console for import_book.py: submit imports, "
                    "watch the sequential job queue, read gate reports.")
    ap.add_argument("--port", type=int, default=None,
                    help="listen port (default: this root's ragPort+100 from "
                         f"~/.reading-kit/registry.json, else {FALLBACK_PORT})")
    ap.add_argument("--root", default=None,
                    help="override project ROOT (default: this script's repo)")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else DEFAULT_ROOT
    entry = registry_entry(root)
    rag_port = entry.get("ragPort") if entry else None
    if args.port:
        port = args.port
    elif isinstance(rag_port, int):
        port = rag_port + 100
    else:
        port = FALLBACK_PORT
    project = entry.get("slug") if entry else root.name

    mgr = JobManager(root)
    handler = make_handler(mgr, project, rag_port)

    print(f"[import-server] project: {project}")
    print(f"[import-server] root:    {root}")
    print(f"[import-server] jobs:    {mgr.jobs_dir} "
          f"({mgr.restored} restored from disk)")
    print(f"[import-server] console: http://localhost:{port}/  (bound 0.0.0.0)")

    class ExclusiveServer(ThreadingHTTPServer):
        # Windows maps allow_reuse_address (http.server's default) to
        # SO_REUSEADDR, which lets a SECOND process silently double-bind the
        # same port — whichever socket wins then serves, so a stale server
        # can keep answering after a "restart" (bit us 2026-07-10: two
        # consoles on :8866, old code served for hours). Exclusive bind makes
        # the second start fail loudly instead.
        allow_reuse_address = os.name != "nt"

    try:
        srv = ExclusiveServer(("0.0.0.0", port), handler)
    except OSError as e:
        sys.exit(f"[import-server] cannot bind port {port} — another server "
                 f"is already running there ({e}). Stop it first, or pass "
                 f"--port to use a different one.")
    srv.serve_forever()


if __name__ == "__main__":
    main()
