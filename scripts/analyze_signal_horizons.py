"""Research-only multi-horizon signal report with explicit label maturity."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from statistics import NormalDist
import time

from qwb import daily, metrics

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


def analyze(panel, scores, *, cutoff, horizons=(1, 5, 10, 20)):
    if not horizons or tuple(sorted(set(horizons))) != tuple(horizons) or any(type(h) is not int or h < 1 for h in horizons):
        raise ValueError("horizons must be increasing unique positive integers")
    cutoff = pd.Timestamp(cutoff)
    opened, factor = panel["open"], panel["adj_factor"]
    if opened.index.has_duplicates or not opened.index.is_monotonic_increasing or opened.columns.has_duplicates:
        raise ValueError("unique sorted trading calendar and stock codes required")
    if not factor.index.equals(opened.index) or not factor.columns.equals(opened.columns):
        raise ValueError("adjustment factors are not aligned")
    if scores.index.has_duplicates or scores.columns.has_duplicates:
        raise ValueError("duplicate signal dates or stock codes")
    if not scores.index.isin(opened.index).all() or not scores.columns.isin(opened.columns).all():
        raise ValueError("signal dates/codes absent from price panel")
    dates = opened.index[opened.index <= cutoff]
    prices = {"open": opened.loc[dates], "adj_factor": factor.loc[dates]}
    aligned = scores.reindex(index=dates, columns=opened.columns).to_numpy(dtype=float, copy=True)
    aligned[~np.isfinite(aligned)] = np.nan
    intervals = [("cumulative", 0, h) for h in horizons]
    intervals += [("incremental", a, b) for a, b in zip((0, *horizons[:-1]), horizons)]
    labels = [daily.labels(prices, {"label": {"start": {"day": a + 1, "price": "open"}, "end": {"day": b + 1, "price": "open"}}}).to_numpy(float) for _, a, b in intervals]
    common = np.isfinite(aligned) & np.logical_and.reduce([np.isfinite(y) for y in labels])
    z = NormalDist().inv_cdf(1 - .05 / (2 * len(intervals)))
    reports, paths = [], pd.DataFrame({"date": dates})
    for (kind, start, end), label in zip(intervals, labels):
        valid = np.isfinite(aligned) & np.isfinite(label)
        for coverage, mask in (("available", valid), ("common", common)):
            values = metrics.rank_ic(np.where(mask, aligned, np.nan), np.where(mask, label, np.nan))
            stat = metrics.newey_west(values, lag=end)
            ci = [stat["mean"] - z * stat["se"], stat["mean"] + z * stat["se"]] if np.isfinite(stat["se"]) else None
            by_year = {str(year): metrics.newey_west(np.where(dates.year == year, values, np.nan), lag=end) for year in sorted(set(dates.year))}
            reports.append({"kind": kind, "from_holding_day": start, "to_holding_day": end, "coverage": coverage,
                "label_start_trading_offset": start + 1, "label_end_trading_offset": end + 1,
                "statistics": stat, "familywise95_interval": ci, "paired_stock_dates": int(mask.sum()), "by_year": by_year})
            paths[f"{kind}_{start}_{end}_{coverage}"] = values
    report = {"schema": "signal_horizons_research_v1", "status": "DIAGNOSTIC_ONLY", "cutoff": str(cutoff.date()),
        "horizons": list(horizons), "family_size": len(intervals), "ci_method": "Newey_West_normal_Bonferroni_per_coverage_family",
        "timing": "decision close; next-session adjusted open through holding-horizon exit adjusted open",
        "series": reports,
        "limitations": ["Input signal OOS lineage must be established separately; this function does not certify model training.",
            "Common coverage conditions on all future labels being observable; available coverage is also reported.",
            "A positive cumulative 20-day IC can come entirely from the first days; inspect incremental intervals.",
            "This is historical cross-sectional association, not an individual stock lifetime or expected return estimate.",
            "No transaction-cost coverage claim, executable buy advice, or model promotion.",
            "Adjusted-price labels are not a corporate-action execution ledger.",
            "No future prices after the declared cutoff enter labels."]}
    return report, paths


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return None
    return value


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_date_bound(path, column, cutoff):
    footer = pq.ParquetFile(path)
    col = footer.schema.names.index(column)
    for i in range(footer.metadata.num_row_groups):
        stat = footer.metadata.row_group(i).column(col).statistics
        if stat is None or not stat.has_min_max or str(stat.max)[:10] > str(pd.Timestamp(cutoff).date()):
            raise ValueError("unsafe or unknown date bounds; rows not opened")


def main():
    cpu_started = time.process_time()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2023)
    args = parser.parse_args()
    if not 2010 <= args.start_year <= args.end_year <= 2023:
        parser.error("this Stage-A command only permits historical development through 2023")
    args.output.mkdir(exist_ok=False, parents=True)
    cutoff = pd.Timestamp(args.end_year, 12, 31)
    # Inspect all date bounds before opening any prediction rows.
    require_date_bound(args.predictions, "decision_time", cutoff)
    sources = [args.data / "bars" / f"{year}.parquet" for year in range(args.start_year, args.end_year + 1)]
    for source in sources:
        require_date_bound(source, "date", cutoff)
    plan = {"created_at": datetime.now(timezone.utc).isoformat(), "cutoff": str(cutoff.date()), "horizons": [1, 5, 10, 20],
        "prediction_sha256": sha(args.predictions), "script_sha256": sha(Path(__file__)), "price_partitions": [{"year": p.stem, "sha256": sha(p)} for p in sources],
        "outcomes": "development_rank_ic_only_no_new_model_fitting_no_2026_reads", "stock_specific_lifetime_claim": False}
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    (args.output / "analyzer-source.py").write_bytes(Path(__file__).read_bytes())
    predictions = pq.read_table(args.predictions).to_pandas()
    predictions["date"] = pd.to_datetime(predictions.decision_time, utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    scores = predictions.pivot(index="date", columns="code", values="prediction").sort_index()
    frames = [pq.read_table(p, columns=["code", "date", "open", "adj_factor"], filters=[("code", "in", scores.columns.tolist())]).to_pandas() for p in sources]
    prices = pd.concat(frames, ignore_index=True)
    prices["date"] = pd.to_datetime(prices.date)
    if prices.date.max() > cutoff or prices.duplicated(["code", "date"]).any():
        raise ValueError("price date guard or unique revision requirement failed")
    panel = {name: prices.pivot(index="date", columns="code", values=name).sort_index() for name in ("open", "adj_factor")}
    report, paths = analyze(panel, scores, cutoff=cutoff)
    report["input_scope"] = {"signal_rows": len(predictions), "codes": len(scores.columns), "price_rows": len(prices), "prediction_sha256": plan["prediction_sha256"], "source": "local_historical_snapshot", "price_source_matches_training_snapshot": "not_certified_by_this_command"}
    report["process_cpu_seconds_after_imports"] = time.process_time() - cpu_started
    (args.output / "report.json").write_text(json.dumps(clean(report), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    paths.to_csv(args.output / "daily_ic.csv", index=False)
    table = pd.DataFrame([{**{k: r[k] for k in ("kind", "from_holding_day", "to_holding_day", "coverage")}, **r["statistics"], "ci_low": r["familywise95_interval"][0] if r["familywise95_interval"] else None, "ci_high": r["familywise95_interval"][1] if r["familywise95_interval"] else None} for r in report["series"]])
    table.to_csv(args.output / "summary.csv", index=False)
    print(table[table.coverage == "common"].to_string(index=False))


if __name__ == "__main__":
    main()
