"""Versioned full-market daily updates, preserving the original research snapshot."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import shutil
import tempfile

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import daily_data, experiments, pipeline
from .connectors import cn_a
from .connectors.cache import Cache
from .connectors.tushare import TushareClient, load_token
from .formats import validate_frame


def daily_tables(cli, cache, day, *, min_stocks=1000):
    params = {"trade_date": pd.Timestamp(day).strftime("%Y%m%d")}
    frames = {}
    for api in ("daily", "adj_factor", "stk_limit", "stock_st", "suspend_d"):
        query = lambda api=api: (cli.call(api, **params) if api == "suspend_d" else
                                 cli.paged(api, 6000 if api != "stock_st" else 3000, ["ts_code"], **params))
        frame = cache.get(api, params, query)
        if frame.empty and api == "suspend_d":
            frames[api] = frame
            continue
        if not {"ts_code", "trade_date"} <= set(frame):
            raise ValueError(f"{api}: missing security/date columns")
        if (frame.trade_date.astype(str).ne(params["trade_date"]).any() or
                not frame.ts_code.astype(str).str.fullmatch(r"\d{6}\.(SH|SZ|BJ)").all()):
            raise ValueError(f"{api}: security/date response mismatch")
        if api in {"daily", "adj_factor", "stk_limit"} and frame.ts_code.duplicated().any():
            raise ValueError(f"{api}: duplicate securities")
        frames[api] = frame
    bars, adj, limits = (frames[k] for k in ("daily", "adj_factor", "stk_limit"))
    if len(bars) < min_stocks or len(bars) < .90 * len(adj):
        raise ValueError("Incomplete full-market daily response")
    if not set(bars.ts_code) <= set(adj.ts_code):
        raise ValueError("Missing adjustment factors for traded securities")
    halted = frames["suspend_d"]
    suspended = set(halted.loc[halted.suspend_type.eq("S"), "ts_code"]) if len(halted) else set()
    absent = sorted((set(adj.ts_code) | suspended) - set(bars.ts_code))
    if absent:
        bars = pd.concat([bars, pd.DataFrame({"ts_code": absent})], ignore_index=True)
    bars = bars.merge(adj[["ts_code", "adj_factor"]], on="ts_code", how="left", validate="one_to_one")
    bars = bars.merge(limits[["ts_code", "up_limit", "down_limit"]], on="ts_code", how="left", validate="one_to_one")
    bars["state"] = "normal"
    missing = bars["close"].isna() | bars["close"].le(0)
    bars.loc[missing, "state"] = "missing"
    bars.loc[bars.ts_code.isin(suspended) & (missing | bars.vol.fillna(0).eq(0)), "state"] = "suspended"
    bars.loc[bars.state.ne("normal"), ["open", "high", "low", "close"]] = np.nan
    bars["date"] = pd.Timestamp(day).strftime("%Y-%m-%d")
    bars["code"] = bars["ts_code"]
    bars["amount"] *= cn_a.AMOUNT_TO_YUAN
    bars["vol"] *= cn_a.VOL_TO_SHARES
    bars["source"] = "tushare.daily"
    bars["record_id"] = bars.code + "|" + bars.date
    bars["revision"] = 0
    bars["available_time"] = (pd.Timestamp(day) + pd.Timedelta(hours=15)).tz_localize("Asia/Shanghai")
    columns = ["source", "record_id", "revision", "available_time", "code", "date", "open", "high", "low",
               "close", "pre_close", "vol", "amount", "adj_factor", "up_limit", "down_limit", "state"]
    bars = bars[columns].sort_values("code").reset_index(drop=True)
    report = validate_frame(bars, "bar")
    if not report.ok:
        raise ValueError(f"{day}: canonical daily validation failed: {report}")
    status = frames["stock_st"][["ts_code", "name"]].rename(columns={"ts_code": "code"})
    status = status.drop_duplicates("code").assign(date=pd.Timestamp(day))
    return bars, status


def update(workdir, feed, end=None, *, workers=2, log=print):
    if not 1 <= workers <= 2:
        raise ValueError("Full-market fetch supports at most two bounded workers")
    workdir, feed = Path(workdir), Path(feed).resolve()
    local = pipeline.find_local_config(workdir)
    settings = local["tushare"]
    token = load_token(settings["token_file"])
    cache_root = Path(local["cache_dir"]) / "daily_api_test_v1"
    def client():
        return TushareClient(token, url=settings["url"], min_interval=max(.5, settings.get("min_interval", .5)),
                             timeout=30, retries=2)
    now = pd.Timestamp.now(tz="Asia/Shanghai")
    requested = pd.Timestamp(end).normalize() if end else now.tz_localize(None).normalize()
    if requested.date() > now.date() or (requested.date() == now.date() and now.hour < 16):
        if end:
            raise ValueError("Only fetch completed daily data after 16:00 Asia/Shanghai")
        requested -= pd.Timedelta(days=1)
    feed.mkdir(parents=True, exist_ok=True)
    with experiments.output_lock(feed / "current.json"):
        existing = (feed / "current.json").is_file()
        base = daily_data.current(feed) if existing else Path(local["data_dir"])
        previous = experiments.read(base / "snapshot.json")
        cal_params = {"exchange": "SSE", "start_date": pd.Timestamp(previous["start"]).strftime("%Y%m%d"),
                      "end_date": (requested + pd.Timedelta(days=14)).strftime("%Y%m%d")}
        cache = Cache(cache_root, refresh=True)
        frame = cache.get("trade_cal", cal_params, lambda: client().call("trade_cal", **cal_params))
        calendar = frame.rename(columns={"cal_date": "date"})[["date", "is_open"]].copy()
        calendar["date"] = pd.to_datetime(calendar["date"], format="%Y%m%d", errors="raise").dt.strftime("%Y-%m-%d")
        calendar["is_open"] = calendar["is_open"].astype(str)
        if calendar.date.duplicated().any() or not calendar.is_open.isin(["0", "1"]).all():
            raise ValueError("Invalid authoritative calendar")
        calendar = calendar.sort_values("date").reset_index(drop=True)
        expected_dates = pd.date_range(previous["start"], requested + pd.Timedelta(days=14)).strftime("%Y-%m-%d")
        if not set(expected_dates) <= set(calendar.date):
            raise ValueError("Authoritative calendar response is incomplete")
        days = pd.DatetimeIndex(pd.to_datetime(calendar.loc[calendar.is_open.eq("1"), "date"]))
        completed = days[days <= requested]
        if not len(completed) or not len(days[days > requested]):
            raise ValueError("Calendar must cover completed and next open sessions")
        target = completed[-1]
        if target < pd.Timestamp(previous["end"]):
            raise ValueError("Cannot move a full-market feed backward")
        new_days = completed[completed > pd.Timestamp(previous["end"])]
        if existing and not len(new_days):
            return {"snapshot": str(base), "data_snapshot_id": previous["data_snapshot_id"],
                    "end": previous["end"], "new_sessions": 0}
        def fetch_one(day):
            local_cache = Cache(cache_root)
            try:
                bars, status = daily_tables(client(), local_cache, day)
            except ValueError:
                # Incomplete cached vendor responses must not block every future retry.
                local_cache = Cache(cache_root, refresh=True)
                bars, status = daily_tables(client(), local_cache, day)
            return day, bars, status, local_cache.snapshot_id()
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, result in enumerate(pool.map(fetch_one, new_days)):
                results.append(result)
                if i % 10 == 0 or i == len(new_days)-1:
                    log(f"full market {i+1}/{len(new_days)} {result[0]:%Y-%m-%d}", flush=True)
        initial_identity = experiments.object_digest({p.relative_to(base).as_posix(): experiments.digest(p)
            for p in [*sorted((base / "bars").glob("*.parquet")), base / "st_status.parquet"]}) if not existing else previous["data_snapshot_id"]
        identity = experiments.object_digest({"base": initial_identity,
            "new_days": [r[3] for r in results], "calendar": calendar.to_dict("records")})
        destination = feed / "snapshots" / identity.removeprefix("sha256:")
        destination.parent.mkdir(exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".updating-", dir=destination.parent))
        try:
            (temporary / "bars").mkdir()
            fresh = pd.concat([r[1] for r in results], ignore_index=True) if results else pd.DataFrame()
            changed_years = set(fresh.date.str[:4]) if len(fresh) else set()
            for source in sorted((base / "bars").glob("*.parquet")):
                if source.stem not in changed_years:
                    target_file = temporary / "bars" / source.name
                    if existing:
                        os.link(source, target_file)
                    else:
                        shutil.copy2(source, target_file)
            for year in sorted(changed_years):
                old_file = base / "bars" / f"{year}.parquet"
                rows = fresh.loc[fresh.date.str.startswith(year)]
                if old_file.exists():
                    rows = pd.concat([pd.read_parquet(old_file), rows], ignore_index=True)
                rows["date"] = pd.to_datetime(rows["date"], format="mixed").dt.strftime("%Y-%m-%d")
                if rows.duplicated(["code", "date"]).any():
                    raise ValueError("Duplicate stock/date in append")
                rows.sort_values(["code", "date"]).to_parquet(temporary / "bars" / f"{year}.parquet", index=False)
            st = pd.read_parquet(base / "st_status.parquet")
            if results:
                st.loc[st.end.isna() | st.end.eq(""), "end"] = new_days[0].strftime("%Y-%m-%d")
                observations = pd.concat([r[2] for r in results], ignore_index=True)
                new_st = cn_a.st_status(observations, new_days) if len(observations) else st.iloc[:0]
                st = pd.concat([st, new_st], ignore_index=True)
            report = validate_frame(st, "status")
            if not report.ok:
                raise ValueError(f"ST intervals failed validation: {report}")
            st.to_parquet(temporary / "st_status.parquet", index=False)
            if (base / "index_members.parquet").is_file():
                shutil.copy2(base / "index_members.parquet", temporary / "index_members.parquet")
            calendar.to_csv(temporary / "calendar.csv", index=False)
            snapshot = {"data_snapshot_id": identity, "source": "configured_api_full_market",
                        "start": previous["start"], "end": target.strftime("%Y-%m-%d"),
                        "trading_days": int(previous["trading_days"]) + len(new_days),
                        "has_st": True, "st_intervals": len(st), "parent_snapshot": previous["data_snapshot_id"],
                        "fetched_at": experiments.now(), "added_sessions": len(new_days),
                        "rows": sum(pq.read_metadata(p).num_rows for p in (temporary / "bars").glob("*.parquet")),
                        "scope": "full_market_historical_append_not_forward_observations"}
            experiments.write(temporary / "snapshot.json", snapshot)
            if not destination.exists():
                temporary.rename(destination)
            experiments.write(feed / "current.json", {"snapshot": destination.relative_to(feed).as_posix(),
                                                       "data_snapshot_id": identity})
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"snapshot": str(destination), "data_snapshot_id": identity,
                "end": snapshot["end"], "new_sessions": len(new_days)}
