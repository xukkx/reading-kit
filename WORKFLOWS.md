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

## 批量导入（import_batch.py）

几十上百本书不必逐本填表：**文件夹就是元数据**。把 PDF 按合集丢进收件箱——

```
Inbox\女性文学\书名A.pdf     ← 子文件夹名 = 合集，文件名 = 书名
Inbox\比较文学\书名B.pdf
```

```powershell
python scripts\import_batch.py D:\AI_Projects\Reading_Hub\Inbox        # 只看计划
python scripts\import_batch.py D:\AI_Projects\Reading_Hub\Inbox --go   # 全部提交进控制台串行队列
```

- 已在架的书自动跳过（`--force` 重导）；控制台对排队中的重复 slug 返回 409——重跑安全。
- `--go` 把 PDF 复制进项目 `Input\`、逐本提交任务，原件移入 `Inbox\_done\`：**收件箱清空即进度条**。批量报告写入 `Inbox\_reports\`。
- 目标项目：`--project`，或收件箱旁 `hub.json` 的 `defaultProject`。控制台没开：先启动，或 `--go --run` 就地串行执行。
- **合订本不能批量**——单独用 `--first/--last` 拆书导入。

## 网页控制台（import_server.py）

「快速阅读」导入的本地网页界面，浏览器里填表提交、看队列：

```powershell
python scripts\import_server.py                 # 默认端口 = 本项目 ragPort + 100（complit 8866 / zhangxianyi 8867；未注册项目 8830）
python scripts\import_server.py --port 8888 --root D:\某个部署
```

- 打开 `http://localhost:<端口>/`：新书导入表单（PDF 下拉 + 手填路径、书名、合集、页码区间、校验阈值），三种模式：正式导入 / 演练（dry-run）/ 仅质检（gate-only）。
- 任务**严格串行**排队执行（控制云端 OCR 花费与限速）；每个任务卡片上有实时日志和质量门报告（校验比、逐页失败原因表），门失败的书按上面的升级策略提示转入学术精校。
- 任务状态与日志落盘在 `staging\_jobs\<id>\`（job.json + job.log），服务重启后历史仍在。
- 它只是 `import_book.py` 的薄封装，不含任何新的管线逻辑：**CLI 始终可独立使用**。
- **人工精校**（v1.5）：控制台第三区按 slug 汇总待精校页（有转录但无 verified 文件的页，按台账状态分组：待裁决 / 已升级 / 出错 / 无台账）。点「开始精校」进入逐页视图：左边扫描图、右边模型 A/B 转录，选 A、选 B 或手写最终文本（快捷键 A / B / Ctrl+Enter 保存 / → 跳过），有分歧的页可看 diff。
- 保存即写 `staging/<slug>/verified/pg-XXXX.md` 并向 `staging/ledger.jsonl` 追加一条 status=verified、note=`human adjudicated: <action>` 的台账行（沿用该页原行的 similarity/containment）——质量门与 reflow.py 从此把该页当作已核验，下游脚本零改动。
- 建议流程：精校完成后重跑「仅质检（gate-only）」确认达标，再走正式导入的后续步骤（重跑 import 会自动跳过已完成的 OCR/共识阶段）。误存的页删掉对应 verified/ 文件即可回退（v1.5 不提供界面撤销）。
