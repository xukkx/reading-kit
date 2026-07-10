# check_sync.ps1 — verify no two reading-kit projects on this machine
# share a 坚果云远程目录 (WebDAV remote base dir) or a rag.py serve 端口.
# Run from any reading-kit project: pwsh -File scripts\check_sync.ps1

$registryPath = "$HOME\.reading-kit\registry.json"
if (-not (Test-Path $registryPath)) {
    Write-Host "尚无注册表：$registryPath"
    Write-Host "（还没有项目通过 setup.ps1 注册，或这是手动创建、未走安装向导的项目）"
    exit 0
}

$projects = @((Get-Content $registryPath -Raw | ConvertFrom-Json).projects)
if ($projects.Count -eq 0) { Write-Host "注册表为空。"; exit 0 }

Write-Host "`n📋 已注册的 reading-kit 项目：`n"
foreach ($p in $projects) {
    $mark = if (Test-Path $p.path) { " " } else { "⚠" }
    Write-Host ("  $mark {0,-14} {1,-22} 端口 {2,-6} {3}" -f $p.slug, $p.remoteBaseDir, $p.ragPort, $p.path)
}

$bad = $false

$dupDir = $projects | Group-Object remoteBaseDir | Where-Object Count -gt 1
if ($dupDir) {
    $bad = $true
    Write-Host "`n❌ 远程目录冲突（这些项目会在坚果云上互相覆盖）：" -ForegroundColor Red
    foreach ($g in $dupDir) { Write-Host ("   {0} ← {1}" -f $g.Name, (($g.Group | ForEach-Object slug) -join ', ')) }
}

$dupPort = $projects | Group-Object ragPort | Where-Object Count -gt 1
if ($dupPort) {
    $bad = $true
    Write-Host "`n❌ 端口冲突（rag.py serve 会抢同一个端口）：" -ForegroundColor Red
    foreach ($g in $dupPort) { Write-Host ("   端口 {0} ← {1}" -f $g.Name, (($g.Group | ForEach-Object slug) -join ', ')) }
}

$missing = $projects | Where-Object { -not (Test-Path $_.path) }
if ($missing) {
    Write-Host "`n⚠️ 以下项目路径已不存在（改名/删除/移动了？）——确认不再使用的话，手动从 registry.json 里删掉对应条目：" -ForegroundColor Yellow
    foreach ($p in $missing) { Write-Host "   $($p.slug) → $($p.path)" }
}

if (-not $bad) { Write-Host "`n✅ 没有冲突" -ForegroundColor Green }
Write-Host "注册表位置：$registryPath`n"
