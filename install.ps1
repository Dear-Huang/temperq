# qwb 安装脚本（Windows）。双击 install.bat，或运行 install.ps1。

#   -WithGpu  额外安装 GPU 版 PyTorch（只有 Kronos 底座需要；默认的 LightGBM 训练不用）
#   -WithSite 安装网站服务依赖；-Dev 安装网站和测试依赖

#   -Mirror   直接使用国内镜像（默认会自动判断）

#   -DryRun   只显示要执行的命令

param([switch]$WithGpu, [switch]$WithSite, [switch]$Dev, [switch]$Mirror, [switch]$DryRun)

$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot

$env:UV_LINK_MODE = 'copy'          # 缓存和安装目录可能不在同一个盘

$env:UV_HTTP_TIMEOUT = '120'



function Say([string]$m) { Write-Host $m -ForegroundColor Cyan }

function Run([string[]]$cmd) {

    Write-Host "+ $($cmd -join ' ')" -ForegroundColor DarkGray

    if ($DryRun) { return 0 }

    & $cmd[0] $cmd[1..($cmd.Length - 1)] | Out-Host

    return $LASTEXITCODE

}

function Reachable([string]$url) {

    try { Invoke-WebRequest -Uri $url -Method Head -TimeoutSec 8 -UseBasicParsing | Out-Null; return $true }

    catch { return $false }

}

function Use-Mirror {

    $env:UV_PYTHON_INSTALL_MIRROR = 'https://registry.npmmirror.com/-/binary/python-build-standalone'

    $env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'

    Say '使用国内镜像下载（清华 PyPI 镜像、npmmirror Python 镜像）'

}

function Step([string[]]$cmd) {

    if ((Run $cmd) -eq 0) { return }

    if (-not $env:UV_DEFAULT_INDEX) {

        Say '下载失败，改用国内镜像重试一次……'

        Use-Mirror

        if ((Run $cmd) -eq 0) { return }

    }

    throw "这一步失败了：$($cmd -join ' ')`n请检查网络后重新双击安装；仍然失败请把这个窗口的内容截图发给维护者。"

}



Say '== TemperQ 安装 =='



# 1. uv（安装工具）：优先用压缩包里自带的，不需要联网下载

$uv = $null

foreach ($p in @('..\tools\uv.exe', 'tools\uv.exe')) {

    if (Test-Path $p) { $uv = (Resolve-Path $p).Path; break }

}

if (-not $uv) { $c = Get-Command uv -ErrorAction SilentlyContinue; if ($c) { $uv = $c.Source } }

if (-not $uv) {

    throw '找不到 uv.exe。请使用完整的安装包（里面有 tools\uv.exe），或先安装 uv：https://docs.astral.sh/uv/'

}

try { Unblock-File $uv } catch { }

Say "安装工具：$uv"



# 2. 网络：国外源连不上就用国内镜像

if ($Mirror) { Use-Mirror }

elseif (-not (Reachable 'https://pypi.org/simple/pip/') -or

        -not (Reachable 'https://github.com/astral-sh/python-build-standalone/releases')) { Use-Mirror }



# 3. 独立的 Python 3.11 和固定版本的依赖（不会碰电脑上已有的 Python）

$py = '.venv\Scripts\python.exe'

Say '[1/4] 准备 Python 3.11（只装在这个文件夹里）'

Step @($uv, 'venv', '.venv', '--python', '3.11', '--managed-python', '--clear')

Say '[2/4] 安装依赖（版本固定在 requirements.lock，约 100MB）'

$lockfile = 'requirements.lock'
if ($WithSite) { $lockfile = 'requirements-site.lock' }
if ($Dev) { $lockfile = 'requirements-dev.lock' }
Step @($uv, 'pip', 'install', '--python', $py, '-r', $lockfile)

Step @($uv, 'pip', 'install', '--python', $py, '--no-deps', '-e', '.')

if (Test-Path 'third_party\ashare-tcost\pyproject.toml') {

    Step @($uv, 'pip', 'install', '--python', $py, '--no-deps', '-e', 'third_party\ashare-tcost')

} else {

    throw '缺少 third_party\ashare-tcost，请获取完整的 TemperQ 源码或发布包。'

}



if ($WithGpu) {

    $tag = 'cpu'

    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {

        $drv = (& nvidia-smi --query-gpu=driver_version --format=csv,noheader | Select-Object -First 1)

        $major = [int]($drv.Split('.')[0])

        if ($major -ge 570) { $tag = 'cu128' } elseif ($major -ge 560) { $tag = 'cu126' } elseif ($major -ge 550) { $tag = 'cu124' }

    }

    Say "[可选] 安装 PyTorch（$tag）。没有 NVIDIA 显卡或驱动太旧时装 CPU 版"

    Step @($uv, 'pip', 'install', '--python', $py, 'torch', '--index-url', "https://download.pytorch.org/whl/$tag")

}



if ($DryRun) { Say '（DryRun：没有实际执行）'; exit 0 }



# 4. 自检：能导入、能跑通一次试运行

Say '[3/4] 检查'

& $py -X utf8 -c "import lightgbm, qwb, ashare_tcost; print('lightgbm', lightgbm.__version__)" | Out-Host

if ($LASTEXITCODE -ne 0) {

    # 干净的 Windows 可能缺 VC++ 运行库（vcomp140.dll），LightGBM 需要它

    Say '缺少 Microsoft VC++ 运行库，正在下载安装（会弹出一个确认窗口，点“是”）'

    $vc = Join-Path $env:TEMP 'vc_redist.x64.exe'

    Invoke-WebRequest 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $vc -UseBasicParsing

    Start-Process $vc -ArgumentList '/install', '/passive', '/norestart' -Wait

    & $py -X utf8 -c "import lightgbm, qwb, ashare_tcost" | Out-Host

    if ($LASTEXITCODE -ne 0) { throw 'LightGBM 仍然无法加载。请把这个窗口截图发给维护者。' }

}

Say '[4/4] 试运行一次（合成数据，几秒钟）'

& $py -X utf8 -m qwb.cli run examples\run_smoke.yaml | Select-Object -Last 1 | Out-Host

if ($LASTEXITCODE -ne 0) { throw '试运行失败。请把这个窗口截图发给维护者。' }



Write-Host ''

Write-Host '安装完成。双击 start.bat 打开工作台。' -ForegroundColor Green

