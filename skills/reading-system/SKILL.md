---
name: reading-system
description: Operate, troubleshoot, extend, and distribute the Reading Kit system (OCR pipeline + Obsidian reader plugin + RAG knowledge base). Use for system health checks, onboarding a new user/machine, releasing plugin updates, adding new capabilities, or diagnosing "it doesn't work" reports.
---

# Reading System — Operations Playbook

The end-to-end system built in this project, distributed as https://github.com/xukkx/reading-kit.

## Architecture (know this before touching anything)

```
PC = kitchen                                 any device = dining room
─────────────────────────────                ─────────────────────────
PDF → paddle.ps1 (whole-book OCR)            Obsidian opens vault/
    → ingest.ps1 --paddle-dir (consensus     → Books/<书名>/正文.md (reflowed)
      verify vs qwen; ledger.jsonl)          → plugin vault-rag:
    → reflow.py (reading edition:              selection toolbar ✍️批注/❓问AI,
      headings, page markers, highlights)      page jump #, Q&A panel 💬
    → rag.py build (bge-m3 embeddings,       → reader.css (typography)
      local ollama → cloud fallback)
    → rag.py serve (:8766, plugin backend)
Sync: vault = plain files → remotely-save + 坚果云/S3. Plugin & 使用说明 ship inside the vault.
Keys: DPAPI-encrypted ~\.secrets\*.dat — never plaintext, never in git.
```

Division of judgment: workers grind (OCR, transcripts), Claude judges (adjudication, annotations, ALL vault writes). Every verification verdict → append-only `staging/ledger.jsonl`. Quotes entering vault must grep-match a verified transcript.

## Health check (run when anything "doesn't work")

1. `python scripts\rag.py ask "test" -k 1` — exercises index + embedder + answer provider in one shot
2. Server up? `curl -s -X POST http://localhost:8766 -d '{"q":"test","k":1}'` — if down: `python scripts\rag.py serve` (background)
3. Ollama local up? `ollama list | grep bge-m3` — missing → cloud embeddings fallback needs `.rag/providers.json` embeddings key
4. Plugin loaded? `vault/.obsidian/plugins/vault-rag/manifest.json` version vs what Obsidian shows; `community-plugins.json` contains `"vault-rag"`
5. Keys present? `ls ~\.secrets\` — expect `paddleocr.dat`, `ollama-cloud.dat` (+ optional others)

## Known traps (all field-verified — check these FIRST on user reports)

| Symptom | Cause & fix |
|---|---|
| "0 plugins installed" | User opened the **project root** as vault, not `vault/`. Fix: Manage vaults → Open folder as vault → the `vault` subfolder |
| Ribbon button says 先打开一本书 though book is open | Sidebar stole focus; plugin ≥0.2.3 falls back to most-recent markdown leaf — check version |
| User types commands into search box | Search pane ≠ command palette. Point to the 📖 ribbon menu (built for exactly this) |
| AI 报错 ⚠️ in panel | `rag.py serve` not running on the PC, or phone still pointing at `localhost` instead of PC LAN IP |
| New annotations not retrievable | Index is manual: 📖 menu → 🔄 重建知识库索引 (POST `{"cmd":"rebuild"}`) |
| OCR verify floods "escalated" | Check for systematic cause before re-running (punctuation width, dropped footnotes → containment metric handles; genuinely new pattern → fix `normalize()`/thresholds in ingest.py) |
| Worker errors twice on a page | Rule: Claude reads the page image personally, transcribes, logs `escalated-manual` to ledger |
| `opencode run`: file flag eats prompt | Prompt must precede `--file`; must run from project root |

## Common operations

**Add a book** (scanned): see `read-book` skill — paddle → ingest → adjudicate flagged → reflow (`--page-offset` = 书页 − PDF页) → `rag.py build` → literature note + permanent notes + MOC.

**Plugin change**: edit `vault/.obsidian/plugins/vault-rag/` → `node --check main.js` → bump `manifest.json` version → user Ctrl+R → **sync to kit**: copy to `reading-kit/plugin/vault-rag/`, commit, push.

**Skill/script change**: same sync rule — project copy is the dev copy, `reading-kit/` is the release copy. Generalize before syncing (no personal paths/history).

**Release to kit**:
```powershell
Copy-Item vault\.obsidian\plugins\vault-rag\* reading-kit\plugin\vault-rag\ -Force
# + any changed scripts/ or generalized skills/
cd reading-kit; git add -A; git commit -m "..."; git push
```

**Onboard a new user/machine**: point them to the repo README (`git clone` → `pwsh -File setup.ps1`). The wizard handles deps, keys, vault creation. Their first stop after install: vault 里的 [[使用说明]]. Design constraint to preserve: **the user only memorizes the 📖 ribbon icon** — every new feature must be reachable from that menu and documented in 使用说明.md.

## Design invariants (do not regress these)

1. Workers never write into `vault/` — staging only; Claude promotes.
2. Ledger is append-only; every adjudication/spot-check/correction gets a record.
3. Anti-hallucination = two independent readers + containment-aware comparison; conservative heading detection (false heading breaks text; missed heading is cosmetic).
4. Everything the user needs ships *inside the vault* (plugin, 使用说明, CSS) so sync = distribution.
5. Providers are config, not code (`.rag/providers.json`, `opencode.json`); keys DPAPI-only.
6. Portability: embeddings auto-detect local → cloud; no GPU assumptions; plugin is pure JS (mobile).
