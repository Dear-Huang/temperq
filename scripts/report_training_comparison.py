"""Paired development inference and independent raw-share qualification audit."""
from __future__ import annotations

import argparse
from dataclasses import replace
import itertools
import json
from pathlib import Path

from run_training_comparison import ROOT, Net, digest, load_data, write
from qwb import experiments, strategy, struct_costs
from qwb.portfolio.reference import Fees
from qwb.portfolio.struct_reference import execute_session
import numpy as np
import pandas as pd
import torch

ARMS = ["B0", "B0-cost", "B1", "B2", "B3", "B4", "B5"]
PAIRS = [(a, "B0") for a in ARMS[1:]] + [("B3", "B2"), ("B4", "B2"), ("B5", "B4"), ("B5", "B3")]


def utility(r, axis=0):
    return 252*(np.log1p(r).mean(axis=axis) - 5*np.var(r, axis=axis))


def raw_share_audit(d, case, arm):
    """Never pass an unexplained adjustment-factor change in raw-share accounting."""
    target_rows = json.loads((case / f"{arm}-targets.json").read_text())
    # Preserve the mandate with a conservative boundary reserve, as governance
    # replay already does, rather than loosening strict executor validation.
    target_rows = {day: {code: min(float(w), .1-1e-12) for code, w in weights.items()}
                   for day, weights in target_rows.items()}
    for weights in target_rows.values():
        total = sum(weights.values())
        if total > .95-1e-12:
            for code in weights:
                weights[code] *= (.95-1e-12)/total
    write(case / f"{arm}-raw-share-numerical-targets.json", target_rows)
    year = int(case.name[1:5])
    days = d["dates"][d["dates"].year == year]
    def at(day, hour, minute=0):
        return (day + pd.Timedelta(hours=hour, minutes=minute)).tz_localize("Asia/Shanghai")
    state = strategy.account(1e6, [], {}, observed_at=at(days[0], 15, 30),
                             snapshot_id="development_comparison", locked_until=at(days[1], 9, 30))
    pending = target_rows.get(str(days[0].date()))
    protocol = "training_comparison_raw_share_qualification_v1"
    costs = pd.DataFrame(d["spread"], index=d["dates"], columns=d["codes"])
    completed, fees_total = 0, 0.
    first_day = str(days[0].date())
    try:
        for i, day in enumerate(days[1:], 1):
            first_day = str(day.date())
            t = d["dates"].get_loc(day)
            assets = tuple(sorted(set(pending or {}) | {p.asset_id for p in state.positions}))
            for pos in state.positions:
                factors = d["p"]["adj_factor"][pos.asset_id].iloc[t-1:t+1].to_numpy()
                if not np.isfinite(factors).all() or not np.isclose(*factors, rtol=1e-10, atol=0):
                    raise ValueError("UNMODELLED_CORPORATE_ACTION_OR_MISSING_FACTOR")
            if assets:
                if pending is not None:
                    target, _, universe = strategy.target_for(state, pending, execution_at=at(day, 9, 30), recipe_hash=protocol)
                else:
                    target, universe = None, experiments.object_digest(assets)
                bars = []
                for code in assets:
                    c = d["codes"].get_loc(code)
                    values = {k: float(d["p"][k].at[day, code]) for k in ("open", "close", "up_limit", "down_limit")}
                    if not all(np.isfinite(v) and v > 0 for v in values.values()):
                        raise ValueError("MISSING_EXECUTION_OR_VALUATION_DATA")
                    bars.append(strategy.session_bar(code, open_price=values["open"], close_price=values["close"],
                        upper=values["up_limit"], lower=values["down_limit"], suspended=not d["normal"][t,c]))
                next_day = days[i+1] if i+1 < len(days) else day + pd.Timedelta(days=3)
                state, audit = execute_session(state, target, tuple(bars), open_at=at(day, 9, 30), close_at=at(day, 15),
                    next_open_at=at(next_day, 9, 30), decision_assets=assets, universe_id=universe,
                    protocol_id=protocol, fees=Fees(), max_weight=.1, min_cash=.05,
                    fees_by_asset=struct_costs.fees_for_day(costs, day, assets, Fees()))
                if audit["notices"]:
                    raise ValueError("UNEXPLAINED_PRICE_NOTICE")
                fees_total += audit["total_cost"]
            state = replace(state, observed_at=str(at(day, 15, 30)))
            pending = target_rows.get(str(day.date()))
            completed += 1
        return {"status": "completed_diagnostic_only", "completed_sessions": completed,
                "fees_until_stop": fees_total, "scope": "raw_share_lots_minimum_fees_T1_STRUCT"}
    except (ValueError, KeyError) as exc:
        return {"status": "failed_qualification", "completed_sessions": completed, "first_failed_date": first_day,
                "reason": str(exc), "fees_until_stop": fees_total,
                "no_dates_dropped": True, "no_corporate_actions_inferred": True}


