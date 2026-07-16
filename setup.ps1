# setup.ps1 — Reading Kit 一键安装
# 用法：git clone 后，在 reading-kit 目录里运行  pwsh -File setup.ps1 [-Target <安装目录>] [-Slug <项目简称>]
# Hub 模式：pwsh -File setup.ps1 -Hub <总目录>  — 创建/刷新"一站式书房"（所有项目的唯一入口，
#           含 junction、Inbox 批量导入、书架管家技能）。项目多了随时重跑，幂等。
param(
    [string]$Target = (Join-Path (Split-Path $PSScriptRoot -Parent) "my-reading"),
    [string]$Slug,
    [string]$Hub
)

$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot

# ---------- Hub 模式（与项目安装互斥，做完即退出） ----------
if ($Hub) {
    $hubAbs = [System.IO.Path]::GetFullPath($Hub)
    Write-Host "`n📚 Reading Hub 创建/刷新：$hubAbs" -ForegroundColor Cyan
    foreach ($d in @('', 'Inbox', 'dashboard', '.claude\skills')) {
        New-Item -ItemType Directory -Force (Join-Path $hubAbs $d) | Out-Null
    }

    # 注册表里的每个项目 → 一个 junction 入口（已存在则跳过；重跑=补新项目）
    $regPath = "$HOME\.reading-kit\registry.json"
    $projs = @()
    if (Test-Path $regPath) { $projs = @((Get-Content $regPath -Raw | ConvertFrom-Json).projects) }
    $rows = ""
    foreach ($p in $projs) {
        $link = Join-Path $hubAbs $p.slug
        if (-not (Test-Path $link)) {
            if (Test-Path $p.path) {
                New-Item -ItemType Junction -Path $link -Target $p.path | Out-Null
                Write-Host "   ✅ junction $($p.slug) → $($p.path)"
            } else {
                Write-Host "   ⚠️ 注册表中的项目路径不存在，跳过：$($p.path)" -ForegroundColor Yellow
            }
        }
        $rows += "| ``$($p.slug)\`` | junction → ``$($p.path)`` (ragPort $($p.ragPort)) |`n"
    }
    if (-not $projs) {
        Write-Host "   ℹ️ 注册表还没有任何项目 — 先跑 setup.ps1 -Target <目录> 建一个，再重跑本命令补 junction"
    }

    # 工具箱本身也挂一个 junction，hub 里的命令统一写 reading-kit\scripts\...
    $kitLink = Join-Path $hubAbs 'reading-kit'
    if (-not (Test-Path $kitLink)) {
        New-Item -ItemType Junction -Path $kitLink -Target $kit | Out-Null
        Write-Host "   ✅ junction reading-kit → $kit"
    }

    # hub.json / CLAUDE.md：只在缺失时生成，绝不覆盖用户已有配置
    if (-not (Test-Path "$hubAbs\hub.json")) {
        $defaultProject = if ($projs) { $projs[0].slug } else { "" }
        (Get-Content "$kit\templates\hub\hub.json" -Raw) `
            -replace '\{\{DEFAULT_PROJECT\}\}', $defaultProject | Set-Content "$hubAbs\hub.json"
        Write-Host "   ✅ hub.json（defaultProject=$defaultProject；sources 留空——之后告诉 Claude 你的书攒在哪些文件夹即可）"
    }
    if (-not (Test-Path "$hubAbs\CLAUDE.md")) {
        (Get-Content "$kit\templates\hub\CLAUDE.md" -Raw) `
            -replace '\{\{PROJECT_ROWS\}\}', $rows | Set-Content "$hubAbs\CLAUDE.md"
        Write-Host "   ✅ CLAUDE.md（hub 章程）"
    }

    # 书架管家技能子集（hub 里只需要这三个）
    foreach ($s in @('library', 'getting-started', 'reading-system')) {
        Copy-Item "$kit\skills\$s" "$hubAbs\.claude\skills\" -Recurse -Force
    }
    Write-Host "   ✅ 技能：library（书架管家）/ getting-started（新手引导）/ reading-system（运维）"

    Write-Host "`n🎉 Hub 就绪。以后所有关于书的事，都在 $hubAbs 打开 Claude Code，说「书架」即可。" -ForegroundColor Green
    Write-Host "   批量导入：把 PDF 丢进 Inbox\<合集名>\，对 Claude 说「导入 Inbox」。`n"
    exit 0
}

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
$pyVerRaw = "$(python --version 2>&1)"
$pyVer = $null
if ($pyVerRaw -match '(\d+)\.(\d+)') { $pyVer = [version]"$($Matches[1]).$($Matches[2])" }
if (-not $pyVer -or $pyVer -lt [version]'3.10') {
    Write-Host "❌ Python 3.10+ 是硬性要求，当前检测到：$pyVerRaw" -ForegroundColor Red
    Write-Host "   （Microsoft Store 的 python 占位程序也会导致这里失败——请从 python.org 安装）" -ForegroundColor Red
    exit 1
}
Write-Host "   ✅ $pyVerRaw"

foreach ($m in 'numpy', 'requests', 'pypdf') {
    python -c "import $m" 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Host "   📦 安装 $m…"; python -m pip install -q $m }
}
Write-Host "   ✅ numpy / requests / pypdf"

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
    @{n='PaddleOCR 令牌（扫描书 OCR 必需，aistudio.baidu.com 免费申请）'; f='paddleocr.dat'},
    @{n='Ollama Cloud 密钥（扫描书 OCR 校验必需 + AI 问答，ollama.com/settings）'; f='ollama-cloud.dat'},
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
foreach ($t in 'CLAUDE.md','AGENTS.md','opencode.json') { if (-not (Test-Path "$Target\$t")) { Copy-Item "$kit\templates\$t" "$Target\" } }
New-Item -ItemType Directory -Force "$Target\.rag" | Out-Null
if (-not (Test-Path "$Target\.rag\providers.json")) { Copy-Item "$kit\templates\rag\providers.json" "$Target\.rag\" }
New-Item -ItemType Directory -Force "$Target\vendor" | Out-Null
if (-not (Test-Path "$Target\vendor\README.md")) { Copy-Item "$kit\vendor\README.md" "$Target\vendor\" }
if (-not (Test-Path "$Target\start_rag_server.vbs")) {
    (Get-Content "$kit\templates\rag\start_rag_server.vbs" -Raw).Replace('{{PYTHON_EXE}}', $py.Source).Replace('{{PROJECT_ROOT}}', $targetAbs).Replace('{{RAG_PORT}}', "$ragPort") | Set-Content "$Target\start_rag_server.vbs"
}
Write-Host "   ✅ Claude 技能 / 配置模板 / vendor（可选依赖拖入处）/ 开机自启脚本 已就位"

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
Write-Host "   3. 导入第一本书（三选一）："
Write-Host "      · 新手推荐：在项目目录打开 Claude Code，说「带我入门」（getting-started 分步引导）"
Write-Host "      · 网页控制台：python -X utf8 scripts\import_server.py → 浏览器开 http://localhost:$($ragPort+100)"
Write-Host "      · 命令行：python -X utf8 scripts\import_book.py --pdf <路径> --title <书名>"
Write-Host "   4. 打开 vault 里的 [[使用说明]] — 日常操作全在里面"
Write-Host "   5. 想同步到手机：vault 里的 [[同步设置]] —— 远程目录名已经帮你填好了"
Write-Host "   6. 书多/项目多之后：pwsh -File setup.ps1 -Hub <总目录> 建一站式书房（Inbox 批量导入 + 书架管家）`n"
