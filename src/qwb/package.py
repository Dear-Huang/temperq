"""Model package manifest, whitelist build and pre-publish scan (SPEC §11).

The publish directory is built from the manifest's file list only; the scan
is a second line of defence against credentials and raw licensed data.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from .issues import ERROR, WARN, Report

MANIFEST = "qwb_package.yaml"

REQUIRED = {
    "name": None,
    "version": None,
    "base_model": ["name", "version"],
    "task_card": None,
    "data_recipe": None,
    "train_config": ["config_hash", "seeds"],
    "environment": ["lockfile"],
    "report": None,
    "licenses": ["model", "code", "data"],
    "files": None,
}

# Weight formats that load without executing code.
SAFE_WEIGHTS = {".safetensors", ".json", ".txt", ".onnx", ".ubj"}
UNSAFE_WEIGHTS = {".pkl", ".pickle", ".pt", ".pth", ".bin", ".joblib", ".ckpt"}
CODE_EXT = {".py", ".pyx", ".so", ".pyd", ".dll", ".sh", ".ps1", ".bat", ".ipynb"}

SECRET_PATTERNS = [
    ("HF_TOKEN", re.compile(r"hf_[A-Za-z0-9]{30,}")),
    ("GITHUB_TOKEN", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("OPENAI_LIKE_KEY", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("ASSIGNED_SECRET", re.compile(
        r"(?i)(token|api[_-]?key|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}")),
]
DATA_EXT = {".csv", ".parquet", ".feather", ".h5", ".hdf5", ".db", ".duckdb",
            ".sqlite", ".xlsx", ".xls"}
DATA_MAX_BYTES = 1_000_000          # small files (consistency samples) only warn
TEXT_EXT = {".py", ".yaml", ".yml", ".json", ".toml", ".txt", ".md", ".cfg",
            ".ini", ".sh", ".ps1", ".ipynb", ".html", ".csv"}
TEXT_SCAN_MAX = 5_000_000


def load_manifest(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate_manifest(m: dict) -> Report:
    rep = Report()
    for key, sub in REQUIRED.items():
        if key not in m:
            rep.add(ERROR, "MISSING_FIELD", f"manifest needs `{key}`")
            continue
        for s in sub or []:
            if not isinstance(m[key], dict) or s not in m[key]:
                rep.add(ERROR, "MISSING_FIELD", f"manifest needs `{key}.{s}`")
    if not rep.ok:
        return rep

    seeds = m["train_config"]["seeds"]
    if not isinstance(seeds, list) or not seeds:
        rep.add(ERROR, "BAD_SEEDS", "train_config.seeds must be a non-empty list")
    files = m["files"]
    if not isinstance(files, list) or not files:
        rep.add(ERROR, "NO_FILES", "files must list every file to publish")
        return rep
    for f in files:
        p = Path(str(f))
        if p.is_absolute() or ".." in p.parts:
            rep.add(ERROR, "BAD_PATH", f"{f}: files must be relative paths inside the package")
    for ref in ("task_card", "data_recipe", "report"):
        if m[ref] not in files:
            rep.add(ERROR, "UNLISTED_REF", f"{ref} ({m[ref]}) is not in files")

    custom = bool(m.get("custom_code", False))
    for f in files:
        ext = Path(str(f)).suffix.lower()
        if ext in CODE_EXT and not custom:
            rep.add(ERROR, "UNDECLARED_CODE",
                    f"{f}: code files require `custom_code: true`")
        if ext in UNSAFE_WEIGHTS:
            rep.add(WARN, "UNSAFE_WEIGHT_FORMAT",
                    f"{f}: pickle-based weights are never auto-loaded; prefer safetensors")
    if custom:
        rep.add(WARN, "CUSTOM_CODE", "package runs its own code; users must opt in to load it")
    if m.get("includes_data"):
        rep.add(WARN, "DECLARES_DATA",
                "package declares bundled data; licenses.data must allow redistribution")
    if "consistency_sample" not in m:
        rep.add(WARN, "NO_CONSISTENCY_SAMPLE",
                "no consistency sample: reproducibility level R2 cannot be checked")
    elif m["consistency_sample"] not in files:
        rep.add(ERROR, "UNLISTED_REF", "consistency_sample is not in files")
    return rep


def build(src: str | Path, dest: str | Path) -> Report:
    """Copy only manifest-listed files into dest, then scan the result."""
    src, dest = Path(src), Path(dest)
    rep = Report()
    mpath = src / MANIFEST
    if not mpath.exists():
        rep.add(ERROR, "NO_MANIFEST", f"{MANIFEST} not found in {src}")
        return rep
    m = load_manifest(mpath)
    rep.extend(validate_manifest(m))
    if not rep.ok:
        return rep
    if dest.exists() and any(dest.iterdir()):
        rep.add(ERROR, "DEST_NOT_EMPTY", f"{dest} must be empty")
        return rep
    dest.mkdir(parents=True, exist_ok=True)
    for f in [MANIFEST] + [str(x) for x in m["files"]]:
        s = src / f
        if not s.is_file():
            rep.add(ERROR, "FILE_MISSING", f"{f} listed but not found")
            continue
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, dest / f)
    rep.extend(scan_dir(dest, check_manifest=False))
    return rep


def scan_dir(root: str | Path, check_manifest: bool = True) -> Report:
    """Scan a package directory before upload."""
    root = Path(root)
    rep = Report()
    if check_manifest:
        mpath = root / MANIFEST
        if not mpath.exists():
            rep.add(ERROR, "NO_MANIFEST", f"{MANIFEST} not found in {root}")
        else:
            rep.extend(validate_manifest(load_manifest(mpath)))

    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()
        if p.name.startswith(".env"):
            rep.add(ERROR, "ENV_FILE", f"{rel}: .env files are never published")
            continue
        if ext in DATA_EXT:
            size = p.stat().st_size
            if size > DATA_MAX_BYTES:
                rep.add(ERROR, "RAW_DATA_IN_PACKAGE",
                        f"{rel} ({size // 1024} KB): ship the data recipe, not the data")
            else:
                rep.add(WARN, "SMALL_DATA_FILE", f"{rel}: check it holds no licensed raw data")
        if ext in TEXT_EXT and p.stat().st_size <= TEXT_SCAN_MAX:
            text = p.read_text(encoding="utf-8", errors="ignore")
            for code, pat in SECRET_PATTERNS:
                if pat.search(text):
                    rep.add(ERROR, "SECRET_" + code, f"{rel}: looks like a credential")
    return rep
