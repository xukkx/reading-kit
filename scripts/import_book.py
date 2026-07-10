#!/usr/bin/env python3
"""
import_book.py — one-command book import: PDF -> OCR -> consensus verify ->
quality gate -> reflowed vault note -> kb_substrate -> RAG index -> registry.

Chains the standalone pipeline scripts (paddle_ocr.py, ingest.py, reflow.py,
substrate_build.py, rag.py) via subprocess. Per this repo's convention the
scripts never import each other; all state passes through the filesystem
(staging/, vault/, .rag/).

Quality gate (the PaddleOCR-backbone vs jury-escalation policy):
  ratio = pages with staging/<slug>/verified/pg-XXXX.md in [first,last]
          / total pages in range.
  ratio < --min-verified-ratio -> STOP before reflow, exit code 2, print an
  escalation report routing the book to the 学术精校 jury workflow
  (mechanical punctuation transfer + 3-model jury + human review — see
  WORKFLOWS.md). staging/<slug>/ is kept for reuse. No jury is auto-run.

Exit codes: 0 ok / gate pass, 1 error or child failure, 2 gate failed.

Usage:
  python scripts/import_book.py --pdf D:\\book.pdf --title 比较文学论 --collection 比较文学
  python scripts/import_book.py --pdf D:\\two-books.pdf --title 上册 --first 1 --last 210
  python scripts/import_book.py --title 比较文学论 --gate-only
  python scripts/import_book.py --pdf D:\\book.pdf --title 书名 --dry-run
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY = Path.home() / ".reading-kit" / "registry.json"
SECRETS = Path.home() / ".secrets"


# ---------- small helpers ----------

def sanitize_slug(text):
    """Filesystem-safe slug: word chars (incl. CJK) and dashes only."""
    s = re.sub(r"[^\w\-]+", "-", text).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s.lower() or "book"


def pdf_page_count(pdf):
    """Page count via poppler's pdfinfo (already required for pdftoppm),
    falling back to pypdf. None if neither is available."""
    try:
        r = subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            m = re.search(r"^Pages:\s+(\d+)", r.stdout, re.M)
            if m:
                return int(m.group(1))
    except FileNotFoundError:
        pass
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return None


def dpapi_secret(dat_name):
    """Decrypt a DPAPI-protected secret from ~/.secrets/<dat_name>, same store
    the ps1 wrappers (paddle.ps1 / ingest.ps1) use."""
    dat = SECRETS / dat_name
    if not dat.exists():
        return None
    cmd = (f'$sec = Get-Content "{dat}" | ConvertTo-SecureString; '
           "[pscredential]::new('x',$sec).GetNetworkCredential().Password")
    for shell in ("pwsh", "powershell"):
        try:
            r = subprocess.run([shell, "-NoProfile", "-Command", cmd],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except FileNotFoundError:
            continue
    return None


def env_with_secret(var, dat_name, stage):
    """Child env with <var> set: inherit it if present, else decrypt the DPAPI
    store. Exits with a clear message if neither works."""
    env = dict(os.environ)
    if not env.get(var):
        secret = dpapi_secret(dat_name)
        if not secret:
            sys.exit(f"[error] {stage} needs {var}: set the env var or save the "
                     f"key to {SECRETS / dat_name} (see setup.ps1 / scripts\\*.ps1)")
        env[var] = secret
    return env


def run_stage(name, cmd, env=None):
    """Run a sibling script, stream its output, stop the pipeline on failure.
    Returns the captured output lines."""
    cmd = [str(c) for c in cmd]
    print(f"[run] {name}: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env)
    lines = []
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        lines.append(line)
        print(f"  {line}", flush=True)
    proc.wait()
    if proc.returncode != 0:
        print(f"[error] {name} exited with code {proc.returncode}; stopping.", flush=True)
        sys.exit(1)
    return lines


# ---------- staging state (read-only inspection) ----------

def slug_ledger(staging, slug):
    """Last-wins ledger records for this slug, keyed by page label. Matching by
    image-parent-dir, same rule substrate_build.py uses (kept as a duplicate,
    not an import, per repo convention)."""
    by_label = {}
    ledger = staging / "ledger.jsonl"
    if not ledger.exists():
        return by_label
    pages_dir = str((staging / slug / "pages").resolve())
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        img = rec.get("image", "")
        try:
            if img and str(Path(img).resolve().parent) == pages_dir:
                by_label[rec["page"]] = rec
        except (OSError, KeyError):
            continue
    return by_label


def collision_source(by_label, pdf):
    """Return a conflicting source path if staging/<slug> was built from a
    DIFFERENT file than --pdf (slug collision), else None."""
    try:
        ours = Path(pdf).resolve()
    except OSError:
        return None
    for rec in by_label.values():
        src = rec.get("source")
        if not src:
            continue
        try:
            if Path(src).resolve() != ours:
                return src
        except OSError:
            if str(src) != str(pdf):
                return src
    return None


def paddle_complete(paddle_dir, first, last):
    return all((paddle_dir / f"pg-{n:04d}.paddle.md").exists()
               for n in range(first, last + 1))


def ingest_complete(staging, slug, first, last, by_label):
    """Every page in range either has a verified file or a non-error ledger
    record (adjudicate/escalated pages are DONE from ingest's point of view —
    the gate decides what happens to them)."""
    vdir = staging / slug / "verified"
    for n in range(first, last + 1):
        label = f"pg-{n:04d}"
        if (vdir / f"{label}.md").exists():
            continue
        rec = by_label.get(label)
        if not rec or rec.get("status") not in ("verified", "adjudicate", "escalated"):
            return False
    return True


def infer_range(staging, slug, by_label):
    """Fallback page range for --gate-only when no --first/--last/PDF is at
    hand: union of page numbers seen anywhere in this slug's staging data."""
    nums = set()
    for label in by_label:
        m = re.match(r"pg-(\d+)$", label)
        if m:
            nums.add(int(m.group(1)))
    for d, pat in ((staging / slug / "verified", "pg-*.md"),
                   (staging / slug / "paddle", "pg-*.paddle.md"),
                   (staging / slug / "pages", "pg-*.png")):
        if d.exists():
            for f in d.glob(pat):
                m = re.match(r"pg-(\d+)", f.stem)
                if m:
                    nums.add(int(m.group(1)))
    return (min(nums), max(nums)) if nums else None