def paired_inference(paths, stress, pairs=PAIRS):
    subset = paths[paths.stress_bp == stress]
    # Every job contributes exactly the same market dates, three seeds, all arms.
    counts = subset.groupby(["year", "date", "arm"]).seed.nunique()
    if not (counts == 3).all():
        raise ValueError("incomplete seed coverage")
    pivot = subset.groupby(["year", "date", "arm"]).net_return.mean().unstack("arm").reindex(columns=ARMS)
    if pivot.isna().any().any():
        raise ValueError("unpaired dates")
    blocks = [part.to_numpy() for _, part in pivot.groupby(level="year")]
    observed = utility(pivot.to_numpy())
    rng = np.random.default_rng(20261026)
    draws = np.empty((4000, len(ARMS)))
    for b in range(4000):
        sampled = []
        for block in blocks:
            starts = rng.integers(0, len(block), size=int(np.ceil(len(block)/20)))
            idx = np.concatenate([(s+np.arange(20)) % len(block) for s in starts])[:len(block)]
            sampled.append(block[idx])
        draws[b] = utility(np.concatenate(sampled))
    comparisons = []
    for a, b in pairs:
        ia, ib = ARMS.index(a), ARMS.index(b)
        values = draws[:, ia] - draws[:, ib]
        lo, hi = np.quantile(values, [.05/(2*len(pairs)), 1-.05/(2*len(pairs))])
        comparisons.append({"arm": a, "reference": b, "annual_utility_difference": float(observed[ia]-observed[ib]),
            "familywise_95_interval": [float(lo), float(hi)], "supported_positive": bool(lo > 0)})
    return comparisons


