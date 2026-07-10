---
name: library
description: Cross-project library manager ("书架总管") — one session manages ALL reading projects. Shows what the reader owns (books, collections, pending adjudications, running servers) as a visual dashboard or plain chat text, and routes any new book import to the right project without changing directories. Use when the user asks "我有哪些书 / 书架 / library / 书库总览", drops a PDF and says import it, or asks which project/collection something belongs to. Designed for the Reading_Hub mega-directory but works from any registered project.
---

# Library — the one-session manager for all reading projects

The single source of truth is `~\.reading-kit\registry.json` — every project
(slug, path, ragPort) is registered there by setup.ps1. This skill makes ONE
Claude Code session (ideally opened in the hub directory) manage all of them:
inventory, imports, adjudication routing, servers. Never ask the user to open
another session in another folder.

## Ground rules

- **Resolve the toolkit first**: `library.py` lives in reading-kit's `scripts\`.
  From the hub use the `reading-kit` junction/folder; otherwise any registered
  project's `scripts\library.py` is the same file. Call it as
  `python -X utf8 <kit>\scripts\library.py` — `-X utf8` is mandatory on Windows
  (cp1252 chokes on Chinese titles).
- **Run project scripts with cwd = that project's root** (`import_book.py`,
  `rag.py`, `import_server.py` resolve `Input/`, `staging/`, `.rag/`
  relatively). The hub's junction folders make
  `cd Reading_Hub\<slug>` equivalent to the real project root.
- Ports: AI answers = `ragPort` (registry) · import console = `ragPort + 100`.

## 1. Inventory — "我有哪些书？"

```powershell
python -X utf8 <kit>\scripts\library.py            # markdown, chat-ready
python -X utf8 <kit>\scripts\library.py --json     # when YOU need the data
python -X utf8 <kit>\scripts\library.py --html <out>  # dashboard page
```

It reports, per project: collections → books (pages, RAG chunks, on-shelf
status), annotation counts, staging books with **pages pending human
adjudication**, recent import jobs with gate results, and whether the AI
server / import console are actually up (live port probe).

**Presentation — pick the richest channel available, in this order:**

1. **Artifact tool available** (claude.ai/code, desktop): write the HTML to a
   STABLE path — `<hub>\dashboard\library.html` (create `dashboard\` if
   missing; same path every time so the artifact URL stays stable) — then
   publish with `Artifact(file_path=..., favicon="📚")`. Keep favicon 📚
   forever. Also give a 2-3 sentence chat summary: totals + anything needing
   attention (pending adjudications, failed gates, down servers).
2. **No Artifact but SendUserFile exists**: send the HTML with
   `display: "render"`.
3. **Plain CLI chat**: print the `--markdown` output directly — it is designed
   to be pasted as-is. Never skip inventory just because no visual channel
   exists.

Lead with attention items, not raw listings: pending 精校 pages, exit-2
escalations, and servers that are down are what the reader needs to act on.

## 2. Import routing — "把这本书导入"

The user gives a PDF (often dropped into the hub's `Inbox\`). Do NOT make them
choose a directory — route it:

1. Decide **target project + collection**. Infer from the book's subject vs.
   existing collections; if genuinely ambiguous, ask ONE question offering the
   registry's projects/collections (plus "new collection"). New collection =
   just a new folder name — no setup needed.
2. Copy the PDF into `<project>\Input\` (H:-drive sources are read-only —
   always copy, never move).
3. Prefer the console if its port answers (job queue + adjudication UI in one
   place): give the user `http://localhost:<ragPort+100>/` and offer to start
   it if down: `python scripts\import_server.py` (cwd = project root,
   run detached/background). Otherwise run the CLI directly:
   `python -X utf8 scripts\import_book.py --pdf Input\<f>.pdf --title <书名> --collection <合集>`.
4. **Multi-book PDF → `--first/--last`, one book per run** (loud warning
   exists, don't rely on it).
5. Report the gate result and route the outcome exactly as the
   `getting-started` skill Stage 3 describes (pass / adjudicate via console
   人工精校 / exit-2 → 学术精校 jury). After anything changed, re-run the
   inventory so the user sees the new shelf state.

## 3. Housekeeping the hub

- **New project**: `pwsh -File <kit>\setup.ps1 -Target <hub>\<Name> -Slug <slug>`
  — physically inside the hub, auto-registered, no port/sync collisions.
  Existing projects elsewhere on disk: add a junction so they appear in the hub:
  `New-Item -ItemType Junction -Path <hub>\<slug> -Target <realpath>`.
- **Servers**: check with inventory's probe; start with
  `python scripts\rag.py serve` / `python scripts\import_server.py`
  (cwd = project root, detached). Port collisions across projects:
  `pwsh -File scripts\check_sync.ps1`.
- Symptom-level troubleshooting ("插件不亮 / AI 报错 / 0 plugins") →
  **reading-system** skill. New-user walkthrough → **getting-started** skill.

## Known shapes & gotchas (field-verified)

- Two vault layouts coexist: standard `Books\<合集>\<书名>\正文.md` and legacy
  flat `Books\<书名>\卷N.md` (zhangxianyi). `library.py` handles both; when
  writing new content ALWAYS use the standard shape.
- Legacy projects may index everything into `.rag\default\` — chunk counts in
  the inventory are computed by file-path prefix across all indexes, so they
  stay correct either way.
- `staging\` dirs that aren't book-shaped (`jury_*`, `ocr`, `_jobs`) are
  intentionally excluded from pending-adjudication counts — the jury workflow
  tracks its own state.
- A book can be on the shelf AND have leftover staging pages (post-hoc audit
  fixes); `on_shelf: true` staging entries are informational, not alerts.
