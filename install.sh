#!/usr/bin/env sh
# qwb 安装脚本（Linux）。用法：  sh install.sh
#   --with-gpu   额外安装 GPU 版 PyTorch（只有 Kronos 底座需要；默认的 LightGBM 训练不用）
#   --with-site  安装网站服务依赖；--dev 安装网站和测试依赖
#   --mirror     直接使用国内镜像（默认会自动判断）
#   --dry-run    只显示要执行的命令
set -eu
cd "$(dirname "$0")"
export UV_LINK_MODE=copy UV_HTTP_TIMEOUT=120

DRY=0; GPU=0; MIRROR=0; SITE=0; DEV=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --with-gpu) GPU=1 ;;
    --with-site) SITE=1 ;;
    --dev) DEV=1 ;;
    --mirror) MIRROR=1 ;;
    *) echo "未知参数：$a" >&2; exit 2 ;;
  esac
done
say() { printf '\033[36m%s\033[0m\n' "$*"; }
run() { echo "+ $*"; [ "$DRY" -eq 1 ] || "$@"; }
reachable() { curl -s -o /dev/null --head --max-time 8 "$1" 2>/dev/null; }
use_mirror() {
  export UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone
  export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
  say "使用国内镜像下载（清华 PyPI 镜像、npmmirror Python 镜像）"
}
step() {
  run "$@" && return 0
  if [ -z "${UV_DEFAULT_INDEX:-}" ]; then
    say "下载失败，改用国内镜像重试一次……"; use_mirror
    run "$@" && return 0
  fi
  echo "这一步失败了：$*" >&2; echo "请检查网络后重新运行 sh install.sh" >&2; exit 1
}

say "== WiseHold 安装 =="
command -v curl >/dev/null 2>&1 || { echo "需要 curl：sudo apt install curl（或 yum install curl）" >&2; exit 1; }

# 1. uv（安装工具）
UV=""
for p in ../tools/uv tools/uv; do [ -x "$p" ] && UV="$(cd "$(dirname "$p")" && pwd)/uv" && break; done
[ -z "$UV" ] && command -v uv >/dev/null 2>&1 && UV="$(command -v uv)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  say "下载安装工具 uv"
  if reachable https://astral.sh/uv/install.sh; then
    run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  else
    run sh -c 'curl -LsSf https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/ >/dev/null && python3 -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple uv'
  fi
  for p in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do [ -x "$p" ] && UV="$p" && break; done
  [ -z "$UV" ] && command -v uv >/dev/null 2>&1 && UV="$(command -v uv)"
  [ -n "$UV" ] || { [ "$DRY" -eq 1 ] && UV=uv || { echo "uv 安装失败" >&2; exit 1; }; }
fi
say "安装工具：$UV"

# 2. 网络
if [ "$MIRROR" -eq 1 ]; then use_mirror
elif ! reachable https://pypi.org/simple/pip/ || ! reachable https://github.com/astral-sh/python-build-standalone/releases; then use_mirror
fi

# 3. 独立的 Python 3.11 和固定版本依赖
PY=.venv/bin/python
say "[1/4] 准备 Python 3.11（只装在这个文件夹里）"
step "$UV" venv .venv --python 3.11 --managed-python --clear
say "[2/4] 安装依赖（版本固定在 requirements.lock）"
LOCK=requirements.lock
[ "$SITE" -eq 0 ] || LOCK=requirements-site.lock
[ "$DEV" -eq 0 ] || LOCK=requirements-dev.lock
step "$UV" pip install --python "$PY" -r "$LOCK"
step "$UV" pip install --python "$PY" --no-deps -e .
if [ -f third_party/ashare-tcost/pyproject.toml ]; then
  step "$UV" pip install --python "$PY" --no-deps -e third_party/ashare-tcost
else
  echo "缺少 third_party/ashare-tcost，请获取完整的 WiseHold 源码或发布包" >&2
  exit 1
fi
if [ "$GPU" -eq 1 ]; then
  TAG=cpu
  if command -v nvidia-smi >/dev/null 2>&1; then
    DRV=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1 || true)
    if [ -n "${DRV:-}" ]; then
      if [ "$DRV" -ge 570 ]; then TAG=cu128; elif [ "$DRV" -ge 560 ]; then TAG=cu126; elif [ "$DRV" -ge 550 ]; then TAG=cu124; fi
    fi
  fi
  say "[可选] 安装 PyTorch（$TAG）"
  step "$UV" pip install --python "$PY" torch --index-url "https://download.pytorch.org/whl/$TAG"
fi
[ "$DRY" -eq 1 ] && { say "（dry-run：没有实际执行）"; exit 0; }

# 4. 自检
say "[3/4] 检查"
"$PY" -X utf8 -c "import lightgbm, qwb, ashare_tcost; print('lightgbm', lightgbm.__version__)" || {
  echo "LightGBM 无法加载。Debian/Ubuntu 请运行：sudo apt install libgomp1；CentOS/RHEL：sudo yum install libgomp" >&2; exit 1; }
say "[4/4] 试运行一次（合成数据，几秒钟）"
"$PY" -X utf8 -m qwb.cli run examples/run_smoke.yaml
echo
printf '\033[32m%s\033[0m\n' "安装完成。启动：sh start.sh   然后用浏览器打开 http://127.0.0.1:8765"
