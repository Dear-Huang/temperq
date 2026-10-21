"""Canonical data formats (SPEC §4) and their validator.

Five shapes, independent of source: bar / observation / text / event / status.
Every row carries the common fields source, record_id, revision and
available_time; revisions of one record share a record_id, and the as-of
rule (asof.py) picks which revision a decision at time tau may see.
"""

from __future__ import annotations

import re

import pandas as pd

from .issues import ERROR, WARN, Report

COMMON = ["source", "record_id", "revision", "available_time"]

# Row states for bar panels (SPEC §4.5). Kept separate on purpose:
# a suspended stock is not "missing data", and a zero price is neither.
STATES = {"normal", "suspended", "missing", "unlisted", "delisted"}
PRICE_COLS = ["open", "high", "low", "close"]

MARKET_TZ = {"cn_a": "Asia/Shanghai", "us": "America/New_York"}

# instants carry a timezone; labels (trading days, report periods) do not
SPECS = {
    "bar": {"required": ["code", "date"], "instants": [], "labels": ["date"]},
    "observation": {"required": ["code", "period_end"], "instants": [],
                    "labels": ["period_end"]},
    "text": {"required": ["codes", "publish_time", "title", "body"],
             "instants": ["publish_time"], "labels": []},
    "event": {"required": ["code", "event_time", "type", "payload"],
              "instants": [], "labels": ["event_time"]},
    # `field` names the status dimension (st / index:csi300 / industry:sw1);
    # intervals are half-open [start, end), end empty = still in effect.
    "status": {"required": ["code", "field", "start", "end", "value"],
               "instants": [], "labels": ["start", "end"], "nullable": ["end"]},
}

_OFFSET = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")


def _rows(df: pd.DataFrame, mask: pd.Series, cols: list[str]) -> list:
    return df.loc[mask, cols].head(5).astype(str).to_dict("records")


def parse_instant(raw: pd.Series, tz: str | None, col: str, rep: Report) -> pd.Series:
    """Parse a timestamp column to UTC. Naive values need a market timezone."""
    if isinstance(raw.dtype, pd.DatetimeTZDtype):
        return raw.dt.tz_convert("UTC")
    if pd.api.types.is_datetime64_any_dtype(raw):
        naive = raw.notna()
        text = raw
    else:
        text = raw.astype("string").str.strip()
        naive = text.notna() & ~text.str.contains(_OFFSET, na=False)
    if naive.any() and tz is None:
        rep.add(ERROR, "NAIVE_TIME",
                f"{col} has no timezone; add an offset or pass the market timezone",
                int(naive.sum()), raw[naive].head(5).astype(str).tolist())
    out = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns, UTC]")
    aware = raw.notna() & ~naive
    if aware.any():
        out[aware] = pd.to_datetime(text[aware], errors="coerce", utc=True, format="mixed")
    if naive.any() and tz is not None:
        local = pd.to_datetime(text[naive], errors="coerce", format="mixed")
        out[naive] = local.dt.tz_localize(tz, ambiguous="NaT",
                                          nonexistent="NaT").dt.tz_convert("UTC")
    bad = out.isna() & raw.notna()
    if tz is None:
        bad &= ~naive
    if bad.any():
        rep.add(ERROR, "BAD_TIME", f"unparseable values in {col}",
                int(bad.sum()), raw[bad].head(5).astype(str).tolist())
    return out


def _parse_label(raw: pd.Series, col: str, rep: Report) -> pd.Series:
    parsed = pd.to_datetime(raw, errors="coerce", format="mixed")
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        parsed = parsed.dt.tz_localize(None)
    bad = parsed.isna() & raw.notna()
    if bad.any():
        rep.add(ERROR, "BAD_TIME", f"unparseable values in {col}",
                int(bad.sum()), raw[bad].head(5).astype(str).tolist())
    return parsed.dt.normalize()


def _local_date(instant: pd.Series, tz: str | None) -> pd.Series:
    return instant.dt.tz_convert(tz or "UTC").dt.tz_localize(None).dt.normalize()


def normalize(df: pd.DataFrame, kind: str, tz: str | None = None,
              rep: Report | None = None) -> pd.DataFrame:
    """Return a copy with instants in UTC and labels as dates."""
    rep = rep if rep is not None else Report()
    spec = SPECS[kind]
    out = df.copy()
    out["available_time"] = parse_instant(out["available_time"], tz, "available_time", rep)
    for c in spec["instants"]:
        out[c] = parse_instant(out[c], tz, c, rep)
    for c in spec["labels"]:
        out[c] = _parse_label(out[c], c, rep)
    out["revision"] = pd.to_numeric(out["revision"], errors="coerce")
    return out