def verify_checkpoints(d, cases):
    import lightgbm as lgb
    verified = []
    torch.set_num_threads(2)
    for case in cases:
        archive = np.load(case / "predictions.npz", allow_pickle=False)
        days = d["dates"].get_indexer(pd.DatetimeIndex(archive["dates"]))
        samples = np.unique([0, len(days)//2, len(days)-1])
        x = torch.from_numpy(d["x"][days[samples]])
        valid = torch.from_numpy(d["u"][days[samples]])
        for arm in ("B2", "B3", "B4", "B5"):
            model = Net(x.shape[-1], arm in ("B4", "B5"))
            name = "prediction-checkpoint.pt" if arm in ("B2", "B4") else "utility-checkpoint.pt"
            model.load_state_dict(torch.load(case / arm / name, weights_only=True, map_location="cpu"))
            model.eval()
            with torch.no_grad():
                pred = model(x, valid).numpy()
            error = float(np.max(np.abs(pred-archive[arm][samples])))
            if not np.allclose(pred, archive[arm][samples], atol=1e-6, rtol=1e-5):
                raise ValueError(f"checkpoint prediction mismatch {case.name} {arm}")
            verified.append({"case": case.name, "arm": arm, "max_absolute_error": error})
        for j, h in enumerate((5, 10, 20)):
            tree = lgb.Booster(model_file=str(case / "trees" / f"tree-{h}.txt"))
            pred = tree.predict(x.numpy().reshape(-1, x.shape[-1]), num_threads=2).reshape(x.shape[:2])
            error = float(np.max(np.abs(pred-archive["B0"][samples, :, j])))
            if not np.allclose(pred, archive["B0"][samples, :, j], atol=1e-6, rtol=1e-5):
                raise ValueError(f"tree checkpoint mismatch {case.name} {h}")
            verified.append({"case": case.name, "arm": f"tree-{h}", "max_absolute_error": error})
    return verified


def summarize(root):
    registration = json.loads((root / "registration.json").read_text())
    identity_errors = []
    if registration["script_sha256"] != digest(ROOT / "scripts/run_training_comparison.py"):
        identity_errors.append("runner_changed")
    if registration["protocol_sha256"] != digest(ROOT / "docs/research/TRAINING_COMPARISON_PROTOCOL_20261026.md"):
        identity_errors.append("protocol_changed")
    for name, expected in registration["source_hashes"].items():
        if digest(ROOT / name) != expected:
            identity_errors.append(name)
    if identity_errors:
        raise ValueError(f"implementation identity changed: {identity_errors}")
    cases = sorted(root.glob("y????-s?"))
    if len(cases) != 9 or not all((p / "completed.json").exists() for p in cases):
        raise ValueError("all nine fold/seed cases must finish before declaring comparison complete")
    parts, rows, ics = [], [], []
    for case in cases:
        for arm in ARMS:
            for stress in (0, 10):
                path = pd.read_parquet(case / f"{arm}-slip{stress}-path.parquet")
                # First row is initial cash, not a daily return interval.
                parts.append(path.iloc[1:])
                row = json.loads((case / f"{arm}-slip{stress}-metrics.json").read_text())
                rows.append({"case": case.name, "arm": arm, "stress_bp": stress, **row})
            ics.append({"case": case.name, "arm": arm, **json.loads((case / f"{arm}-ic.json").read_text())})
    paths = pd.concat(parts, ignore_index=True)
    table = pd.DataFrame(rows)
    table.to_csv(root / "per-fold-seed-metrics.csv", index=False)
    pd.DataFrame(ics).to_csv(root / "per-fold-seed-ic.csv", index=False)
    paths.to_parquet(root / "all-daily-paths.parquet", index=False)
    estimates = []
    for (arm, stress), part in paths.groupby(["arm", "stress_bp"]):
        mean_path = part.groupby(["year", "date"])[["net_return", "equity", "cash", "holdings_value", "one_way_turnover"]].mean()
        r = mean_path.net_return.to_numpy()
        eq = np.cumprod(1+r)
        metrics = table[(table.arm == arm) & (table.stress_bp == stress)]
        estimates.append({"arm": arm, "stress_bp": int(stress), "utility_ann": float(utility(r)),
            "cagr": float(eq[-1]**(252/len(r))-1), "volatility_ann": float(r.std()*np.sqrt(252)),
            "max_drawdown": float((eq/np.maximum.accumulate(np.r_[1, eq])[1:]-1).min()),
            "mean_exposure": float((part.holdings_value/part.equity).mean()),
            "mean_daily_one_way_turnover": float(part.one_way_turnover.mean()),
            "mean_year_cost_yuan": float(metrics.total_cost.mean()),
            "unexplained_stale_close_name_days": int(metrics.unexplained_stale_close_name_days.sum()),
            "unexplained_data_gap_orders": int(metrics.unexplained_data_gap_orders.sum()),
            "mean_ic5": float(np.mean([r["5"] for r in ics if r["arm"] == arm])),
            "individual_seed_utility_ann": {str(seed): float(utility(part[part.seed == seed].sort_values(["year", "date"]).net_return.to_numpy())) for seed in (0, 1, 2)},
            "individual_year_utility_ann": {str(year): float(utility(mean_path.xs(year).net_return.to_numpy())) for year in (2021, 2022, 2023)}})
    pd.DataFrame(estimates).to_csv(root / "comparison.csv", index=False)
    inference = {str(stress): paired_inference(paths, stress) for stress in (0, 10)}
    all_pairs = [(b, a) for a, b in itertools.combinations(ARMS, 2)]
    attribution = {str(stress): paired_inference(paths, stress, all_pairs) for stress in (0, 10)}
    d = load_data()
    frozen_data = json.loads((root / "data.json").read_text())
    if d["hashes"] != frozen_data["hashes"]:
        raise ValueError("data changed since fitting")
    checkpoint_verification = verify_checkpoints(d, cases)
    write(root / "checkpoint-verification.json", checkpoint_verification)
    audits = {case.name: {arm: raw_share_audit(d, case, arm) for arm in ARMS} for case in cases}
    write(root / "exact-account-qualification.json", audits)
    base = sorted([r for r in estimates if r["stress_bp"] == 0], key=lambda r:r["utility_ann"], reverse=True)
    winner = base[0]["arm"]
    resources = [json.loads((p / "completed.json").read_text()) for p in cases]
    fine_tuning = {case.name: {arm: json.loads((case / arm / "utility-selection.json").read_text())["selected_epochs"]
                            for arm in ("B3", "B5")} for case in cases}
    result = {"scope": "2021_2023_development_240_hash_selected_names",
        "point_estimate_leader": winner, "qualified_economic_winner": None,
        "qualification": "not_certified_exact_account_or_fresh_holdout",
        "estimates": estimates, "paired_inference": inference,
        "supplemental_all_pairs_familywise_21": attribution, "fine_tuning_epochs": fine_tuning,
        "total_case_cpu_seconds": sum(r["cpu_seconds"] for r in resources),
        "total_case_wall_seconds": sum(r["wall_seconds"] for r in resources),
        "report_script_sha256": digest(Path(__file__)),
        "implementation_and_data_identity_verified": True,
        "checkpoints_verified": len(checkpoint_verification),
        "limitations": ["reused_development_history", "240_name_sample_not_full_market",
            "fractional_adjusted_unit_ledger", "no_minimum_commission_or_integer_lots_in_primary_paths",
            "STRUCT_coefficients_fitted_on_2021_2023_L2", "surrogate_execution_mismatch",
            "historical_vendor_revisions_and_PIT_not_certified",
            "no_fresh_2026_holdout_opened", "seed_mean_is_not_an_executed_ensemble"]}
    write(root / "result.json", result)
    lines = ["# B0–B5 actual training comparison", "", "Development sample: 240 hash-selected names, 2021–2023 rolling outer folds, three seeds.",
        "All six groups trained; B0-cost is an additional allocator control. No 2026 outcomes read.", "",
        "| Arm | Annual utility | CAGR | Volatility | Max drawdown | Exposure | Daily one-way turnover | IC5 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in base:
        lines.append(f"| {r['arm']} | {r['utility_ann']:.2%} | {r['cagr']:.2%} | {r['volatility_ann']:.2%} | {r['max_drawdown']:.2%} | {r['mean_exposure']:.2%} | {r['mean_daily_one_way_turnover']:.2%} | {r['mean_ic5']:.4f} |")
    lines += ["", "Annual utility is 252 * [mean(log(1+r)) - 5 * variance(r)], not CAGR.",
        "CAGR/drawdown use chained seed-mean daily returns with annual account resets; this is not a live account.", "",
        f"Point-estimate leader: {winner}. Qualified economic winner: none.", "",
        "## Paired comparisons", "", "Within-year 20-day circular blocks, 4000 draws; 95% familywise Bonferroni intervals for ten comparisons.", "",
        "| Arm minus reference | Annual utility difference | Corrected interval |", "| --- | ---: | ---: |"]
    for r in inference["0"]:
        lo, hi = r["familywise_95_interval"]
        lines.append(f"| {r['arm']} − {r['reference']} | {r['annual_utility_difference']:.2%} | [{lo:.2%}, {hi:.2%}] |")
    failures = sum(a["status"] == "failed_qualification" for case in audits.values() for a in case.values())
    lines += ["", "## Execution qualification", "", f"Independent raw-share STRUCT replays failed qualification in {failures}/63 cases. See exact-account-qualification.json for the first failure in each case.",
        "No dates were removed and no dividends/splits were inferred from adjustment factors.",
        "Raw-share audits use a documented 1e-12 conservative weight/cash boundary reserve; normalized targets are retained separately, and strict executor constraints are unchanged.",
        "Primary paths use the existing fractional adjusted-unit ledger. Integer lots, minimum commissions and exact T+1 are absent; unexplained stale marks and missing-price orders remain flagged.",
        "STRUCT uses previous-month inputs but coefficients estimated using 2021–2023 L2; costs are retrospective estimates.", "",
        "## Resources and selection", "", f"Actual case CPU time: {result['total_case_cpu_seconds']/3600:.3f} hours; sum of per-case wall times: {result['total_case_wall_seconds']/3600:.3f} hours (some jobs ran concurrently).",
        "Inner validation selects utility epoch including zero. See result.json and each utility-selection.json for zero selections, gradients and actual training action changes.",
        "The former GRU/QP was not repaired; B3/B5 use the preregistered exploratory piecewise top-k head.", "",
        "## Artifacts", "", "comparison.csv; per-fold-seed-metrics.csv; per-fold-seed-ic.csv; all-daily-paths.parquet; result.json; exact-account-qualification.json.",
        "Each case retains models, selection histories, calibration, predictions, targets, fills, paths and source/data identity.",
        "No default model, product workflow, real paper account or deployment was changed."]
    (root / "report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(json.dumps({"leader": winner, "qualified_winner": None, "cpu_hours": result["total_case_cpu_seconds"]/3600,
                      "estimates": [{k:v for k,v in r.items() if k in ("arm", "utility_ann", "cagr", "mean_ic5")} for r in base]}, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    summarize(p.parse_args().output)
