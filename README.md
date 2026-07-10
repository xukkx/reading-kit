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
git clone https://github.com/<你>/reading-kit.git
cd reading-kit
pwsh -File setup.ps1          # 或 powershell -File setup.ps1
```

向导会：检查依赖 → 引导保存 API 密钥（DPAPI 加密，只属于你的 Windows 账户）→ 生成项目（vault、插件、Claude 技能、配置）。

需要的（免费/低价）服务，按需配置：

| 用途 | 服务 | 获取 |
|---|---|---|
| 扫描书 OCR | PaddleOCR-VL | aistudio.baidu.com 免费令牌 |
| AI 问答 | Ollama Cloud / DeepSeek / 任何 OpenAI 兼容 API | 各官网；`.rag/providers.json` 里随便换 |
| 知识库嵌入 | 本地 Ollama（免费，无需显卡）或 SiliconFlow 云端 | ollama.com |

## 日常使用

1. **导入书**：装了 [Claude Code](https://claude.com/claude-code) 的话直接说 `/read-book D:\书.pdf`——OCR、校验、排版、笔记一条龙；没装就手动跑 `CLAUDE.md` 里的 4 条命令
2. **读**：Obsidian 打开 `vault` → `Books/` → 正文。大纲=目录；📖 图标菜单=全部功能（跳页码等）
3. **批注 / 问AI**：选中文字，弹出小工具条 → ✍️ 或 ❓（问答需电脑跑着 `python scripts\rag.py serve`）
4. **多设备**：vault 是纯文件夹——用 [remotely-save](https://github.com/remotely-save/remotely-save) 插件 + 坚果云(WebDAV)/S3 同步；插件和使用说明会跟着 vault 一起同步到每台设备
5. **忘了怎么用**：vault 里的 [[使用说明]]

## 组成

```
plugin/vault-rag/   Obsidian 插件：问答面板、划线批注、跳页码、选中弹条（手机可用）
scripts/            流水线：paddle_ocr(整本OCR) → ingest(共识校验+台账) → reflow(重排) → rag(问答)
WORKFLOWS.md        两大工作流：快速阅读(import_book 一条命令导入) / 学术精校(陪审团) 及升级策略
skills/             Claude Code 技能：read-book / note / organize-vault
templates/          vault 模板（含阅读排版CSS、使用说明）、CLAUDE.md、providers.json
setup.ps1           一键安装
```

## 设计原则

- **防幻觉**：每页由 OCR 引擎 + 视觉大模型独立转录，字符级比对；分歧升级人工裁决；所有判定写入只追加的 `staging/ledger.jsonl`。进入笔记的引文必须能在校验稿里找到
- **本地优先**：笔记、索引、密钥都在本机；嵌入自动检测本地 Ollama，没有才走云端
- **API 可换**：问答模型是 `providers.json` 里的一行配置

## License

MIT
