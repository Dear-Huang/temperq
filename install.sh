#!/usr/bin/env sh
# One-command install (SPEC §2): uv venv + matching torch wheel + qwb.
#   sh install.sh            install
#   sh install.sh --cpu      force CPU torch
#   sh install.sh --dry-run  print the commands only
set -eu
cd "$(dirname "$0")"

DRY=0; FORCE_CPU=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --cpu) FORCE_CPU=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
run() { echo "+ $*"; [ "$DRY" -eq 1 ] || "$@"; }

command -v uv >/dev/null 2>&1 || { echo "uv not found: https://docs.astral.sh/uv/ (pip install uv)" >&2; exit 1; }

# pick the torch wheel line from the NVIDIA driver major version
TAG=cpu
if [ "$FORCE_CPU" -eq 0 ] && command -v nvidia-smi >/dev/null 2>&1; then
  DRV=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1 || true)
  if [ -n "${DRV:-}" ]; then
    if   [ "$DRV" -ge 570 ]; then TAG=cu128
    elif [ "$DRV" -ge 560 ]; then TAG=cu126
    elif [ "$DRV" -ge 550 ]; then TAG=cu124
    fi
  fi
fi
echo "torch wheel: $TAG"

# decided by OS, not by what exists: the venv is created below
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) PY=.venv/Scripts/python.exe ;;
  *) PY=.venv/bin/python ;;
esac
run uv venv .venv --python 3.11
run uv pip install --python "$PY" torch --index-url "https://download.pytorch.org/whl/$TAG"
run uv pip install --python "$PY" -e ".[dev]"
if [ -f ../ashare-tcost/pyproject.toml ]; then
  run uv pip install --python "$PY" -e ../ashare-tcost
else
  echo "note: ../ashare-tcost not found; reports will skip the net-of-cost check"
fi
run "$PY" -X utf8 -m qwb.cli doctor
echo "done. run: $PY -X utf8 -m qwb.cli ui    (or qwb run <config.yaml>)"
