"""Fetch A-share daily data into canonical tables, as a resumable job.

Usage:  python -X utf8 scripts/fetch_cn_daily.py --start 2018-01-02 --end 2025-12-31
Output (under data_dir from config.local.yaml):
    bars/<year>.parquet     canonical bar rows, full market
    st_status.parquet       canonical status rows (field=st)
    snapshot.json           data snapshot id and row counts
One unit per year; a retry skips finished years. Raw responses stay in the cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import qwb  # noqa: E402,F401  (import-order guard before pandas)
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from qwb.connectors import cn_a  # noqa: E402
from qwb.connectors.cache import Cache  # noqa: E402
from qwb.connectors.tushare import TushareClient, load_token  # noqa: E402
from qwb.formats import validate_frame  # noqa: E402
from qwb.jobs import JobSpec, Store  # noqa: E402


def job(ctx, cfg):
    local = yaml.safe_load((ROOT / "config.local.yaml").read_text(encoding="utf-8"))
    t = local["tushare"]
    cli = TushareClient(load_token(t["token_file"]), url=t["url"],
                        min_interval=t.get("min_interval", 0.25))
    cache = Cache(local["cache_dir"])
    out = Path(local["data_dir"])
    (out / "bars").mkdir(parents=True, exist_ok=True)

    days = cn_a.trading_days(cli, cache, cfg["start"], cfg["end"])
    years = sorted({d.year for d in days})
    st_frames = []
    for i, y in enumerate(years):
        ctx.check_cancel()
        yd = days[days.year == y]
        st_frames.append(cn_a.st_daily(cli, cache, yd))
        unit = f"bars_{y}"
        if not ctx.unit_done(unit):
            bars = cn_a.daily_bars(cli, cache, yd)
            rep = validate_frame(bars, "bar")
            if not rep.ok:
                raise RuntimeError(f"{y} bars failed validation:\n{rep}")
            bars.to_parquet(out / "bars" / f"{y}.parquet", index=False)
            ctx.log(f"{y}: {len(bars)} bars", warnings=[str(x) for x in rep.issues])
            ctx.mark_done(unit)
        ctx.progress(i + 1, len(years), str(y))

    if cfg.get("bars_only"):     # helper worker: fills cache and bars, leaves status to main
        return
    st = cn_a.st_status(pd.concat(st_frames, ignore_index=True), days)
    rep = validate_frame(st, "status")
    if not rep.ok:
        raise RuntimeError(f"st_status failed validation:\n{rep}")
    st.to_parquet(out / "st_status.parquet", index=False)
    snap = {"data_snapshot_id": cache.snapshot_id(), "start": cfg["start"], "end": cfg["end"],
            "trading_days": len(days), "st_intervals": len(st)}
    (out / "snapshot.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
    (ctx.artifacts / "snapshot.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
    ctx.log("done", **snap)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2018-01-02")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--retry", help="job id to resume")
    ap.add_argument("--bars-only", action="store_true",
                    help="parallel helper: write bars only, no st_status/snapshot")
    args = ap.parse_args()
    store = Store(ROOT / "runs" / "jobs")
    if args.retry:
        jid = args.retry
        store.recover(jid)
        store.retry(jid)
    else:
        jid = store.submit(JobSpec("fetch", {"start": args.start, "end": args.end,
                                             "bars_only": args.bars_only}))
    print("job", jid, flush=True)
    state = store.run(jid, job)
    print("state", state, store.status(jid).get("error") or "", flush=True)
    return 0 if state == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
