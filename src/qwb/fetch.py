"""Fetch A-share daily data from a Tushare-compatible API into a local dataset.

Used by scripts/fetch_cn_daily.py and the web UI. One resumable unit per
year; raw responses stay in the request cache, so a retry is cheap.
The token is passed in memory only: it is never written to the job spec,
the dataset or the logs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .connectors import cn_a
from .connectors.cache import Cache
from .connectors.tushare import TushareClient
from .formats import validate_frame


def make_job(token: str, url: str, cache_dir: Path, out_dir: Path, min_interval: float = 0.25):
    def job(ctx, cfg):
        cli = TushareClient(token, url=url, min_interval=min_interval)
        cache = Cache(cache_dir)
        out = Path(out_dir)
        (out / "bars").mkdir(parents=True, exist_ok=True)
        days = cn_a.trading_days(cli, cache, cfg["start"], cfg["end"])
        years = sorted({d.year for d in days})
        ctx.log(f"{len(days)} trading days, {years[0]}-{years[-1]}")
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
                ctx.log(f"{y}: {len(bars)} rows")
                ctx.mark_done(unit)
            ctx.progress(i + 1, len(years), str(y))
        if cfg.get("bars_only"):
            return
        st = cn_a.st_status(pd.concat(st_frames, ignore_index=True), days)
        rep = validate_frame(st, "status")
        if not rep.ok:
            raise RuntimeError(f"st_status failed validation:\n{rep}")
        st.to_parquet(out / "st_status.parquet", index=False)
        # index constituents for the CSI 300 / 500 / 1000 universes; optional, a
        # missing permission only disables those universes
        try:
            frames = [cn_a.index_members(cli, cache, code, days[0], days[-1])
                      for code in cn_a.INDEXES.values()]
            pd.concat(frames, ignore_index=True).to_parquet(out / "index_members.parquet",
                                                            index=False)
            ctx.log("index constituents: CSI 300 / 500 / 1000")
        except Exception as e:
            ctx.log(f"index constituents skipped ({type(e).__name__}); "
                    "index universes will not be available for this dataset")
        snap = {"data_snapshot_id": cache.snapshot_id(), "source": f"tushare:{url}",
                "start": str(days[0].date()), "end": str(days[-1].date()),
                "trading_days": len(days), "st_intervals": len(st), "has_st": True}
        (out / "snapshot.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
        (ctx.artifacts / "snapshot.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
        ctx.log("done")
    return job


def make_factor_job(token: str, url: str, cache_dir: Path, combined_dir: Path, apis: list[str],
                    min_interval: float = 0.25):
    """Pull built-in factor sources over the dataset's trading days and add each as a source.

    Values from these endpoints are published after the close of their date, so
    the source rule is "evening" (usable for that day's after-close decision).
    """
    from . import dataset

    def job(ctx, cfg):
        cli = TushareClient(token, url=url, min_interval=min_interval)
        cache = Cache(cache_dir)
        cal = dataset.calendar(combined_dir)
        for api in apis:
            frames = []
            for i, d in enumerate(cal):
                ctx.check_cancel()
                frames.append(cn_a.factor_frame(cli, cache, api, d))
                if i % 20 == 0 or i == len(cal) - 1:
                    ctx.progress(i + 1, len(cal), f"{api} {d:%Y-%m-%d}")
            df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
            if df.empty:
                raise RuntimeError(f"{api} returned no rows")
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")
            src = dataset.add_factor(combined_dir, df, "evening", api, f"tushare:{api}")
            ctx.log(f"{api}: {src['rows']} rows, columns {', '.join(src['columns'])}")
    return job
