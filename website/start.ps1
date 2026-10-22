$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Run the project installer first to create .venv.'
}
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
Push-Location $repoRoot
try {
    & $pythonExe -X utf8 -m uvicorn share_site.app:app --host 127.0.0.1 --port $sitePort
} finally { Pop-Location }
