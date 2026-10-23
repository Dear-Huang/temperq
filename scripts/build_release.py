"""Export current source (--source-only) or a local-app download (default).

Includes eligible untracked source; never overwrites an existing export.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from repository_files import ROOT, source_files
from check_repository import check

UV_VERSION = "0.11.24"
UV_LINUX = (f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
            "uv-x86_64-unknown-linux-gnu.tar.gz")
LOCAL_EXCLUDE = {"SPEC.md", "SETUP-依赖拉取清单.md", "docs/QW_BENCH_RESEARCH.md",
                 "docs/DEVELOPMENT.md", "docs/INTEGRATION.md", "docs/VALIDATION.md",
                 "scripts/build_release.py", "scripts/check_repository.py",
                 "scripts/repository_files.py", "scripts/m1_pilot.py", "scripts/m2_kronos.py", "scripts/qa_workbench.mjs",
                 "tests/test_repository_export.py", "docs/使用手册.docx"}
LOCAL_PREFIX = ("website/", "share_site/", "deploy/", ".github/", ".dockerignore",
                "tests/test_site.py", "tests/test_forum.py")
LOCAL_README = """# WiseHold 本地工作台

本包用于本地/计算服务器训练，包含 third_party/ashare-tcost 成本库。
完整源码仓库还提供网站、社区和部署配置，本地用户包不包含这些服务。

Windows：双击 install.bat，安装完成后双击 start.bat。
Linux：sh install.sh，然后 sh start.sh --no-browser。
默认地址：http://127.0.0.1:8765（占用时递增）。先选合成数据走通流程。

详细步骤见 [使用手册](docs/使用手册.md)，模型评价见 [Bench](bench/README.md)。
真实数据路径填写在 config.local.yaml，可从 config.example.yaml 复制修改。
不要分发本机密钥、真实行情、模型权重或含私密信息的运行目录。

命令行试运行：wisehold run examples/run_smoke.yaml
训练与评价使用同一 Python 环境；GPU/Kronos 为可选扩展。
实验记录、独立 Bench、版本比较与研究摘要导出见 [研究闭环说明](docs/RESEARCH_WORKFLOW.md)。
这是研究工具，不提供收益承诺或实盘认证。代码许可见 LICENSE。
"""


def bundle_uv(dest: Path, tools: Path | None) -> None:
    windows = tools / "uv.exe" if tools else None
    if not windows or not windows.is_file():
        executable = shutil.which("uv")
        windows = Path(executable) if executable else None
    if not windows or not windows.is_file() or windows.suffix.lower() != ".exe":
        raise RuntimeError("Windows uv.exe is required; pass --tools-dir containing uv.exe and uv")
    shutil.copy2(windows, dest / "uv.exe")
    linux = tools / "uv" if tools else None
    if linux and linux.is_file():
        shutil.copy2(linux, dest / "uv")
    else:
        with urllib.request.urlopen(UV_LINUX, timeout=120) as response:
            data = response.read()
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith("/uv") and m.isfile())
            (dest / "uv").write_bytes(archive.extractfile(member).read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", nargs="?", type=Path)
    parser.add_argument("--source-only", action="store_true", help="include website, deployment and CI; no uv binaries")
    parser.add_argument("--tools-dir", type=Path, help="offline cache containing uv.exe and Linux uv")
    args = parser.parse_args()
    files = source_files()
    problems = check(files)
    if problems:
        raise RuntimeError("Repository checks failed:\n" + "\n".join(problems))
    kind = "source" if args.source_only else "local"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (args.out_dir or ROOT / "artifacts" / "releases" / f"{kind}-{stamp}").resolve()
    stage = out / "wisehold"
    zpath = out / f"wisehold-{kind}.zip"
    if stage.exists() or zpath.exists():
        raise FileExistsError(f"use a new output directory: {out}")
    if not args.source_only:
        files = [p for p in files if p not in LOCAL_EXCLUDE and not p.startswith(LOCAL_PREFIX)]
    stage.mkdir(parents=True)
    for name in files:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    if not args.source_only:
        (stage / "README.md").write_text(LOCAL_README, encoding="utf-8")
        (stage / "tools").mkdir()
        bundle_uv(stage / "tools", args.tools_dir)
    names = sorted(set(files) | ({"tools/uv", "tools/uv.exe"} if not args.source_only else set()))
    manifest = {name: hashlib.sha256((stage / name).read_bytes()).hexdigest() for name in names}
    (stage / "SOURCE_MANIFEST.json").write_text(json.dumps({
        "created_utc": stamp, "kind": kind, "files_sha256": manifest,
        "note": "Current working files; an export is not a Git commit or signed release.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        # Sync clients may create sidecars while staging; export only inventoried files.
        for name in [*names, "SOURCE_MANIFEST.json"]:
            path = stage / name
            content = path.read_bytes()
            if name in manifest and hashlib.sha256(content).hexdigest() != manifest[name]:
                raise RuntimeError(f"staged source changed during export: {name}")
            info = zipfile.ZipInfo.from_file(path, Path("wisehold") / path.relative_to(stage))
            if path.name == "uv" or path.suffix == ".sh":
                info.external_attr = 0o100755 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    print(f"Exported {len(manifest)} files ({kind}); SHA-256 inventory: SOURCE_MANIFEST.json")
    print(f"zip: {zpath} ({zpath.stat().st_size / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
