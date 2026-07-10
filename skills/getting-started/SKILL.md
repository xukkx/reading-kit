---
name: getting-started
description: Interactive step-by-step onboarding for new reading-kit users — walks them through the FULL journey one stage at a time: one-time setup → first book import (web console or CLI) → quality gate & human adjudication → reading/annotating/asking AI in Obsidian → mobile sync → growing the library. Use when a user says "how do I use this", "教我怎么用", "getting started", "带我导入第一本书", "走一遍全流程", "新手引导", or seems lost about what to do next with reading-kit.
---

# Getting Started — guided onboarding for reading-kit

You are the guide, not a manual. Do NOT dump this whole file at the user. Probe
their state, find the first incomplete stage, and walk **one stage per turn**:
explain why the stage matters (one sentence), run or give the exact command,
**verify it actually worked** before advancing. Speak the user's language
(usually中文). Run commands FOR the user whenever you have tool access; only
hand them a command when it needs their interaction (e.g. entering API keys).

## State probe (run first, silently)

```powershell
Get-Content ~\.reading-kit\registry.json          # no file → Stage 1 (never set up)
ls <ROOT>\vault\Books\                            # empty → Stage 2 (no book yet)
ls <ROOT>\staging\                                # has <slug>/ but vault empty → import in progress / gate-blocked → Stage 3
python scripts\rag.py ask "test" -k 1             # errors → index/server problem, see reading-system skill
ls <ROOT>\vault\.obsidian\plugins\vault-rag\      # missing → plugin not installed → Stage 1 incomplete
```

Match the user to the journey map and start at their first incomplete stage.

## Journey map

| Stage | Goal | Done when |
|---|---|---|
| 0 心智模型 | Understand what the system is | User can say what happens to a PDF |
| 1 一次性安装 | Machine + project set up | `registry.json` has the project; Obsidian opens the vault; keys saved |
| 2 第一本书 | PDF → 可读正文 in vault | Job passes the quality gate; `vault/Books/<合集>/<书名>/正文.md` exists |
| 3 质量门三岔口 | Handle gate outcomes | Pass → done · flagged pages adjudicated · fail(exit 2) → escalate to 学术精校 |
| 4 读·批注·问 AI | Daily reading loop in Obsidian | User has made an annotation and asked the Q&A panel one question |
| 5 手机与同步 | Read anywhere | Phone opens the vault; AI panel answers via PC's LAN IP |
| 6 批量扩库 | Many books, least steps | User queues 2+ books in the console unattended |

## Stage 0 — 心智模型 (30 seconds)

One paragraph, then move on:

> PC 是厨房，其他设备是餐厅。厨房里：PDF → PaddleOCR-VL 整本识别 → 第二个视觉大
> 模型独立转录、逐字比对（防幻觉的**双证人**机制）→ 质量门 → 重排成带页码锚点的
> 阅读版 `正文.md` → 建 RAG 索引。餐厅里：Obsidian 打开 vault，选中文字即可批注
> 或问 AI，答案永远带页码引用。机器文本进入 vault 只有两条路：双模型共识，或你
> 亲手裁决——**没有第三条路**。

Two first-class workflows (they share the OCR backbone, the quality gate joins them):
- **快速阅读** — one command per book, fully automatic. The default.
- **学术精校** — publication-grade jury workflow (3-model voting + human rounds)
  for books the gate rejects. Deliberately human-in-the-loop. See `WORKFLOWS.md`.

## Stage 1 — 一次性安装

```powershell
git clone https://github.com/xukkx/reading-kit && cd reading-kit
pwsh -File setup.ps1 -Target D:\my-reading -Slug myreading
```

setup.ps1 handles: project identity (auto-assigns unique sync dir + rag port into
`~\.reading-kit\registry.json` — never hand-pick these), dependency checks
(Python 3.10+, poppler for scanned PDFs, ollama+bge-m3 for free local
embeddings), **API keys** (interactive; DPAPI-encrypted to `~\.secrets\*.dat`,
never plaintext — user must type these themselves), vault + plugin install, and
firewall rule for mobile access.

Then in Obsidian: **Manage vaults → Open folder as vault → the `vault`
SUBFOLDER** of the target, not the project root. This is the #1 new-user trap
("0 plugins installed" = they opened the root). Verify: Settings → Community
plugins shows `vault-rag` enabled.

Keys needed: `paddleocr.dat` (OCR, required for scanned books), `ollama-cloud.dat`
(consensus model B + Q&A answers). Re-run setup.ps1 anytime to add missing keys.

## Stage 2 — 第一本书 (the default path: web console)

