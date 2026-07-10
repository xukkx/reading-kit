# 工作流（Workflows）

reading-kit 有两条一等公民工作流。它们共享同一个 OCR 骨干（PaddleOCR-VL），由质量门（quality gate）衔接：快速阅读走全自动，质量门不达标的书升级到学术精校。

## 场景一：快速阅读（批量消费型导入）

目标：一条命令把一本书从 PDF 变成 vault 里可读、可批注、可问 AI 的正文。适合大批量、以"读懂"为目的的书。

```powershell
python scripts\import_book.py --pdf D:\书.pdf --title 书名 --collection 分区名
```

一条命令自动完成：

1. PaddleOCR-VL 整本 OCR（`paddle_ocr.py`）
2. 双模型共识校验防幻觉（`ingest.py`：OCR 底稿 vs 独立视觉大模型，字符级比对，判定写入 `staging/ledger.jsonl`）
3. **质量门**：`verified 页数 / 区间总页数 >= --min-verified-ratio`（默认 0.85）。不达标即停（退出码 2），打印逐页失败原因与升级报告，staging 保留复用——**不会**自动跑陪审团
4. 重排为阅读版正文（`reflow.py`，带页码锚点）
5. kb_substrate 入库（`substrate_build.py`）+ RAG 索引（`rag.py build`）
6. 确保项目已注册到 `~\.reading-kit\registry.json`

注意：**多书合订 PDF 必须用 `--first/--last` 按书拆开、分次导入**（此前发生过两本书的合订 PDF 被当成一本导入的事故；不给这两个参数时 import_book.py 会打印显眼警告）。

## 场景二：学术精校（陪审团工作流）

目标：出版级 / 可引用级的文本精度，供学术写作直接引用。流程：

1. PaddleOCR-VL 底稿（与快速阅读同一骨干，staging 产物直接复用）
2. 机械标点迁移（把底稿标点系统性迁移到校对稿，先排除标点噪音）
3. 3 模型陪审团逐页表决（binary rubric 投票）
4. 人工复核轮次（陪审团分歧页逐页裁决，可多轮）

现状：**有意不自动化**。以可改编脚本的形式存在于 张献翼 项目的 `staging/jury_*` 目录与 `swarm-jury` 技能中，每本书按需改编。人工复核是流程的组成部分，不是待修的缺陷。

## 升级策略（两条工作流的衔接）

- **PaddleOCR-VL 是共同骨干**：两条工作流第一步相同。快速阅读被门拦下后，`staging/<slug>/`（OCR 底稿、双模型转录、台账）原样保留、直接复用，不重复花钱。
- **质量门失败（退出码 2）= 升级信号**：说明扫描质量或版式超出了共识校验的能力，这本书转入学术精校工作流处理。
- 反向不成立：学术精校不走质量门，从头到尾人在环内。
