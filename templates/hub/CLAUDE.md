# Reading Hub — 所有阅读项目的唯一入口

This is the **mega-directory**: open Claude Code HERE for anything related to
reading, books, imports, or the vaults. One session manages every project —
never ask the user to open a session in another folder.

**First move in any session about books: load the `library` skill**
(`.claude\skills\library\SKILL.md`) and show the inventory.

## What's in here

| Entry | What it is |
|---|---|
{{PROJECT_ROWS}}| `reading-kit\` | junction → the toolkit repo (source of truth for scripts/skills) |
| `Inbox\` | drop new PDFs here — `Inbox\<合集>\书名.pdf` (folder = collection, filename = title). Batch-sweep: `python -X utf8 reading-kit\scripts\import_batch.py Inbox --go`. Originals move to `Inbox\_done\` as they're queued |
| `hub.json` | `defaultProject` for batch imports + `sources`: folders where the user's book PDFs accumulate. Empty Inbox? `import_batch.py Inbox --scan` discovers candidates there, `--pull` fetches them — **the user never moves files by hand** |
| `dashboard\library.html` | stable path for the published 书架总览 artifact |
| `.claude\skills\` | library (manager) · getting-started (onboarding) · reading-system (ops) |

The registry `~\.reading-kit\registry.json` is the source of truth for
projects/ports — junctions here are conveniences, not the registry.

## Conventions

- **Inventory**: `python -X utf8 reading-kit\scripts\library.py` (always `-X utf8`).
- **Imports**: route per the library skill — copy PDF to `<project>\Input\`,
  run that project's `import_book.py`/console **with cwd = the project root**
  (junction cd works: `cd <slug>` ≡ the real root).
- **New reading project**: `pwsh -File reading-kit\setup.ps1 -Target <本目录>\<Name>`
  — physically inside the hub, auto-registered. Existing projects elsewhere:
  don't move them (sync/ports/Obsidian all point at the real path) — re-run
  `pwsh -File reading-kit\setup.ps1 -Hub <本目录>` to refresh junctions instead.
- Books/vault content is NOT git-tracked; only the toolkit repo is.
- Archive/source drives may be read-only — copy PDFs out, never modify in place.
