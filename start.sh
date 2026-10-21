#!/usr/bin/env sh
# 启动 qwb 工作台。服务器上用：sh start.sh --no-browser，再在自己电脑上做端口转发（见使用手册）
set -eu
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "还没有安装：先运行 sh install.sh" >&2; exit 1; }
exec .venv/bin/python -X utf8 -m qwb.cli ui "$@"