# ---------- quality gate ----------

def compute_gate(staging, slug, first, last):
    vdir = staging / slug / "verified"
    by_label = slug_ledger(staging, slug)
    total = last - first + 1
    verified = 0
    tally = {"verified": 0, "adjudicate": 0, "escalated": 0, "error": 0, "missing": 0}
    failed = []
    for n in range(first, last + 1):
        label = f"pg-{n:04d}"
        rec = by_label.get(label)
        status = rec.get("status", "missing") if rec else "missing"
        tally[status] = tally.get(status, 0) + 1
        if (vdir / f"{label}.md").exists():
            verified += 1
        else:
            sim = rec.get("similarity") if rec else None
            detail = (rec.get("note") or rec.get("error") or "")[:120] if rec \
                else "(no ledger record)"
            failed.append((label, status, sim, detail))
    return {"slug": slug, "first": first, "last": last, "total": total,
            "verified": verified, "ratio": verified / total if total else 0.0,
            "tally": tally, "failed": failed}


def print_gate(gate, threshold, staging):
    passed = gate["ratio"] >= threshold
    print("[gate] QUALITY GATE: PASS" if passed
          else "[gate] QUALITY GATE: FAIL - verified ratio below threshold")
    print(f"[gate] slug={gate['slug']} range={gate['first']}-{gate['last']} "
          f"pages={gate['total']}")
    print(f"[gate] verified={gate['verified']}/{gate['total']} "
          f"ratio={gate['ratio']:.4f} threshold={threshold}")
    t = gate["tally"]
    print(f"[gate] ledger statuses: verified={t.get('verified', 0)} "
          f"adjudicate={t.get('adjudicate', 0)} escalated={t.get('escalated', 0)} "
          f"error={t.get('error', 0)} missing={t.get('missing', 0)}")
    if not passed:
        print(f"[gate] failed pages ({len(gate['failed'])}):")
        for label, status, sim, detail in gate["failed"]:
            sim_s = f"{sim}" if sim is not None else "-"
            print(f"[gate]   {label}  {status:<10}  sim={sim_s}"
                  + (f"  {detail}" if detail else ""))
        print("[gate] ESCALATION: this book needs the 学术精校 jury workflow")
        print("[gate] (mechanical punctuation transfer + 3-model jury + human review).")
        print(f"[gate] See WORKFLOWS.md section 学术精校. Staging output kept at "
              f"{staging / gate['slug']}")
        print("[gate] for reuse; nothing was written to the vault.")
    return passed


