#!/usr/bin/env python3
r"""
import_batch.py — sweep a whole folder of PDFs into the import pipeline.

THE convention (folder = metadata, zero forms):

    Inbox\
      女性文学\        ← subfolder name = target 合集 (collection)
        书名A.pdf      ← filename = 书名 (title)
        书名B.pdf
      比较文学\
        书名C.pdf
      书名D.pdf        ← loose PDF: --collection, else goes to 未分类

One command plans/executes the whole tree:

    python scripts/import_batch.py D:\AI_Projects\Reading_Hub\Inbox           # PLAN only
    python scripts/import_batch.py D:\AI_Projects\Reading_Hub\Inbox --go     # copy + submit to console queue
    python scripts/import_batch.py Inbox --go --run                          # no console: run serially here

The user should NEVER move files by hand. Book sources (folders where PDFs
naturally accumulate — download dirs, archive drives) are registered once in
hub.json next to the Inbox:

    {"defaultProject": "complit",
     "sources": [{"path": "H:\\...\\女性文学材料包", "collection": "女性文学"}]}

then discovery is automatic:

    python scripts/import_batch.py Inbox --scan            # list candidates: 新 / 已在架 / 疑似已导入
    python scripts/import_batch.py Inbox --pull all        # copy every 新 candidate into Inbox
    python scripts/import_batch.py Inbox --pull 2=倾城之恋 --pull 3   # pick + retitle
    # then the normal plan / --go flow above

Behavior:
  * Books already on the shelf (vault/Books/<coll>/<title>/正文.md) are skipped
    (--force to re-import). The console additionally rejects duplicate queued
    slugs (409), so re-running --go is safe.
  * --go copies each PDF into <project>\Input\ then submits POST /api/import
    to the project's console (ragPort+100). On success the Inbox original
    moves to Inbox\_done\<coll>\ — the emptying Inbox IS the progress bar.
  * --run executes import_book.py serially in-process instead (for headless
    use without the console); per-book logs + a summary land in
    Inbox\_reports\.
  * Target project: --project SLUG, else hub.json next to the Inbox
    ({"defaultProject": "slug"}), else the registry's only project.
  * Multi-book bound PDFs (合订本) CANNOT be batched — they need --first/--last
    per book. Put them aside and import individually.

Per repo convention this script never imports its siblings.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

REGISTRY = Path.home() / ".reading-kit" / "registry.json"
CONSOLE_PORT_OFFSET = 100
SKIP_DIRS = {"_done", "_reports"}


def load_registry():
    if not REGISTRY.exists():
        sys.exit("[batch] no ~/.reading-kit/registry.json — run setup.ps1 first")
    return json.loads(REGISTRY.read_text(encoding="utf-8")).get("projects", [])


def pick_project(args_project, inbox):
    projects = load_registry()
    if args_project:
        for p in projects:
            if p["slug"] == args_project:
                return p
        sys.exit(f"[batch] project '{args_project}' not in registry "
                 f"({', '.join(x['slug'] for x in projects)})")
    hub_cfg = inbox.parent / "hub.json"
    if hub_cfg.exists():
        try:
            slug = json.loads(hub_cfg.read_text(encoding="utf-8")).get("defaultProject")
        except (OSError, json.JSONDecodeError):
            slug = None
        if slug:
            for p in projects:
                if p["slug"] == slug:
                    return p
            sys.exit(f"[batch] hub.json defaultProject '{slug}' not in registry")
    if len(projects) == 1:
        return projects[0]
    sys.exit("[batch] several projects registered — pass --project SLUG or set "
             f"defaultProject in {hub_cfg} "
             f"(choices: {', '.join(x['slug'] for x in projects)})")


def clean_title(stem):
    """Filename → vault folder name: drop illegal chars, collapse whitespace."""
    t = re.sub(r'[<>:"/\\|?*]', " ", stem)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t


def suggest_title(stem):
    """Aggressive cleanup for DISCOVERED files (z-library junk, author parens,
    trailing page ranges). Suggestions only — --pull N=标题 overrides."""
    t = re.sub(r"[（(][^）)]*[）)]", " ", stem)        # any parenthesized group
    t = re.sub(r"[\s_]*\d+\s*[-—]\s*\d+\s*$", " ", t)  # trailing page range
    return clean_title(t)


def load_hub(inbox):
    cfg = inbox.parent / "hub.json"
    if not cfg.exists():
        return {}
    try:
        return json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"[batch] hub.json unreadable: {e}")


def known_pdf_sizes(inbox):
    """Sizes of every PDF already inside the pipeline: hub Inbox (incl. _done)
    + every registered project's Input/. Byte-size match = same file already
    imported, even if it was renamed on the way in."""
    sizes = set()
    for pdf in inbox.rglob("*.pdf"):
        sizes.add(pdf.stat().st_size)
    for p in load_registry():
        input_dir = Path(p["path"]) / "Input"
        if input_dir.is_dir():
            for pdf in input_dir.rglob("*.pdf"):
                sizes.add(pdf.stat().st_size)
    return sizes


def shelved_titles():
    """All book titles on any registered project's shelf (both vault shapes)."""
    titles = set()
    for p in load_registry():
        books = Path(p["path"]) / "vault" / "Books"
        if not books.is_dir():
            continue
        for cdir in books.iterdir():
            if not cdir.is_dir():
                continue
            subdirs = [d for d in cdir.iterdir() if d.is_dir()]
            titles.update(d.name for d in subdirs)
            if not subdirs and any(cdir.glob("*.md")):
                titles.add(cdir.name)
    return titles


