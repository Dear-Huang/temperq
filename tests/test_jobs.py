import pytest

from qwb.jobs import JobSpec, Store


def test_lifecycle_success(tmp_path):
    s = Store(tmp_path)
    jid = s.submit(JobSpec("fetch", {"a": 1}))
    assert s.status(jid)["state"] == "queued"

    def fn(ctx, cfg):
        ctx.progress(1, 2)
        (ctx.artifacts / "out.txt").write_text("x")
        ctx.progress(2, 2)

    assert s.run(jid, fn) == "succeeded"
    assert s.artifacts(jid) == ["out.txt"]
    kinds = [e["type"] for e in s.logs(jid)]
    assert kinds.count("progress") == 2 and kinds[-1] == "state"
    assert s.spec(jid)["config_hash"].startswith("sha256:")


def test_failure_is_recorded_and_retry_resumes(tmp_path):
    s = Store(tmp_path)
    jid = s.submit(JobSpec("train", {}))
    ran, failed_once = [], []

    def fn(ctx, cfg):
        for unit in ("fold0", "fold1"):
            if ctx.unit_done(unit):
                continue
            if unit == "fold1" and not failed_once:
                failed_once.append(1)
                raise RuntimeError("boom")
            ran.append(unit)
            ctx.mark_done(unit)

    assert s.run(jid, fn) == "failed"
    assert "boom" in s.status(jid)["error"]
    s.retry(jid)
    assert s.run(jid, fn) == "succeeded"
    assert ran.count("fold0") == 1 and "fold1" in ran   # fold0 not redone


def test_cancel_running_and_queued(tmp_path):
    s = Store(tmp_path)
    jid = s.submit(JobSpec("fetch", {}))

    def fn(ctx, cfg):
        s.cancel(jid)          # as if the UI pressed cancel mid-run
        ctx.check_cancel()

    assert s.run(jid, fn) == "cancelled"
    q = s.submit(JobSpec("fetch", {}))
    s.cancel(q)
    assert s.status(q)["state"] == "cancelled"


def test_one_gpu_job_at_a_time(tmp_path):
    s = Store(tmp_path)
    a = s.submit(JobSpec("train", {}, resources="gpu"))
    b = s.submit(JobSpec("train", {}, resources="gpu"))

    def outer(ctx, cfg):
        assert s.run(b, lambda c, k: None) == "queued"   # lock held by a

    assert s.run(a, outer) == "succeeded"
    assert s.status(b)["state"] == "queued"
    assert s.run(b, lambda c, k: None) == "succeeded"


def test_log_write_failure_does_not_kill_job(tmp_path, monkeypatch):
    import builtins
    s = Store(tmp_path)
    jid = s.submit(JobSpec("fetch", {}))
    monkeypatch.setattr("qwb.jobs.time.sleep", lambda x: None)
    real_open = builtins.open

    def flaky_open(path, mode="r", *a, **k):
        if str(path).endswith("events.jsonl") and "a" in mode and flaky_open.fail:
            raise PermissionError("locked by a reader")
        return real_open(path, mode, *a, **k)
    flaky_open.fail = False
    monkeypatch.setattr(builtins, "open", flaky_open)

    def fn(ctx, cfg):
        flaky_open.fail = True
        ctx.log("this event is lost, the job is not")
        flaky_open.fail = False

    assert s.run(jid, fn) == "succeeded"


def test_recover_stale_running(tmp_path):
    s = Store(tmp_path)
    jid = s.submit(JobSpec("fetch", {}))
    s._set(jid, "running")            # as left by a killed process
    s.recover(jid)
    assert s.status(jid)["state"] == "failed"
    s.retry(jid)
    assert s.status(jid)["state"] == "queued"


def test_bad_kind():
    with pytest.raises(ValueError):
        JobSpec("deploy", {})


def test_status_update_survives_longer_transient_windows_reader(tmp_path,monkeypatch):
    from pathlib import Path
    store=Store(tmp_path);jid=store.submit(JobSpec("fetch",{}))
    replace=Path.replace;attempts=[]
    monkeypatch.setattr("qwb.experiments.time.sleep",lambda _:None)
    def transient(source,target):
        if Path(target).name=="status.json":
            attempts.append(1)
            if len(attempts)<7:raise PermissionError("antivirus reader")
        return replace(source,target)
    monkeypatch.setattr(Path,"replace",transient)
    assert store.run(jid,lambda *_:None)=="succeeded"
    assert store.status(jid)["state"]=="succeeded"
    assert not list((tmp_path/jid).glob("*.tmp"))


def test_lock_open_retries_without_stealing_an_existing_lock(tmp_path,monkeypatch):
    from pathlib import Path
    from qwb import experiments
    original=Path.open;attempts=[]
    monkeypatch.setattr("qwb.experiments.time.sleep",lambda _:None)
    def transient(path,*args,**kwargs):
        if path.name==".resource.lock":
            attempts.append(1)
            if len(attempts)<7:raise PermissionError("temporary scanner handle")
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"open",transient)
    with experiments.output_lock(tmp_path/"resource"):
        with pytest.raises(ValueError,match="another run"):
            with experiments.output_lock(tmp_path/"resource"):pass
    with experiments.output_lock(tmp_path/"resource"):pass
