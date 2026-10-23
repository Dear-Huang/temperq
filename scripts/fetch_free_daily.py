"""Token-free BaoStock daily data -> a versioned local feed (optional SDK)."""

from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import qwb  # noqa: E402,F401
import pandas as pd  # noqa: E402
from qwb import daily_data, experiments, rules, strategy  # noqa: E402


def table(response):
    if response.error_code != "0":
        raise ValueError(f"BaoStock {response.error_code}: {response.error_msg}")
    frame = response.get_data()
    if response.error_code != "0":
        raise ValueError(f"BaoStock {response.error_code}: {response.error_msg}")
    return frame


def fetch(feed, codes_file, start, end):
    try:
        import baostock as bs
    except ImportError:
        raise ValueError("先安装免费数据扩展：uv pip install --python .venv/Scripts/python.exe baostock==0.8.9") from None
    socket.setdefaulttimeout(15)
    feed = Path(feed)
    codes = [strategy.stock_code(code) for code in pd.read_csv(codes_file, dtype=str)["code"]]
    if len(set(codes)) != len(codes):
        raise ValueError("股票名单重复")
    if (feed / "current.json").is_file():
        latest = daily_data.current(feed)
        start = str((pd.Timestamp(experiments.read(latest / "snapshot.json")["end"]) + pd.Timedelta(days=1)).date())
    login = bs.login()
    if login.error_code != "0":
        raise ValueError(f"BaoStock 连接失败：{login.error_msg}；未生成或覆盖快照")
    try:
        end_day = pd.Timestamp(end)
        calendar_start = start
        if (feed / "current.json").is_file():
            calendar_start = experiments.read(daily_data.current(feed) / "snapshot.json")["start"]
        calendar = table(bs.query_trade_dates(calendar_start, str((end_day + pd.Timedelta(days=14)).date())))
        if calendar.empty:
            raise ValueError("未取得交易日历")
        calendar = calendar.rename(columns={"calendar_date": "date", "is_trading_day": "is_open"})[["date", "is_open"]]
        frames = []
        fields = "date,code,open,high,low,close,preclose,amount,tradestatus,isST"
        for i, code in enumerate(codes):
            symbol = code[-2:].lower() + "." + code[:6]
            raw = table(bs.query_history_k_data_plus(symbol, fields, start_date=start, end_date=end, frequency="d", adjustflag="3"))
            if raw.empty:
                raise ValueError(f"{code} 无日线数据，未发布不完整更新")
            raw["date"] = pd.to_datetime(raw["date"])
            raw = raw.sort_values("date")
            factors = table(bs.query_adjust_factor(symbol, start_date="1990-01-01", end_date=end))
            if factors.empty:
                raw["adj_factor"] = 1.0
            else:
                factors = factors.rename(columns={"dividOperateDate": "date", "backAdjustFactor": "adj_factor"})
                factors["date"] = pd.to_datetime(factors["date"])
                factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="raise")
                raw = pd.merge_asof(raw, factors[["date", "adj_factor"]].sort_values("date"), on="date")
                raw["adj_factor"] = raw["adj_factor"].fillna(1.0)
            raw["code"] = code
            raw["is_st"] = pd.to_numeric(raw["isST"], errors="raise")
            raw["state"] = raw["tradestatus"].map({"1": "normal", "0": "suspended"})
            preclose = pd.to_numeric(raw["preclose"], errors="coerce")
            raw["up_limit"], raw["down_limit"] = rules.limit_prices_vec(raw["code"], raw["is_st"], raw["date"], preclose)
            raw["date"] = raw["date"].dt.strftime("%Y-%m-%d")
            frames.append(raw[["code", "date", "open", "high", "low", "close", "amount", "adj_factor",
                               "is_st", "state", "up_limit", "down_limit"]])
            print(f"{i+1}/{len(codes)} {code}: {len(raw)} rows", flush=True)
        combined = pd.concat(frames, ignore_index=True)
        return daily_data.update(feed, combined.to_csv(index=False).encode(), calendar.to_csv(index=False).encode(), source="baostock")
    finally:
        bs.logout()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", default="data/free_daily")
    parser.add_argument("--codes", default=str(ROOT / "examples" / "free_daily_codes.csv"))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=str(pd.Timestamp.now(tz="Asia/Shanghai").date()))
    args = parser.parse_args()
    try:
        print(fetch(args.feed, args.codes, args.start, args.end))
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
