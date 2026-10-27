"""Stage-A power calculation and date-only holdout inventory; no 2026 outcomes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from statistics import NormalDist
import time

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[1]
DATE = re.compile(r"2026[-/]\d{2}[-/]\d{2}")
SKIP = {"environments", "site-packages", "releases", "node_modules", ".git", "__pycache__", "TemperQ"}


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def date_hits(value, pointer=""):
    if isinstance(value, dict):
        return [row for key, item in value.items() for row in date_hits(item, pointer + "/" + str(key))]
    if isinstance(value, list):
        return [row for i, item in enumerate(value) for row in date_hits(item, pointer + "/" + str(i))]
    if isinstance(value, str):
        return [{"field": pointer, "date": day} for day in sorted(set(DATE.findall(value)))]
    return []


def inventory():
    rows, unknown, examined = [], [], 0
    for base in (ROOT / "runs", ROOT / "models", ROOT / "artifacts"):
        paths = []
        for directory, children, files in os.walk(base):
            children[:] = [n for n in children if n not in SKIP and not n.startswith("便携") and not n.startswith("training-stage-a-")]
            paths.extend(Path(directory) / name for name in files)
        for path in paths:
            rel = path.relative_to(ROOT).as_posix()
            suffix = path.suffix.lower()
            if suffix == ".parquet" and any(s in path.name for s in ("prediction", "account_path", "daily_metric")):
                examined += 1
                try:
                    f = pq.ParquetFile(path)
                    names = f.schema.names
                    indices = [i for i, n in enumerate(names) if n in {"date", "decision_date", "trade_date"}]
                    bounds = []
                    for group in range(f.metadata.num_row_groups):
                        for i in indices:
                            stat = f.metadata.row_group(group).column(i).statistics
                            if stat is not None and stat.has_min_max:
                                bounds += [str(stat.min)[:10], str(stat.max)[:10]]
                    if not bounds:
                        unknown.append({"file": rel, "reason": "no_date_footer_statistics_no_rows_read"})
                    elif max(bounds) >= "2026-01-01":
                        rows.append({"file": rel, "role": "output_date_footer_only", "minimum": min(bounds), "maximum": max(bounds), "rows": f.metadata.num_rows})
                except Exception as e:
                    unknown.append({"file": rel, "reason": type(e).__name__})
            elif suffix in {".json", ".yaml", ".yml"} and any(s in path.name for s in ("config", "protocol", "fold", "manifest", "model", "training-run", "snapshot", "task", "record")):
                examined += 1
                if path.stat().st_size > 20_000_000:
                    unknown.append({"file": rel, "reason": "metadata_above_20MB_not_opened"})
                    continue
                try:
                    text = path.read_text(encoding="utf-8-sig")
                    hits = date_hits(json.loads(text)) if suffix == ".json" else [{"date": x, "field": "yaml_date_token_only"} for x in sorted(set(DATE.findall(text)))]
                    if hits:
                        rows.append({"file": rel, "role": "metadata_dates_only_not_evaluation_proof", "hits": hits[:100], "hit_count": len(hits)})
                except Exception as e:
                    unknown.append({"file": rel, "reason": type(e).__name__})
    return {"status": "QUARANTINED_NOT_CERTIFIED_CLEAN", "examined_files": examined, "date_hits": rows, "unresolved": unknown,
            "known_exposure": ["docs/specs/PRODUCT_WORKFLOW_SPEC.md: 2026-10-12 through 2026-10-22 replay", "docs/research/STRATEGY_WORKSPACE_ACCEPTANCE_20261023.md: 2026-10-09 through 2026-10-22 UI evaluation"],
            "limits": ["Footer dates and metadata only; no 2026 prediction or performance rows read.", "Bounded local inventory excludes environment, portable copies, release archives, external worktrees and remote/cloud history.", "Dates alone cannot establish whether human selection used the results.", "Do not certify January-September as clean without a complete use-history audit."]}


def series(path):
    footer = pq.ParquetFile(path)
    date_index = footer.schema.names.index("date")
    for i in range(footer.metadata.num_row_groups):
        stat = footer.metadata.row_group(i).column(date_index).statistics
        if stat is None or not stat.has_min_max or str(stat.max)[:10] > "2023-12-31":
            raise ValueError("power input lacks safe pre-2024 date bounds; no rows read")
    frame = pq.read_table(path).to_pandas()
    if pd.to_datetime(frame.date).max().year > 2023:
        raise ValueError("power input exceeds registered development years")
    sub = frame[(frame.strategy == "candidate") & (frame.scenario == "extra_0bp_per_side")].copy()
    sub["date"] = pd.to_datetime(sub.date)
    if sub.date.duplicated().any():
        raise ValueError("duplicate account dates")
    sub = sub.sort_values("date").set_index("date")
    # The initial cash state has no preceding return interval.
    return sub.iloc[1:]


def utility(r, penalty):
    return np.log1p(r).mean(axis=-1) - penalty * r.var(axis=-1)


def power(reg):
    base = ROOT / "artifacts/model-quality-20261022/bench"
    first_path, second_path = base / "selected-final/account_paths.parquet", base / "fixed-final/account_paths.parquet"
    a, b = series(first_path), series(second_path)
    if not a.index.equals(b.index):
        raise ValueError("paired account dates differ")
    if set(a.index.year) != {2021, 2022, 2023}:
        raise ValueError("registered fold coverage differs")
    cfg = reg["power"]
    x, y = a.net_return.to_numpy(float), b.net_return.to_numpy(float)
    if not np.isfinite([x, y]).all() or np.any(np.array([x, y]) <= -1):
        raise ValueError("invalid daily returns")
    z = NormalDist().inv_cdf(.975) + NormalDist().inv_cdf(.8)
    results = []
    for block in [cfg["primary_block_sessions"], *cfg["sensitivity_block_sessions"]]:
        rng = np.random.default_rng(cfg["seed"])
        samples = []
        for year in sorted(set(a.index.year)):
            members = np.flatnonzero(a.index.year == year)
            starts = rng.integers(0, len(members), size=(cfg["bootstrap_repetitions"], int(np.ceil(len(members) / block))))
            indices = (starts[..., None] + np.arange(block)) % len(members)
            samples.append(members[indices.reshape(len(starts), -1)[:, :len(members)]])
        ix = np.concatenate(samples, axis=1)
        for penalty in [cfg["primary_lambda"], *cfg["sensitivity_lambda"]]:
            ua = utility(x[ix], penalty)
            ub = utility(y[ix], penalty)
            paired = ua - ub
            se = paired.std(ddof=1)
            base_se = ua.std(ddof=1)
            annual_pp = cfg["annualization"] * 100
            mde = z * se * annual_pp
            results.append({"block_sessions": block, "lambda": penalty, "n": len(x),
                "baseline_annual_utility_pp": float(utility(x, penalty) * annual_pp),
                "proxy_pair_annual_utility_difference_pp": float((utility(x, penalty) - utility(y, penalty)) * annual_pp),
                "proxy_pair_mde80_annual_utility_pp": float(mde),
                "proxy_pair_percentile95_annual_utility_pp": (np.quantile(paired, [.025, .975]) * annual_pp).tolist(),
                "single_path_mde80_annual_utility_pp": float(z * base_se * annual_pp),
                "equal_volatility_correlation_scenarios": [{"rho": rho, "mde80_annual_utility_pp": float(z * base_se * (2 * (1 - rho))**.5 * annual_pp)} for rho in cfg["equal_volatility_paired_correlation_scenarios"]],
                "stationary_approx_sample_multiplier_for_2pp": float((mde / cfg["small_effect_annual_utility_pp"])**2)})
    return {"status": "DEVELOPMENT_DIAGNOSTIC_NOT_ECONOMIC_CERTIFICATION", "inputs": [{"file": p.relative_to(ROOT).as_posix(), "sha256": digest(p)} for p in (first_path, second_path)],
        "years": {str(yr): int((a.index.year == yr).sum()) for yr in sorted(set(a.index.year))}, "results": results,
        "decision": reg["power_decision"],
        "limitations": ["B0 alone does not identify covariance with future B3/B5; the old fixed-vs-selected pair is only a design proxy.", "Selected baseline is a 240-stock development subset with known valuation/data gaps, not a clean full-market B0 certification.", "MDE uses block bootstrap standard error and normal local-alternative approximation; includes neither retraining/search uncertainty nor multiple comparisons.", "Annual utility percentage points are not compounded annual return percentage points.", "Sample multiplier assumes stationarity and is illustrative, not a forecast of years required.", "No 2026 outcome data consumed."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.process_time()
    out = args.output.resolve()
    reg_path = out / "registration.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    if (out / "power.json").exists() or (out / "holdout-audit.json").exists():
        raise ValueError("preserve existing audit results; choose a new registered directory")
    identity = {"registration_sha256": digest(reg_path), "script_sha256": digest(Path(__file__)), "started_at": datetime.now(timezone.utc).isoformat()}
    write(out / ("identity-" + identity["script_sha256"][:8] + ".json"), identity)
    audit = inventory()
    write(out / "holdout-audit.json", audit)
    result = power(reg)
    write(out / "power.json", result)
    write(out / "preparation-resource.json", {"process_cpu_seconds": time.process_time() - started})
    print(json.dumps({"holdout_status": audit["status"], "examined": audit["examined_files"], "positive_files": len(audit["date_hits"]), "power_primary": result["results"][0], "years": result["years"]}, indent=2))


if __name__ == "__main__":
    main()
