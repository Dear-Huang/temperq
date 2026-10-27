"""Persistent UI drafts, separate from immutable trading versions and ledgers."""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

from . import experiments as ex
from .jobs import Store

_lock = threading.RLock()


def path(workdir, pid):
    if not ex.ID_RE.fullmatch(pid):
        raise ValueError("策略草稿编号无效")
    return workdir / "runs/strategy_drafts" / (pid + ".json")


def read(workdir, pid):
    return ex.read(path(workdir, pid))


def update(workdir, pid, **values):
    with _lock:
        draft = read(workdir, pid)
        draft.update(values, updated_at=ex.now())
        ex.write(path(workdir, pid), draft)
        return draft


def create(workdir, *, route="train", name="我的第一个策略", sid="", parent=""):
    pid = uuid.uuid4().hex
    with _lock:
        path(workdir,pid).parent.mkdir(parents=True,exist_ok=True)
        ex.write(path(workdir, pid), dict(id=pid, name=name, route=route, sid=sid,
            parent=parent, created_at=ex.now(), updated_at=ex.now()))
    return pid


def resolved(workdir, pid):
    """Resolve job artifacts without changing a draft on page refresh."""
    draft = read(workdir, pid)
    if draft.get('data_job'):
        draft['data_job_status']=Store(Path(draft['job_root'])).status(draft['data_job'])['state']
    if draft.get("job"):
        store = Store(Path(draft["job_root"]))
        status = store.status(draft["job"])
        draft["job_status"] = status["state"]
        output = store.root / draft["job"] / "artifacts/output.txt"
        if status["state"] == "succeeded" and output.is_file():
            draft["source"] = Path(output.read_text(encoding="utf-8")).name
    return draft