def scan_sources(inbox, hub):
    """Discover candidate PDFs in registered sources, dedup against everything
    already in the pipeline. Deterministic order (source order, then path)."""
    sources = hub.get("sources") or []
    if not sources:
        sys.exit("[batch] no sources in hub.json — add "
                 '{"sources": [{"path": "...", "collection": "..."}]} '
                 "(folders where the user's book PDFs accumulate)")
    known_sizes = known_pdf_sizes(inbox)
    shelf = shelved_titles()
    candidates = []
    for src in sources:
        base = Path(src["path"])
        if not base.is_dir():
            print(f"[batch] ⚠ source missing, skipped: {base}", file=sys.stderr)
            continue
        for pdf in sorted(base.rglob("*.pdf")):
            rel = pdf.relative_to(base)
            coll = src.get("collection") or (rel.parts[0] if len(rel.parts) > 1
                                             else "未分类")
            title = suggest_title(pdf.stem)
            if title in shelf or clean_title(pdf.stem) in shelf:
                status = "已在架"
            elif pdf.stat().st_size in known_sizes:
                status = "疑似已导入"
            else:
                status = "新"
            candidates.append({"pdf": pdf, "collection": coll, "title": title,
                               "status": status,
                               "mb": round(pdf.stat().st_size / 1e6, 1)})
    return candidates


def parse_pull(specs, candidates):
    """--pull all | --pull N | --pull N=标题 (repeatable) → items to copy."""
    if any(s.strip().lower() == "all" for s in specs):
        return [c for c in candidates if c["status"] == "新"]
    picked = []
    for spec in specs:
        m = re.match(r"^\s*(\d+)\s*(?:=(.+))?$", spec)
        if not m:
            sys.exit(f"[batch] bad --pull '{spec}' (use all / N / N=标题)")
        idx = int(m.group(1))
        if not 1 <= idx <= len(candidates):
            sys.exit(f"[batch] --pull {idx}: out of range 1..{len(candidates)}")
        item = dict(candidates[idx - 1])
        if m.group(2):
            item["title"] = clean_title(m.group(2).strip())
        picked.append(item)
    return picked


def scan_inbox(inbox, default_collection):
    """Yield {pdf, collection, title} for every candidate, deterministic order."""
    plan = []
    for pdf in sorted(inbox.glob("*.pdf")):
        plan.append({"pdf": pdf, "collection": default_collection,
                     "title": clean_title(pdf.stem)})
    for sub in sorted(d for d in inbox.iterdir()
                      if d.is_dir() and d.name not in SKIP_DIRS):
        for pdf in sorted(sub.rglob("*.pdf")):
            plan.append({"pdf": pdf, "collection": sub.name,
                         "title": clean_title(pdf.stem)})
    return plan


def on_shelf(root, item):
    return (root / "vault" / "Books" / item["collection"] / item["title"] / "正文.md").exists()


def stage_pdf(root, pdf):
    """Copy Inbox PDF into <project>/Input/ (reuse identical existing copy)."""
    dest = root / "Input" / pdf.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == pdf.stat().st_size:
        return dest
    if dest.exists():
        dest = dest.with_stem(dest.stem + "-" + datetime.now().strftime("%H%M%S"))
    shutil.copy2(pdf, dest)
    return dest


