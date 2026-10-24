param(
    [ValidateSet("All", "SecretFiles", "RuntimeConfig", "PythonSyntax", "JavaScriptSyntax", "Contracts", "Tests")]
    [string]$Stage = "All"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot\..

if ($Stage -in @("All", "SecretFiles")) {
    Write-Host "[1/6] Secret file tracking"
    $trackedEnvFiles = git ls-files -- .env '.env.*' | Where-Object { $_ -ne ".env.example" }
    if ($trackedEnvFiles) {
        throw "Secret environment files must not be tracked by Git: $($trackedEnvFiles -join ', ')"
    }
}

if ($Stage -in @("All", "RuntimeConfig")) {
    Write-Host "[2/6] Runtime configuration"
    $previousDatabaseUrl = $env:LITLOOM_DATABASE_URL
    try {
        if (-not $env:LITLOOM_DATABASE_URL) {
            $env:LITLOOM_DATABASE_URL = "postgresql://ci:ci@127.0.0.1:5432/litloom"
        }
        python .\scripts\doctor.py --strict
    } finally {
        if ($null -eq $previousDatabaseUrl) {
            Remove-Item Env:LITLOOM_DATABASE_URL -ErrorAction SilentlyContinue
        } else {
            $env:LITLOOM_DATABASE_URL = $previousDatabaseUrl
        }
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Stage -in @("All", "PythonSyntax")) {
    Write-Host "[3/6] Python syntax"
    python -m compileall -q .\app.py .\mcp_server.py .\literature_desk .\scripts .\tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Stage -in @("All", "JavaScriptSyntax")) {
    Write-Host "[4/6] JavaScript syntax"
    Get-ChildItem -LiteralPath .\static -Filter *.js -Recurse | ForEach-Object {
        node --check $_.FullName
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}

if ($Stage -in @("All", "Contracts")) {
    Write-Host "[5/6] API contract files"
    if (-not (Test-Path -LiteralPath .\openapi.yaml)) { throw "openapi.yaml is missing" }
    if (-not (Test-Path -LiteralPath .\literature_desk\migrations\versions\0002_segment_annotations.py)) { throw "database migration directory is incomplete" }
}

if ($Stage -in @("All", "Tests")) {
    Write-Host "[6/6] Unit and integration tests"
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "Quality gates passed."