# ---------- registry ----------

def registry_entry_for(root):
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"projects": []}
    for p in data.get("projects", []):
        try:
            if Path(p["path"]).resolve() == root.resolve():
                return data, p
        except (KeyError, OSError):
            continue
    return data, None


def ensure_registered(root, dry_run):
    """Ensure this project ROOT is in ~/.reading-kit/registry.json, appending
    an entry matching the existing entry format if absent.
    Returns (entry, added)."""
    data, entry = registry_entry_for(root)
    if entry:
        return entry, False
    ports = [p.get("ragPort") for p in data.get("projects", [])
             if isinstance(p.get("ragPort"), int)]
    slug = sanitize_slug(root.name)
    entry = {"slug": slug, "path": str(root),
             "remoteBaseDir": f"obsidian-{slug}",
             "ragPort": max(ports) + 1 if ports else 8766}
    if not dry_run:
        data.setdefault("projects", []).append(entry)
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    return entry, True


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(
        description="One-command book import: PDF -> OCR -> consensus verify -> "
                    "quality gate -> vault note -> substrate -> RAG index.")
    ap.add_argument("--pdf", help="source PDF (required for a full run)")
    ap.add_argument("--title", required=True, help="book title (vault folder name)")
    ap.add_argument("--collection", default=None,
                    help="vault/Books/<collection>/<title>; omit for vault/Books/<title> "
                         "(RAG collection 'default')")
    ap.add_argument("--slug", default=None,
                    help="staging slug (default: sanitized from --title)")
    ap.add_argument("--first", type=int, default=None,
                    help="first PDF page of THIS book (default: 1)")
    ap.add_argument("--last", type=int, default=None,
                    help="last PDF page of THIS book (default: end of PDF — "
                         "see the multi-book warning)")
    ap.add_argument("--min-verified-ratio", type=float, default=0.85,
                    help="quality gate threshold (default 0.85)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the full execution plan; run nothing, no network")
    ap.add_argument("--gate-only", action="store_true",
                    help="compute and report the quality gate from existing "
                         "staging data; run nothing else")
    ap.add_argument("--root", default=None,
                    help="override project ROOT (default: this script's repo)")
    ap.add_argument("--db", default=None,
                    help="kb_substrate SQLite path (default: <root>/.rag/kb.sqlite3)")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else DEFAULT_ROOT
    vault = root / "vault"
    staging = root / "staging"
    db = Path(args.db) if args.db else root / ".rag" / "kb.sqlite3"
    title = args.title.strip()
    slug = args.slug or sanitize_slug(title)
    collection = args.collection
    out_dir = (vault / "Books" / collection / title) if collection \
        else (vault / "Books" / title)

    def script(name):
        p = root / "scripts" / name
        return p if p.exists() else Path(__file__).resolve().parent / name

    # ---- gate-only: report the gate from existing staging data, nothing else
    if args.gate_only:
        first, last = args.first, args.last
        if first is None or last is None:
            if args.pdf and Path(args.pdf).exists():
                count = pdf_page_count(args.pdf)
                first = first or 1
                last = last or count
            if first is None or last is None:
                rng = infer_range(staging, slug, slug_ledger(staging, slug))
                if not rng:
                    sys.exit(f"[error] --gate-only: no staging data for slug "
                             f"'{slug}' under {staging} and no --first/--last/"
                             f"--pdf to derive a page range from")
                first = args.first if args.first is not None else rng[0]
                last = args.last if args.last is not None else rng[1]
                print(f"[gate] range inferred from staging data: {first}-{last}")
        gate = compute_gate(staging, slug, first, last)
        sys.exit(0 if print_gate(gate, args.min_verified_ratio, staging) else 2)

    # ---- full run: resolve and validate
    if not args.pdf:
        sys.exit("[error] --pdf is required (unless --gate-only)")
    pdf = Path(args.pdf)
    pdf_exists = pdf.exists()
    count = pdf_page_count(pdf) if pdf_exists else None
    first = args.first if args.first is not None else 1
    last = args.last if args.last is not None else count

    if args.last is None:
        n_s = str(count) if count else "?"
        print("*" * 70)
        print(f"*** WARNING: --first/--last not given. The ENTIRE {n_s}-page PDF")
        print("*** is treated as ONE book. If this file bundles several works")
        print("*** (this exact failure happened before with a two-book PDF),")
        print("*** re-run once per book with explicit --first/--last ranges.")
        print("*" * 70)

    if not args.dry_run:
        if not pdf_exists:
            sys.exit(f"[error] pdf not found: {pdf}")
        if last is None:
            sys.exit("[error] could not determine the PDF page count (need "
                     "poppler's pdfinfo or 'pip install pypdf') — or pass "
                     "--first/--last explicitly")
        if first < 1 or last < first:
            sys.exit(f"[error] bad page range: {first}-{last}")

    # slug-collision check: same slug, different source file
    by_label = slug_ledger(staging, slug)
    conflict = collision_source(by_label, pdf) if by_label else None
    if conflict and not args.dry_run:
        sys.exit(f"[error] slug collision: staging/{slug} already holds pages "
                 f"from a different source:\n        {conflict}\n"
                 f"        pass a distinct --slug to keep the books separate")

    paddle_dir = staging / slug / "paddle"
    range_known = last is not None
    skip_paddle = range_known and paddle_complete(paddle_dir, first, last)
    skip_ingest = range_known and ingest_complete(staging, slug, first, last, by_label)

    # ---- dry run: print the plan and stop (no network, no writes)
    if args.dry_run:
        last_s = str(last) if last is not None else "?"
        print("[plan] DRY RUN - nothing executed, nothing written, no network")
        print(f"[plan] root:        {root}")
        print(f"[plan] pdf:         {pdf}" + ("" if pdf_exists else "  (NOT FOUND)"))
        print(f"[plan] title:       {title}")
        print(f"[plan] slug:        {slug}")
        print(f"[plan] collection:  {collection or '(none - RAG collection default)'}")
        print(f"[plan] out dir:     {out_dir}")
        print(f"[plan] range:       {first}-{last_s}"
              + ("" if range_known else "  (page count unavailable)"))
        print(f"[plan] gate:        verified ratio >= {args.min_verified_ratio} "
              f"else exit 2 + escalation report")
        print(f"[plan] db:          {db}")
        if conflict:
            print(f"[plan] WARNING slug collision would abort the real run: "
                  f"staging/{slug} was built from {conflict}")
        def mark(skip):
            return "[SKIP - already complete]" if skip else "[RUN]"
        print("[plan] steps:")
        print(f"[plan]   1. {script('paddle_ocr.py')} --file {pdf} "
              f"--out {paddle_dir} --first-page 1  {mark(skip_paddle)}")
        print(f"[plan]   2. {script('ingest.py')} --pdf {pdf} --first {first} "
              f"--last {last_s} --slug {slug} --paddle-dir {paddle_dir}  "
              f"{mark(skip_ingest)}")
        print(f"[plan]   3. quality gate on staging/{slug}/verified "
              f"(threshold {args.min_verified_ratio})")
        print(f"[plan]   4. {script('reflow.py')} --slug {slug} --first {first} "
              f"--last {last_s} --title {title} --out {out_dir}")
        print(f"[plan]   5. {script('substrate_build.py')} --slug {slug} "
              f"--out {out_dir} --first {first} --last {last_s} --db {db}")
        print(f"[plan]   6. {script('rag.py')} build"
              + (f" --collection {collection}" if collection else "  (all collections)"))
        entry, would_add = ensure_registered(root, dry_run=True)
        print(f"[plan]   7. registry {REGISTRY}: "
              + (f"would add slug={entry['slug']} ragPort={entry['ragPort']}"
                 if would_add else f"already registered as slug={entry['slug']}"))
        return

    # ---- stage 1: whole-book OCR (PaddleOCR-VL backbone)
    if skip_paddle:
        print(f"[skip] paddle OCR already complete: pg-{first:04d}..pg-{last:04d} "
              f"present in {paddle_dir}")
    else:
        env = env_with_secret("PADDLEOCR_TOKEN", "paddleocr.dat", "paddle_ocr.py")
        run_stage("paddle_ocr", [sys.executable, script("paddle_ocr.py"),
                                 "--file", pdf, "--out", paddle_dir,
                                 "--first-page", 1], env)

    # ---- stage 2: dual-model consensus verification
    if skip_ingest:
        print(f"[skip] ingest already complete: all pages {first}-{last} of slug "
              f"'{slug}' are in the ledger / verified/")
    else:
        env = env_with_secret("OLLAMA_API_KEY", "ollama-cloud.dat", "ingest.py")
        run_stage("ingest", [sys.executable, script("ingest.py"),
                             "--pdf", pdf, "--first", first, "--last", last,
                             "--slug", slug, "--paddle-dir", paddle_dir], env)

    # ---- stage 3: quality gate
    gate = compute_gate(staging, slug, first, last)
    if not print_gate(gate, args.min_verified_ratio, staging):
        sys.exit(2)

    # ---- stage 4: reflow into the vault note
    run_stage("reflow", [sys.executable, script("reflow.py"),
                         "--slug", slug, "--first", first, "--last", last,
                         "--title", title, "--out", out_dir])

    # ---- stage 5: kb_substrate ingestion
    run_stage("substrate_build", [sys.executable, script("substrate_build.py"),
                                  "--slug", slug, "--out", out_dir,
                                  "--first", first, "--last", last, "--db", db])

    # ---- stage 6: RAG index build
    build_cmd = [sys.executable, script("rag.py"), "build"]
    if collection:
        build_cmd += ["--collection", collection]
    build_lines = run_stage("rag build", build_cmd)
    chunk_counts = {}
    for line in build_lines:
        m = re.match(r"\[(.+)\] index saved: (\d+) vectors", line)
        if m:
            chunk_counts[m.group(1)] = int(m.group(2))

    # ---- stage 7: project registry
    entry, added = ensure_registered(root, dry_run=False)
    port = entry.get("ragPort", 8766)

    # ---- stage 8: final report
    key = collection or "default"
    if key in chunk_counts:
        chunks_s = f"{chunk_counts[key]} (collection {key})"
        others = {c: n for c, n in chunk_counts.items() if c != key}
        if others:
            chunks_s += "; also rebuilt " + ", ".join(
                f"{c}={n}" for c, n in sorted(others.items()))
    elif chunk_counts:
        chunks_s = ", ".join(f"{c}={n}" for c, n in sorted(chunk_counts.items()))
    else:
        chunks_s = "unknown (no 'index saved' line in build output)"
    print("=" * 22 + " IMPORT REPORT " + "=" * 22)
    print(f"title:       {title}")
    print(f"slug:        {slug}")
    print(f"range:       pages {first}-{last} ({gate['total']} pages)")
    print(f"verified:    {gate['verified']}/{gate['total']} "
          f"(ratio {gate['ratio']:.4f}, threshold {args.min_verified_ratio})")
    print(f"vault note:  {out_dir / '正文.md'}")
    print(f"collection:  {key}")
    print(f"chunks:      {chunks_s}")
    print(f"registry:    {entry.get('slug')} "
          f"(ragPort {port}, {'added' if added else 'already registered'})")
    print(f"plugin:      start the endpoint with: python scripts\\rag.py serve "
          f"--port {port}")
    print("             then use the Obsidian vault-rag plugin's ask panel")
    print("=" * 59)


if __name__ == "__main__":
    main()
