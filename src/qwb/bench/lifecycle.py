"""Explain stale valuations with point-in-time security lifecycle records.

A stale mark (last adjusted price carried forward) is expected when the stock
is on the exchange suspension list that day, or after its delisting date.
Only marks with no such record are treated as unexplained data gaps.

The lifecycle file is optional: without it every stale mark stays unknown and
the account keeps DIAGNOSTIC_ONLY, as before.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

FILE = "lifecycle.parquet"
COLUMNS = ["code", "date", "event"]          # event in {"suspended", "delisted"}
EVENTS = {"suspended", "delisted"}


def load_declared(spec: dict | None) -> pd.DataFrame | None:
    """Load the lifecycle file a protocol declares, after checking its hash.

    Protocols without a `lifecycle` block keep the old behaviour (no records).
    """
    if not spec:
        return None
    from .contracts import digest
    p = Path(spec["path"])
    if digest(p) != spec["sha256"]:
        raise ValueError("lifecycle file changed: create a new protocol")
    return load(p)


def load(p: Path) -> pd.DataFrame | None:
    p = Path(p)
    if not p.is_file():
        return None
    df = pd.read_parquet(p)
    if list(df.columns[:3]) != COLUMNS or not set(df["event"]) <= EVENTS:
        raise ValueError(f"{FILE}: expected columns {COLUMNS} and events {sorted(EVENTS)}")
    df = df[COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"])
    if df.duplicated().any():
        raise ValueError(f"{FILE}: duplicate rows")
    return df


def masks(events: pd.DataFrame | None, dates: pd.DatetimeIndex, codes: pd.Index):
    """Return (suspended, delisted) boolean arrays aligned to the panel.

    `suspended[t, j]` is true on listed suspension days. `delisted[t, j]` is
    true on and after the delisting date. Both use only the recorded dates.
    """
    shape = (len(dates), len(codes))
    sus, gone = np.zeros(shape, bool), np.zeros(shape, bool)
    if events is None or events.empty:
        return sus, gone
    col = pd.Series(np.arange(len(codes)), index=codes)
    row = pd.Series(np.arange(len(dates)), index=dates)
    s = events[(events["event"] == "suspended") & events["code"].isin(codes) & events["date"].isin(dates)]
    sus[row[s["date"]].to_numpy(), col[s["code"]].to_numpy()] = True
    d = events[(events["event"] == "delisted") & events["code"].isin(codes)]
    for code, day in zip(d["code"], d["date"]):
        start = dates.searchsorted(day)
        gone[start:, col[code]] = True
    return sus, gone


def classify(holdings: pd.DataFrame, events: pd.DataFrame | None) -> pd.DataFrame:
    """Add `stale_reason` to stale holding rows: suspended / delisted / unknown."""
    h = holdings.copy()
    h["stale_reason"] = np.where(h["stale"], "unknown", "")
    if events is None or not h["stale"].any():
        return h
    key = pd.to_datetime(h["date"])
    sus = events[events["event"] == "suspended"]
    sus_keys = set(zip(sus["code"], sus["date"]))
    delist = events[events["event"] == "delisted"].set_index("code")["date"]
    stale = h["stale"].to_numpy()
    is_sus = np.array([(c, d) in sus_keys for c, d in zip(h["code"], key)])
    after = key.to_numpy() >= h["code"].map(delist).to_numpy(dtype="datetime64[ns]")
    h.loc[stale & is_sus, "stale_reason"] = "suspended"
    h.loc[stale & ~is_sus & after, "stale_reason"] = "delisted"
    return h


def build_from_cache(cache_dir: Path, out: Path) -> dict:
    """Write lifecycle.parquet from cached tushare suspend_d and stock_basic files."""
    cache_dir = Path(cache_dir)
    rows = []
    for f in sorted((cache_dir / "suspend_d").glob("*.parquet")):
        d = pd.read_parquet(f)
        if len(d):
            d = d[d["suspend_type"] == "S"]
            rows.append(pd.DataFrame({"code": d["ts_code"], "date": pd.to_datetime(d["trade_date"]),
                                      "event": "suspended"}))
    basic = pd.concat([pd.read_parquet(f) for f in sorted((cache_dir / "stock_basic").glob("*.parquet"))])
    basic = basic.drop_duplicates("ts_code")
    basic["delist_date"] = pd.to_datetime(basic["delist_date"], errors="coerce")
    b = basic[basic["delist_date"].notna()]
    rows.append(pd.DataFrame({"code": b["ts_code"], "date": b["delist_date"], "event": "delisted"}))
    df = pd.concat(rows, ignore_index=True).drop_duplicates().sort_values(["event", "code", "date"])
    df.to_parquet(out, index=False)
    return {"rows": int(len(df)), "suspended": int((df["event"] == "suspended").sum()),
            "delisted": int((df["event"] == "delisted").sum())}
