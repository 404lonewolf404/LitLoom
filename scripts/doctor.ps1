param(
    [switch]$Strict
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Join-Path $PSScriptRoot "..")
$arguments = @(".\scripts\doctor.py")
if ($Strict) { $arguments += "--strict" }
python @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
