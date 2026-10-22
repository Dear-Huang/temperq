"""Build the user download: one folder, one zip.

    python -X utf8 scripts/build_release.py [out_dir]

Layout (what the user unzips):
    qwb/
      1_安装.bat  2_启动.bat  使用手册.docx  使用手册.md
      tools/uv.exe  tools/uv (linux)          bundled installer, no download needed
      quant-workbench/   (git-tracked files only: no runs/, data/, config.local.yaml)
      ashare-tcost/      (git-tracked files only)
Only files tracked by git are copied, so local data, tokens and results cannot
leak into the zip; a secret scan runs on the result as a second check.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TCOST = ROOT.parent / "ashare-tcost"
sys.path.insert(0, str(ROOT / "src"))

UV_VERSION = "0.11.24"
UV_LINUX = (f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
            "uv-x86_64-unknown-linux-gnu.tar.gz")

LAUNCH_INSTALL = """@echo off
chcp 65001 >nul
cd /d "%~dp0quant-workbench"
call install.bat %*
"""
LAUNCH_START = """@echo off
chcp 65001 >nul
cd /d "%~dp0quant-workbench"
call start.bat
"""


def tracked(repo: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True,
                         check=True).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


# internal design / research notes: kept in git, not shipped to users
EXCLUDE = {"SPEC.md", "SETUP-依赖拉取清单.md", "docs/QW_BENCH_RESEARCH.md",
           "scripts/build_release.py", "scripts/m1_pilot.py", "scripts/m2_kronos.py"}


# server side (share site, public website) and their tests: not part of the local app
EXCLUDE_PREFIX = ("website/", "share_site/", "tests/test_site.py", "tests/test_forum.py")


def copy_tracked(repo: Path, dest: Path) -> int:
    files = [f for f in tracked(repo) if f not in EXCLUDE and not f.startswith(EXCLUDE_PREFIX)]
    for f in files:
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / f, dest / f)
    return len(files)


def crlf(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


def uv_windows(dest: Path) -> None:
    exe = shutil.which("uv")
    if not exe:
        raise SystemExit("uv not found on PATH (needed to bundle tools/uv.exe)")
    shutil.copy2(exe, dest / "uv.exe")


def uv_linux(dest: Path) -> bool:
    try:
        data = urllib.request.urlopen(UV_LINUX, timeout=120).read()
    except Exception as e:  # optional: the Linux installer can download uv itself
        print(f"  (linux uv not bundled: {type(e).__name__}; install.sh will fetch it)")
        return False
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        m = next(m for m in t.getmembers() if m.name.endswith("/uv"))
        (dest / "uv").write_bytes(t.extractfile(m).read())
    return True


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "qwb_release"
    stage = out / "qwb"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "tools").mkdir(parents=True)
    n1 = copy_tracked(ROOT, stage / "quant-workbench")
    n2 = copy_tracked(TCOST, stage / "ashare-tcost") if (TCOST / ".git").exists() else 0
    uv_windows(stage / "tools")
    has_linux_uv = uv_linux(stage / "tools")
    crlf(stage / "1_安装.bat", LAUNCH_INSTALL)
    crlf(stage / "2_启动.bat", LAUNCH_START)
    for f in ("使用手册.docx", "使用手册.md"):
        src = ROOT / "docs" / f
        if src.exists():
            shutil.copy2(src, stage / f)

    from qwb import package
    rep = package.scan_dir(stage, check_manifest=False)
    bad = [i for i in rep.issues if i.level == "ERROR"]
    if bad:
        print("\n".join(str(i) for i in bad))
        raise SystemExit("secret/data scan failed; nothing zipped")

    zpath = out / "qwb_windows_linux.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(stage.rglob("*")):
            # sync clients (e.g. Baidu Netdisk) drop marker files next to new files
            if p.is_file() and not p.name.endswith((".baiduyun.uploading.cfg", ".tmp")):
                info = zipfile.ZipInfo.from_file(p, Path("qwb") / p.relative_to(stage))
                if p.name in ("uv", "install.sh", "start.sh"):
                    info.external_attr = 0o755 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, p.read_bytes())
    print(f"files: workbench {n1}, tcost {n2}; linux uv bundled: {has_linux_uv}")
    print(f"zip: {zpath}  ({zpath.stat().st_size / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
