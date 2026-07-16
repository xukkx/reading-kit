---
name: worker
description: Delegate bulk/mechanical work to cheap cloud-model workers (Ollama cloud) with a verification and accountability layer. Use for scanned-page OCR, first-pass summaries, batch drafts, translations — anything high-volume where Claude orchestrates and verifies rather than grinds.
---

# Cloud Worker Delegation — with Accountability Layer

Workers are billed to the user's Ollama cloud plan (swappable to any cheap plan — that's the design). API key: DPAPI-encrypted at `~\.secrets\ollama-cloud.dat`; scripts decrypt it themselves — never echo or store plaintext.

**Optional feature.** The OCR pipeline (paddle/ingest) works without it. `worker.ps1` additionally needs the [opencode](https://opencode.ai) CLI on PATH and an `opencode.json` at the project root (setup ships a template listing the cloud models). No opencode → skip this skill; everything else in the kit is unaffected.

## Core principle

**Claude judges, workers grind.** Workers do token-heavy mechanical work; Claude does judgment (what to annotate, how to link) and is the ONLY writer to `vault/`. Everything workers produce lands in `staging/` first and passes verification before promotion.

## The scripts

| Script | What | When |
|---|---|---|
| `scripts\paddle.ps1` / `paddle_ocr.py` | **Batch OCR**: whole PDF/image → PaddleOCR-VL-1.6 job API → per-page `pg-XXXX.paddle.md`. Token: `~\.secrets\paddleocr.dat` (DPAPI) | First step for every scanned source — one job OCRs the entire document in seconds |
| `scripts\ingest.ps1` | **Verified ingestion**: consensus check of PaddleOCR output vs an independent vision-LLM transcript → `staging/<slug>/verified/` + ledger | Second step; use `--paddle-dir` |
| `scripts\ocr.ps1` | Single stateless vision call (one image → text) | Quick one-off looks; NOT for ingestion (no verification) |
| `scripts\worker.ps1` | opencode headless agentic text worker (`-Model ollama/<model>`) | Batch drafting/summarizing over already-verified transcripts |

Standard flow for a scanned source:
```powershell
pwsh -NoProfile -File scripts\paddle.ps1 --file <pdf> --out staging\<slug>\paddle          # 1. batch OCR
pwsh -NoProfile -File scripts\ingest.ps1 --pdf <pdf> --first 1 --last N --slug <slug> --paddle-dir staging\<slug>\paddle   # 2. verify
```
(`--images "a.jpg,b.jpg"` instead of `--pdf` for handwritten-note photos; paddle.ps1 also accepts single images.)

## Model roles (updated 2026-07-03)

- **OCR engine (model A)**: `PaddleOCR-VL-1.6` — dedicated document parser, whole-doc batch jobs
- **Independent verifier (model B)**: `qwen3.5:397b` — vision LLM re-transcription with `==highlight==` markers (verified-transcript text comes from B: it carries the highlights, consensus guarantees the characters)
- **Gemini 3 Flash**: NOT for OCR — reassign to cheap non-OCR roles: summaries, article-boundary detection, drafting
- Tie-break third vote: `gemma3:27b` or manual

## Anti-hallucination: engine + model consensus

Every page gets two **independent** readings (OCR engine + vision LLM). Different systems don't hallucinate identically, so:

- **similarity ≥ 0.97** → `verified` — transcript trusted, written to `verified/`
- **0.90–0.97** → `adjudicate` — a `.diff` file is written; Claude reads the diff AND the page image, decides the correct text, writes the corrected transcript to `verified/` and appends an adjudication record to the ledger
- **< 0.90** → `escalated` — Claude reads the page image personally, transcribes/verifies it fully
- **error** → retry once; twice failed → Claude does the page

Plus **random spot-checks**: per batch, Claude visually reads 1–2 pages that auto-verified and compares against the transcript. A failed spot-check invalidates the batch's trust — re-verify with a third model or manually.

## Accountability: the ledger

`staging/ledger.jsonl` — append-only provenance log. One record per page: timestamp, source, page, both models, similarity score, status, error if any. Adjudications append a second record (`"status": "adjudicated", "by": "claude", "notes": ...`). Never edit or delete ledger lines.

**Quote-anchoring rule (hard):** every quotation that enters a vault note MUST exist in a `staging/**/verified/` transcript — check with Grep before writing. If a quote can't be anchored, verify against the page image or drop it. This makes every vault quote traceable: vault note → verified transcript → ledger record → source page.

## Input-type matrix

| Source | Pipeline |
|---|---|
| **Scanned PDF** (no text layer — check `pdffonts`) | `ingest.ps1 --pdf` → dual-model OCR consensus |
| **Text-layer PDF** (papers, ebooks) | `pdftotext -enc UTF-8` extract = **ground truth, no OCR risk**. Workers only summarize; quotes grep-anchored to the extracted text. No dual-model needed. |
| **Handwritten notes** (photos) | `ingest.ps1 --images` — same dual-model flow; expect lower similarity, more adjudication; use `□` for illegible |
| **Markdown/txt files** | Already text — copy to staging as-is (it IS the verified transcript), workers may summarize |
| **Mixed collections** (several works bound in one PDF) | Ingest in batches of 20–30 pages; detect article boundaries from verified transcripts (title patterns), update the collection's literature note table |

## Worker summaries are advisory

Vault notes are written by Claude **from verified transcripts**, never copied from a worker's summary. A worker summary is a map for where to look, not a source of content. Any claim in a summary must be traceable to transcript lines before it influences a note.

## Models on the current plan

Text: `deepseek-v4-flash` (fast default), `glm-5.2` (stronger), `qwen3-coder:480b`, `kimi-k2.7-code` (code). Vision: `gemini-3-flash-preview` (default A), `qwen3.5:397b` (default B), `gemma3:27b` (spare third for tie-breaks).
Provider swap: update `opencode.json` provider block + key file + defaults in `scripts/ingest.py`; skills stay unchanged.

## Known issues

- OpenCode Zen/Go credentials invalid (401) as of 2026-07-03 — don't use `opencode/*` models until renewed.
- `opencode run`: prompt must precede `--file` flags; must run from project root (per-directory config).
- Never route vision through opencode's agent loop (verified failure: it shelled out to a broken local tool instead of reading the image).
