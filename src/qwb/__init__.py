"""qwb: quant model workbench."""

# Import-order guard (docs/PITFALLS.md). On the reference Windows machine:
#   pyarrow (pulled in by pandas) before torch -> c10.dll WinError 1114
#   pandas/numpy before osqp                   -> segfault 0xC0000005
# So torch, then osqp, are loaded before anything imports pandas. Both are
# optional; set QWB_SKIP_PRELOAD=1 to skip (e.g. pure data tools on CI).
# If pandas/pyarrow are already loaded the preload is too late: it is skipped
# (a later `import torch` will fail with a clear pointer) instead of crashing
# data-only code that never needs torch.
import importlib.util as _ilu
import os as _os
import sys as _sys
import warnings as _warnings

PRELOAD_SKIPPED: list[str] = []

if not _os.environ.get("QWB_SKIP_PRELOAD"):
    _late = [m for m in ("pyarrow", "pandas") if m in _sys.modules]
    for _mod in ("torch", "osqp"):
        if _mod in _sys.modules or _ilu.find_spec(_mod) is None:
            continue
        if _late:
            PRELOAD_SKIPPED.append(_mod)
            continue
        __import__(_mod)
    if PRELOAD_SKIPPED:
        _warnings.warn(f"qwb imported after {_late}; skipped preloading {PRELOAD_SKIPPED}. "
                       "Import qwb (or torch, then osqp) before pandas if you need them.",
                       RuntimeWarning, stacklevel=2)

__version__ = "0.0.1"
