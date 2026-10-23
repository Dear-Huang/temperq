"""Local, append-only training attempts and verifiable artifact lineage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

ID_RE = re.compile(r"^[0-9a-f]{32}$")
TRAIN_FILES = ("task.yaml", "config.resolved.yaml", "folds.json", "predictions.parquet",
               "metrics.json", "report.md", "acceptance.lock", "models", "final_model", "data_manifest.json")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def object_digest(value) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str,
                      separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def write(path: Path, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    # Keep temporary names short for nested candidate archives on Windows.
    descriptor, name = tempfile.mkstemp(prefix=".qwb-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
        for attempt in range(9):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 8:
                    raise
                time.sleep(min(0.1 * 2 ** attempt, 1.0))
    finally:
        temporary.unlink(missing_ok=True)


def inventory(root: Path) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"artifact symlink is not supported: {path}")
        if (path.is_file() and path != root / "run.json"
                and not path.name.endswith((".baiduyun.uploading.cfg", ".tmp"))):
            files[path.relative_to(root).as_posix()] = digest(path)
    return files


def code_identity() -> str:
    root = Path(__file__).resolve().parent
    return object_digest({p.relative_to(root).as_posix(): digest(p)
                          for p in sorted(root.rglob("*")) if p.suffix in {".py", ".yaml"}})


def run_dir(workdir: Path, run_id: str) -> Path:
    if not ID_RE.fullmatch(run_id):
        raise ValueError("invalid run id")
    return Path(workdir).resolve() / "runs" / "history" / run_id


def begin(workdir: Path, rc: dict, target: Path) -> tuple[Path, dict]:
    run_id = uuid.uuid4().hex
    out = run_dir(workdir, run_id)
    out.mkdir(parents=True, exist_ok=False)
    record = {"schema_version": 1, "run_id": run_id,
              "experiment_id": object_digest(rc["card"]), "name": rc["name"],
              "task": rc["card"]["name"], "model": rc["model"],
              "config_hash": rc["config_hash"], "code_hash": code_identity(),
              "created_at": now(), "finished_at": None, "state": "running",
              "origin": "training", "archive_dir": str(out), "latest_dir": str(target),
              "job_id": rc.get("_job_id"),
              "model_version": None, "artifacts": {}, "error": None}
    write(out / "run.json", record)
    return out, record


def finish(out: Path, record: dict, summary: dict) -> dict:
    files = inventory(out)
    record.update(state="succeeded", finished_at=now(), artifacts=files,
                  artifact_hash=object_digest(files), model_version=summary.get("model_version"),
                  prediction_version=summary.get("prediction_version"),
                  data_snapshot_id=summary.get("data_snapshot_id"),
                  data_content_hash=summary.get("data_content_hash"),
                  final_test_years=summary.get("final_test_years"))
    write(out / "run.json", record)
    return record


def model_version(out: Path, rc: dict, snapshot: str) -> str | None:
    weights = {p.relative_to(out).as_posix(): digest(p)
               for p in sorted((out / "final_model").rglob("*"))
               if p.is_file() and p.name != "meta.json"}
    return object_digest({"weights": weights, "config": rc["config_hash"],
                          "task": rc["card"], "snapshot": snapshot,
                          "data_content_hash": rc.get("_data_content_hash")}) if weights else None


def verify(root: Path) -> dict:
    root = Path(root).resolve()
    record = read(root / "run.json")
    if record.get("schema_version") != 1 or record.get("state") != "succeeded":
        raise ValueError("training run is not a completed version")
    if not ID_RE.fullmatch(str(record.get("run_id", ""))):
        raise ValueError("invalid training run identity")
    files = record.get("artifacts")
    if not isinstance(files, dict) or not files or object_digest(files) != record.get("artifact_hash"):
        raise ValueError("training artifacts changed or are incomplete")
    for name, expected in files.items():
        path = root / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or ":" in name or path.is_symlink()
                or not path.resolve().is_relative_to(root) or digest(path) != expected):
            raise ValueError(f"training artifact changed: {name}")
    for required in ("task.yaml", "config.resolved.yaml", "folds.json", "predictions.parquet", "metrics.json", "report.md"):
        if required not in files:
            raise ValueError(f"missing training artifact: {required}")
    summary = read(root / "metrics.json")
    if record.get("origin") == "training":
        for key in ("run_id", "experiment_id", "model_version", "prediction_version", "config_hash", "data_snapshot_id", "data_content_hash"):
            if summary.get(key) != record.get(key):
                raise ValueError(f"training lineage mismatch: {key}")
    return record


def frozen_source(source: Path) -> Path:
    """Follow a latest copy to its archive only after checking both identities."""
    source = Path(source).resolve()
    if not source.is_dir() or not (source / "run.json").is_file():
        return source
    record = verify(source)
    archive = Path(record["archive_dir"])
    if archive != source and archive.is_dir():
        stored = verify(archive)
        if stored != record:
            raise ValueError("latest copy and archived training identity differ")
        return archive
    return source


def import_legacy(source: Path, workdir: Path) -> dict:
    source = Path(source).resolve()
    if (source / "run.json").exists():
        return verify(source)
    required = TRAIN_FILES[:6]
    if not all((source / name).is_file() for name in required):
        raise ValueError("legacy import needs task, resolved config, folds, predictions, metrics and report")
    raw = yaml.safe_load((source / "config.resolved.yaml").read_text(encoding="utf-8"))
    card = yaml.safe_load((source / "task.yaml").read_text(encoding="utf-8"))
    rc = {"card": card, "name": raw.get("name", source.name),
          "model": raw.get("model", "unknown"), "config_hash": raw.get("config_hash")}
    out, record = begin(workdir, rc, source)
    try:
        for name in TRAIN_FILES:
            path = source / name
            if path.is_dir():
                # Reject links before copying so unrelated files cannot enter a run.
                inventory(path)
                shutil.copytree(path, out / name)
            elif path.is_file():
                if path.is_symlink():
                    raise ValueError(f"artifact symlink is not supported: {path}")
                shutil.copy2(path, out / name)
        summary = read(out / "metrics.json")
        record.update(origin="legacy_import", code_hash=None, imported_at=now(),
                      original_created_at=None)
        summary = dict(summary, model_version=model_version(out, rc, summary.get("data_snapshot_id")))
        return finish(out, record, summary)
    except BaseException as exc:
        record.update(state="failed", finished_at=now(), error=f"{type(exc).__name__}: {exc}")
        write(out / "run.json", record)
        raise


def preserve_latest(source: Path, workdir: Path) -> dict:
    if not (source / "run.json").exists():
        return import_legacy(source, workdir)
    record = verify(source)
    archive = run_dir(workdir, record["run_id"])
    if archive.exists():
        if verify(archive) != record:
            raise ValueError("existing archive differs from latest output")
        return record
    archive.mkdir(parents=True, exist_ok=False)
    for name in [*record["artifacts"], "run.json"]:
        target = archive / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    verify(archive)
    return record


def list_runs(workdir: Path) -> list[dict]:
    rows = []
    for path in (Path(workdir) / "runs" / "history").glob("*/run.json"):
        try:
            record = read(path)
            if not ID_RE.fullmatch(str(record.get("run_id", ""))) or path.parent.name != record["run_id"]:
                continue
            rows.append(dict(record, path=str(path.parent)))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)


def register_evaluation(workdir: Path, out: Path, scorecard: dict) -> None:
    root = Path(workdir) / "runs" / "evaluations"
    root.mkdir(parents=True, exist_ok=True)
    lineage = scorecard.get("lineage") or {}
    write(root / f"{scorecard['evaluation_id']}.json", {
        "evaluation_id": scorecard["evaluation_id"], "run_id": lineage.get("run_id"),
        "path": str(out.resolve()), "scorecard_hash": digest(out / "scorecard.json"),
        "created_at": scorecard["created_at"], "status": scorecard["status"],
        "protocol_hash": scorecard["protocol_hash"]})


def list_evaluations(workdir: Path, run_id: str | None = None) -> list[dict]:
    rows = []
    for path in (Path(workdir) / "runs" / "evaluations").glob("*.json"):
        try:
            row = read(path)
            if ID_RE.fullmatch(str(row.get("evaluation_id", ""))) and (run_id is None or row.get("run_id") == run_id):
                rows.append(row)
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)


def evaluation_dir(workdir: Path, evaluation_id: str) -> Path:
    if not ID_RE.fullmatch(evaluation_id):
        raise ValueError("invalid evaluation id")
    entry = read(Path(workdir) / "runs" / "evaluations" / f"{evaluation_id}.json")
    root = Path(entry["path"])
    if digest(root / "scorecard.json") != entry["scorecard_hash"]:
        raise ValueError("registered evaluation changed")
    return root


@contextmanager
def output_lock(target: Path):
    """OS releases the lock on process exit; the empty lock file may remain."""
    target.parent.mkdir(parents=True, exist_ok=True)
    path = target.parent / f".{target.name}.lock"
    for attempt in range(9):
        try:
            stream=path.open("a+b")
            break
        except PermissionError:
            if attempt==8:raise
            time.sleep(min(.1*2**attempt,1.))
    with stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"another run is using output: {target}") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
