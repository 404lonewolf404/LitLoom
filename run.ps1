param(
    [int]$Port = 8765,
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if ($Port -gt 0) { $env:LITERATURE_PORT = [string]$Port }
if ($DataDir) { $env:LITERATURE_DATA_DIR = $DataDir }
python .\app.py
