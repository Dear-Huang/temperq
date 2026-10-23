"""Append a fixed test universe from the locally configured Tushare-compatible API."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qwb import daily, daily_data, experiments, pipeline, strategy
from qwb.connectors import cn_a
from qwb.connectors.cache import Cache
from qwb.connectors.tushare import TushareClient, load_token

import numpy as np
import pandas as pd


COLUMNS = ["code", "date", "open", "high", "low", "close", "amount", "adj_factor",
           "up_limit", "down_limit", "is_st", "state"]


def normalize_response(frame, code, days, api, columns):
    required = {"ts_code", "trade_date", *columns}
    if not required <= set(frame):
        raise ValueError(f"{api}: response lacks required columns")
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="raise")
    if frame["ts_code"].ne(code).any() or not frame["date"].isin(days).all():
        raise ValueError(f"{api}: response violates requested security/date range")
    if frame["date"].duplicated().any():
        raise ValueError(f"{api}: duplicate security/date")
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    return frame.set_index("date")[columns].reindex(days)


def fetch(feed, codes_file, end, *, start="2021-01-01", config_root=ROOT):
    local = pipeline.find_local_config(Path(config_root))
    settings = local["tushare"]
    cli = TushareClient(load_token(settings["token_file"]), url=settings["url"],
                        min_interval=settings.get("min_interval", .25), timeout=30, retries=1)
    cache = Cache(Path(local["cache_dir"]) / "daily_api_test_v1")
    codes = sorted(strategy.stock_code(c) for c in pd.read_csv(codes_file, dtype=str)["code"])
    if len(set(codes)) != len(codes):
        raise ValueError("Duplicate codes")
    end = pd.Timestamp(end).normalize()
    now = pd.Timestamp.now(tz="Asia/Shanghai")
    if end.date() > now.date() or (end.date() == now.date() and now.hour < 16):
        raise ValueError("Only request completed daily sessions (after 16:00 local)")
    feed = Path(feed).resolve()
    if (feed / "current.json").exists():
        prior = daily_data.current(feed)
        history = pd.read_csv(prior / "source.csv", dtype={"code": str})
        if sorted(history["code"].unique()) != codes:
            raise ValueError("Feed security set differs from the requested code list")
        first = experiments.read(prior / "snapshot.json")["start"]
        fetch_start = pd.Timestamp(experiments.read(prior / "snapshot.json")["end"]) + pd.Timedelta(days=1)
    else:
        # Select by code while reading; never modify or relabel the full-market source.
        source = Path(local["data_dir"])
        frames = [pd.read_parquet(path, filters=[("code", "in", codes)])
                  for path in sorted((source / "bars").glob("*.parquet"))]
        if not frames:
            raise ValueError("No local baseline bars")
        history = pd.concat(frames, ignore_index=True)
        history["date"] = pd.to_datetime(history["date"])
        history = history.loc[history["date"].between(pd.Timestamp(start), end)].copy()
        if history.empty or set(history["code"]) != set(codes):
            raise ValueError("Baseline does not cover the full declared test universe")
        first = str(history["date"].min().date())
        fetch_start = history["date"].max() + pd.Timedelta(days=1)
        dates = pd.DatetimeIndex(sorted(history["date"].unique()))
        st = pd.read_parquet(source / "st_status.parquet")
        st_mask = daily.st_wide(st, dates, pd.Index(codes))
        history["is_st"] = [int(st_mask.at[d, c]) for d, c in zip(history["date"], history["code"])]
        history["date"] = history["date"].dt.strftime("%Y-%m-%d")
        history = history[COLUMNS]
    calendar_params = {"exchange": "SSE", "start_date": pd.Timestamp(first).strftime("%Y%m%d"),
                       "end_date": (end + pd.Timedelta(days=14)).strftime("%Y%m%d")}
    cal = cache.get("trade_cal", calendar_params, lambda: cli.call("trade_cal", **calendar_params))
    calendar = cal.rename(columns={"cal_date": "date"})[["date", "is_open"]].copy()
    calendar["date"] = pd.to_datetime(calendar["date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    days = pd.DatetimeIndex(pd.to_datetime(calendar.loc[calendar.is_open.astype(int).eq(1), "date"]))
    days = days[(days >= fetch_start) & (days <= end)].sort_values()
    fresh = []
    if len(days):
        for i, code in enumerate(codes):
            query = {"ts_code": code, "start_date": days[0].strftime("%Y%m%d"),
                     "end_date": days[-1].strftime("%Y%m%d")}
            pieces = {}
            for api, columns in [("daily", ["open", "high", "low", "close", "amount"]),
                                 ("adj_factor", ["adj_factor"]), ("stk_limit", ["up_limit", "down_limit"])]:
                frame = cache.get(api, query, lambda api=api: cli.paged(api, 6000, ["ts_code", "trade_date"], **query))
                pieces[api] = normalize_response(frame, code, days, api, columns)
            data = pd.concat(pieces.values(), axis=1)
            data["amount"] *= cn_a.AMOUNT_TO_YUAN
            data["code"] = code
            data["state"] = np.where(data["close"].notna(), "normal", "missing")
            data = data.rename_axis("date").reset_index()
            fresh.append(data)
            print(f"prices {i+1}/{len(codes)} {code}", flush=True)
        incoming = pd.concat(fresh, ignore_index=True)
        st_pairs = set()
        for i, day in enumerate(days):
            query = {"trade_date": day.strftime("%Y%m%d")}
            status = cache.get("stock_st", query, lambda: cli.paged("stock_st", 3000, ["ts_code"], **query))
            if "ts_code" not in status or "trade_date" not in status or status["trade_date"].astype(str).ne(query["trade_date"]).any():
                raise ValueError("stock_st: unverified daily status response")
            st_pairs.update((code, day) for code in status["ts_code"] if code in codes)
            absent = incoming.loc[incoming.date.eq(day) & incoming.state.eq("missing"), "code"]
            if len(absent):
                halted = cache.get("suspend_d", query, lambda: cli.call("suspend_d", **query))
                suspended = set(halted.loc[halted["suspend_type"].eq("S"), "ts_code"]) if len(halted) else set()
                if not set(absent) <= suspended:
                    raise ValueError(f"{day.date()}: unexplained missing daily bars; no snapshot published")
                incoming.loc[incoming.date.eq(day) & incoming.code.isin(absent), "state"] = "suspended"
            if i % 20 == 0 or i == len(days)-1:
                print(f"status {i+1}/{len(days)} {day.date()}", flush=True)
        incoming["is_st"] = [int((c, d) in st_pairs) for c, d in zip(incoming.code, incoming.date)]
        incoming["date"] = incoming["date"].dt.strftime("%Y-%m-%d")
        incoming = incoming[COLUMNS]
        combined = pd.concat([history, incoming], ignore_index=True) if not (feed / "current.json").exists() else incoming
    else:
        combined = history
    result = daily_data.update(feed, combined.to_csv(index=False).encode(), calendar.to_csv(index=False).encode(),
                               source="configured_api_fixed_universe_test")
    print({**result, "end": experiments.read(Path(result["snapshot"]) / "snapshot.json")["end"],
           "codes": len(codes), "new_sessions": len(days)}, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--feed", required=True)
    parser.add_argument("--codes", default=str(ROOT / "examples/free_daily_codes.csv"))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    try:
        fetch(args.feed, args.codes, args.end, start=args.start)
    except Exception as exc:
        # Vendor exception messages may contain private endpoint details.
        print(f"Fetch stopped ({type(exc).__name__}); existing snapshot preserved", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