def validate_frame(df: pd.DataFrame, kind: str, tz: str | None = None) -> Report:
    """Validate one canonical-format table. Returns a Report (never raises).

    tz: market timezone used to interpret naive timestamps (e.g. Asia/Shanghai).
    """
    rep = Report()
    if kind not in SPECS:
        rep.add(ERROR, "UNKNOWN_KIND", f"kind must be one of {sorted(SPECS)}, got {kind!r}")
        return rep
    spec = SPECS[kind]
    missing = [c for c in COMMON + spec["required"] if c not in df.columns]
    if missing:
        rep.add(ERROR, "MISSING_COLUMN", f"required columns absent: {missing}")
        return rep

    raw_rev = df["revision"]
    d = normalize(df, kind, tz, rep)

    if df["available_time"].isna().any():
        rep.add(ERROR, "NO_AVAILABLE_TIME", "available_time is null",
                int(df["available_time"].isna().sum()))
    nullable = set(spec.get("nullable", []))
    for c in spec["instants"] + spec["labels"]:
        if c not in nullable and df[c].isna().any():
            rep.add(ERROR, "NULL_TIME", f"{c} is null", int(df[c].isna().sum()))
    for c in ("source", "record_id"):
        if df[c].isna().any():
            rep.add(ERROR, "NULL_FIELD", f"{c} is null", int(df[c].isna().sum()))

    rev = d["revision"]
    bad_rev = rev.isna() | (rev < 0) | (rev % 1 != 0)
    if bad_rev.any():
        rep.add(ERROR, "BAD_REVISION", "revision must be a non-negative integer",
                int(bad_rev.sum()), raw_rev[bad_rev].head(5).astype(str).tolist())

    dup = d.duplicated(["record_id", "revision"], keep=False)
    if dup.any():
        rep.add(ERROR, "DUPLICATE_KEY", "duplicate (record_id, revision)",
                int(dup.sum()), _rows(d, dup, ["record_id", "revision"]))

    # a later revision cannot have been known before an earlier one
    s = d.sort_values(["record_id", "revision"])
    prev = s.groupby("record_id", sort=False)["available_time"].shift()
    back = s["available_time"] < prev
    if back.any():
        rep.add(ERROR, "REVISION_ORDER",
                "a later revision became available before an earlier one",
                int(back.sum()), _rows(s, back, ["record_id", "revision", "available_time"]))

    {"bar": _check_bar, "observation": _check_observation, "text": _check_text,
     "event": _check_event, "status": _check_status}[kind](d, rep, tz)
    return rep


