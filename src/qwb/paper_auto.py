"""Opt-in daily ledger checks while the local workbench is running."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

import pandas as pd

from . import daily_operation, experiments, paper


def settings(workdir, name):
    path = paper.location(workdir, name) / "automation.json"
    return experiments.read(path) if path.exists() else {"enabled": False}


def configure(workdir, name, enabled):
    paper.inspect(workdir, name)
    value = dict(settings(workdir, name), enabled=bool(enabled))
    experiments.write(paper.location(workdir, name) / "automation.json", value)
    return value


def check(workdir, name, *, now=None):
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Shanghai")
    if stamp.tzinfo is None:
        raise ValueError("Automation requires a timezone")
    stamp = stamp.tz_convert("Asia/Shanghai")
    if (stamp.hour, stamp.minute) < (17, 30):
        return None
    root = paper.location(workdir, name)
    with experiments.output_lock(root / "automation.json"):
        value = settings(workdir, name)
        day = str(stamp.date())
        if not value.get("enabled") or value.get("attempted_day") == day:
            return None
        # Persist before execution so restarting the UI never runs a second daily attempt.
        value.update(attempted_day=day, attempted_at=stamp.isoformat(), state="RUNNING",
                     message="正在检查收盘数据并记账")
        experiments.write(root / "automation.json", value)
        try:
            if (root / "operation.json").exists():
                config = paper.inspect(workdir, name)["config"]
                result = daily_operation.run(workdir, name, config["data"]["dir"], now=stamp)
            else:
                result = paper.tick(workdir, name, now=stamp)
            value.update(state=result["state"], message=result.get("message", "本日检查完成"))
        except Exception as exc:
            value.update(state="BLOCKED", message=f"自动记账失败（{type(exc).__name__}），请检查数据与账户状态")
        experiments.write(root / "automation.json", value)
        return value


def lifespan(workdir):
    @asynccontextmanager
    async def manage(app):
        stopped = threading.Event()

        def work():
            while not stopped.is_set():
                for path in (workdir / "runs" / "paper").glob("*/automation.json"):
                    if stopped.is_set():
                        break
                    try:
                        check(workdir, path.parent.name)
                    except (OSError, ValueError, KeyError):
                        # A different workbench may hold the per-account OS lock.
                        continue
                from . import strategy_workspace
                strategy_workspace.auto_check(workdir)
                stopped.wait(30)

        thread = threading.Thread(target=work, daemon=True, name="temperq-paper")
        thread.start()
        try:
            yield
        finally:
            stopped.set()
    return manage
