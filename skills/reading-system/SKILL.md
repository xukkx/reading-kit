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
    → rag.py serve (port from registry, default :8766, plugin backend)
Sync: vault = plain files → remotely-save + 坚果云/S3. Plugin & 使用说明 ship inside the vault.
Keys: DPAPI-encrypted ~\.secrets\*.dat — never plaintext, never in git.
Multi-project identity: ~\.reading-kit\registry.json — one {slug, remoteBaseDir, ragPort} per project, assigned by setup.ps1. Never hand-pick these; check for collisions with `scripts\check_sync.ps1`.
Collections (within one project): Books/<Collection>/<书名>/, mirrored into 10-Notes/<Collection>/, 20-Literature/<Collection>/. Each collection is a fully independent RAG partition — `.rag/<collection>/{index.json,vectors.npy}` — so retrieval never crosses collections. Collection names come ONLY from Books/ (a Books/<X>/ containing further subfolders); 10-Notes/20-Literature reuse that same set. A file with no matching collection folder lands in `.rag/default/`.
```

Division of judgment: workers grind (OCR, transcripts), Claude judges (adjudication, annotations, ALL vault writes). Every verification verdict → append-only `staging/ledger.jsonl`. Quotes entering vault must grep-match a verified transcript.

## Health check (run when anything "doesn't work")

1. `python scripts\rag.py ask "test" -k 1` — exercises index + embedder + answer provider in one shot
2. Server up? Check this project's port first — `vault/.obsidian/plugins/vault-rag/data.json` (`endpoint` field) or `~\.reading-kit\registry.json` — then `curl -s -X POST http://localhost:<port> -d '{"q":"test","k":1}'`; if down: `python scripts\rag.py serve --port <port>` (background)
3. Ollama local up? `ollama list | grep bge-m3` — missing → cloud embeddings fallback needs `.rag/providers.json` embeddings key
4. Plugin loaded? `vault/.obsidian/plugins/vault-rag/manifest.json` version vs what Obsidian shows; `community-plugins.json` contains `"vault-rag"`
5. Keys present? `ls ~\.secrets\` — expect `paddleocr.dat`, `ollama-cloud.dat` (+ optional others)
6. Multiple reading-kit projects on this machine? `pwsh -File scripts\check_sync.ps1` — flags any two projects sharing a remote sync dir or RAG port
7. Multi-collection vault? `python scripts\rag.py build` and check the printed per-collection chunk counts look right — a collection with a suspiciously huge or tiny count may mean `10-Notes/` or `20-Literature/` has a same-named subfolder that isn't actually a collection (see the known trap below)

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
| Two projects' notes/settings merging on sync | Remote sync dir or RAG port collided across projects. Should be structurally impossible (registry auto-assigns both) — means someone hand-edited a WebDAV remote-dir field or `~\.reading-kit\registry.json`. Run `scripts\check_sync.ps1` to find the pair |
| One collection's index has way more/fewer chunks than expected, or a topic's AI answers feel oddly incomplete | A `10-Notes/<X>/` or `20-Literature/<X>/` subfolder shares a name with something that isn't a real Books/ collection (e.g. an `X/` used for a "full text vs stub" split, unrelated to topic). `discover_collections()` only trusts collection names that exist as `Books/<X>/` with further subfolders inside — anything else silently falls into `default`, which is correct; but if you *do* want that folder treated as a collection, add a matching `Books/<X>/<something>/` |

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

**Onboard a new user/machine**: point them to the repo README (`git clone` → `pwsh -File setup.ps1 -Target <dir>`, optional `-Slug <name>`). The wizard handles deps, keys, vault creation, AND registers the project in `~\.reading-kit\registry.json` — auto-assigning a globally-unique sync remote-dir and RAG port, pre-filled into 同步设置.md/使用说明.md and the plugin's `data.json`. Nobody needs to hand-pick or remember these values. Their first stop after install: vault 里的 [[使用说明]]. Design constraint to preserve: **the user only memorizes the 📖 ribbon icon** — every new feature must be reachable from that menu and documented in 使用说明.md.

**Add a second/third project on the same machine**: same `setup.ps1` command with a different `-Target`. Identity (slug/remote-dir/port) is assigned automatically and never collides with prior projects — verify anytime with `scripts\check_sync.ps1`.

## Design invariants (do not regress these)

1. Workers never write into `vault/` — staging only; Claude promotes.
2. Ledger is append-only; every adjudication/spot-check/correction gets a record.
3. Anti-hallucination = two independent readers + containment-aware comparison; conservative heading detection (false heading breaks text; missed heading is cosmetic).
4. Everything the user needs ships *inside the vault* (plugin, 使用说明, CSS) so sync = distribution.
5. Providers are config, not code (`.rag/providers.json`, `opencode.json`); keys DPAPI-only.
6. Portability: embeddings auto-detect local → cloud; no GPU assumptions; plugin is pure JS (mobile).
7. Multi-project identity (sync remote-dir, RAG port) is allocated by the global registry, never hand-typed — a human picking names is exactly the failure mode that causes silent cross-project data merges.
8. Collection discovery is asymmetric on purpose: only Books/ nesting defines a collection name; 10-Notes/20-Literature reuse that set rather than each independently inferring collections from their own subfolders. Don't "simplify" this to symmetric per-root inference — it was tried, and it silently fractured an existing project's single research corpus into disconnected partitions the first time a non-collection subfolder (e.g. a full-text-vs-stub split) happened to share a name pattern.
