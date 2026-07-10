# 阅读知识库（Reading Kit）

个人知识管理系统：Obsidian vault + Claude Code 技能 + 本地 RAG 问答。
笔记是纯 markdown；`[[双链]]` 构成 Obsidian 原生知识图谱。

## 布局

- `vault/` — Obsidian 知识库（在 Obsidian 里打开**这个文件夹**，不是项目根目录）
  - `00-Inbox/` 捕捉 → `10-Notes/<收藏>/` 永久原子笔记 → `30-MOCs/` 主题地图
  - `20-Literature/<收藏>/` 文献笔记；`Books/<收藏>/<书名>/` 重排阅读版；`90-Attachments/` PDF 原件；`_templates/` 模板
  - **收藏（collection）**：`Books/`、`10-Notes/`、`20-Literature/` 下同名的子文件夹（如 `比较文学`）——不只是整理，也是 RAG 索引的分区单元（见下）。新内容优先归到已有收藏；只有真正的新主题才新建
- `scripts/` — 流水线：`paddle.ps1`（整本 OCR）→ `ingest.ps1`（双引擎共识校验）→ `reflow.py`（重排成书）→ `rag.py`（build/ask/serve 知识库问答，`--collection` 指定收藏）
- `staging/` — 工作区：转录稿、`ledger.jsonl`（校验台账，只追加不修改）。worker 输出只进这里，**永不直接写 vault**
- `.claude/skills/` — `read-book`、`note`、`organize-vault`、`reading-system`
- `.rag/` — 每个收藏一个子目录：`.rag/<收藏>/{index.json,vectors.npy}`；没有收藏文件夹的内容归入 `.rag/default/`。`providers.json` 可切换任意 OpenAI 兼容 API

## 约定

- 一条笔记一个想法；标题是断言或概念；文件名=标题（禁用 `\ / : * ? " < > | [ ] # ^`）
- 每条笔记 frontmatter：`created`、`tags`（小写-连字符）、`aliases`
- 链接密度优先于文件夹：文件夹是仓库，链接才是组织——收藏这一层文件夹例外，它同时决定 RAG 检索范围
- 引文必须能在 `staging/**/verified/` 或 pdftotext 提取稿中 grep 到（防幻觉锚定规则）
- 永不删改用户笔记内容，除非明确同意；大改先提案

## 工作流（scanned PDF → 可读可问的书）

```powershell
pwsh -File scripts\paddle.ps1 --file <pdf> --out staging\<slug>\paddle
pwsh -File scripts\ingest.ps1 --pdf <pdf> --first 1 --last N --slug <slug> --paddle-dir staging\<slug>\paddle
python scripts\reflow.py --slug <slug> --first 1 --last N --title <书名> --out "vault/Books/<收藏>/<书名>"
python scripts\rag.py build --collection <收藏>
```
校验规则见 `read-book` 技能：共识≥0.97 自动通过；分歧由 Claude 对照页面图像裁决并记入 ledger。
