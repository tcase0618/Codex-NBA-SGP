param([int]$Port = 8020)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
Write-Host "NBA Quant research terminal: http://127.0.0.1:$Port"
$mlPython = Join-Path $PSScriptRoot '.venv-ml\Scripts\python.exe'
if (Test-Path -LiteralPath $mlPython) {
    & $mlPython -m nba_quant serve --port $Port
} else {
    python -m nba_quant serve --port $Port
}
