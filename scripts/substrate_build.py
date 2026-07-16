#!/usr/bin/env python3
"""
substrate_build.py — ingest one staging/<slug>/ batch of already-verified OCR
pages into kb_substrate's SQLite storage (durable, queryable atoms).

Does NOT touch ingest.py's output — reads it as-is:
  staging/ledger.jsonl              (shared across slugs; provenance per page)
  staging/<slug>/verified/pg-XXXX.md
  staging/<slug>/pages/pg-XXXX.png

Only pages that made it to verified/ (ingest.py's consensus already passed)
are ingested here; adjudicate/escalated pages are out of scope until a human
promotes them.

--out (recommended): same convention as reflow.py's --out (e.g.
"vault/Books/比较文学/比较文学论") — source_uri becomes the vault-relative
book path ("Books/比较文学/比较文学论/正文.md") instead of the raw PDF/staging
path, so rag.py's file_collection() can derive a collection from it later
with no kb_substrate change. A staging slug that becomes more than one book
(one PDF, several works) needs one invocation per book, scoped with
--first/--last to that book's page range; omit both to apply --out to every
page in the slug (the common single-book-per-slug case). Without --out at
all, source_uri falls back to the ledger's raw source path (no collection
attribution possible from it).

Idempotent: staging/<slug>/substrate_ledger.jsonl records which page numbers
have already been ingested (by atom_id), so re-running only picks up new
pages. Use --force to re-ingest everything (creates new occurrence rows,
kb_substrate never overwrites).

Usage:
  python scripts/substrate_build.py --slug yuedu-heji --out "vault/Books/比较文学/比较文学论" --first 10 --last 32
  python scripts/substrate_build.py --slug yuedu-heji --force
"""
import argparse, json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "vault"
STAGING = ROOT / "staging"
REGISTRY = Path.home() / ".reading-kit" / "registry.json"
# Optional dependency drop-in: a kb_substrate/ folder placed in <project>\vendor\
# is importable with no pip install (the package is not on PyPI).
VENDOR = ROOT / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))


def book_source_uri(out_arg):
    """Vault-relative book path matching reflow.py's --out convention and
    rag.py's file_collection() expectations, e.g. "Books/比较文学/比较文学论/正文.md".
    Falls back to the folder name if --out isn't actually under vault/."""
    out_path = Path(out_arg)
    out_path = out_path if out_path.is_absolute() else ROOT / out_path
    try:
        rel = out_path.resolve().relative_to(VAULT.resolve())
    except ValueError:
        rel = Path(out_path.name)
    return str(rel / "正文.md").replace("\\", "/")


def project_id():
    """This project's registry slug (identifies it in kb_substrate's shared
    SQLite the same way it already identifies it for sync/RAG-port purposes).
    Falls back to the folder name for a vault not yet registered."""
    try:
        projects = json.loads(REGISTRY.read_text(encoding="utf-8"))["projects"]
        for p in projects:
            if Path(p["path"]).resolve() == ROOT.resolve():
                return p["slug"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        pass
    return ROOT.name


def load_ledger_for_slug(slug):
    """staging/ledger.jsonl is shared across all slugs; index the records
    belonging to this slug's pages dir by page label (e.g. "pg-0012")."""
    ledger_path = STAGING / "ledger.jsonl"
    by_label = {}
    if not ledger_path.exists():
        return by_label
    pages_dir = str((STAGING / slug / "pages").resolve())
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if str(Path(rec.get("image", "")).resolve().parent) == pages_dir:
            by_label[rec["page"]] = rec  # last record for a label wins (re-run overwrites)
    return by_label


def load_substrate_ledger(slug):
    f = STAGING / slug / "substrate_ledger.jsonl"
    done = {}
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["page_number"]] = rec
    return done


def append_substrate_ledger(slug, rec):
    f = STAGING / slug / "substrate_ledger.jsonl"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--out", default=None,
                     help="vault-relative book folder, reflow.py's --out convention "
                          "(e.g. vault/Books/比较文学/比较文学论) -- sets source_uri to "
                          "the vault path instead of the raw PDF/staging path")
    ap.add_argument("--first", type=int, default=None,
                     help="first page --out applies to (default: every page in the slug)")
    ap.add_argument("--last", type=int, default=None,
                     help="last page --out applies to (default: every page in the slug)")
    ap.add_argument("--db", default=None, help="defaults to .rag/kb.sqlite3")
    ap.add_argument("--force", action="store_true", help="re-ingest pages already in substrate_ledger.jsonl")
    args = ap.parse_args()

    try:
        import kb_substrate  # deferred: only required once the substrate work actually runs
    except ImportError:
        print("[substrate] kb_substrate 未安装 — 已跳过原子化入库（可选功能，"
              "不影响阅读与 AI 问答）。启用方法：把 kb_substrate 文件夹放进项目根的 "
              "vendor\\ 目录即可，无需任何安装命令。")
        sys.exit(0)

    vdir = STAGING / args.slug / "verified"
    pages_dir = STAGING / args.slug / "pages"
    if not vdir.exists():
        sys.exit(f"no such staging slug: {vdir}")

    db_path = str(Path(args.db) if args.db else ROOT / ".rag" / "kb.sqlite3")
    Path(db_path).parent.mkdir(exist_ok=True)
    pid = project_id()
    kb_substrate.ensure_project(db_path, pid)

    ledger_by_label = load_ledger_for_slug(args.slug)
    already_done = {} if args.force else load_substrate_ledger(args.slug)

    ingested, skipped, missing_ledger = 0, 0, 0
    for f in sorted(vdir.glob("pg-*.md")):
        label = f.stem  # "pg-0012"
        page_number = int(label.split("-")[1])
        if page_number in already_done:
            skipped += 1
            continue

        rec = ledger_by_label.get(label)
        if not rec:
            missing_ledger += 1
            print(f"WARNING no ledger record for {label}, ingesting without provenance")
            rec = {}

        # pages/ filenames aren't guaranteed to share verified/'s zero-padding
        # width (pdftoppm pads to its own scheme) -- trust the ledger's own
        # recorded image path first, only fall back to matching by numeric
        # value (not by reconstructing a padding width) if that's missing.
        image_path = Path(rec["image"]) if rec.get("image") else None
        if not image_path or not image_path.exists():
            image_path = next((p for p in pages_dir.glob("pg-*.png")
                                if int(p.stem.split("-")[1]) == page_number), None)

        in_range = (args.first is None or page_number >= args.first) and \
                   (args.last is None or page_number <= args.last)
        if args.out and in_range:
            source_uri = book_source_uri(args.out)
        else:
            source_uri = rec.get("source", f"staging/{args.slug}")

        atom_id = kb_substrate.ingest_verified_page(
            db_path=db_path,
            project_id=pid,
            source_uri=source_uri,
            page_number=page_number,
            text=f.read_text(encoding="utf-8"),
            image_path=str(image_path) if image_path and image_path.exists() else None,
            provenance=rec,
        )
        append_substrate_ledger(args.slug, {"page_number": page_number, "atom_id": atom_id})
        ingested += 1

    print(f"[{args.slug}] ingested {ingested}, skipped {skipped} (already done), "
          f"{missing_ledger} without a ledger match -> {db_path}")


if __name__ == "__main__":
    main()
