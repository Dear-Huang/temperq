"""Download-gated paper operation and an explicit audit of missed recording dates."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

import pandas as pd

from . import daily_data, experiments, full_market, paper


def activate_unstarted(workdir, name, feed):
    root = paper.location(workdir, name)
    snapshot = daily_data.current(feed)
    config = experiments.read(root / "strategy.json")
    with paper.connection(root) as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]:
            raise ValueError("Cannot change the data source of an account that already has records")
        stored = con.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()[0]
        if stored != experiments.object_digest(config) or config["implementation_hash"] != paper.implementation_hash():
            raise ValueError("Original frozen configuration or implementation changed")
        old = dict(config)
        config["data"] = dict(config["data"], dir=str(Path(feed).resolve()))
        history = root / "config_history"
        history.mkdir(exist_ok=True)
        experiments.write(history / (experiments.object_digest(old).split(":")[1] + ".json"), old)
        snapshot_id = experiments.read(snapshot / "snapshot.json")["data_snapshot_id"]
        try:
            experiments.write(root / "strategy.json", config)
            con.execute("UPDATE metadata SET value=? WHERE key='config_hash'", (experiments.object_digest(config),))
            con.execute("INSERT INTO events(created_at,payload) VALUES (?,?)", (experiments.now(), json.dumps({
                "type": "bind_unstarted_account", "old_config_hash": stored,
                "new_config_hash": experiments.object_digest(config), "snapshot_id": snapshot_id})))
            con.commit()
        except Exception:
            con.rollback()
            experiments.write(root / "strategy.json", old)
            raise
    return config


def record_attempt(workdir, name, value):
    root = paper.location(workdir, name)
    history = root / "operation_history"
    history.mkdir(exist_ok=True)
    experiments.write(history / (uuid.uuid4().hex + ".json"), value)
    experiments.write(root / "operation.json", value)
    if value["state"] == "BLOCKED":
        with paper.connection(root) as con:
            con.execute("UPDATE metadata SET value=? WHERE key='status'", (json.dumps(value, ensure_ascii=False),))
            con.execute("INSERT INTO events(created_at,payload) VALUES (?,?)",
                        (experiments.now(), json.dumps(value, ensure_ascii=False)))


def run(workdir, name, feed, *, now=None):
    workdir, feed = Path(workdir), Path(feed).resolve()
    root = paper.location(workdir, name)
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Shanghai")
    if stamp.tzinfo is None:
        raise ValueError("Operation time requires a timezone")
    stamp = stamp.tz_convert("Asia/Shanghai")
    requested = stamp.normalize().tz_localize(None)
    if stamp.hour < 16:
        requested -= pd.Timedelta(days=1)
    stage = "data_update"
    base = {"attempted_at": stamp.isoformat(), "requested_date": str(requested.date()),
            "account": name, "ledger_triggered": False}
    # The operation lock prevents two schedulers downloading and ticking this account concurrently.
    with experiments.output_lock(root / "operation.json"):
        try:
            config = experiments.read(root / "strategy.json")
            if Path(config["data"]["dir"]).resolve() != feed:
                raise ValueError("Account is not bound to this live feed")
            refreshed = full_market.update(workdir, feed, str(requested.date()))
            ledger_stamp = pd.Timestamp.now(tz="Asia/Shanghai") if now is None else stamp
            cutoff = ledger_stamp.normalize().tz_localize(None) - pd.Timedelta(days=int(ledger_stamp.hour < 16))
            snapshot = daily_data.current(feed)
            metadata = experiments.read(snapshot / "snapshot.json")
            calendar = pd.read_csv(snapshot / "calendar.csv", dtype=str)
            available = calendar.loc[calendar.is_open.eq("1") & calendar.date.le(str(cutoff.date())), "date"]
            base.update(data_end=metadata["end"], input_snapshot_id=metadata["data_snapshot_id"])
            if available.empty or metadata["end"] != available.max() or refreshed["end"] != metadata["end"]:
                raise ValueError("Data does not reach the required completed session")
            base.update(expected_session=available.max(), data_end=metadata["end"],
                        input_snapshot_id=metadata["data_snapshot_id"])
            stage = "ledger"
            result = paper.tick(workdir, name, now=ledger_stamp)
            value = dict(base, **result, ledger_triggered=True, stage="complete")
        except Exception as exc:
            value = dict(base, state="BLOCKED", stage=stage, error_type=type(exc).__name__,
                         reason_code=stage + "_failed", message=("数据更新" if stage == "data_update" else "账户记账") +
                         f"失败（{type(exc).__name__}）；本次未新增账本，请检查数据或运行日志")
            if isinstance(exc, ValueError):
                value["detail"] = str(exc)
        record_attempt(workdir, name, value)
    return value


def coverage(workdir, name, *, now=None):
    root = paper.location(workdir, name)
    account = paper.inspect(workdir, name)
    operation = experiments.read(root / "operation.json") if (root / "operation.json").exists() else None
    if account["config"]["scope"] != "forward_paper":
        return {"operation": operation, "missed": []}
    source = Path(account["config"]["data"].get("dir", ""))
    if (source / "current.json").exists():
        source = daily_data.current(source)
    if not (source / "calendar.csv").exists():
        return {"operation": operation, "missed": [], "calendar_available": False}
    calendar = pd.read_csv(source / "calendar.csv", dtype=str)
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Shanghai")
    stamp = stamp.tz_convert("Asia/Shanghai")
    cutoff = stamp.normalize().tz_localize(None) - pd.Timedelta(days=int(stamp.hour < 18))
    days = calendar.loc[calendar.is_open.eq("1") & calendar.date.ge(account["config"]["start_date"]) &
                        calendar.date.le(str(cutoff.date())), "date"]
    attempts = {}
    for path in (root / "operation_history").glob("*.json"):
        attempt = experiments.read(path)
        attempted_day = str(pd.Timestamp(attempt["attempted_at"]).tz_convert("Asia/Shanghai").date())
        attempts.setdefault(attempted_day, []).append(attempt)
    recorded = {r["day"]: r for r in account["sessions"]}
    missed = []
    for day in days:
        row = recorded.get(day)
        if row and row["scope"] == "forward_paper":
            continue
        failures = [a for a in attempts.get(day, []) if a["state"] == "BLOCKED"]
        if failures:
            reason = max(failures, key=lambda a: pd.Timestamp(a["attempted_at"]))["message"]
        elif not attempts.get(day):
            reason = "无当日任务记录，可能未开机或未登录；具体原因未知"
        else:
            reason = "当日未及时取得完整数据或完成记账"
        missed.append({"day": day, "record_type": "晚到补记" if row else "尚未补记", "reason": reason})
    return {"operation": operation, "missed": missed,
            "calendar_available": calendar.date.max() >= str(cutoff.date())}
