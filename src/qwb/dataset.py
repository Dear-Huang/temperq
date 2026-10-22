"""Datasets made of several sources (UI step 2).

A base dataset (Tushare download, CSV import, an existing folder) has its own
bars/. A combined dataset points to a base and adds sources:

    <dir>/dataset.json        {"base": "<path>", "sources": [...]}
    <dir>/sources/<id>.parquet
    <dir>/snapshot.json       id = hash(base snapshot, every source file)

Source kinds:
    bars    more daily bars (same columns as the CSV template), e.g. another
            period; "override" replaces base rows on the same stock-day,
            "fill" only adds stock-days the base lacks
    st      ST flags (code, date, is_st); replaces the base ST status
    factor  numeric columns per stock and day; each column becomes a feature

Every source declares when its data becomes usable. The importer moves each
row to the first trading day on which it may be used for that day's decision
(made after the close, executed at the next open), so loading needs no more
time logic and no row is visible before it was known.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import daily

KINDS = {
    "factor": "因子 / 指标（资金流、财务、估值、自建因子等，每列一个数值）",
    "bars": "行情（补充或覆盖某段时间的日线）",
    "st": "ST 标记",
}
RULES = {
    "evening": "当天收盘后到次日开盘前能拿到（用于当天收盘后的决策）",
    "next_day": "次日开盘后才能拿到（晚一天使用）",
    "announce": "按公告日：文件里有公告日列 ann_date，公告后的下一个交易日起使用，并一直有效到下一条",
}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# column names keep letters, digits and CJK (情绪_利好 stays readable)
COL_RE = re.compile(r"[^A-Za-z0-9_一-鿿]")
MAX_FACTOR_COLS = 30
MAX_LEVELS = 20               # text column with at most this many values -> 0/1 columns
MIN_LEVEL_ROWS = 20           # a value must appear this often to become its own column
OVERLAP_BAD_SHARE = 0.05      # share of overlapping stock-days whose close disagrees


class SourceError(ValueError):
    """User-facing problem with a source (message shown as is)."""


# ----------------------------------------------------------------- reading

def is_dataset(path: Path) -> bool:
    path = Path(path)
    return (path / "snapshot.json").is_file() and (
        (path / "bars").is_dir() or (path / "dataset.json").is_file())


def spec(path: Path) -> dict:
    p = Path(path) / "dataset.json"
    if not p.is_file():
        return {"base": None, "sources": []}
    return json.loads(p.read_text(encoding="utf-8"))


def root(path: Path) -> Path:
    """The folder that holds the base bars."""
    b = spec(path)["base"]
    return Path(b) if b else Path(path)


def has_index(path: Path) -> bool:
    return (root(path) / "index_members.parquet").is_file()


def has_st(path: Path) -> bool:
    s = spec(path)
    return any(x["kind"] == "st" for x in s["sources"]) or \
        (root(path) / "st_status.parquet").is_file()


def calendar(path: Path) -> pd.DatetimeIndex:
    files = sorted((root(path) / "bars").glob("*.parquet"))
    d = pd.concat([pd.read_parquet(f, columns=["date"]) for f in files], ignore_index=True)
    return pd.DatetimeIndex(sorted(pd.to_datetime(d["date"].unique())))


def _base_bars(r: Path) -> pd.DataFrame:
    import pyarrow.parquet as pq
    files = sorted((r / "bars").glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no bars under {r / 'bars'}")
    have = set(pq.read_schema(files[0]).names)
    cols = ["code", "date"] + daily.FIELDS + [f for f in daily.OPTIONAL_FIELDS if f in have]
    return pd.concat([pd.read_parquet(f, columns=cols) for f in files], ignore_index=True)


def load(path: Path) -> dict:
    """Panel (with x_* factor frames), snapshot id, ST status, index members, sources."""
    path = Path(path)
    s, r = spec(path), root(path)
    bars = _base_bars(r)
    bars["date"] = pd.to_datetime(bars["date"])
    for src in [x for x in s["sources"] if x["kind"] == "bars"]:
        extra = pd.read_parquet(path / "sources" / f"{src['id']}.parquet")
        extra["date"] = pd.to_datetime(extra["date"])
        extra = extra[[c for c in bars.columns if c in extra.columns]]
        first, second = (extra, bars) if src.get("priority") == "override" else (bars, extra)
        bars = pd.concat([first, second], ignore_index=True).drop_duplicates(
            ["code", "date"], keep="first")
    P = daily.panel_from_bars(bars)
    dates, codes = P["close"].index, P["close"].columns
    for src in [x for x in s["sources"] if x["kind"] == "factor"]:
        f = pd.read_parquet(path / "sources" / f"{src['id']}.parquet")
        f["date"] = pd.to_datetime(f["date"])
        for c in src["columns"]:
            w = f.pivot(index="date", columns="code", values=c).reindex(index=dates, columns=codes)
            if src.get("ffill"):
                w = w.ffill()                 # per stock (column); never across stocks
            P[f"x_{src['id']}_{c}"] = w.astype("float64")
    st_src = [x for x in s["sources"] if x["kind"] == "st"]
    st = None
    if st_src:
        st = pd.read_parquet(path / "sources" / f"{st_src[-1]['id']}.parquet")
    elif (r / "st_status.parquet").is_file():
        st = pd.read_parquet(r / "st_status.parquet")
    members = pd.read_parquet(r / "index_members.parquet") \
        if (r / "index_members.parquet").is_file() else None
    snap = json.loads((path / "snapshot.json").read_text(encoding="utf-8"))
    return {"P": P, "snapshot_id": snap["data_snapshot_id"], "st": st, "members": members,
            "sources": s["sources"]}


# ----------------------------------------------------------------- writing

def create(base: Path, dest: Path) -> dict:
    """New combined dataset on top of `base` (itself a base or a combined dataset)."""
    base, dest = Path(base), Path(dest)
    if not is_dataset(base):
        raise SourceError("基础数据不是 qwb 数据文件夹")
    if dest.exists():
        raise SourceError(f"已经有叫 {dest.name} 的数据了，换个名字")
    s = spec(base)
    if s["base"]:                                   # flatten: always one level deep
        (dest / "sources").mkdir(parents=True)
        for src in s["sources"]:
            (dest / "sources" / f"{src['id']}.parquet").write_bytes(
                (base / "sources" / f"{src['id']}.parquet").read_bytes())
        new = {"base": s["base"], "sources": list(s["sources"])}
    else:
        (dest / "sources").mkdir(parents=True)
        new = {"base": str(base.resolve()), "sources": []}
    _write(dest, new)
    return new


def _put(path: Path, text: str, tries: int = 10) -> None:
    """Write via a temp file and rename; retry while a sync client or reader holds it."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.3)


