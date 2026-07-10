# setup.ps1 — Reading Kit 一键安装
# 用法：git clone 后，在 reading-kit 目录里运行  pwsh -File setup.ps1 [-Target <安装目录>] [-Slug <项目简称>]
param(
    [string]$Target = (Join-Path (Split-Path $PSScriptRoot -Parent) "my-reading"),
    [string]$Slug
)

$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
Write-Host "`n📖 Reading Kit 安装向导" -ForegroundColor Cyan
Write-Host "   安装到：$Target`n"

# ---------- 0. 项目身份（多项目不冲突：全局注册表） ----------
# 每台机器上可能装多个 reading-kit 项目。每个项目需要两个全局唯一的东西：
# 坚果云远程目录名、rag.py serve 端口。这里自动分配并记入 ~\.reading-kit\registry.json——
# 不需要用户去挑名字/记名字，也不依赖任何人手工保证不撞车。
Write-Host "0️⃣  项目身份（多项目防撞车）…"
$registryDir = "$HOME\.reading-kit"
$registryPath = "$registryDir\registry.json"
New-Item -ItemType Directory -Force $registryDir | Out-Null
$registry = if (Test-Path $registryPath) { Get-Content $registryPath -Raw | ConvertFrom-Json } else { [PSCustomObject]@{ projects = @() } }
$projects = @($registry.projects)
$targetAbs = [System.IO.Path]::GetFullPath($Target)

$existing = $projects | Where-Object { $_.path -eq $targetAbs }
if ($existing) {
    $projSlug = $existing.slug
    $remoteDir = $existing.remoteBaseDir
    $ragPort = $existing.ragPort
    Write-Host "   ℹ️ 项目已注册：slug=$projSlug，远程目录=$remoteDir，端口=$ragPort"
} else {
    if (-not $Slug) {
        $Slug = (Split-Path $targetAbs -Leaf) -replace '[^a-zA-Z0-9]+', '-'
        $Slug = $Slug.ToLower().Trim('-')
        if (-not $Slug) { $Slug = "project" }
    }
    $projSlug = $Slug
    $n = 1
    while ($projects | Where-Object { $_.slug -eq $projSlug }) { $n++; $projSlug = "$Slug-$n" }
    $remoteDir = "obsidian-$projSlug"
    $usedPorts = @($projects | ForEach-Object { $_.ragPort })
    $ragPort = 8766
    while ($usedPorts -contains $ragPort) { $ragPort++ }
    $projects += [PSCustomObject]@{ slug = $projSlug; path = $targetAbs; remoteBaseDir = $remoteDir; ragPort = $ragPort }
    ([PSCustomObject]@{ projects = $projects }) | ConvertTo-Json -Depth 5 | Set-Content $registryPath
    Write-Host "   ✅ 新项目已注册：slug=$projSlug，远程目录=$remoteDir，端口=$ragPort（登记于 $registryPath）"
}

# ---------- 1. 依赖检查 ----------
Write-Host "`n1️⃣  检查依赖…"
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
    foreach ($f in @('同步设置.md', '使用说明.md', '系统架构.md', '90-Attachments\系统架构图.svg')) {
        $p = "$Target\vault\$f"
        if (Test-Path $p) {
            (Get-Content $p -Raw) `
                -replace '\{\{REMOTE_DIR\}\}', $remoteDir `
                -replace '\{\{RAG_PORT\}\}', $ragPort `
                -replace '\{\{SLUG\}\}', $projSlug `
                | Set-Content $p
        }
    }
    Write-Host "   ✅ vault 已创建（含阅读排版、插件配置，已填入本项目专属的同步目录/端口）"
} else { Write-Host "   ℹ️ vault 已存在，未覆盖" }
Copy-Item "$kit\plugin\vault-rag" "$Target\vault\.obsidian\plugins\vault-rag" -Recurse -Force
Write-Host "   ✅ 插件 vault-rag 已装入 vault"

