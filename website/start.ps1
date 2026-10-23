$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Run the project installer first to create .venv.'
}
& $pythonExe -c "import httpx, itsdangerous, jinja2"
if ($LASTEXITCODE -ne 0) { throw 'Install site dependencies first: .\install.ps1 -WithSite' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'dist\index.html'))) {
    Push-Location $PSScriptRoot
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Website build failed.' }
    } finally { Pop-Location }
}
$sitePort = 8800
while (Get-NetTCPConnection -State Listen -LocalPort $sitePort -ErrorAction SilentlyContinue) {
    $sitePort++
}
Write-Host "Quant Workbench: http://127.0.0.1:$sitePort"
# local preview only: passwordless dev login on 127.0.0.1. A server deployment sets
# HF_OAUTH_* and QWB_SITE_SECRET instead; the site refuses to start without them.
if (-not $env:HF_OAUTH_CLIENT_ID) { $env:QWB_SITE_DEV = '1' }
Push-Location $repoRoot
try {
    & $pythonExe -X utf8 -m uvicorn share_site.app:app --host 127.0.0.1 --port $sitePort
} finally { Pop-Location }