def _write(dest: Path, s: dict) -> None:
    _put(dest / "dataset.json", json.dumps(s, ensure_ascii=False, indent=2))
    bsnap = json.loads((Path(s["base"]) / "snapshot.json").read_text(encoding="utf-8"))
    h = hashlib.sha256(bsnap["data_snapshot_id"].encode())
    for src in s["sources"]:
        h.update(f"|{src['id']}|{src['sha256']}|{src.get('rule')}|{src.get('priority')}".encode())
    snap = {k: bsnap.get(k) for k in ("start", "end", "trading_days", "codes")}
    snap.update({"data_snapshot_id": "snap-" + h.hexdigest()[:16],
                 "source": "combined", "base_snapshot_id": bsnap["data_snapshot_id"],
                 "has_st": bsnap.get("has_st", True) or any(x["kind"] == "st" for x in s["sources"]),
                 "sources": len(s["sources"])})
    _put(dest / "snapshot.json", json.dumps(snap, ensure_ascii=False, indent=2))


def effective_days(dates: pd.Series, cal: pd.DatetimeIndex, rule: str) -> pd.Series:
    """First trading day on which a row dated `dates` may be used (NaT past the calendar).

    evening:  the same trading day (a non-trading date moves to the next one)
    next_day / announce: the first trading day strictly after the date
    """
    d = pd.to_datetime(dates).dt.normalize().to_numpy()
    side = "left" if rule == "evening" else "right"
    pos = np.searchsorted(cal.to_numpy(), d, side=side)
    out = np.full(len(d), np.datetime64("NaT"), dtype="datetime64[ns]")
    ok = pos < len(cal)
    out[ok] = cal.to_numpy()[pos[ok]]
    return pd.Series(out, index=dates.index)


def _codes(s: pd.Series) -> pd.Series:
    from .csvimport import _suffix
    return s.astype(str).map(_suffix)