# ---------- 3b. 局域网 IP（供插件预填 + 手机连接） ----------
# 排除 VPN/虚拟网卡，优先选真正能被手机连到的家庭 Wi-Fi/有线网段
$lanIP = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and
        $_.InterfaceAlias -notmatch 'NordLynx|WireGuard|TAP|TUN|Loopback|vEthernet|Virtual|VPN'
    } |
    Sort-Object { if ($_.IPAddress -like '192.168.*') { 0 } elseif ($_.IPAddress -like '10.*') { 1 } else { 2 } } |
    Select-Object -First 1).IPAddress

# 预填插件设置：省去手动到 Obsidian 设置里粘贴 RAG 服务地址这一步
$pluginDataPath = "$Target\vault\.obsidian\plugins\vault-rag\data.json"
if (-not (Test-Path $pluginDataPath)) {
    $endpoint = "http://localhost:$ragPort"
    if ($lanIP) { $endpoint += ", http://${lanIP}:$ragPort" }
    (@{ endpoint = $endpoint; provider = ''; topK = 6 } | ConvertTo-Json) | Set-Content $pluginDataPath
    Write-Host "   ✅ 已预填插件的 RAG 服务地址：$endpoint（Obsidian 设置里可再改）"
}

New-Item -ItemType Directory -Force "$Target\.claude\skills" | Out-Null
Copy-Item "$kit\skills\*" "$Target\.claude\skills\" -Recurse -Force
foreach ($t in 'CLAUDE.md','AGENTS.md') { if (-not (Test-Path "$Target\$t")) { Copy-Item "$kit\templates\$t" "$Target\" } }
New-Item -ItemType Directory -Force "$Target\.rag" | Out-Null
if (-not (Test-Path "$Target\.rag\providers.json")) { Copy-Item "$kit\templates\rag\providers.json" "$Target\.rag\" }
Write-Host "   ✅ Claude 技能 / 配置模板已就位"

# ---------- 4. 局域网（手机访问用） ----------
$fwRuleName = "Reading Kit RAG - $projSlug"
try {
    if (-not (Get-NetFirewallRule -DisplayName $fwRuleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $fwRuleName -Direction Inbound -LocalPort $ragPort -Protocol TCP -Action Allow -ErrorAction Stop | Out-Null
        Write-Host "`n4️⃣  防火墙：已放行 $ragPort 端口（手机访问 AI 问答用）"
    } else { Write-Host "`n4️⃣  防火墙规则已存在（$fwRuleName）" }
} catch {
    Write-Host "`n4️⃣  ⚠️ 无法自动放行端口（需管理员）。手机连不上 AI 时，用管理员 PowerShell 运行：" -ForegroundColor Yellow
    Write-Host "   New-NetFirewallRule -DisplayName '$fwRuleName' -Direction Inbound -LocalPort $ragPort -Protocol TCP -Action Allow"
}
if ($lanIP) {
    Write-Host "   📱 本机局域网地址：http://${lanIP}:$ragPort"
} else {
    Write-Host "   ⚠️ 未能自动探测到局域网 IP（可能只有 VPN 网卡在线）——手机连不上时手动跑 ipconfig 找真实 Wi-Fi 的 IPv4 地址" -ForegroundColor Yellow
}

# ---------- 5. 下一步 ----------
Write-Host "`n🎉 安装完成！本项目身份：slug=$projSlug ｜ 坚果云远程目录=$remoteDir ｜ RAG 端口=$ragPort" -ForegroundColor Green
Write-Host "   这三个值已自动写进 vault 里的说明文档和插件设置，不用手填。"
Write-Host "   多项目查冲突随时跑：pwsh -File `"$Target\scripts\check_sync.ps1`"`n"
Write-Host "接下来：" -ForegroundColor Green
Write-Host "   1. Obsidian → Open folder as vault → $Target\vault"
Write-Host "      （Settings → Community plugins → 关闭 Restricted mode → 启用 Vault RAG）"
Write-Host "   2. 问答服务：cd $Target ; python scripts\rag.py serve --port $ragPort"
Write-Host "   3. 导入第一本书：把 PDF 给 Claude Code，说 /read-book <路径>"
Write-Host "      （或手动跑 CLAUDE.md 里的四条流水线命令）"
Write-Host "   4. 打开 vault 里的 [[使用说明]] — 日常操作全在里面"
Write-Host "   5. 想同步到手机：vault 里的 [[同步设置]] —— 远程目录名已经帮你填好了`n"
