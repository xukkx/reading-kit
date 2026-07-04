# ingest.ps1 — decrypt Ollama cloud key and run the verified ingestion pipeline
# Usage: pwsh -File scripts\ingest.ps1 --pdf vault\90-Attachments\book.pdf --first 10 --last 39 --slug yuedu-heji

$sec = Get-Content "$HOME\.secrets\ollama-cloud.dat" | ConvertTo-SecureString
$env:OLLAMA_API_KEY = [pscredential]::new('x', $sec).GetNetworkCredential().Password

python "$PSScriptRoot\ingest.py" @args
exit $LASTEXITCODE
