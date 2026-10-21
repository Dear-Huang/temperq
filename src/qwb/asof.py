"""As-of revision selection (SPEC §4.4).

At decision time tau each record_id contributes the revision with the
largest number among those with available_time <= tau. Policy
`first_release` keeps revision 0 only (if it was available by tau).
"""

from __future__ import annotations

import pandas as pd

POLICIES = {"latest", "first_release"}


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        raise ValueError("decision time must be timezone-aware")
    return ts.tz_convert("UTC")


def select(df: pd.DataFrame, tau, policy: str = "latest") -> pd.DataFrame:
    """Rows visible at tau. `df` must be normalized (UTC available_time)."""
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {sorted(POLICIES)}")
    seen = df[df["available_time"] <= _utc(tau)]
    if policy == "first_release":
        return seen[seen["revision"] == 0]
    idx = seen.groupby("record_id")["revision"].idxmax()
    return seen.loc[idx.sort_values()]


def status_on(df: pd.DataFrame, tau, on_date, field: str, policy: str = "latest") -> pd.DataFrame:
    """Status records in effect on `on_date`, as known at `tau`.

    Separating the two clocks is what stops a later correction (e.g. an
    industry reclassification) from leaking into history.
    """
    day = pd.Timestamp(on_date).normalize()
    v = select(df[df["field"] == field], tau, policy)
    live = (v["start"] <= day) & (v["end"].isna() | (day < v["end"]))
    return v[live]
