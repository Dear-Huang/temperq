"""A-share point-in-time building blocks on a Tushare-compatible source.

- ST universe from the daily `stock_st` list (primary) with a `namechange`
  cross-check that reports disagreements instead of hiding them;
- daily bars in the canonical bar format with unit conversion and
  suspended / missing states.

Timing assumptions (defaults, documented in the pilot report):
- ST status on day d is known before that day's open (09:15 local);
- a daily bar is known at the close of its own day (15:00 local).
"""

from __future__ import annotations

import pandas as pd

from .cache import Cache
from .tushare import ApiError, TushareClient

TZ = "Asia/Shanghai"
SOURCE = "tushare"
VOL_TO_SHARES = 100        # daily.vol is in lots (手)
AMOUNT_TO_YUAN = 1000      # daily.amount is in thousand yuan (千元)
PAGE = 6000


def _ymd(d) -> str:
    return pd.Timestamp(d).strftime("%Y%m%d")


def _at(dates: pd.Series, hhmm: str) -> pd.Series:
    h, m = map(int, hhmm.split(":"))
    return (pd.to_datetime(dates) + pd.Timedelta(hours=h, minutes=m)).dt.tz_localize(TZ)


def trading_days(cli: TushareClient, cache: Cache, start, end) -> pd.DatetimeIndex:
    p = {"exchange": "SSE", "start_date": _ymd(start), "end_date": _ymd(end)}
    df = cache.get("trade_cal", p, lambda: cli.call("trade_cal", **p))
    if df.empty:
        raise ApiError(f"trade_cal returned no rows for {p}")
    open_days = pd.to_datetime(df.loc[df["is_open"].astype(int) == 1, "cal_date"])
    return pd.DatetimeIndex(sorted(open_days))


def stock_basic(cli: TushareClient, cache: Cache) -> pd.DataFrame:
    frames = []
    for status in ("L", "D", "P"):
        p = {"list_status": status,
             "fields": "ts_code,name,list_date,delist_date,list_status,market"}
        frames.append(cache.get("stock_basic", p, lambda p=p: cli.call("stock_basic", **p)))
    out = pd.concat(frames, ignore_index=True)
    out["list_date"] = pd.to_datetime(out["list_date"], errors="coerce")
    out["delist_date"] = pd.to_datetime(out["delist_date"], errors="coerce")
    return out


# ---------------------------------------------------------------- ST universe