def _read_csv(raw: bytes) -> pd.DataFrame:
    from .csvimport import ALIASES
    df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding_errors="replace")
    df.columns = [ALIASES.get(str(c).strip().lower(), ALIASES.get(str(c).strip(),
                                                              str(c).strip().lower()))
                  for c in df.columns]
    df = df.rename(columns={"公告日": "ann_date", "公告日期": "ann_date"})
    if df.empty:
        raise SourceError("文件是空的")
    for c in ("code", "date"):
        if c not in df.columns and not (c == "date" and "ann_date" in df.columns):
            raise SourceError(f"缺少必填列：{c}")
    return df


def _parse_dates(s: pd.Series, what: str) -> pd.Series:
    d = pd.to_datetime(s.astype(str).str.strip(), format="mixed", errors="coerce")
    if d.isna().any():
        bad = s[d.isna()].head(3).tolist()
        raise SourceError(f"{what}有 {int(d.isna().sum())} 行看不懂，例如 {bad}")
    return d.dt.normalize()


def add_factor(dest: Path, df: pd.DataFrame, rule: str, label: str, origin: str) -> dict:
    """Factor rows (code, date or ann_date, numeric columns) -> a source of `dest`."""
    if rule not in RULES:
        raise SourceError("请选择数据什么时候能拿到")
    key = "ann_date" if rule == "announce" else "date"
    if key not in df.columns:
        raise SourceError("选了“按公告日”，文件里需要 ann_date（公告日）列" if rule == "announce"
                          else "缺少 date 列")
    skip = {"code", "date", "ann_date", "end_date", "name", "名称"}
    cols = [c for c in df.columns if c not in skip]
    vals, cat_notes, skipped = {}, [], []
    for c in cols:
        key_c = COL_RE.sub("_", str(c))[:30] or "col"
        v = pd.to_numeric(df[c], errors="coerce")
        if v.notna().mean() >= 0.5:               # mostly numeric -> a factor column
            vals[key_c] = v
            continue
        s = df[c].astype("string").str.strip()
        levels = s.dropna().value_counts()
        if 2 <= len(levels) <= MAX_LEVELS:
            # text labels (industry, sentiment, event type) -> one 0/1 column per value;
            # rare values are pooled so a label seen a handful of times is not a feature
            keep = [x for x in levels.index if levels[x] >= MIN_LEVEL_ROWS][:MAX_LEVELS]
            for lv in keep:
                vals[f"{key_c}_{COL_RE.sub('_', str(lv))[:12] or 'v'}"[:30]] = \
                    (s == lv).astype("float64").where(s.notna())
            cat_notes.append(f"文字列“{c}”有 {len(levels)} 种取值，转成 {len(keep)} 个 0/1 指标")
        else:
            skipped.append(str(c))
    if not vals:
        raise SourceError("没找到能用的列。除了 code、date（或 ann_date）之外，每一列应是数值指标，"
                          f"或取值不超过 {MAX_LEVELS} 种的文字标签（如行业、情绪：利好/利空）")
    if len(vals) > MAX_FACTOR_COLS:
        raise SourceError(f"一次最多 {MAX_FACTOR_COLS} 个指标列，现在有 {len(vals)} 个")
    cal = calendar(dest)
    obs = _parse_dates(df[key], "日期" if key == "date" else "公告日")
    out = pd.DataFrame({"code": _codes(df["code"]).to_numpy(), "obs": obs.to_numpy(),
                        **{k: v.to_numpy() for k, v in vals.items()}})
    out["date"] = effective_days(out["obs"], cal, rule)
    dropped = int(out["date"].isna().sum())
    out = out.dropna(subset=["date"]).sort_values(["code", "date", "obs"])
    out = out.drop_duplicates(["code", "date"], keep="last").drop(columns="obs")
    if out.empty:
        raise SourceError("没有一行落在基础数据的交易日范围内，检查日期")
    known = set(pd.read_parquet(next((root(dest) / "bars").glob("*.parquet")), columns=["code"])["code"])
    match = out["code"].isin(known).mean() if known else 1.0
    notes = list(cat_notes)
    if skipped:
        notes.append(f"这些列没有使用：{'、'.join(skipped[:8])}（不是数值，或文字取值太多，"
                     "比如新闻正文需要先转成打分或标签）")
    if match < 0.5:
        notes.append(f"只有 {match:.0%} 的股票代码在基础数据里找到，检查代码格式")
    if dropped:
        notes.append(f"{dropped} 行日期晚于基础数据最后一个交易日，已忽略")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return _save(dest, "factor", out, label, origin,
                 {"rule": rule, "ffill": rule == "announce", "columns": list(vals)}, notes)


