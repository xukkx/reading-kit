# worker.ps1 — run an opencode headless worker with the Ollama cloud key
# Usage: pwsh -File scripts\worker.ps1 -Model ollama/deepseek-v4-flash -Prompt "task" [-Files img.png,...]
param(
    [Parameter(Mandatory)][string]$Model,
    [Parameter(Mandatory)][string]$Prompt,
    [string[]]$Files = @()
)

# Decrypt the DPAPI-protected API key (never stored in plaintext)
$sec = Get-Content "$HOME\.secrets\ollama-cloud.dat" | ConvertTo-SecureString
$env:OLLAMA_API_KEY = [pscredential]::new('x', $sec).GetNetworkCredential().Password

# NOTE: prompt must come BEFORE --file flags (yargs array flags greedily consume
# trailing positionals). Must run from the project root so opencode.json loads.
$oargs = @('run', $Prompt, '-m', $Model)
foreach ($f in $Files) { $oargs += "--file=$f" }

& opencode @oargs
exit $LASTEXITCODE
