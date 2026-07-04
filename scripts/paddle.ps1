# paddle.ps1 — decrypt PaddleOCR token and run the batch OCR client
# Usage: pwsh -File scripts\paddle.ps1 --file book.pdf --out staging\slug\paddle

$sec = Get-Content "$HOME\.secrets\paddleocr.dat" | ConvertTo-SecureString
$env:PADDLEOCR_TOKEN = [pscredential]::new('x', $sec).GetNetworkCredential().Password

python "$PSScriptRoot\paddle_ocr.py" @args
exit $LASTEXITCODE
