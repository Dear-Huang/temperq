"""Anonymous BaoStock daily feed; publish only after every symbol validates."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import socket

import pandas as pd

from . import daily_data, experiments, rules, strategy

DEFAULT_CODES = Path(__file__).parent / "static" / "free_daily_codes.csv"


def completed_day():
    # Conservative: never request a possibly incomplete current session.
    return str(pd.Timestamp.now(tz="Asia/Shanghai").date() - timedelta(days=1))


def dates(start, end):
    try:
        a, b = pd.Timestamp(start), pd.Timestamp(end)
        if str(a.date()) != start or str(b.date()) != end:
            raise ValueError()
        if a > b or a < pd.Timestamp("2005-01-01") or end > completed_day():
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("日期须为 YYYY-MM-DD，开始不早于 2005 年，结束不晚于昨天") from None
    return a, b


def table(response):
    if response.error_code != "0":
        raise ValueError(f"BaoStock {response.error_code}: {response.error_msg}")
    # SDK get_data() still calls pandas.DataFrame.append() for paged results.
    # Use the public row iterator so large histories work with pandas >= 2.
    rows = []
    while response.error_code == "0" and response.next():
        rows.append(response.get_row_data())
    if response.error_code != "0":
        raise ValueError(f"BaoStock {response.error_code}: {response.error_msg}")
    return pd.DataFrame(rows, columns=response.fields)


def fetch(feed, codes_file=DEFAULT_CODES, start="2021-01-01", end=None, *,
          log=print, progress=None, check_cancel=lambda: None, sdk=None):
    """Use a serialized worker: BaoStock owns process-global socket state."""
    end = end or completed_day()
    _, end_day = dates(start, end)
    if sdk is None:
        import baostock as sdk
    feed = Path(feed)
    codes = [strategy.stock_code(c) for c in pd.read_csv(codes_file, dtype=str)["code"]]
    if not codes or len(set(codes)) != len(codes):
        raise ValueError("股票名单为空或重复")
    if any(not c.endswith((".SH", ".SZ")) for c in codes):
        raise ValueError("免费数据首版只支持沪深 A 股")
    calendar_start = start
    if (feed / "current.json").is_file():
        latest = daily_data.current(feed)
        meta = experiments.read(latest / "snapshot.json")
        if meta["source"] != "baostock":
            raise ValueError("不能将 BaoStock 追加到其他来源的数据，请另建数据源")
        old_codes = set(pd.read_csv(latest / "source.csv", usecols=["code"])["code"])
        if set(codes) != old_codes:
            raise ValueError("股票名单改变，请另建数据源")
        if meta["end"] >= end:
            log(f"数据已更新到 {meta['end']}，无需重复下载")
            return {"snapshot": str(latest), "data_snapshot_id": meta["data_snapshot_id"], "added_rows": 0}
        # Keep holidays before the first bar too: historical calendars are immutable.
        calendar_start = pd.read_csv(latest / "calendar.csv", dtype=str)["date"].min()
        start = str((pd.Timestamp(meta["end"]) + timedelta(days=1)).date())
    check_cancel()
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(15)
    logged_in = False
    try:
        log("连接 BaoStock 免费服务（无需 token）…")
        login = sdk.login()
        if login.error_code != "0":
            raise ValueError(f"BaoStock 连接失败：{login.error_msg}；未发布更新，请稍后重试")
        logged_in = True
        check_cancel()
        calendar = table(sdk.query_trade_dates(calendar_start, str((end_day + timedelta(days=14)).date())))
        if calendar.empty:
            raise ValueError("未取得交易日历")
        calendar = calendar.rename(columns={"calendar_date": "date", "is_trading_day": "is_open"})[["date", "is_open"]]
        calendar_end = str((end_day + timedelta(days=14)).date())
        if (set(calendar.date) != set(pd.date_range(calendar_start, calendar_end).strftime("%Y-%m-%d"))
                or calendar.date.duplicated().any() or not calendar.is_open.isin(["0", "1"]).all()):
            raise ValueError("交易日历不完整；未发布更新")
        expected = set(calendar.loc[(calendar.is_open == "1") & calendar.date.ge(start)
                                    & calendar.date.le(end), "date"])
        if not expected:
            if (feed / "current.json").is_file():
                latest = daily_data.current(feed)
                meta = experiments.read(latest / "snapshot.json")
                log("所选区间没有新交易日")
                return {"snapshot": str(latest), "data_snapshot_id": meta["data_snapshot_id"], "added_rows": 0}
            raise ValueError("所选日期没有交易日")
        frames = []
        fields = "date,code,open,high,low,close,preclose,amount,tradestatus,isST"
        for i, code in enumerate(codes):
            check_cancel()
            symbol = code[-2:].lower() + "." + code[:6]
            raw = table(sdk.query_history_k_data_plus(symbol, fields, start_date=start, end_date=end,
                                                     frequency="d", adjustflag="3"))
            if set(raw.get("date", [])) != expected or raw["date"].duplicated().any():
                raise ValueError(f"{code} 行情缺日或重复（上市前或缺失数据不填造）；未发布不完整更新")
            if not raw["tradestatus"].isin(["0", "1"]).all():
                raise ValueError(f"{code} 交易状态不明")
            raw["date"] = pd.to_datetime(raw["date"])
            raw = raw.sort_values("date")
            check_cancel()
            factors = table(sdk.query_adjust_factor(symbol, start_date="1990-01-01", end_date=end))
            if factors.empty:
                raw["adj_factor"] = 1.0
            else:
                factors = factors.rename(columns={"dividOperateDate": "date", "backAdjustFactor": "adj_factor"})
                factors["date"] = pd.to_datetime(factors["date"])
                factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="raise")
                if not factors["adj_factor"].gt(0).all():
                    raise ValueError(f"{code} 复权因子无效")
                raw = pd.merge_asof(raw, factors[["date", "adj_factor"]].sort_values("date"), on="date")
                raw["adj_factor"] = raw["adj_factor"].fillna(1.0)
            raw["code"] = code
            raw["is_st"] = pd.to_numeric(raw["isST"], errors="raise")
            raw["state"] = raw["tradestatus"].map({"1": "normal", "0": "suspended"})
            preclose = pd.to_numeric(raw["preclose"], errors="raise")
            raw["up_limit"], raw["down_limit"] = rules.limit_prices_vec(raw["code"], raw["is_st"], raw["date"], preclose)
            raw["date"] = raw["date"].dt.strftime("%Y-%m-%d")
            frames.append(raw[["code", "date", "open", "high", "low", "close", "amount", "adj_factor",
                               "is_st", "state", "up_limit", "down_limit"]])
            log(f"{i+1}/{len(codes)} {code}：{len(raw)} 条日线")
            if progress:
                progress(i + 1, len(codes), code)
        check_cancel()
        combined = pd.concat(frames, ignore_index=True)
        result = daily_data.update(feed, combined.to_csv(index=False).encode(),
                                   calendar.to_csv(index=False).encode(), source="baostock")
        log(f"已发布完整快照：{min(expected)} 至 {max(expected)}，新增 {result['added_rows']} 条")
        return result
    except (OSError, TimeoutError) as exc:
        raise ValueError(f"BaoStock 网络连接失败：{exc}；未发布不完整更新，请稍后重试") from exc
    finally:
        try:
            if logged_in:
                sdk.logout()
        finally:
            socket.setdefaulttimeout(previous_timeout)
