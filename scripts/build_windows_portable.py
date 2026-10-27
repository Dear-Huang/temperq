"""Build a Windows x64 CPU app with an independent Python runtime (no venv)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from repository_files import ROOT, source_files
from check_repository import check


LAUNCHER = r'''@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
"%~dp0runtime\python.exe" -E -s -X utf8 -m qwb.cli ui %*
if errorlevel 1 (
  echo TemperQ could not start. Keep this window open and share the error above.
  pause
)
'''

README = """# TemperQ · Windows 下载即用版

1. 将整个 ZIP 解压到可写文件夹（不要直接在压缩包里运行）。
2. 双击 start.bat，保留命令窗口，浏览器会打开本机工作台。
3. 首页进入治理诊断，导入预测、持仓、参考价格与规则；可先载入合成示例。
4. 比较方案、确认后导出参考计划。原训练与运行账户通过“我的策略”继续，不自动改动。

Windows 10/11 x64；内含 Python 和 CPU 训练依赖，无需 Python、uv、git、Node 或显卡。
启动及合成数据训练可离线；下载行情需要联网，免费服务可能中断。
建议 16GB 内存；首次使用先保留默认配置。40 股数据只用于入门，不代表全市场收益。
关闭命令窗口会停止网页；已提交的独立训练/下载任务继续运行，可重新打开查看。
纸面账户自动任务需要工作台运行或另配系统计划任务；关机时不会记账。

