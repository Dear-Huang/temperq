"""Shared, Git-aware source inventory for checks and exports."""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOTS = {"archives", "artifacts", "tools", "data", "runs", "models", "backups", "前期经验", "signals", "policy_templates"}
PRIVATE_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules",
                 "dist", "test-results", "playwright-report"}


def source_files() -> list[str]:
    result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others",
                             "--exclude-standard", "-z"], check=True, capture_output=True)
    files = sorted(set(n for n in result.stdout.decode("utf-8").split("\0") if n))
    for name in files:
        path = ROOT / name
        if not path.is_file():
            raise ValueError(f"tracked file is missing (record its deletion before export): {name}")
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"source must be a regular file within the repository: {name}")
    return files


def private_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (path.parts[0] in PRIVATE_ROOTS or bool(set(path.parts) & PRIVATE_PARTS)
            or path.name == "config.local.yaml"
            or (path.name.startswith(".env") and path.name != ".env.example")
            or (path.parts[0] == "third_party" and path.parts[1] != "ashare-tcost")
            or name.startswith("third_party/ashare-tcost/runs/")
            or path.name.endswith((".baiduyun.uploading.cfg", ".tmp", ".pyc")))