def submit_console(port, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/import",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            return None, str(e)
    except OSError as e:
        return None, f"console unreachable ({e})"


def archive(inbox, item):
    dest_dir = inbox / "_done" / item["collection"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / item["pdf"].name
    if dest.exists():
        dest = dest.with_stem(dest.stem + "-" + datetime.now().strftime("%H%M%S"))
    shutil.move(str(item["pdf"]), dest)


def main():
    ap = argparse.ArgumentParser(description="batch-import every PDF under a folder")
    ap.add_argument("inbox", nargs="?", default=None,
                    help="folder to sweep (default: <hub>/Inbox if this is a hub, else ./Inbox)")
    ap.add_argument("--project", help="target registry slug")
    ap.add_argument("--collection", default="未分类",
                    help="collection for loose PDFs directly in the inbox (default: 未分类)")
    ap.add_argument("--mode", default="import", choices=("import", "dry-run"),
                    help="job mode submitted per book (default: import)")
    ap.add_argument("--min-verified-ratio", type=float, default=None)
    ap.add_argument("--go", action="store_true",
                    help="actually copy + submit (default is plan-only)")
    ap.add_argument("--run", action="store_true",
                    help="with --go: run import_book.py serially here instead of "
                         "submitting to the console")
    ap.add_argument("--force", action="store_true",
                    help="include books already on the shelf")
    ap.add_argument("--scan", action="store_true",
                    help="discover candidate PDFs in hub.json sources (no copying)")
    ap.add_argument("--pull", action="append", default=[], metavar="SPEC",
                    help="copy scanned candidates into the inbox: all / N / N=标题 "
                         "(repeatable; 'all' = every 新 candidate)")
    ap.add_argument("--json", action="store_true",
                    help="with --scan: machine-readable candidate list")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    inbox = Path(args.inbox or "Inbox").resolve()
    if not inbox.is_dir():
        sys.exit(f"[batch] inbox folder not found: {inbox}")

    if args.scan or args.pull:
        candidates = scan_sources(inbox, load_hub(inbox))
        if args.scan:
            if args.json:
                print(json.dumps(
                    [{**c, "pdf": str(c["pdf"])} for c in candidates],
                    ensure_ascii=False, indent=1))
            else:
                if not candidates:
                    print("[batch] sources scanned — no PDFs found")
                for i, c in enumerate(candidates, 1):
                    print(f"  {i:>2}. [{c['status']}] {c['collection']} / "
                          f"{c['title']}  ({c['mb']} MB)  ← {c['pdf']}")
                fresh = sum(1 for c in candidates if c["status"] == "新")
                print(f"\n[batch] {len(candidates)} PDF in sources, {fresh} 新 — "
                      f"pull with --pull all (只拉新书) or --pull N[=标题]")
            if not args.pull:
                return 0
        pulled = parse_pull(args.pull, candidates)
        for item in pulled:
            dest = inbox / item["collection"] / f"{item['title']}.pdf"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                print(f"  = already in inbox: {dest.name}")
                continue
            shutil.copy2(item["pdf"], dest)
            print(f"  ⬇ {item['collection']} / {item['title']}.pdf")
        print(f"\n[batch] {len(pulled)} 本已入收件箱 — next: plan "
              f"(no flags) then --go")
        return 0

    proj = pick_project(args.project, inbox)
    root = Path(proj["path"])
    port = (proj.get("ragPort") or 0) + CONSOLE_PORT_OFFSET

    plan = scan_inbox(inbox, args.collection)
    if not plan:
        print(f"[batch] no PDFs under {inbox} — nothing to do")
        return 0

    todo, skipped = [], []
    for item in plan:
        (skipped if (not args.force and on_shelf(root, item)) else todo).append(item)

    print(f"[batch] project={proj['slug']} root={root}")
    print(f"[batch] {len(plan)} PDF found, {len(todo)} to import, {len(skipped)} already on shelf\n")
    for item in todo:
        print(f"  → {item['collection']} / {item['title']}   ({item['pdf'].name})")
    for item in skipped:
        print(f"  ✓ on shelf, skip: {item['collection']} / {item['title']}")

    if not args.go:
        print("\n[batch] plan only — re-run with --go to execute "
              "(--go --run for headless serial mode)")
        return 0

    results = []
    for item in todo:
        staged = stage_pdf(root, item["pdf"])
        rel = staged.relative_to(root).as_posix()
        if args.run:
            cmd = [sys.executable, "-X", "utf8", str(root / "scripts" / "import_book.py"),
                   "--pdf", rel, "--title", item["title"],
                   "--collection", item["collection"]]
            if args.min_verified_ratio:
                cmd += ["--min-verified-ratio", str(args.min_verified_ratio)]
            if args.mode == "dry-run":
                cmd += ["--dry-run"]
            print(f"\n[batch] ▶ {item['title']} …")
            rc = subprocess.run(cmd, cwd=root).returncode
            state = {0: "passed", 2: "escalate"}.get(rc, f"error({rc})")
            results.append((item, state))
            if rc == 0:
                archive(inbox, item)
        else:
            payload = {"pdf": rel, "title": item["title"],
                       "collection": item["collection"], "mode": args.mode}
            if args.min_verified_ratio:
                payload["min_verified_ratio"] = args.min_verified_ratio
            out, err = submit_console(port, payload)
            if err and "unreachable" in err:
                sys.exit(f"\n[batch] import console not running on :{port} — start it "
                         f"first (python scripts\\import_server.py in {root}) or use "
                         f"--go --run for headless mode. Nothing was submitted for "
                         f"'{item['title']}'; already-submitted jobs stay queued.")
            state = f"queued {out['id']}" if out else f"rejected: {err}"
            results.append((item, state))
            print(f"  {'✓' if out else '✗'} {item['title']} → {state}")
            if out:
                archive(inbox, item)

    reports = inbox / "_reports"
    reports.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    lines = [f"# 批量导入 {stamp} — project {proj['slug']}", ""]
    lines += [f"- {it['collection']} / {it['title']}: {st}" for it, st in results]
    lines += [f"- (skipped, on shelf) {it['collection']} / {it['title']}" for it in skipped]
    (reports / f"batch-{stamp}.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n[batch] report: {reports / f'batch-{stamp}.md'}")
    if not args.run:
        print(f"[batch] watch the queue: http://localhost:{port}/  "
              f"(jobs run one at a time; gate failures need 精校/升级 as usual)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