def _check_bar(df: pd.DataFrame, rep: Report, tz: str | None) -> None:
    early = _local_date(df["available_time"], tz) < df["date"]
    if early.any():
        rep.add(ERROR, "AVAILABLE_BEFORE_DATE",
                "available_time earlier than the bar's own date",
                int(early.sum()), _rows(df, early, ["code", "date", "available_time"]))

    if "state" in df.columns:
        state = df["state"].astype("string")
        bad = ~state.isin(STATES) | state.isna()
        if bad.any():
            rep.add(ERROR, "BAD_STATE", f"state must be one of {sorted(STATES)}",
                    int(bad.sum()), state[bad].head(5).tolist())
    else:
        rep.add(WARN, "NO_STATE_COLUMN",
                "no state column: suspension/missing/delisting cannot be told apart")
        state = pd.Series("normal", index=df.index, dtype="string")

    for c in [c for c in PRICE_COLS if c in df.columns]:
        v = pd.to_numeric(df[c], errors="coerce")
        nonpos = v.notna() & (v <= 0)
        if nonpos.any():
            halted = nonpos & (state == "suspended")
            if halted.any():
                rep.add(ERROR, "HALT_ENCODED_AS_ZERO",
                        f"{c}<=0 on suspended rows; leave trade price empty, keep state=suspended",
                        int(halted.sum()), _rows(df, halted, ["code", "date", c]))
            other = nonpos & ~halted
            if other.any():
                rep.add(ERROR, "PRICE_NONPOSITIVE", f"{c}<=0 treated as data error",
                        int(other.sum()), _rows(df, other, ["code", "date", c]))

    if "close" in df.columns:
        gap = (state == "normal") & pd.to_numeric(df["close"], errors="coerce").isna()
        if gap.any():
            rep.add(ERROR, "NORMAL_WITH_MISSING_PRICE",
                    "state=normal but close is empty; mark the row missing/suspended",
                    int(gap.sum()), _rows(df, gap, ["code", "date"]))

    if {"high", "low"} <= set(df.columns):
        hi = pd.to_numeric(df["high"], errors="coerce")
        lo = pd.to_numeric(df["low"], errors="coerce")
        bad = hi < lo
        for c in ("open", "close"):
            if c in df.columns:
                v = pd.to_numeric(df[c], errors="coerce")
                bad |= (v > hi) | (v < lo)
        if bad.any():
            rep.add(ERROR, "OHLC_INCONSISTENT", "high<low or open/close outside [low, high]",
                    int(bad.sum()), _rows(df, bad, ["code", "date"]))

    if "vol" in df.columns:
        zero = (state == "normal") & (pd.to_numeric(df["vol"], errors="coerce") == 0)
        if zero.any():
            rep.add(WARN, "ZERO_VOLUME_NORMAL",
                    "vol=0 on state=normal rows; possible halt not flagged",
                    int(zero.sum()), _rows(df, zero, ["code", "date"]))

    if "adj_close" in df.columns or "qfq_close" in df.columns:
        rep.add(WARN, "ADJUSTED_PRICE_LEVEL",
                "forward-adjusted price levels depend on today's factor; "
                "store raw prices plus adj_factor")


def _check_observation(df: pd.DataFrame, rep: Report, tz: str | None) -> None:
    early = _local_date(df["available_time"], tz) <= df["period_end"]
    if early.any():
        rep.add(ERROR, "AVAILABLE_BEFORE_PERIOD_END",
                "a period's figure cannot be known on or before the period ends",
                int(early.sum()), _rows(df, early, ["code", "period_end", "available_time"]))


def _check_text(df: pd.DataFrame, rep: Report, tz: str | None) -> None:
    early = df["available_time"] < df["publish_time"]
    if early.any():
        rep.add(ERROR, "AVAILABLE_BEFORE_PUBLISH", "available_time earlier than publish_time",
                int(early.sum()), _rows(df, early, ["record_id", "publish_time",
                                                    "available_time"]))
    empty = df["codes"].isna() | (df["codes"].astype("string").str.strip() == "")
    if empty.any():
        rep.add(WARN, "NO_ENTITY", "documents not mapped to any code", int(empty.sum()))
    valid = df["available_time"].notna() & df["publish_time"].notna()
    if valid.any() and (df.loc[valid, "available_time"] == df.loc[valid, "publish_time"]).all():
        rep.add(WARN, "AVAILABLE_EQUALS_PUBLISH",
                "available_time == publish_time on every row; collection latency not modelled")


def _check_event(df: pd.DataFrame, rep: Report, tz: str | None) -> None:
    early = _local_date(df["available_time"], tz) < df["event_time"]
    if early.any():
        # Fine for scheduled events (announced ahead); flag so the user confirms.
        rep.add(WARN, "AVAILABLE_BEFORE_EVENT",
                "event known before it happens; correct only for pre-announced events",
                int(early.sum()), _rows(df, early, ["code", "type", "event_time"]))


def _check_status(df: pd.DataFrame, rep: Report, tz: str | None) -> None:
    rev = df["end"].notna() & (df["start"] >= df["end"])
    if rev.any():
        rep.add(ERROR, "INTERVAL_REVERSED", "start must be before end (half-open [start, end))",
                int(rev.sum()), _rows(df, rev, ["code", "field", "start", "end"]))
    # overlap is judged on the latest revision of each record
    latest = df.sort_values("revision").groupby("record_id").tail(1)
    s = latest.sort_values(["code", "field", "start"])
    end_filled = s["end"].fillna(pd.Timestamp.max)
    prev_end = end_filled.groupby([s["code"], s["field"]]).shift()
    overlap = prev_end.notna() & (s["start"] < prev_end)
    if overlap.any():
        rep.add(ERROR, "INTERVAL_OVERLAP",
                "intervals of the same code and field overlap",
                int(overlap.sum()), _rows(s, overlap, ["code", "field", "start"]))
