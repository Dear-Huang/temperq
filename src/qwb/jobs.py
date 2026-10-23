"""Local job service skeleton (SPEC §8).

Filesystem-backed so UI, CLI and MCP share one protocol without a server:

    <root>/<job_id>/spec.json      JobSpec
    <root>/<job_id>/status.json    {"state": ..., "updated": ..., "error": ...}
    <root>/<job_id>/events.jsonl   progress / log events, one JSON per line
    <root>/<job_id>/artifacts/     outputs
    <root>/<job_id>/cancel         presence = cancel requested
    <root>/<job_id>/done/<unit>    completed resumable units

States: queued -> running -> succeeded | failed | cancelled.
GPU jobs take a lock file, so one GPU job runs at a time.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

KINDS = {"fetch", "validate", "train", "evaluate", "package", "publish"}
STATES = {"queued", "running", "succeeded", "failed", "cancelled"}
TERMINAL = {"succeeded", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Cancelled(Exception):
    pass


@dataclass
class JobSpec:
    kind: str
    config: dict
    resources: str = "cpu"           # cpu | gpu
    parent: str | None = None
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    config_hash: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        if self.resources not in {"cpu", "gpu"}:
            raise ValueError("resources must be cpu or gpu")
        blob = json.dumps(self.config, sort_keys=True, ensure_ascii=False, default=str)
        self.config_hash = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Context:
    """Handed to a job function: progress events, cancel checks, resumable units."""

    def __init__(self, store: "Store", job_id: str):
        self.store, self.job_id = store, job_id
        self.dir = store.root / job_id
        self.artifacts = self.dir / "artifacts"

    def log(self, msg: str, **extra) -> None:
        self.store._event(self.job_id, {"type": "log", "msg": msg, **extra})

    def progress(self, done: int, total: int, msg: str = "") -> None:
        self.store._event(self.job_id, {"type": "progress", "done": done, "total": total,
                                        "msg": msg})

    def check_cancel(self) -> None:
        if (self.dir / "cancel").exists():
            raise Cancelled()

    def unit_done(self, unit: str) -> bool:
        return (self.dir / "done" / unit).exists()

    def mark_done(self, unit: str) -> None:
        p = self.dir / "done" / unit
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- protocol -------------------------------------------------------
    def submit(self, spec: JobSpec) -> str:
        d = self.root / spec.job_id
        (d / "artifacts").mkdir(parents=True, exist_ok=False)
        (d / "spec.json").write_text(json.dumps(asdict(spec), ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        self._set(spec.job_id, "queued")
        return spec.job_id

    def status(self, job_id: str) -> dict:
        return json.loads((self.root / job_id / "status.json").read_text(encoding="utf-8"))

    def spec(self, job_id: str) -> dict:
        return json.loads((self.root / job_id / "spec.json").read_text(encoding="utf-8"))

    def logs(self, job_id: str) -> list[dict]:
        p = self.root / job_id / "events.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]

    def artifacts(self, job_id: str) -> list[str]:
        a = self.root / job_id / "artifacts"
        return sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())

    def cancel(self, job_id: str) -> None:
        if self.status(job_id)["state"] == "queued":
            self._set(job_id, "cancelled")
        (self.root / job_id / "cancel").touch()

    def retry(self, job_id: str) -> None:
        """Re-queue a failed/cancelled job; completed units are kept for resume."""
        if self.status(job_id)["state"] not in {"failed", "cancelled"}:
            raise ValueError("only failed or cancelled jobs can be retried")
        (self.root / job_id / "cancel").unlink(missing_ok=True)
        self._set(job_id, "queued")

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir()):
            if (d / "status.json").exists():
                out.append({"job_id": d.name, **self.status(d.name)})
        return out

    # ---- execution ------------------------------------------------------
    def run(self, job_id: str, fn: Callable[[Context, dict], None]) -> str:
        """Run a queued job in this process. Returns the final state."""
        from .experiments import output_lock
        with output_lock(self.root / job_id / "execution"):
            return self._run(job_id, fn)

    def _run(self, job_id: str, fn: Callable[[Context, dict], None]) -> str:
        if self.status(job_id)["state"] != "queued":
            raise ValueError("job is not queued")
        spec = self.spec(job_id)
        from .experiments import output_lock
        lock = output_lock(self.root / "gpu-resource") if spec["resources"] == "gpu" else None
        if lock is not None:
            try:
                lock.__enter__()
            except ValueError:
                self._event(job_id, {"type": "log", "msg": "gpu busy, left queued"})
                return "queued"
        self._set(job_id, "running")
        ctx = Context(self, job_id)
        try:
            from .env import metadata
            self._event(job_id, {"type": "env", **metadata()})
            ctx.check_cancel()
            fn(ctx, spec["config"])
            state, err = "succeeded", None
        except (Cancelled, KeyboardInterrupt):
            state, err = "cancelled", None
        except Exception as e:  # recorded, not swallowed: state=failed with traceback
            state, err = "failed", f"{type(e).__name__}: {e}"
            self._event(job_id, {"type": "error", "msg": err,
                                 "traceback": traceback.format_exc()})
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)
        self._set(job_id, state, err)
        return state

    # ---- internals ------------------------------------------------------
    def _set(self, job_id: str, state: str, error: str | None = None) -> None:
        assert state in STATES
        p = self.root / job_id / "status.json"
        tmp = p.with_name(f"status.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"state": state, "updated": _now(), "error": error,
                                   "pid": os.getpid() if state == "running" else None}),
                       encoding="utf-8")
        for attempt in range(5):     # a reader may hold status.json open briefly
            try:
                tmp.replace(p)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
        self._event(job_id, {"type": "state", "state": state})

    def _event(self, job_id: str, ev: dict) -> None:
        """Append one event. A log write must never kill the job: on Windows a
        concurrent reader can briefly deny the append, so retry, then fall back
        to stderr."""
        line = json.dumps({"t": _now(), **ev}, ensure_ascii=False, default=str) + "\n"
        for attempt in range(5):
            try:
                with open(self.root / job_id / "events.jsonl", "a", encoding="utf-8") as f:
                    f.write(line)
                return
            except PermissionError:
                time.sleep(0.2 * (attempt + 1))
        sys.stderr.write(f"[qwb] could not write event for {job_id}: {line}")

    def recover(self, job_id: str) -> None:
        """Mark a job left in `running` by a dead process as failed, so it can be retried."""
        from .experiments import list_runs, output_lock, read, write, now
        with output_lock(self.root / job_id / "execution"):
            if self.status(job_id)["state"] == "running":
                self._set(job_id, "failed", "process exited while running (recovered)")
                workdir = self.root.parent.parent
                for row in list_runs(workdir):
                    if row.get("job_id") == job_id and row["state"] == "running":
                        path = Path(row["path"]) / "run.json"
                        record = read(path)
                        record.update(state="failed", finished_at=now(), error="worker exited while running (recovered)")
                        write(path, record)
