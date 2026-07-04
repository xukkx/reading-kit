---
name: read-book
description: Import a PDF/book into the Obsidian vault — OCR with verification, build a reflowable reading edition, then read/annotate passage-by-passage and extract permanent notes. Use when the user wants to read, annotate, summarize, or take notes on a PDF, book, paper, or article.
---

# PDF / Book Reading Workflow

The Obsidian vault is the `vault/` folder at this project's root. All pipeline scripts are in `scripts/`.

## Phase A — Ingest (scanned PDFs)

1. Copy the PDF into `vault/90-Attachments/` (sanitize the filename for Windows).
2. Check for a text layer with `pdffonts`. **Text-layer PDFs**: `pdftotext -enc UTF-8` extract is ground truth — skip to Phase B with it. **Scanned PDFs**: continue.
3. Batch-OCR the whole file: `pwsh -File scripts\paddle.ps1 --file <pdf> --out staging\<slug>\paddle`
4. Verify with engine+LLM consensus: `pwsh -File scripts\ingest.ps1 --pdf <pdf> --first 1 --last <N> --slug <slug> --paddle-dir staging\<slug>\paddle`
   - similarity ≥ 0.97 (or B-superset containment ≥ 0.97) → auto-verified
   - `adjudicate`/`escalated` → YOU read the `.diff` AND the page image, decide, write the corrected transcript to `verified/`, append the verdict to `staging/ledger.jsonl`
   - worker errors twice on a page → YOU read the page image and transcribe it yourself
   - spot-check 1–2 auto-verified pages per batch against their images
5. **Quote-anchoring rule (hard)**: any quote entering the vault must grep-match a verified transcript.

## Phase B — Build the reading edition

```
python scripts\reflow.py --slug <slug> --first <a> --last <b> --title <书名> --page-offset <offset> --out "vault/Books/<书名>"
```
(offset: book page = pdf page + offset.) This merges cross-page paragraphs, strips page numbers, converts section titles to headings, keeps `==highlights==`, and embeds visible/jumpable page markers. Then `python scripts\rag.py build` so the book is retrievable.

For multi-article collections: detect article boundaries from transcript openings, build one Books/ entry per article, and maintain a collection literature note with a per-article table + progress line.

## Phase C — Read, annotate, extract

1. Create a literature note in `vault/20-Literature/` (use `vault/_templates/literature.md`): frontmatter (author/year/source/status), summary.
2. Annotate what matters — reader `==highlights==` in the source are prime annotation targets:
   `> "exact quote" (p.N)` + `💭 comment` (in the user's language). Link concepts to existing notes inline.
3. Extract the 3–6 strongest ideas into atomic notes in `vault/10-Notes/` (one idea per note, wikilinks, tags); list them under the literature note's "Extracted ideas"; make each reachable from a MOC in `vault/30-MOCs/`.
4. Update the Reading MOC and record progress (page to resume from) in the literature note.
5. Work chapter-by-chapter on long books; check in with the user between chapters.

## Rules

- Always cite page numbers with quotes.
- Resuming: read the literature note's Progress line and continue from there.
- Summarizing every page is not the goal — judgment about what matters is.
