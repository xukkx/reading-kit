# 📖 Reading Kit — 自己的「微信读书 + 知识库 + AI」

把任何 PDF（包括扫描版）变成：**手机/电脑上排版舒服的可读正文** + **划线批注** + **知识图谱** + **能引用你笔记回答问题的 AI**。全部数据是你自己电脑里的 markdown 文件。

```
扫描PDF → OCR(双引擎共识校验，防幻觉) → 重排成阅读版 → Obsidian 里读
                                                    │
        选中文字 → ✍️批注(自动记页码) / ❓问AI(答案带来源，可跳回原页)
                                                    │
              批注、笔记、AI存档 → 本地RAG索引 → AI 越用越懂你的库
```

## 安装（Windows）

前置：[Python 3.10+](https://www.python.org/downloads/)（勾选 Add to PATH）、[Obsidian](https://obsidian.md)

```powershell
git clone https://github.com/xukkx/reading-kit.git
cd reading-kit
pwsh -File setup.ps1          # 或 powershell -File setup.ps1
```

向导会：检查依赖 → 引导保存 API 密钥（DPAPI 加密，只属于你的 Windows 账户）→ 生成项目（vault、插件、Claude 技能、配置）。

需要的（免费/低价）服务，按需配置：

| 用途 | 服务 | 获取 |
|---|---|---|
| 扫描书 OCR（引擎） | PaddleOCR-VL | aistudio.baidu.com 免费令牌。**导入扫描书必需** |
| 扫描书 OCR 校验 + AI 问答 | Ollama Cloud | ollama.com/settings。**导入扫描书必需**（每页由它做第二遍独立转录防幻觉），同时兼任问答模型 |
| AI 问答（可换） | DeepSeek / 任何 OpenAI 兼容 API | 各官网；`.rag/providers.json` 里随便换 |
| 知识库嵌入 | 本地 Ollama（免费，无需显卡）或 SiliconFlow 云端 | ollama.com |

> 只读纯文字 PDF、不导扫描书的话，前两个密钥都可以先不配。

## 日常使用

1. **导入书**（按顺推荐）：
   - **新手**：在项目目录打开 [Claude Code](https://claude.com/claude-code)，说「带我入门」——`getting-started` 技能分七步手把手带完全程
   - **网页控制台**：`python -X utf8 scripts\import_server.py` → 浏览器打开 `http://localhost:<RAG端口+100>`，选 PDF、填书名、看进度条。自带防重复导入（同书名、同文件都会拦下确认）
   - **一条命令**：`python -X utf8 scripts\import_book.py --pdf D:\书.pdf --title 书名`——OCR、共识校验、质量闸门、排版、索引一条龙；质量不达标会停下并给出升级报告
   - 底层的 4 条流水线命令（`CLAUDE.md` 里）仍可手动逐步跑，供调试用
2. **读**：Obsidian 打开 `vault` → `Books/` → 正文。大纲=目录；📖 图标菜单=全部功能（跳页码等）
3. **批注 / 问AI**：选中文字，弹出小工具条 → ✍️ 或 ❓（问答需电脑跑着 `python scripts\rag.py serve`）
4. **多设备**：vault 是纯文件夹——用 [remotely-save](https://github.com/remotely-save/remotely-save) 插件 + 坚果云(WebDAV)/S3 同步；插件和使用说明会跟着 vault 一起同步到每台设备
5. **忘了怎么用**：vault 里的 [[使用说明]]，或对 Claude Code 说「书架」

## 书多了以后：一站式书房（Hub）

```powershell
pwsh -File setup.ps1 -Hub D:\我的书房
```

生成一个**唯一入口目录**：每个阅读项目一个 junction 快捷入口、`Inbox\` 批量导入收件箱（`Inbox\<合集>\书名.pdf`，文件夹=合集、文件名=书名，一条命令全部入队）、书架管家技能（对 Claude Code 说「书架」即出全馆清单，说「导入 Inbox」即批量导入——**你永远不需要手动搬文件**）。项目多了随时重跑该命令补挂新项目。

## 组成

```
plugin/vault-rag/   Obsidian 插件：问答面板、划线批注、跳页码、选中弹条（手机可用）
scripts/            流水线：paddle_ocr(整本OCR) → ingest(共识校验+台账) → reflow(重排) → rag(问答)
  import_book.py      一条命令导入（串起全流水线 + 质量闸门）
  import_server.py    网页导入控制台（选文件、填元数据、看进度、防重复；端口=RAG端口+100）
  import_batch.py     批量导入：Inbox 收件箱扫一遍全入队；--scan/--pull 从来源文件夹自动发现新书
  library.py          跨项目书架总览（markdown / JSON / 自包含 HTML 仪表盘）
  worker.ps1          （可选）opencode 云端廉价模型代工，Claude 复核
WORKFLOWS.md        两大工作流：快速阅读(一条命令/控制台) / 学术精校(陪审团) 及升级策略
skills/             Claude Code 技能（setup 会装进项目的 .claude/skills/）：
  getting-started     新用户分步引导（说「带我入门」）
  library             书架管家：全馆清单、导入路由、批量导入（Hub 里说「书架」）
  read-book / note / organize-vault   导入一本书 / 记笔记 / 整理库
  reading-system      运维手册：健康检查、已知坑、发布流程、设计不变量
  worker              （可选）云端代工委托与问责层
templates/          vault 模板（含阅读排版CSS、使用说明）、CLAUDE.md、providers.json、hub 模板、opencode.json
vendor/             可选依赖拖入处（如 kb_substrate——放进来即启用，无需安装命令）
setup.ps1           一键安装；-Hub 模式生成一站式书房
```

## 开发 / 维护（给未来的你或接手的人）

- **维护手册在 `skills/reading-system/SKILL.md`**：架构图、健康检查步骤、全部实战验证过的已知坑、设计不变量。改任何东西前先读它
- **双拷贝模型**：项目里的 `scripts/`、`.claude/skills/`、`vault/.obsidian/plugins/` 是**开发拷贝**；本仓库是**发布拷贝**。改动先在项目里验证，再泛化（去掉个人路径）同步回本仓库提交
- **技能即文档**：每个 `skills/*/SKILL.md` 教 Claude Code 一类操作，`setup.ps1` 会把它们装进新项目。加新功能时同步更新对应技能和 `WORKFLOWS.md`

## 设计原则

- **防幻觉**：每页由 OCR 引擎 + 视觉大模型独立转录，字符级比对；分歧升级人工裁决；所有判定写入只追加的 `staging/ledger.jsonl`。进入笔记的引文必须能在校验稿里找到
- **本地优先**：笔记、索引、密钥都在本机；嵌入自动检测本地 Ollama，没有才走云端
- **API 可换**：问答模型是 `providers.json` 里的一行配置

## License

MIT
