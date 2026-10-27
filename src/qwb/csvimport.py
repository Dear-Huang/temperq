"""CSV upload -> a local dataset in the same layout as a Tushare snapshot.

One row per stock-day. Required: code, date, open, high, low, close, amount
(traded value in yuan). Optional: adj_factor (default 1; prices are taken as
raw/unadjusted), is_st (0/1), up_limit, down_limit (computed from the rule
pack when missing). Common Chinese headers are accepted (代码, 日期, ...).

Output under <data_root>/<name>/: bars/<year>.parquet, st_status.parquet
(only if is_st given), snapshot.json.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from . import data_readiness, rules
from .connectors import cn_a
from .formats import validate_frame

REQUIRED = ["code", "date", "open", "high", "low", "close", "amount"]
ALIASES = {
    "代码": "code", "股票代码": "code", "证券代码": "code", "ts_code": "code",
    "日期": "date", "交易日期": "date", "trade_date": "date",
    "开盘": "open", "开盘价": "open", "最高": "high", "最高价": "high",
    "最低": "low", "最低价": "low", "收盘": "close", "收盘价": "close",
    "成交额": "amount", "成交金额": "amount", "复权因子": "adj_factor",
    "是否st": "is_st", "st": "is_st", "涨停价": "up_limit", "跌停价": "down_limit",
    "成交量": "volume", "vol": "volume",
}
FIELDS = REQUIRED + ['adj_factor','is_st','up_limit','down_limit','volume']
UNITS = {'yuan':1, 'thousand':1000, 'ten_thousand':10000}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MIN_YEARS = 3
TZ = "Asia/Shanghai"


class ImportError_(ValueError):
    """User-facing import problem (message is shown as is)."""


def _suffix(code: str) -> str:
    c = code.strip().upper()
    if "." in c:
        return c
    c = c.zfill(6)
    if c[0] == "6":
        return c + ".SH"
    if c[0] in "48" or c.startswith("92"):
        return c + ".BJ"
    return c + ".SZ"


def read(src, *, mapping=None, amount_unit="yuan") -> pd.DataFrame:
    df = pd.read_csv(src, dtype=str, encoding_errors="replace")
    if mapping is not None:
        if set(mapping)!=set(df.columns):raise ImportError_('文件列已变化，请重新确认列映射')
        targets=[v for v in mapping.values() if v]
        if any(v not in FIELDS for v in targets) or len(targets)!=len(set(targets)):
            raise ImportError_('每种字段只能选择一次，请检查重复映射')
        df=df[[c for c in df.columns if mapping[c]]].rename(columns=mapping)
    df.columns = [ALIASES.get(str(c).strip().lower(), ALIASES.get(str(c).strip(),
                                                              str(c).strip().lower()))
                  for c in df.columns]
    if df.columns.duplicated().any():raise ImportError_('多个列对应同一个字段，请重新选择')
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ImportError_(f"缺少必填列：{', '.join(missing)}（模板见 examples/csv_template.csv）")
    if df.empty:
        raise ImportError_("文件是空的")
    if amount_unit not in UNITS:raise ImportError_('请选择有效的成交额单位')
    if UNITS[amount_unit]!=1:df['amount']=pd.to_numeric(df['amount'],errors='coerce')*UNITS[amount_unit]
    return df


def to_bars(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame | None, list[str]]:
    notes = []
    out = pd.DataFrame({"code": df["code"].astype(str).map(_suffix)})
    d = pd.to_datetime(df["date"].astype(str).str.strip(), format="mixed", errors="coerce")
    if d.isna().any():
        bad = df.loc[d.isna(), "date"].head(3).tolist()
        raise ImportError_(f"有 {int(d.isna().sum())} 行日期看不懂，例如 {bad}；请用 2024-01-02 或 20240102")
    out["date"] = d.dt.normalize()
    for c in ["open", "high", "low", "close", "amount"]:
        out[c] = pd.to_numeric(df[c], errors="coerce")
    out["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce") if "adj_factor" in df \
        else 1.0
    if "adj_factor" not in df:
        notes.append("没有复权因子列：按不复权价格处理（除权日会出现假涨跌，建议提供 adj_factor）")
    if out.duplicated(["code", "date"]).any():
        n = int(out.duplicated(["code", "date"]).sum())
        raise ImportError_(f"有 {n} 行重复（同一只股票同一天出现两次）")
    med = out["amount"].median()
    if med < 1e5:
        notes.append(f"成交额中位数只有 {med:,.0f}，单位应该是“元”；如果是千元或万元，请先换算")

    # optional columns travel with their row through the sort
    for c in ("is_st", "up_limit", "down_limit", "volume"):
        if c in df:
            out[c] = pd.to_numeric(df[c], errors="coerce").to_numpy()
    out = out.sort_values(["code", "date"]).reset_index(drop=True)
    pre = out.groupby("code")["close"].shift()
    st_flag = (out.pop("is_st").fillna(0) > 0).to_numpy() if "is_st" in out \
        else np.zeros(len(out), dtype=bool)
    up, dn = rules.limit_prices_vec(out["code"], st_flag, out["date"], pre)
    if "up_limit" not in out:
        out["up_limit"] = up
    if "down_limit" not in out:
        out["down_limit"] = dn
    if "up_limit" not in df:
        notes.append("涨跌停价按规则包计算（主板 10%、创业板/科创板 20%、北交所 30%、ST 5%）")

    out["state"] = np.where(out["close"].notna() & (out["close"] > 0), "normal", "missing")
    for c in ("open", "high", "low", "close"):
        out.loc[out["state"] != "normal", c] = np.nan
    out["source"] = "csv"
    out["record_id"] = out["code"] + "|" + out["date"].dt.strftime("%Y-%m-%d")
    out["revision"] = 0
    out["available_time"] = (out["date"] + pd.Timedelta(hours=15)).dt.tz_localize(TZ)
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")

    st = None
    if "is_st" in df:
        flagged = out.loc[st_flag, ["code", "date"]].assign(name="ST")
        days = pd.DatetimeIndex(sorted(pd.to_datetime(out["date"].unique())))
        if len(flagged):
            st = cn_a.st_status(flagged.assign(date=pd.to_datetime(flagged["date"])), days)
            st["source"] = "csv"
        else:
            st = pd.DataFrame(columns=["source", "record_id", "revision", "available_time",
                                       "code", "field", "start", "end", "value"])
    return out, st, notes


def calendar_frame(raw, dates):
    import io
    calendar=pd.read_csv(io.BytesIO(raw),dtype=str)
    if set(calendar.columns)!={'date','is_open'} or not calendar.is_open.isin(['0','1']).all():
        raise ImportError_('交易日历需要 date、is_open 两列，is_open 只能是 0 或 1')
    calendar['date']=pd.to_datetime(calendar.date,format='%Y-%m-%d',errors='raise').dt.strftime('%Y-%m-%d')
    if calendar.date.duplicated().any():raise ImportError_('交易日历存在重复日期')
    start,end=min(dates),max(dates)
    expected=set(pd.date_range(start,end).strftime('%Y-%m-%d'))
    if not expected.issubset(set(calendar.date)):raise ImportError_('交易日历未覆盖行情起止日期之间的每一天')
    opens=set(calendar.loc[calendar.is_open.eq('1') & calendar.date.between(start,end),'date'])
    if opens!=set(dates):raise ImportError_('行情日期和交易日历不一致，请补齐缺失交易日或修正日历')
    return calendar


def import_csv(src, data_root: Path, name: str, original_name: str = "upload.csv", *,
               mapping=None, amount_unit="yuan", dry_run=False, calendar_raw=None) -> dict:
    if not NAME_RE.match(name):
        raise ImportError_("数据名称只能用字母、数字、下划线、连字符")
    dest = Path(data_root) / name
    if dest.exists():
        raise ImportError_(f"已经有叫 {name} 的数据了，换个名字")
    raw = src.read() if hasattr(src, "read") else Path(src).read_bytes()
    import io
    df = read(io.BytesIO(raw),mapping=mapping,amount_unit=amount_unit)
    bars, st, notes = to_bars(df)
    rep = validate_frame(bars, "bar")
    if not rep.ok:
        raise ImportError_("数据检查没通过：\n" + "\n".join(str(i) for i in rep.issues
                                                     if i.level == "ERROR"))
    years = sorted({int(x[:4]) for x in bars["date"].unique()})
    if len(years) < MIN_YEARS:
        raise ImportError_(f"只有 {len(years)} 年数据；滚动训练加测试至少需要 {MIN_YEARS} 个年份")
    calendar=None
    if calendar_raw is not None:
        calendar=calendar_frame(calendar_raw,bars.date)
    identity=raw if mapping is None and amount_unit=='yuan' else raw+json.dumps([mapping,amount_unit],sort_keys=True).encode()
    if calendar_raw is not None:identity+=calendar_raw
    snap = {"data_snapshot_id": "snap-" + hashlib.sha256(identity).hexdigest()[:16],
            "source": f"csv:{original_name}", "start": bars["date"].min(),
            "end": bars["date"].max(), "trading_days": int(bars["date"].nunique()),
            "codes": int(bars["code"].nunique()), "rows": int(len(bars)),
            "has_st": st is not None, "notes": notes,
            "readiness": data_readiness.csv_profile(bars, df.columns),
            "warnings": [str(i) for i in rep.issues if i.level != "ERROR"]}
    if dry_run:return snap
    (dest / "bars").mkdir(parents=True)
    for y in years:
        bars[bars["date"].str.startswith(str(y))].to_parquet(dest / "bars" / f"{y}.parquet",index=False)
    if st is not None:st.to_parquet(dest / "st_status.parquet",index=False)
    if calendar is not None:calendar.to_csv(dest/'calendar.csv',index=False)
    if mapping is not None:
        (dest/'csv_mapping.json').write_text(json.dumps({'columns':mapping,'amount_unit':amount_unit},ensure_ascii=False,indent=2),encoding='utf-8')
    (dest / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return snap