数据、模型和任务保存在本文件夹的 data/ 与 runs/ 中。升级前备份，勿覆盖删除这些文件夹。
完整手册：docs/使用手册.md。运行环境及各组件许可：THIRD_PARTY_NOTICES.md、runtime/LICENSE.txt。
本项目源代码包含于 src/ 和 third_party/ashare-tcost/，AGPL-3.0-or-later，见 LICENSE。
这是研究与模拟工具，不自动下单，不承诺收益。新策略默认计 STRUCT 上月报价半价差估计；实际成交冲击未标定，旧账户保留原口径。
"""


def run(args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def install_sources(python, stage, uv):
    # Cloud-sync daemons can create/remove sidecars while setuptools scans its
    # automatic LICENSE* glob. Build outside the synced release directory.
    with tempfile.TemporaryDirectory(prefix="temperq-wheel-") as temporary:
        source = Path(temporary) / "source"
        shutil.copytree(stage, source, ignore=shutil.ignore_patterns(
            "runtime", "build", "*.egg-info", "__pycache__", "*.baiduyun.uploading.cfg"))
        run([uv, "pip", "install", "--python", python, "--no-deps", source, source / "third_party/ashare-tcost"])


def build(out: Path, python_home: Path, uv: str):
    if os.name != "nt":
        raise ValueError("Build on Windows x64 with a standalone CPython 3.11 runtime")
    out, python_home = out.resolve(), python_home.resolve()
    if out.exists():
        raise FileExistsError(f"Use a new output directory: {out}")
    if (python_home / "pyvenv.cfg").exists() or not (python_home / "python311.dll").is_file():
        raise ValueError("Expected independent CPython 3.11, not a virtual environment")
    version = subprocess.check_output([str(python_home / "python.exe"), "-I", "-c",
                                      "import sys,struct; print(sys.version); assert struct.calcsize('P') == 8"], text=True)
    files = source_files()
    errors = check(files)
    if errors:
        raise ValueError("\n".join(errors))
    stage = out / "TemperQ"
    stage.mkdir(parents=True)
    # Keep the corresponding source, examples and manuals; no site or private data.
    prefixes = ("src/", "examples/", "bench/", "third_party/ashare-tcost/", "docs/", "scripts/")
    roots = {"pyproject.toml", "requirements.lock", "LICENSE", "config.example.yaml", "AGENTS.md"}
    for name in files:
        if name not in roots and not name.startswith(prefixes):
            continue
        if name.startswith(("docs/dev/", "docs/research/")) or name.endswith((".docx", ".mjs")):
            continue
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    # pyproject readme is also needed for a non-editable package build.
    shutil.copy2(ROOT / "README.md", stage / "README.md")
    runtime = stage / "runtime"
    runtime.mkdir()
    for folder in ("Lib", "DLLs", "tcl"):
        shutil.copytree(python_home / folder, runtime / folder,
                        ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"))
    for name in ("python.exe", "pythonw.exe", "python3.dll", "python311.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt", "BUILD"):
        if (python_home / name).is_file():
            shutil.copy2(python_home / name, runtime / name)
    python = runtime / "python.exe"
    # The copied runtime is ours, not the uv-managed original installation.
    (runtime / "Lib" / "EXTERNALLY-MANAGED").unlink(missing_ok=True)
    # LightGBM's wheel requires MSVC/OpenMP beyond the DLLs shipped by CPython.
    # Use a versioned redistributable wheel, never arbitrary developer System32 files.
    microsoft = out / "microsoft-runtime-wheel"
    run([uv, "pip", "install", "--python", python, "--target", microsoft, "--no-deps",
         "--only-binary", ":all:", "msvc-runtime==14.44.35112"])
    for dll in microsoft.glob("*.dll"):
        shutil.copy2(dll, runtime / dll.name)
    shutil.copy2(microsoft / "msvc_runtime-14.44.35112.dist-info/licenses/LICENSE",
                 runtime / "MICROSOFT_RUNTIME_LICENSE.txt")
    signature_check = "$ErrorActionPreference='Stop'; Get-ChildItem -LiteralPath $env:TEMPERQ_RUNTIME_PAYLOAD -Filter '*.dll' | ForEach-Object { $s=Get-AuthenticodeSignature -LiteralPath $_.FullName; if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw ('Invalid Microsoft signature: '+$_.Name) } }"
    run([shutil.which("pwsh") or "powershell.exe", "-NoProfile", "-Command", signature_check],
        env=dict(os.environ, TEMPERQ_RUNTIME_PAYLOAD=str(microsoft)))
    run([uv, "pip", "install", "--python", python, "--only-binary", ":all:", "-r", ROOT / "requirements.lock"])
    install_sources(python, stage, uv)
    packages = runtime / "Lib" / "site-packages"
    # Local build provenance contains machine paths; source identity is in our manifest.
    for item in packages.glob("*/direct_url.json"):
        item.unlink()
    # Wheel entry-point EXEs embed the build-time interpreter path. The BAT
    # launchers below resolve the shipped interpreter relative to themselves.
    if (runtime / "Scripts").exists():
        shutil.rmtree(runtime / "Scripts")
    for project in (stage, stage / "third_party/ashare-tcost"):
        for generated in [project / "build", *project.glob("src/*.egg-info")]:
            if generated.exists():
                shutil.rmtree(generated)
    run([python, "-I", "-c", "import qwb,ashare_tcost,lightgbm,baostock; import importlib.util; assert importlib.util.find_spec('torch') is None"])
    inventory_code = "import importlib.metadata as m,json; print(json.dumps(sorted([(d.metadata['Name'],d.version,d.metadata.get('License-Expression') or d.metadata.get('License','')) for d in m.distributions()]),ensure_ascii=False))"
    dependencies = json.loads(subprocess.check_output([str(python), "-I", "-X", "utf8", "-c", inventory_code], text=True, encoding="utf-8"))
    notices = ["# Bundled software\n", "Python: " + version.strip(),
               "\nMicrosoft C++/OpenMP DLLs: msvc-runtime 14.44.35112 wheel; original signatures verified. Redistribution terms: runtime/MICROSOFT_RUNTIME_LICENSE.txt.",
               "\nPython distribution license: runtime/LICENSE.txt. Dependency license files are retained in runtime/Lib/site-packages/*dist-info/ (including licenses/).",
               "\nThe complete corresponding TemperQ and ashare-tcost source is included. Build script: scripts/build_windows_portable.py. Font license: src/qwb/static/OFL.txt.",
               "\n| Package | Version | Declared license |\n| --- | --- | --- |"]
    for name, ver, license_text in dependencies:
        notices.append(f"| {name} | {ver} | {license_text.replace(chr(10), ' ').replace('|', '/')} |")
    (stage / "THIRD_PARTY_NOTICES.md").write_text("\n".join(notices) + "\n", encoding="utf-8")
    (stage / "README.md").write_text(README, encoding="utf-8")
    (stage / "start.bat").write_bytes(LAUNCHER.replace("\n", "\r\n").encode("ascii"))
    (stage / "temperq.bat").write_bytes(LAUNCHER.replace("qwb.cli ui %*", "qwb.cli %*")
                                        .replace("\n", "\r\n").encode("ascii"))
    # Bytecode caches and build metadata are generated locally and not needed to run.
    for directory in sorted(stage.rglob("__pycache__"), reverse=True):
        shutil.rmtree(directory)
    names = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*")
                   if p.is_file() and not p.name.endswith(".baiduyun.uploading.cfg"))
    manifest = {n: hashlib.sha256((stage / n).read_bytes()).hexdigest() for n in names}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (stage / "MANIFEST.json").write_text(json.dumps({"source_commit": commit,
        "working_tree_changes": subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True),
        "python": version.strip(), "dependencies": dependencies, "files_sha256": manifest},
        ensure_ascii=False, indent=2), encoding="utf-8")
    archive = out / "TemperQ-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in [*names, "MANIFEST.json"]:
            payload = (stage / name).read_bytes()
            if name in manifest and hashlib.sha256(payload).hexdigest() != manifest[name]:
                raise RuntimeError(f"Staged file changed: {name}")
            z.writestr("TemperQ/" + name, payload)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Portable: {archive} ({archive.stat().st_size / 2**20:.1f} MB)", flush=True)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--python-home", type=Path, required=True,
                        help="Independent python-build-standalone CPython 3.11 installation")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    build(args.out, args.python_home, args.uv)


if __name__ == "__main__":
    main()
