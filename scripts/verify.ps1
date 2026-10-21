$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot\..

Write-Host "[1/5] Runtime configuration"
python .\scripts\doctor.py --strict
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/5] Python syntax"
python -m compileall -q .\app.py .\mcp_server.py .\literature_desk .\scripts .\tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/5] JavaScript syntax"
Get-ChildItem -LiteralPath .\static -Filter *.js -Recurse | ForEach-Object {
    node --check $_.FullName
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "[4/5] API contract files"
if (-not (Test-Path -LiteralPath .\openapi.yaml)) { throw "openapi.yaml is missing" }
if (-not (Test-Path -LiteralPath .\literature_desk\migrations\versions\0002_segment_annotations.py)) { throw "database migration directory is incomplete" }

Write-Host "[5/5] Unit and integration tests"
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Quality gates passed."
