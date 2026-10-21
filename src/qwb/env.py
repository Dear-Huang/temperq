"""Environment detection for the installer and job metadata (SPEC §2, §8).

`qwb doctor` reports what is installed and which PyTorch wheel index fits
this machine; it never installs anything itself.
"""

from __future__ import annotations

import importlib.metadata as md
import platform
import re
import shutil
import subprocess
import sys

TRACKED = ["numpy", "pandas", "pyarrow", "pyyaml", "lightgbm", "torch", "mlflow-skinny",
           "huggingface-hub", "osqp", "cvxpy"]

# Minimum driver for each CUDA wheel line (NVIDIA release notes, Windows/Linux
# differ slightly; the lower bound is used). Blackwell (RTX 50) needs cu128+.
CUDA_INDEX = [((12, 8), "cu128", 570), ((12, 6), "cu126", 560), ((12, 4), "cu124", 550)]
TORCH_INDEX = "https://download.pytorch.org/whl/{tag}"


def versions() -> dict:
    out = {}
    for name in TRACKED:
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            out[name] = None
    return out


def gpu() -> dict | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        q = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=15, check=True)
    except (subprocess.SubprocessError, OSError):
        return None
    line = q.stdout.strip().splitlines()[0]
    name, driver, mem = [x.strip() for x in line.split(",")]
    return {"name": name, "driver": driver, "memory_mb": int(float(mem))}


def torch_index(g: dict | None) -> tuple[str, str]:
    """(wheel tag, reason) for this machine."""
    if g is None:
        return "cpu", "no NVIDIA GPU found"
    major = int(re.match(r"\d+", g["driver"]).group())
    blackwell = bool(re.search(r"RTX\s*50\d\d", g["name"]))
    for _, tag, min_drv in CUDA_INDEX:
        if major >= min_drv:
            return tag, f"driver {g['driver']}"
    if blackwell:
        return "cpu", f"{g['name']} needs driver >= 570 for cu128; update the driver"
    return "cpu", f"driver {g['driver']} older than supported CUDA wheels"


def metadata() -> dict:
    """Recorded with every job."""
    return {"python": sys.version.split()[0], "platform": platform.platform(),
            "packages": versions(), "gpu": gpu()}


def doctor() -> list[str]:
    lines = [f"python {sys.version.split()[0]} on {platform.platform()}"]
    g = gpu()
    lines.append("gpu: " + (f"{g['name']} driver {g['driver']} {g['memory_mb']} MB" if g
                            else "none"))
    tag, why = torch_index(g)
    lines.append(f"torch wheel: {tag} ({why})")
    if tag != "cpu":
        lines.append(f"  uv pip install torch --index-url {TORCH_INDEX.format(tag=tag)}")
    v = versions()
    for k, x in v.items():
        lines.append(f"  {k:16s} {x or '-'}")
    if v.get("torch"):
        try:
            import torch
            ok = torch.cuda.is_available()
            lines.append(f"torch.cuda.is_available() = {ok}")
            if tag != "cpu" and not ok:
                lines.append("WARN: GPU present but torch is a CPU build; reinstall from the index above")
        except Exception as e:  # report, do not crash the doctor
            lines.append(f"WARN: import torch failed: {type(e).__name__}: {e}")
    if v.get("osqp"):
        lines.append("note: import osqp before pandas/numpy on this machine (docs/PITFALLS.md)")
    return lines
