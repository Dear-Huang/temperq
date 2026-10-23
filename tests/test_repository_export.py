"""Check export boundaries, including new source and tracked private files."""
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("repository_files", SCRIPTS / "repository_files.py")
repository = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repository)


@pytest.mark.parametrize("path", [
    "archives/releases/old.zip", "artifacts/source.zip", "data/token.txt",
    "runs/site.db", "config.local.yaml", "deploy/.env", "website/node_modules/a.js",
    "third_party/ashare-tcost/runs/paper.csv", "third_party/ashare-tcost/.git/config",
    "third_party/Kronos/model.py",
    "signals/private/predictions.parquet", "policy_templates/private/template.json",
])
def test_private_paths_cannot_enter_export(path):
    assert repository.private_path(path)


@pytest.mark.parametrize("path", [
    "deploy/.env.example", "config.example.yaml", "website/src/main.jsx",
    "third_party/ashare-tcost/src/ashare_tcost/model.py", "src/qwb/bench/lifecycle.py",
])
def test_source_paths_can_enter_export(path):
    assert not repository.private_path(path)


def test_inventory_includes_new_source_and_checks_tracked_ignored_files(tmp_path, monkeypatch):
    (tmp_path / "new.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text("local: yes\n", encoding="utf-8")
    monkeypatch.setattr(repository, "ROOT", tmp_path)
    def run(args, **kwargs):
        assert "--cached" in args and "--others" in args and "--exclude-standard" in args
        return type("Result", (), {"stdout": b"new.py\0config.local.yaml\0"})()
    monkeypatch.setattr(repository.subprocess, "run", run)
    assert repository.source_files() == ["config.local.yaml", "new.py"]


def test_release_ignores_sync_sidecars_created_during_copy(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import build_release

    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("release\n", encoding="utf-8")
    output = tmp_path / "export"
    monkeypatch.setattr(build_release, "ROOT", source)
    monkeypatch.setattr(build_release, "source_files", lambda: ["README.md"])
    monkeypatch.setattr(build_release, "check", lambda _: [])
    monkeypatch.setattr(sys, "argv", ["build_release.py", str(output), "--source-only"])
    original = build_release.shutil.copy2

    def copy_with_sidecar(src, dst):
        copied = original(src, dst)
        Path(str(dst) + ".baiduyun.uploading.cfg").write_text("sync state", encoding="utf-8")
        return copied

    monkeypatch.setattr(build_release.shutil, "copy2", copy_with_sidecar)
    assert build_release.main() == 0
    manifest = json.loads((output / "temperq/SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    assert set(manifest["files_sha256"]) == {"README.md"}
    with zipfile.ZipFile(output / "temperq-source.zip") as archive:
        assert set(archive.namelist()) == {"temperq/README.md", "temperq/SOURCE_MANIFEST.json"}


def test_portable_wheel_build_isolated_from_sync_sidecars_and_runtime(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import build_windows_portable as portable
    stage=tmp_path/'synced-release';stage.mkdir()
    for name in ('LICENSE','LICENSE.baiduyun.uploading.cfg','src/qwb/static/deployment.js',
                 'src/qwb/static/deployment.js.baiduyun.uploading.cfg','runtime/private.dll',
                 'build/stale.py','src/temperq.egg-info/PKG-INFO'):
        target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('payload',encoding='utf-8')
    observed=[]
    def install(args,**kwargs):
        source=Path(args[-2]);observed.append(source)
        assert not source.is_relative_to(stage)
        assert (source/'LICENSE').read_text(encoding='utf-8')=='payload'
        assert (source/'src/qwb/static/deployment.js').is_file()
        assert not list(source.rglob('*.baiduyun.uploading.cfg'))
        assert not (source/'runtime').exists() and not (source/'build').exists()
        assert not list(source.rglob('*.egg-info'))
    monkeypatch.setattr(portable,'run',install)
    portable.install_sources('python.exe',stage,'uv.exe')
    assert observed and not observed[0].exists()
    assert (stage/'LICENSE.baiduyun.uploading.cfg').is_file()  # leave sync state untouched