def add_bars(dest: Path, raw: bytes, label: str, priority: str, origin: str) -> dict:
    from . import csvimport
    if priority not in ("override", "fill"):
        raise SourceError("请选择和已有行情重叠时怎么处理")
    try:
        bars, _, notes = csvimport.to_bars(csvimport.read(io.BytesIO(raw)))
    except csvimport.ImportError_ as e:
        raise SourceError(str(e)) from None
    base = _base_bars(root(dest))
    base["date"] = pd.to_datetime(base["date"]).dt.strftime("%Y-%m-%d")
    ov = bars.merge(base[["code", "date", "close"]], on=["code", "date"], suffixes=("", "_b"))
    ov = ov.dropna(subset=["close", "close_b"])
    if len(ov):
        bad = (np.abs(ov["close"] / ov["close_b"] - 1) > 0.005).mean()
        if bad > OVERLAP_BAD_SHARE:
            raise SourceError(f"和已有行情重叠的 {len(ov):,} 个股票-日里，{bad:.0%} 的收盘价相差超过 0.5%。"
                              "通常是复权口径或单位不同（应为不复权价、成交额单位元），请检查后再上传")
        notes.append(f"与已有行情重叠 {len(ov):,} 个股票-日，收盘价一致"
                     + ("；重叠部分以新数据为准" if priority == "override" else "；重叠部分保留原数据"))
    keep = [c for c in ["code", "date"] + daily.FIELDS + daily.OPTIONAL_FIELDS if c in bars.columns]
    return _save(dest, "bars", bars[keep], label, origin, {"priority": priority}, notes)


def add_st(dest: Path, df: pd.DataFrame, label: str, origin: str) -> dict:
    from .connectors import cn_a
    if "is_st" not in df.columns:
        raise SourceError("ST 标记文件需要 code、date、is_st（0/1）三列")
    d = _parse_dates(df["date"], "日期")
    flag = pd.to_numeric(df["is_st"], errors="coerce").fillna(0) > 0
    rows = pd.DataFrame({"code": _codes(df["code"]).to_numpy(), "date": d.to_numpy(),
                         "name": "ST"})[flag.to_numpy()]
    cal = calendar(dest)
    rows = rows[rows["date"].isin(cal)]
    st = cn_a.st_status(rows, cal) if len(rows) else pd.DataFrame(
        columns=["source", "record_id", "revision", "available_time", "code", "field",
                 "start", "end", "value"])
    st["source"] = "csv"
    return _save(dest, "st", st, label, origin, {}, [f"{len(rows):,} 个 ST 股票-日"])


def _save(dest: Path, kind: str, frame: pd.DataFrame, label: str, origin: str,
          extra: dict, notes: list) -> dict:
    s = spec(dest)
    if not s["base"]:
        raise SourceError("只能给组合数据添加来源；请先以它为基础新建一份组合数据")
    sid = f"s{len(s['sources']) + 1}"
    p = dest / "sources" / f"{sid}.parquet"
    frame.to_parquet(p, index=False)
    src = {"id": sid, "kind": kind, "label": label[:60] or KINDS[kind], "origin": origin,
           "rows": int(len(frame)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
           "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "notes": notes, **extra}
    if kind == "factor":
        src["start"], src["end"] = frame["date"].min(), frame["date"].max()
    s["sources"].append(src)
    _write(dest, s)
    return src


def remove(dest: Path, sid: str) -> None:
    s = spec(dest)
    s["sources"] = [x for x in s["sources"] if x["id"] != sid]
    (dest / "sources" / f"{sid}.parquet").unlink(missing_ok=True)
    _write(dest, s)


def add_csv(dest: Path, raw: bytes, kind: str, rule: str, label: str, filename: str,
            priority: str = "fill") -> dict:
    if kind not in KINDS:
        raise SourceError("请选择这份数据是什么")
    if kind == "bars":
        return add_bars(dest, raw, label, priority, f"csv:{filename}")
    df = _read_csv(raw)
    if kind == "st":
        return add_st(dest, df, label, f"csv:{filename}")
    return add_factor(dest, df, rule, label, f"csv:{filename}")
