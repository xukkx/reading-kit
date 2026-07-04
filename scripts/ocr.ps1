# ocr.ps1 — direct Ollama cloud vision call (no agent loop, stateless worker)
# Usage: pwsh -File scripts\ocr.ps1 -Image page.png [-Model gemini-3-flash-preview] [-Prompt "..."]
param(
    [Parameter(Mandatory)][string]$Image,
    [string]$Model = 'gemini-3-flash-preview',
    [string]$Prompt = '这是一页扫描的中文书页。请逐字转录页面上的全部文字，保持原有段落结构。页面上有荧光笔高亮标记的文字用==文字==包裹。看不清的字用□代替。只输出转录内容，不要任何解释。'
)

$sec = Get-Content "$HOME\.secrets\ollama-cloud.dat" | ConvertTo-SecureString
$key = [pscredential]::new('x', $sec).GetNetworkCredential().Password

$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($Image))
$body = @{
    model    = $Model
    stream   = $false
    messages = @(@{ role = 'user'; content = $Prompt; images = @($b64) })
} | ConvertTo-Json -Depth 6

$r = Invoke-RestMethod -Uri 'https://ollama.com/api/chat' -Method Post `
    -Headers @{ Authorization = "Bearer $key" } -ContentType 'application/json; charset=utf-8' `
    -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 300

$r.message.content
