"""Append-only CSV updates into immutable daily snapshots and a small feed pointer."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import hashlib

import pandas as pd

from . import csvimport, experiments
from .formats import validate_frame


def current(feed):
    feed = Path(feed).resolve()
    if (feed / "snapshot.json").is_file():
        return feed
    pointer = feed / "current.json"
    if not pointer.is_file():
        raise ValueError("数据源尚无快照，请先导入日线 CSV")
    target = (feed / experiments.read(pointer)["snapshot"]).resolve()
    if not target.is_relative_to(feed / "snapshots") or not (target / "snapshot.json").is_file():
        raise ValueError("数据快照指针无效")
    return target


def update(feed, raw, calendar_raw, *, source="user_csv", dry_run=False):
    """Updates may append; historical revisions or symbol changes need a new feed."""
    feed = Path(feed).resolve()
    feed.mkdir(parents=True, exist_ok=True)
    with experiments.output_lock(feed / "current.json"):
        incoming = csvimport.read(io.BytesIO(raw))
        incoming["code"] = incoming["code"].map(csvimport._suffix)
        incoming["date"] = pd.to_datetime(incoming["date"], format="mixed", errors="raise").dt.strftime("%Y-%m-%d")
        # Use the same round-trip conversion for both sides of exact revision checks.
        numeric=[*csvimport.REQUIRED[2:], "adj_factor", "is_st", "up_limit", "down_limit", "volume"]
        for column in numeric:
            if column in incoming:
                incoming[column] = incoming[column].astype(float)
        if incoming.duplicated(["code", "date"]).any():
            raise ValueError("更新 CSV 存在重复证券日期")
        if "adj_factor" not in incoming:
            raise ValueError("每日更新必须提供 adj_factor，不能默认不存在公司行动")
        if incoming["date"].max() > str(pd.Timestamp.now(tz="Asia/Shanghai").date()):
            raise ValueError("不能导入未来日期行情")
        parent = None
        added = len(incoming)
        if (feed / "current.json").is_file():
            previous = current(feed)
            old = pd.read_csv(previous / "source.csv", dtype=str)
            for column in numeric:
                if column in old:old[column]=old[column].astype(float)
            if set(old.columns) != set(incoming.columns) or set(old["code"]) != set(incoming["code"]):
                raise ValueError("更新不得改变股票集合或 CSV 列；请另建数据源")
            keys = ["code", "date"]
            a, b = old.set_index(keys).sort_index(), incoming.set_index(keys).sort_index()
            overlap = a.index.intersection(b.index)
            left, right = a.loc[overlap], b.loc[overlap, a.columns]
            same = (left.eq(right) | (left.isna() & right.isna())).all().all()
            if not same:
                raise ValueError("检测到历史数据修订；首版拒绝改写，请另建数据源")
            new_rows = b.loc[~b.index.isin(a.index)].reset_index()
            if not new_rows.empty and new_rows["date"].min() <= old["date"].max():
                raise ValueError("首版仅支持在数据末尾追加日期")
            added = len(new_rows)
            incoming = pd.concat([old, new_rows], ignore_index=True)
            parent = experiments.read(previous / "snapshot.json")["data_snapshot_id"]
        calendar = pd.read_csv(io.BytesIO(calendar_raw), dtype=str)
        if set(calendar.columns) != {"date", "is_open"} or not calendar["is_open"].isin(["0", "1"]).all():
            raise ValueError("日历 CSV 需为 date,is_open，1 表示开市，0 表示休市")
        calendar["date"] = pd.to_datetime(calendar["date"], format="%Y-%m-%d", errors="raise").dt.strftime("%Y-%m-%d")
        if calendar["date"].duplicated().any():
            raise ValueError("日历日期重复")
        if parent:
            previous_calendar = pd.read_csv(previous / "calendar.csv", dtype=str)
            fixed = previous_calendar.loc[previous_calendar.date.le(old["date"].max())].set_index("date")["is_open"]
            updated = calendar.set_index("date")["is_open"].reindex(fixed.index)
            if not fixed.eq(updated).all():
                raise ValueError("检测到历史交易日历修订；请另建数据源")
        open_days = set(calendar.loc[calendar["is_open"] == "1", "date"])
        if not set(incoming["date"]) <= open_days:
            raise ValueError("行情日期必须全部在日历开市日中")
        new_days = sorted(incoming.loc[incoming["date"] > (
            experiments.read(current(feed) / "snapshot.json")["end"] if parent else ""), "date"].unique())
        for day in new_days if parent else []:
            if incoming.loc[incoming["date"] == day, "code"].nunique() != incoming["code"].nunique():
                raise ValueError(f"{day} 缺少部分股票行情；停牌也需显式记录")
        incoming = incoming.sort_values(["code", "date"]).reset_index(drop=True)
        bars, st, notes = csvimport.to_bars(incoming)
        if "state" in incoming:
            if not incoming["state"].isin(["normal", "suspended", "missing"]).all():
                raise ValueError("首版不支持退市/公司行动等特殊状态")
            bars["state"] = incoming["state"].to_numpy()
            bars.loc[bars["state"] != "normal", ["open", "high", "low", "close"]] = float("nan")
        report = validate_frame(bars, "bar")
        if not report.ok:
            raise ValueError(str(report))
        raw_all = incoming.to_csv(index=False).encode("utf-8")
        identity = experiments.object_digest({"source": source,
            "rows": hashlib.sha256(raw_all).hexdigest(),
            "calendar": experiments.object_digest(calendar.to_dict("records"))})
        name = identity.removeprefix("sha256:")
        destination = feed / "snapshots" / name
        if dry_run:
            return {"snapshot":str(destination),"data_snapshot_id":identity,"added_rows":added,
                    "start":incoming['date'].min(),"end":incoming['date'].max(),
                    "codes":int(incoming.code.nunique()),"rows":len(bars),"notes":notes}
        destination.parent.mkdir(exist_ok=True)
        if not destination.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".updating-", dir=destination.parent))
            try:
                (temporary / "bars").mkdir()
                for year in sorted(bars["date"].str[:4].unique()):
                    bars[bars["date"].str.startswith(year)].to_parquet(temporary / "bars" / f"{year}.parquet", index=False)
                if st is not None:
                    st.to_parquet(temporary / "st_status.parquet", index=False)
                incoming.to_csv(temporary / "source.csv", index=False)
                calendar.to_csv(temporary / "calendar.csv", index=False)
                experiments.write(temporary / "snapshot.json", {
                    "data_snapshot_id": identity, "start": incoming["date"].min(), "end": incoming["date"].max(),
                    "source": source, "fetched_at": experiments.now(), "parent_snapshot": parent,
                    "added_rows": added, "rows": len(bars), "codes": int(incoming["code"].nunique()),
                    "has_st": st is not None,
                    "trading_days": int(incoming["date"].nunique()), "notes": notes,
                    "scope": "declared_csv_not_independently_verified_market_data"})
                temporary.rename(destination)
            finally:
                if temporary.exists():
                    import shutil
                    shutil.rmtree(temporary)
        experiments.write(feed / "current.json", {"snapshot": destination.relative_to(feed).as_posix(),
                                                   "data_snapshot_id": identity})
        return {"snapshot": str(destination), "data_snapshot_id": identity, "added_rows": added}
