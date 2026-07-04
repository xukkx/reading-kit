# setup.ps1 — Reading Kit 一键安装
# 用法：git clone 后，在 reading-kit 目录里运行  pwsh -File setup.ps1 [-Target <安装目录>]
param([string]$Target = (Join-Path (Split-Path $PSScriptRoot -Parent) "my-reading"))

$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
Write-Host "`n📖 Reading Kit 安装向导" -ForegroundColor Cyan
Write-Host "   安装到：$Target`n"

# ---------- 1. 依赖检查 ----------
Write-Host "1️⃣  检查依赖…"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "❌ 需要 Python 3.10+：https://www.python.org/downloads/（安装时勾选 Add to PATH）" -ForegroundColor Red; exit 1 }
Write-Host "   ✅ python $(python --version 2>&1)"

python -c "import numpy" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "   📦 安装 numpy…"; python -m pip install -q numpy }
python -c "import requests" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "   📦 安装 requests…"; python -m pip install -q requests }
Write-Host "   ✅ numpy / requests"

$poppler = Get-Command pdftoppm -ErrorAction SilentlyContinue
if ($poppler) { Write-Host "   ✅ poppler (pdftoppm)" }
else { Write-Host "   ⚠️ 未找到 pdftoppm（扫描版 PDF 校验需要）。安装：scoop install poppler 或 choco install poppler。纯文字 PDF 不受影响。" -ForegroundColor Yellow }

$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollama) {
    Write-Host "   ✅ ollama（本地嵌入，免费）— 拉取 bge-m3 模型…"
    ollama pull bge-m3 2>$null | Out-Null
} else {
    Write-Host "   ℹ️ 未装 Ollama — 知识库将使用云端嵌入（需在 providers.json 配置 embeddings 密钥）。想免费本地跑：https://ollama.com 装好后执行 ollama pull bge-m3" -ForegroundColor Yellow
}

# ---------- 2. API 密钥（DPAPI 加密保存，只属于当前 Windows 用户） ----------
Write-Host "`n2️⃣  API 密钥（直接回车=跳过；之后可重跑本脚本补充）"
New-Item -ItemType Directory -Force "$HOME\.secrets" | Out-Null
$keys = @(
    @{n='PaddleOCR 令牌（扫描书 OCR，aistudio.baidu.com 免费申请）'; f='paddleocr.dat'},
    @{n='Ollama Cloud 密钥（问答模型，ollama.com/settings）';        f='ollama-cloud.dat'},
    @{n='DeepSeek 密钥（可选，platform.deepseek.com）';               f='deepseek.dat'},
    @{n='SiliconFlow 密钥（可选：云端嵌入 + MiMo）';                   f='siliconflow.dat'}
)
foreach ($k in $keys) {
    $path = "$HOME\.secrets\$($k.f)"
    if (Test-Path $path) { Write-Host "   ✅ $($k.f) 已存在，跳过"; continue }
    $v = Read-Host "   🔑 $($k.n)"
    if ($v.Trim()) {
        ConvertTo-SecureString $v.Trim() -AsPlainText -Force | ConvertFrom-SecureString | Set-Content $path
        icacls $path /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
        Write-Host "   ✅ 已加密保存 → $path"
    }
}

# ---------- 3. 创建项目 ----------
Write-Host "`n3️⃣  创建项目结构…"
New-Item -ItemType Directory -Force $Target | Out-Null
Copy-Item "$kit\scripts"  "$Target\" -Recurse -Force
New-Item -ItemType Directory -Force "$Target\staging" | Out-Null
if (-not (Test-Path "$Target\vault")) {
    Copy-Item "$kit\templates\vault" "$Target\vault" -Recurse
    Write-Host "   ✅ vault 已创建（含阅读排版、插件配置）"
} else { Write-Host "   ℹ️ vault 已存在，未覆盖" }
Copy-Item "$kit\plugin\vault-rag" "$Target\vault\.obsidian\plugins\vault-rag" -Recurse -Force
Write-Host "   ✅ 插件 vault-rag 已装入 vault"
New-Item -ItemType Directory -Force "$Target\.claude\skills" | Out-Null
Copy-Item "$kit\skills\*" "$Target\.claude\skills\" -Recurse -Force
foreach ($t in 'CLAUDE.md','AGENTS.md') { if (-not (Test-Path "$Target\$t")) { Copy-Item "$kit\templates\$t" "$Target\" } }
New-Item -ItemType Directory -Force "$Target\.rag" | Out-Null
if (-not (Test-Path "$Target\.rag\providers.json")) { Copy-Item "$kit\templates\rag\providers.json" "$Target\.rag\" }
Write-Host "   ✅ Claude 技能 / 配置模板已就位"

# ---------- 4. 下一步 ----------
Write-Host "`n🎉 安装完成！接下来：" -ForegroundColor Green
Write-Host "   1. Obsidian → Open folder as vault → $Target\vault"
Write-Host "      （Settings → Community plugins → 关闭 Restricted mode → 启用 Vault RAG）"
Write-Host "   2. 问答服务：cd $Target ; python scripts\rag.py serve"
Write-Host "   3. 导入第一本书：把 PDF 给 Claude Code，说 /read-book <路径>"
Write-Host "      （或手动跑 CLAUDE.md 里的四条流水线命令）"
Write-Host "   4. 打开 vault 里的 [[使用说明]] — 日常操作全在里面`n"
