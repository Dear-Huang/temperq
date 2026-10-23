"""Daily forward simulation: frozen model, CSV initial account, transactional ledger."""

from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

import numpy as np
import pandas as pd

from . import daily, daily_data, experiments, pipeline, picks, strategy
from .portfolio.contracts import AccountState
from .portfolio.reference import Fees, execute_session

NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")


def location(workdir, name):
    if not NAME.fullmatch(name):
        raise ValueError("账户名称只能用 1–40 位字母、数字、下划线、连字符")
    return Path(workdir) / "runs" / "paper" / name


@contextmanager
def connection(root):
    con = sqlite3.connect(Path(root) / "ledger.sqlite3", timeout=30)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        with con:
            yield con
    finally:
        con.close()


def implementation_hash():
    return experiments.object_digest({name: experiments.digest(path) for name, path in
        (("strategy", Path(strategy.__file__)), ("paper", Path(__file__)),
         ("executor", Path(__file__).parent / "portfolio" / "reference.py"),
         ("features", Path(daily.__file__)))})


def initialize(workdir, name, run_dir, cash, holdings, *, data_dir=None, replay_start=None):
    root = location(workdir, name)
    if root.exists():
        raise ValueError("模拟账户已存在，请换名；不能覆盖已有账本")
    if not np.isfinite(float(cash)) or float(cash) < 0 or (not holdings and float(cash) <= 0):
        raise ValueError("账户需要非负现金与正权益")
    # Reuse the strict CSV parser even for CLI/internal callers.
    holdings = strategy.read_holdings(pd.DataFrame(holdings, columns=sorted(strategy.HOLDING_COLUMNS))
                                      .to_csv(index=False).encode())
    meta = picks.load(Path(run_dir))
    recipe, recipe_hash = strategy.locked_recipe(meta)
    data = dict(meta["data"])
    if data.get("source") == "synthetic":
        if replay_start is None:
            raise ValueError("合成模型只能运行明确标识的历史演示，不能启用真实前向模拟")
    else:
        source = data_dir or data.get("dir") or pipeline.find_local_config(Path(workdir)).get("data_dir")
        if not source:
            raise ValueError("模型未绑定行情目录，请指定 data_dir")
        path = Path(source)
        if path.parent.name == "snapshots" and (path.parent.parent / "current.json").is_file():
            path = path.parent.parent
        data["dir"] = str(path.resolve() if path.is_absolute() else (Path(workdir) / path).resolve())
    config = {"schema": "paper_daily_v1", "name": name, "activated_at": experiments.now(),
              "scope": "historical_replay" if replay_start else "forward_paper",
              "start_date": str(pd.Timestamp(replay_start).date()) if replay_start else
                            str(pd.Timestamp.now(tz="Asia/Shanghai").date()),
              "model_hash": strategy.model_identity(meta["dir"]), "recipe": recipe,
              "protocol_hash": recipe_hash, "cash": float(cash), "holdings": holdings,
              "data": data, "implementation_hash": implementation_hash()}
    root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".initializing-", dir=root.parent))
    try:
        shutil.copytree(meta["dir"], temporary / "final_model")
        experiments.write(temporary / "strategy.json", config)
        with connection(temporary) as con:
            con.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            con.execute("CREATE TABLE sessions (day TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
            con.execute("INSERT INTO metadata VALUES ('config_hash', ?)", (experiments.object_digest(config),))
            con.execute("INSERT INTO metadata VALUES ('status', ?)",
                        (json.dumps({"state": "WAITING_DATA", "message": "等待启用后的首个收盘数据与交易日历"}, ensure_ascii=False),))
        temporary.rename(root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return config


def inspect(workdir, name):
    root = location(workdir, name)
    if not (root / "ledger.sqlite3").is_file():
        raise ValueError("模拟账户不存在")
    config = experiments.read(root / "strategy.json")
    with connection(root) as con:
        sessions = [json.loads(row[0]) for row in con.execute("SELECT payload FROM sessions ORDER BY day")]
        status = json.loads(con.execute("SELECT value FROM metadata WHERE key='status'").fetchone()[0])
    return {"config": config, "status": status, "sessions": sessions}


def load_inputs(config, root, workdir):
    meta = experiments.read(root / "final_model" / "meta.json")
    meta["dir"] = root / "final_model"
    data = dict(config["data"])
    if data.get("source") != "synthetic" and (Path(data["dir"]) / "current.json").is_file():
        data["dir"] = str(daily_data.current(data["dir"]))
    rc = {"data": data, "cfg": meta["cfg"]}
    panel, snapshot_id, st, members = pipeline.load_data(rc, Path(workdir))
    calendar_file = Path(data.get("dir", "")) / "calendar.csv"
    calendar = pd.DatetimeIndex([])
    if calendar_file.is_file():
        frame = pd.read_csv(calendar_file, dtype=str)
        if set(frame.columns) != {"date", "is_open"} or not frame["is_open"].isin(["0", "1"]).all():
            raise ValueError("calendar.csv 需为 date,is_open 两列，开市为 1，休市为 0")
        parsed = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
        if parsed.duplicated().any():
            raise ValueError("交易日历日期重复")
        calendar = pd.DatetimeIndex(parsed[frame["is_open"] == "1"])
    if config["scope"] == "historical_replay":
        calendar = calendar.union(panel["close"].index)
    return meta, panel, snapshot_id, st, members, calendar.sort_values()


def tick(workdir, name, *, now=None):
    root = location(workdir, name)
    config = experiments.read(root / "strategy.json")
    if (strategy.model_identity(root / "final_model") != config["model_hash"] or
            implementation_hash() != config["implementation_hash"]):
        raise ValueError("冻结模型或执行实现发生变化，停止记账，请建立新账户")
    meta, panel, snapshot_id, st, members, calendar = load_inputs(config, root, workdir)
    _, recipe_hash = strategy.locked_recipe(meta)
    if recipe_hash != config["protocol_hash"]:
        raise ValueError("冻结协议发生变化，停止记账")
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Shanghai")
    if stamp.tzinfo is None:
        raise ValueError("运行时间需要时区")
    stamp = stamp.tz_convert("Asia/Shanghai")
    features = None
    dates = panel["close"].index
    with connection(root) as con:
        con.execute("BEGIN IMMEDIATE")
        stored_hash = con.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()[0]
        if stored_hash != experiments.object_digest(config):
            raise ValueError("策略配置被修改，停止记账")
        records = [json.loads(r[0]) for r in con.execute("SELECT payload FROM sessions ORDER BY day")]
        latest = records[-1] if records else None
        state = AccountState.from_dict(latest["state"]) if latest else None
        pending = latest["next_weights"] if latest else None
        inserted = 0
        status = {"state": "WAITING_DATA", "message": "尚无新的完整收盘数据", "checked_at": str(stamp)}
        for day in dates:
            key = str(day.date())
            at = lambda hour, minute=0: (day + pd.Timedelta(hours=hour, minutes=minute)).tz_localize("Asia/Shanghai")
            if key < config["start_date"] or (latest and key <= latest["day"]) or at(15, 30) > stamp:
                continue
            # Calendar gaps stop rather than silently skipping an unobserved session.
            if state:
                expected = calendar[calendar > pd.Timestamp(latest["day"])]
                if not len(expected) or expected[0] != day:
                    status = {"state": "WAITING_DATA", "message": "缺少连续交易日行情或权威日历"}
                    break
            future = calendar[calendar > day]
            if day not in calendar or not len(future):
                status = {"state": "WAITING_CALENDAR", "message": "需要 calendar.csv 覆盖本日及下个开市日"}
                break
            op, cl = at(9, 30), at(15)
            nxt = (future[0] + pd.Timedelta(hours=9, minutes=30)).tz_localize("Asia/Shanghai")
            active = set(pending or {}) | {p.asset_id for p in state.positions} if state else set()
            if active:
                previous_index = dates.get_loc(day) - 1
                if previous_index < 0:
                    raise ValueError("需要上一交易日数据检查公司行动")
                for code in active:
                    factors = panel["adj_factor"][code].iloc[[previous_index, dates.get_loc(day)]].to_numpy()
                    if not np.isfinite(factors).all() or not np.isclose(factors[0], factors[1], rtol=1e-10, atol=0):
                        raise ValueError(f"{code} 复权因子变化或缺失：首版不支持公司行动，已停止记账")
            audit = None
            previous_equity = state.equity if state else None
            if state is None:
                marks = panel["close"].loc[day].to_dict()
                state = strategy.account(config["cash"], config["holdings"], marks,
                                         observed_at=at(15, 30), snapshot_id=snapshot_id, locked_until=nxt)
            else:
                if pending is not None:
                    target, assets, universe_id = strategy.target_for(state, pending, execution_at=op,
                                                                    recipe_hash=recipe_hash)
                else:
                    target = None
                    assets = tuple(sorted(p.asset_id for p in state.positions))
                    universe_id = experiments.object_digest(assets)
                if assets:
                    bars = []
                    for code in assets:
                        def price(field):
                            value = panel[field].at[day, code] if field in panel else np.nan
                            return float(value) if np.isfinite(value) and value > 0 else None
                        bars.append(strategy.session_bar(code, open_price=price("open"), close_price=price("close"),
                                    upper=price("up_limit"), lower=price("down_limit"),
                                    suspended=price("open") is None or price("close") is None))
                    state, audit = execute_session(state, target, tuple(bars), open_at=op, close_at=cl,
                                                   next_open_at=nxt, decision_assets=assets,
                                                   universe_id=universe_id, protocol_id=recipe_hash,
                                                   fees=Fees(**config["recipe"]["base_protocol"]["fees"]))
                    if audit["notices"]:
                        raise ValueError("持仓估值或成交数据不完整，停止记账")
                state = replace(state, observed_at=str(at(15, 30)), data_snapshot_id=snapshot_id)
            index = len(records)
            weights = None
            if index % 5 == 0:
                if features is None:
                    features = daily.selected_features(panel, meta["cfg"])
                weights = strategy.weights_on(meta, panel, st, members, day, feature_frames=features)
            recorded_at = experiments.now()
            timely = pending is None or latest["decision_recorded_before_execution"]
            scope = "historical_replay" if config["scope"] == "historical_replay" else (
                "forward_paper" if stamp.normalize().tz_localize(None) == day and timely else "late_reconstruction")
            record = {"day": key, "recorded_at": recorded_at, "scope": scope,
                      "state": state.to_dict(), "equity": state.equity,
                      "daily_return": None if previous_equity is None else state.equity / previous_equity - 1,
                      "audit": audit, "next_weights": weights, "next_execution_day": str(future[0].date()) if weights else None,
                      "input_snapshot_id": snapshot_id, "protocol_hash": recipe_hash,
                      "model_hash": config["model_hash"], "decision_recorded_before_execution": bool(stamp < nxt)}
            con.execute("INSERT INTO sessions VALUES (?, ?)", (key, json.dumps(record, ensure_ascii=False)))
            records.append(record)
            latest, pending = record, weights
            inserted += 1
            status = {"state": "RUNNING", "message": f"已记账至 {key}", "checked_at": str(stamp)}
        con.execute("UPDATE metadata SET value=? WHERE key='status'", (json.dumps(status, ensure_ascii=False),))
        con.execute("INSERT INTO events(created_at,payload) VALUES (?,?)",
                    (experiments.now(), json.dumps({**status, "new_sessions": inserted}, ensure_ascii=False)))
    return {**status, "new_sessions": inserted, "total_sessions": len(records)}


def tick_all(workdir):
    result = {}
    for root in sorted((Path(workdir) / "runs" / "paper").glob("*/strategy.json")):
        name = root.parent.name
        try:
            result[name] = tick(workdir, name)
        except (ValueError, OSError, KeyError) as exc:
            result[name] = {"state": "BLOCKED", "message": str(exc)}
            with connection(root.parent) as con:
                con.execute("UPDATE metadata SET value=? WHERE key='status'",
                            (json.dumps(result[name], ensure_ascii=False),))
                con.execute("INSERT INTO events(created_at,payload) VALUES (?,?)",
                            (experiments.now(), json.dumps(result[name], ensure_ascii=False)))
    return result
