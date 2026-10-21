# One-click install for Windows (SPEC §2). Double-click install.bat, or:
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Cpu] [-DryRun]
param([switch]$Cpu, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Run([string[]]$cmd) {
    Write-Host "+ $($cmd -join ' ')"
    if (-not $DryRun) {
        & $cmd[0] $cmd[1..($cmd.Length - 1)]
        if ($LASTEXITCODE -ne 0) { throw "failed: $($cmd -join ' ')" }
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv not found. Install it: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
}

$tag = 'cpu'
if (-not $Cpu -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    $drv = (& nvidia-smi --query-gpu=driver_version --format=csv,noheader | Select-Object -First 1)
    $major = [int]($drv.Split('.')[0])
    if ($major -ge 570) { $tag = 'cu128' } elseif ($major -ge 560) { $tag = 'cu126' } elseif ($major -ge 550) { $tag = 'cu124' }
}
Write-Host "torch wheel: $tag"

$py = '.venv\Scripts\python.exe'
Run @('uv', 'venv', '.venv', '--python', '3.11')
Run @('uv', 'pip', 'install', '--python', $py, 'torch', '--index-url', "https://download.pytorch.org/whl/$tag")
Run @('uv', 'pip', 'install', '--python', $py, '-e', '.[dev]', 'lightgbm', 'pyarrow')
Run @($py, '-X', 'utf8', '-m', 'qwb.cli', 'doctor')
Write-Host 'done. activate with: .venv\Scripts\activate'
