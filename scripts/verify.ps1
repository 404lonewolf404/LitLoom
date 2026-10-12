$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot\..

Write-Host "[1/3] Python syntax"
python -m py_compile .\app.py .\tests\test_app.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/3] JavaScript syntax"
node --check .\static\app.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/3] Unit and integration tests"
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Quality gates passed."
