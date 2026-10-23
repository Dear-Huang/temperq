"""Check publishable files without reading ignored local data."""
from __future__ import annotations

import sys
from pathlib import PurePosixPath

from repository_files import ROOT, private_path, source_files

sys.path.insert(0, str(ROOT / "src"))
from qwb.package import SECRET_PATTERNS, TEXT_EXT, TEXT_SCAN_MAX  # noqa: E402

REQUIRED = {"README.md", "LICENSE", "pyproject.toml", "requirements.lock",
            "requirements-site.lock", "requirements-dev.lock", "config.example.yaml",
            "third_party/ashare-tcost/pyproject.toml", "third_party/ashare-tcost/LICENSE",
            "website/package-lock.json", "share_site/app.py", "bench/README.md",
            "deploy/Dockerfile", "deploy/compose.yaml", "deploy/.env.example"}


def check(files: list[str]) -> list[str]:
    errors = [f"Missing required source: {name}" for name in sorted(REQUIRED - set(files))]
    for name in files:
        if private_path(name):
            errors.append(f"Private/build path is included in Git: {name}")
            continue
        path = ROOT / name
        if path.stat().st_size >= 100_000_000:
            errors.append(f"File exceeds GitHub's 100 MB limit: {name}")
        if ((PurePosixPath(name).suffix.lower() in TEXT_EXT or path.name == ".env.example")
                and path.stat().st_size <= TEXT_SCAN_MAX):
            content = path.read_text(encoding="utf-8", errors="ignore")
            if path.name == ".env.example":
                for placeholder in ("replace-with-huggingface-oauth-client-id",
                                    "replace-with-huggingface-oauth-client-secret",
                                    "replace-with-a-random-session-secret"):
                    content = content.replace("=" + placeholder + "\n", "=EXAMPLE\n")
            for code, pattern in SECRET_PATTERNS:
                if pattern.search(content):
                    errors.append(f"Possible credential ({code}): {name}")
    return errors


if __name__ == "__main__":
    files = source_files()
    errors = check(files)
    for error in errors:
        print(error)
    print(f"Checked {len(files)} publishable files; {len(errors)} error(s).")
    sys.exit(bool(errors))
