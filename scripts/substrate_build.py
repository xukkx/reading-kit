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

Idempotent: staging/<slug>/substrate_ledger.jsonl records which page numbers
have already been ingested (by atom_id), so re-running only picks up new
pages. Use --force to re-ingest everything (creates new occurrence rows,
kb_substrate never overwrites).

Usage:
  python scripts/substrate_build.py --slug yuedu-heji
  python scripts/substrate_build.py --slug yuedu-heji --force
"""
import argparse, json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "staging"
REGISTRY = Path.home() / ".reading-kit" / "registry.json"


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
    ap.add_argument("--db", default=None, help="defaults to .rag/kb.sqlite3")
    ap.add_argument("--force", action="store_true", help="re-ingest pages already in substrate_ledger.jsonl")
    args = ap.parse_args()

    import kb_substrate  # deferred: only required once the substrate work actually runs

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

        atom_id = kb_substrate.ingest_verified_page(
            db_path=db_path,
            project_id=pid,
            source_uri=rec.get("source", f"staging/{args.slug}"),
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