Drop the PDF into `<ROOT>\Input\`, then:

```powershell
python scripts\import_server.py    # port = 本项目 ragPort + 100 (unregistered: 8830)
```

Open `http://localhost:<port>/` → 新书导入 form: pick the PDF from the dropdown
(auto-discovers `Input\**\*.pdf` + project root), fill 书名 + 合集(collection),
choose mode 正式导入. Jobs queue strictly serially (protects cloud OCR spend);
each job card shows live log + gate report. State survives server restarts
(`staging\_jobs\<id>\`).

CLI equivalent (the console is a thin wrapper — CLI always works standalone):

```powershell
python scripts\import_book.py --pdf Input\书.pdf --title 书名 --collection 合集名
```

Set expectations, from a field-verified real run: a 205-page scanned book took
**~54 minutes unattended**, 94.6% pages auto-verified, ~10 minutes of human
review after. Tell the user to go do something else and come back.

**Traps at this stage:**
- 多书合订 PDF **must** be split with `--first/--last` (page range in the console
  form) and imported one book at a time. Omitting them prints a loud warning.
- Re-running an interrupted/failed import is safe and cheap: completed stages
  (OCR, consensus) are detected in `staging\<slug>\` and skipped.
- `--dry-run` (演练) previews the plan; `--gate-only` (仅质检) re-checks the gate
  without importing — useful after adjudication.

## Stage 3 — 质量门三岔口

The gate = verified pages / total pages in range, threshold `--min-verified-ratio`
(default 0.85). Three outcomes:

1. **Pass** → pipeline continues automatically to 正文.md + RAG index. Done.
2. **Pass, but some pages flagged** → console section 3 (人工精校) lists pending
   pages per book. Click 开始精校: scan image left, model A/B transcripts right;
   pick A / pick B / hand-write final text (keys: `A` / `B` / `Ctrl+Enter` save /
   `→` skip); disagreeing pages show a diff. Saving writes
   `staging\<slug>\verified\pg-XXXX.md` + a ledger line — downstream treats the
   page exactly like an auto-verified one. **When only ONE model's transcript
   exists (模型B shows 无该模型转录), specifically check the page bottom for
   dropped 脚注 and quoted passages missing ==emphasis== — pure-Paddle text
   loses both** (field-verified failure signature). After adjudicating, re-run
   仅质检 to confirm, then re-run the import (skips completed stages).
3. **Fail — exit code 2** → this book exceeds what consensus can handle
   (woodblock scans, bad print). Escalate to 学术精校: staging artifacts are
   reused as-is (no re-OCR cost). Route via the `swarm-jury` skill; the jury
   scripts live in the 张献翼 project's `staging/jury_*` as adaptable templates.
   This is deliberately NOT automated — human review rounds are part of the
   design, not a defect.

## Stage 4 — 读·批注·问 AI (daily loop)

Make sure the answer server runs on the PC: `python scripts\rag.py serve`
(port from registry, default 8766). Then in Obsidian:

- Open `Books\<合集>\<书名>\正文.md`. Page anchors (`data-p`) let citations jump
  to the exact page.
- Select text → floating toolbar: **✍️批注** (annotation, saved to 10-Notes) or
  **❓问AI** (asks with the selection as context).
- 📖 ribbon menu = command center: open Q&A panel 💬, jump to page #,
  **🔄 重建知识库索引** — run this after adding annotations, indexing is manual
  by design. Rebuilds are incremental (only changed files re-embed).
- Answers cite 书名+页码; quotes entering the vault must match a verified
  transcript — if the AI can't cite it, it doesn't say it.

Have the user actually do one annotation + one question before calling this
stage done.

## Stage 5 — 手机与同步

Vault = plain files. Sync via remotely-save + 坚果云/S3 (the remote dir was
auto-assigned by setup.ps1; plugin config ships inside the vault, so the phone
just opens the synced folder in Obsidian mobile). For AI answers on the phone:
plugin settings → endpoint = `http://<PC局域网IP>:<ragPort>` (setup.ps1 printed
it; firewall was opened in Stage 1). The PC must be on and serving. Trap: phone
still pointing at `localhost` → ⚠️ in the panel.

## Stage 6 — 批量扩库

Adding book #20 does not re-process books #1–19: RAG builds are incremental
(per-file cache), each collection is an independent index partition, and the
console queue is serial — so the user can queue an evening's worth of PDFs and
review gate reports next morning. 快速阅读 books stay one-command; anything the
gate rejects goes to the jury path. Both scenarios stay supported forever —
never trade one away for the other.

## Where to go deeper

- Operations / troubleshooting / releases / "it doesn't work": **reading-system**
  skill (health-check sequence + field-verified trap table). Symptom-level fixes
  live there, not here.
- Workflow rationale + console details: `WORKFLOWS.md` in the repo.
- 学术精校 execution: **swarm-jury** skill.
- Reading and note-taking practice (how to read with this system, not how to run
  it): **read-book** and **note** skills.