def st_daily(cli: TushareClient, cache: Cache, days: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per (code, day) the stock was ST, from stock_st."""
    frames = []
    for d in days:
        p = {"trade_date": _ymd(d)}
        df = cache.get("stock_st", p, lambda p=p: cli.paged("stock_st", 3000, ["ts_code"], **p))
        frames.append(df.assign(date=d)[["ts_code", "name", "date"]])
    out = pd.concat(frames, ignore_index=True).rename(columns={"ts_code": "code"})
    return out.drop_duplicates(["code", "date"])


def namechange_events(cli: TushareClient, cache: Cache, first_year: int,
                      last_year: int) -> pd.DataFrame:
    """All name-change events by effective start_date, fetched one year at a time.

    Bulk offset paging on this endpoint returned overlapping pages, so the
    query is narrowed by date and each page is checked for overlap.
    """
    frames = []
    for y in range(first_year, last_year + 1):
        p = {"start_date": f"{y}0101", "end_date": f"{y}1231"}
        frames.append(cache.get("namechange", p, lambda p=p: cli.paged(
            "namechange", 3000, ["ts_code", "name", "start_date", "ann_date"], **p)))
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise ApiError(f"namechange returned no rows for {first_year}-{last_year}")
    ev = pd.concat(frames, ignore_index=True).rename(columns={"ts_code": "code"})
    ev = ev.drop_duplicates(["code", "name", "start_date"])
    ev["start_date"] = pd.to_datetime(ev["start_date"], errors="coerce")
    return ev.dropna(subset=["start_date"]).sort_values(["code", "start_date"])


def _is_st_name(name: pd.Series) -> pd.Series:
    return name.astype("string").str.contains("ST", regex=False, na=False)


def st_daily_from_namechange(ev: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """(code, day) pairs that the latest name change before the day marks as ST."""
    grid = pd.DataFrame({"date": days})
    ev = ev.assign(st=_is_st_name(ev["name"]))
    rows = []
    for code, g in ev.groupby("code", sort=False):
        if not g["st"].any():
            continue
        m = pd.merge_asof(grid, g[["start_date", "st"]].sort_values("start_date"),
                          left_on="date", right_on="start_date", direction="backward")
        hit = m.loc[m["st"].fillna(False).astype(bool), "date"]
        rows.append(pd.DataFrame({"code": code, "date": hit.values}))
    if not rows:
        return pd.DataFrame(columns=["code", "date"])
    return pd.concat(rows, ignore_index=True)


def cross_check(primary: pd.DataFrame, check: pd.DataFrame) -> dict:
    a = set(zip(primary["code"], pd.to_datetime(primary["date"])))
    b = set(zip(check["code"], pd.to_datetime(check["date"])))
    only_a, only_b = sorted(a - b), sorted(b - a)
    return {
        "primary_days": len(a), "check_days": len(b), "both": len(a & b),
        "only_primary": len(only_a), "only_check": len(only_b),
        "agreement": len(a & b) / max(len(a | b), 1),
        "only_primary_examples": [(c, str(d.date())) for c, d in only_a[:10]],
        "only_check_examples": [(c, str(d.date())) for c, d in only_b[:10]],
    }


def st_status(daily_st: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Collapse daily ST membership into canonical status intervals [start, end).

    A run that reaches the last day of `days` stays open (end empty).
    """
    pos = {d: i for i, d in enumerate(days)}
    df = daily_st.assign(i=pd.to_datetime(daily_st["date"]).map(pos)).dropna(subset=["i"])
    df["i"] = df["i"].astype(int)
    df["value"] = df["name"].astype("string").str.contains("*ST", regex=False).map(
        {True: "*ST", False: "ST"})
    df = df.sort_values(["code", "i"])
    brk = (df["i"].diff() != 1) | (df["code"] != df["code"].shift()) \
        | (df["value"] != df["value"].shift())
    df["run"] = brk.cumsum()
    runs = df.groupby("run").agg(code=("code", "first"), value=("value", "first"),
                                 i0=("i", "min"), i1=("i", "max")).reset_index(drop=True)
    last = len(days) - 1
    start = runs["i0"].map(lambda i: days[i])
    end = runs["i1"].map(lambda i: days[i + 1] if i < last else pd.NaT)
    out = pd.DataFrame({
        "source": f"{SOURCE}.stock_st",
        "record_id": runs["code"] + "|st|" + start.dt.strftime("%Y-%m-%d"),
        "revision": 0,
        "available_time": _at(start, "09:15"),
        "code": runs["code"], "field": "st",
        "start": start.dt.strftime("%Y-%m-%d"),
        "end": end.dt.strftime("%Y-%m-%d").astype(object).where(end.notna(), None),
        "value": runs["value"],
    })
    return out


# ---------------------------------------------------------------- index members

INDEXES = {"csi300": "000300.SH", "csi500": "000905.SH", "csi1000": "000852.SH"}


def index_members(cli: TushareClient, cache: Cache, index_code: str, start, end) -> pd.DataFrame:
    """Constituent snapshots (code, index, date) from index_weight, one query per month.

    Each snapshot lists the members from its date until the next snapshot. Query
    from a month before the data starts so the first trading days are covered by
    a snapshot dated before them, never by a later one.
    """
    months = pd.period_range(pd.Timestamp(start) - pd.Timedelta(days=40), pd.Timestamp(end), freq="M")
    frames = []
    for m in months:
        p = {"index_code": index_code, "start_date": m.start_time.strftime("%Y%m%d"),
             "end_date": m.end_time.strftime("%Y%m%d")}
        df = cache.get("index_weight", p, lambda p=p: cli.paged(
            "index_weight", 6000, ["con_code", "trade_date"], **p))
        if not df.empty:
            frames.append(df[["con_code", "trade_date"]])
    if not frames:
        raise ApiError(f"index_weight returned no rows for {index_code}")
    out = pd.concat(frames, ignore_index=True).drop_duplicates()
    out = out.rename(columns={"con_code": "code", "trade_date": "date"})
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out["index"] = index_code
    return out[["code", "index", "date"]].sort_values(["date", "code"]).reset_index(drop=True)


# ---------------------------------------------------------------- daily bars

def daily_bars(cli: TushareClient, cache: Cache, days: pd.DatetimeIndex,
               universe: pd.DataFrame | None = None) -> pd.DataFrame:
    """Canonical bars. `universe` (code, date) limits output; None = all codes.

    Rows for universe members without a bar become state=suspended when the
    stock is on the suspension list (type S) that day, otherwise missing.
    """
    out = []
    for d in days:
        p = {"trade_date": _ymd(d)}
        # one page per day: the full market fits in 6000 rows (cache key is the day only)
        bars = cache.get("daily", p, lambda p=p: cli.paged("daily", PAGE, ["ts_code"], **p))
        adj = cache.get("adj_factor", p, lambda p=p: cli.paged("adj_factor", PAGE,
                                                               ["ts_code"], **p))
        lim = cache.get("stk_limit", p, lambda p=p: cli.paged("stk_limit", PAGE,
                                                              ["ts_code"], **p))
        sus = cache.get("suspend_d", p, lambda p=p: cli.call("suspend_d", **p))
        df = bars.merge(adj[["ts_code", "adj_factor"]], on="ts_code", how="left") \
                 .merge(lim[["ts_code", "up_limit", "down_limit"]], on="ts_code", how="left")
        df["state"] = "normal"
        halted = set(sus.loc[sus["suspend_type"] == "S", "ts_code"]) if not sus.empty else set()
        if universe is not None:
            members = set(universe.loc[pd.to_datetime(universe["date"]) == d, "code"])
            df = df[df["ts_code"].isin(members)]
            absent = sorted(members - set(df["ts_code"]))
            if absent:
                df = pd.concat([df, pd.DataFrame({
                    "ts_code": absent,
                    "state": ["suspended" if c in halted else "missing" for c in absent]})],
                    ignore_index=True)
        df.loc[df["ts_code"].isin(halted) & df["vol"].fillna(0).eq(0), "state"] = "suspended"
        df["date"] = d
        out.append(df)
    df = pd.concat(out, ignore_index=True).rename(columns={"ts_code": "code"})
    df["vol"] = df["vol"] * VOL_TO_SHARES
    df["amount"] = df["amount"] * AMOUNT_TO_YUAN
    for c in ("open", "high", "low", "close"):
        df.loc[df["state"] == "suspended", c] = None
    df["source"] = f"{SOURCE}.daily"
    df["record_id"] = df["code"] + "|" + df["date"].dt.strftime("%Y-%m-%d")
    df["revision"] = 0
    df["available_time"] = _at(df["date"], "15:00")
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    cols = ["source", "record_id", "revision", "available_time", "code", "date",
            "open", "high", "low", "close", "pre_close", "vol", "amount",
            "adj_factor", "up_limit", "down_limit", "state"]
    return df[cols].sort_values(["code", "date"]).reset_index(drop=True)
