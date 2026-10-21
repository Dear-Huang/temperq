"""M1 pilot: ST universe + bars + rule pack on the live Tushare-compatible source.

Usage:  python -X utf8 scripts/m1_pilot.py [--start 2024-01-02 --end 2024-03-29]
Writes runs/m1_pilot/{report.md, summary.json, st_status.parquet, bars.parquet}.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qwb import rules  # noqa: E402
from qwb.connectors import cn_a  # noqa: E402
from qwb.connectors.cache import Cache  # noqa: E402
from qwb.connectors.tushare import TushareClient, load_token  # noqa: E402
from qwb.formats import validate_frame  # noqa: E402

# days on which a price-limit rule changes; checked in addition to the window
RULE_BOUNDARY_DAYS = ["2019-07-22", "2020-08-21", "2020-08-24", "2021-11-15"]


def limit_check(cli, cache, days, basic, st_days, pack) -> dict:
    """Compare rule-pack limit prices with the vendor's stk_limit."""
    bars = cn_a.daily_bars(cli, cache, days)
    bars = bars[bars["state"] == "normal"].copy()
    bars["date"] = pd.to_datetime(bars["date"])
    st_set = set(zip(st_days["code"], pd.to_datetime(st_days["date"])))
    listed = basic.set_index("code")["list_date"]
    cal = cn_a.trading_days(cli, cache, "2015-01-01", days.max())
    pos = {d: i for i, d in enumerate(cal)}

    def age(row):
        ld = listed.get(row.code)
        if pd.isna(ld):
            return 10 ** 6
        later = cal[cal >= ld]
        return pos[row.date] - pos[later[0]] if len(later) else 10 ** 6

    bars["age"] = [age(r) for r in bars[["code", "date"]].itertuples()]
    bars = bars[bars["age"] >= pack["skip_first_trading_days"]]
    bars["is_st"] = [(c, d) in st_set for c, d in zip(bars["code"], bars["date"])]
    bars["pct"] = [rules.limit_pct(c, s, d, pack)
                   for c, s, d in zip(bars["code"], bars["is_st"], bars["date"])]
    lp = [rules.limit_prices(c, s, d, p, pack) or (np.nan, np.nan)
          for c, s, d, p in zip(bars["code"], bars["is_st"], bars["date"], bars["pre_close"])]
    bars["exp_up"], bars["exp_dn"] = zip(*lp) if lp else ([], [])
    bars["ok"] = ((bars["exp_up"] - bars["up_limit"]).abs() < 0.0051) & \
                 ((bars["exp_dn"] - bars["down_limit"]).abs() < 0.0051)
    bars["board"] = bars["code"].map(rules.cn_board)
    by = bars.groupby(["board", "is_st"])["ok"].agg(["size", "mean"]).reset_index()
    bars = bars[bars["up_limit"].notna()]
    # Mismatches are labelled with the regime the rule pack does not encode.
    # Names come from stock_basic (current name), so the label is a hint for
    # the report, not a filter on the data.
    names = basic.set_index("code")["name"].astype("string")
    nm = bars["code"].map(names).fillna("")
    reason = np.select(
        [bars["up_limit"] >= 99999, nm.str.contains("退", regex=False),
         nm.str.startswith("S") & ~nm.str.contains("ST", regex=False)],
        ["no_limit_day", "delisting_now", "s_share"], default="unexplained")
    bars["reason"] = np.where(bars["ok"], "", reason)
    bad = bars[~bars["ok"]]
    special = bad
    return {
        "mismatch_reasons": special["reason"].value_counts().to_dict(),
        "mismatch_rows": int(len(bad)),
        "mismatch_by_date": bad["date"].dt.strftime("%Y-%m-%d").value_counts().to_dict(),
        "mismatch_names": {c: str(names.get(c, "")) for c in bad["code"].unique()[:20]},
        "rows": int(len(bars)),
        "match_rate": float(bars["ok"].mean()),
        "by_board_st": by.to_dict("records"),
        "mismatch_examples": bad[["code", "date", "reason", "is_st", "pre_close", "pct", "exp_up",
                                  "up_limit", "exp_dn", "down_limit"]]
        .head(15).astype(str).to_dict("records"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="2024-03-29")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.local.yaml").read_text(encoding="utf-8"))
    t = cfg["tushare"]
    cli = TushareClient(load_token(t["token_file"]), url=t["url"],
                        min_interval=t.get("min_interval", 0.25))
    cache = Cache(cfg["cache_dir"])
    out = ROOT / "runs" / "m1_pilot"
    out.mkdir(parents=True, exist_ok=True)
    pack = rules.load("cn_a")

    days = cn_a.trading_days(cli, cache, args.start, args.end)
    print(f"window {days[0].date()}..{days[-1].date()}: {len(days)} trading days", flush=True)
    basic = cn_a.stock_basic(cli, cache).rename(columns={"ts_code": "code"})

    # 1. ST universe: stock_st primary, namechange cross-check
    st_days = cn_a.st_daily(cli, cache, days)
    ev = cn_a.namechange_events(cli, cache, 1990, days[-1].year)
    nc_days = cn_a.st_daily_from_namechange(ev, days)
    # namechange can list stocks that are already delisted; restrict to listed ones
    alive = basic[["code", "list_date", "delist_date"]]
    nc_days = nc_days.merge(alive, on="code", how="left")
    nc_days = nc_days[(nc_days["list_date"] <= nc_days["date"]) &
                      (nc_days["delist_date"].isna() | (nc_days["date"] < nc_days["delist_date"]))]
    cc = cn_a.cross_check(st_days, nc_days[["code", "date"]])
    status = cn_a.st_status(st_days, days)
    rep_status = validate_frame(status, "status")
    print(f"ST: {cc['primary_days']} stock-days, agreement {cc['agreement']:.4f}", flush=True)

    # 2. bars for the ST universe
    bars = cn_a.daily_bars(cli, cache, days, universe=st_days)
    rep_bars = validate_frame(bars, "bar")
    states = bars["state"].value_counts().to_dict()
    print(f"bars: {len(bars)} rows, states {states}", flush=True)

    # 3. rule pack vs vendor limit prices: window sample + rule-boundary days
    sample = days[:: max(len(days) // 10, 1)]
    extra = cn_a.trading_days(cli, cache, "2019-07-01", "2021-12-31")
    extra = extra[extra.isin(pd.to_datetime(RULE_BOUNDARY_DAYS))]
    chk_days = sample.union(extra)
    st_chk = cn_a.st_daily(cli, cache, chk_days)
    lc = limit_check(cli, cache, chk_days, basic, st_chk, pack)
    print(f"limit rules: {lc['rows']} rows, match {lc['match_rate']:.4f}", flush=True)

    summary = {
        "window": [str(days[0].date()), str(days[-1].date())],
        "trading_days": len(days),
        "data_snapshot_id": cache.snapshot_id(),
        "st_cross_check": cc,
        "st_intervals": int(len(status)),
        "status_validation": [str(i) for i in rep_status.issues],
        "bars_rows": int(len(bars)),
        "bar_states": {k: int(v) for k, v in states.items()},
        "bar_validation": [str(i) for i in rep_bars.issues],
        "limit_check_days": [str(d.date()) for d in chk_days],
        "limit_check": lc,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                                 default=str), encoding="utf-8")
    status.to_parquet(out / "st_status.parquet", index=False)
    bars.to_parquet(out / "bars.parquet", index=False)
    print("snapshot", summary["data_snapshot_id"])
    print("status issues:", rep_status.issues and [str(i) for i in rep_status.issues])
    print("bar issues:", [str(i) for i in rep_bars.issues])
    return 0


if __name__ == "__main__":
    sys.exit(main())
