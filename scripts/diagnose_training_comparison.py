"""No-fit turnover matching and cost-head audits on frozen development predictions."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import time

import run_training_comparison as r
from qwb.bench import account
import numpy as np
import pandas as pd
import torch

ALPHAS = (1., .9, .8, .7, .6, .5, .4, .3, .2, .15, .1, .075, .05, .025, .01)
ARMS = ("B0", "B0-matched", "B2", "tie-equal", "soft-no-penalty", "penalty-equal", "B0-cost")
PAIRS = (("B2", "B0"), ("B0-matched", "B0"), ("B2", "B0-matched"),
         ("tie-equal", "B0"), ("soft-no-penalty", "tie-equal"),
         ("penalty-equal", "tie-equal"), ("B0-cost", "soft-no-penalty"))


def equal_weights(ids, n):
    w = np.zeros(n)
    w[ids] = .95/len(ids)
    if w.sum() > .95:
        w *= .95/w.sum()
    return w


def smooth_targets(pred, eligible, spread, test, alpha):
    """Causal EWMA of daily percentile ranks; current eligibility always wins."""
    rank = pd.DataFrame(pred).where(eligible[test]).rank(axis=1, pct=True).to_numpy()
    state = np.full(rank.shape[1], np.nan)
    targets = {}
    for i, t in enumerate(test):
        current = rank[i]
        valid = np.isfinite(current)
        initialized = valid & np.isfinite(state)
        state[initialized] = alpha*current[initialized] + (1-alpha)*state[initialized]
        state[valid & ~initialized] = current[valid & ~initialized]
        # A missing observation breaks that stock's smoothing history.
        state[~valid] = np.nan
        if i % 5 == 0:
            allowed = valid & np.isfinite(spread[t])
            ids = np.flatnonzero(allowed)
            ids = ids[np.argsort(-state[ids], kind="stable")[:20]]
            if len(ids) != 20:
                raise ValueError("insufficient eligible names")
            targets[int(t)] = equal_weights(ids, rank.shape[1])
    return targets


def head_targets(pred, cal, d, test, mode):
    forecast = r.calibrated(pred, cal)
    previous = np.zeros(len(d["codes"]), dtype="float32")
    targets = {}
    for i in range(0, len(test), 5):
        t = int(test[i])
        valid = d["u"][t] & np.isfinite(forecast[i]) & np.isfinite(d["spread"][t])
        score = torch.tensor(forecast[i], dtype=torch.float32)
        spread = torch.tensor(np.nan_to_num(d["spread"][t], nan=100), dtype=torch.float32)
        prev = torch.from_numpy(previous)
        if mode in ("tie-equal", "soft-no-penalty"):
            # Penalty off is an allocator ablation. Ledger fees remain unchanged.
            prev = torch.ones_like(prev)
        penalty = (.0003+spread/10000+.0005)*(prev <= 1e-8)
        value = (score*5-penalty).masked_fill(~torch.from_numpy(valid), -1e9)
        ids = torch.topk(value, 20).indices.numpy()
        if mode == "soft-no-penalty":
            with torch.no_grad():
                w = r.torch_weights(score, torch.from_numpy(valid), prev, spread).numpy().astype(float)
        else:
            w = equal_weights(ids, len(previous))
        w = np.minimum(w, .1)
        if w.sum() > .95:
            w *= .95/w.sum()
        targets[t] = w
        previous = w.astype("float32")
    return targets


def replay(d, targets, test, stress):
    return account.replay(d["p"], d["normal"], d["spread"], targets, start=int(test[0]), end=int(test[-1]),
                          initial_cash=1e6, commission_bp=3., extra_slippage_bp=float(stress))


def path_metrics(path):
    x = path.iloc[1:]
    ret = x.net_return.to_numpy()
    eq = np.cumprod(1+ret)
    return {"utility_ann": float(252*(np.log1p(ret).mean()-5*np.var(ret))),
            "cagr": float(eq[-1]**(252/len(ret))-1),
            "volatility_ann": float(ret.std()*np.sqrt(252)),
            "mean_turnover": float(x.one_way_turnover.mean()),
            "mean_exposure": float((x.holdings_value/x.equity).mean())}


def paired(paths):
    result = {}
    for stress in (0, 10):
        frame = paths[paths.stress_bp == stress]
        counts = frame.groupby(["year", "date", "arm"]).seed.nunique()
        if not counts.eq(3).all():
            raise ValueError("incomplete seeds")
        pivot = frame.groupby(["year", "date", "arm"]).net_return.mean().unstack("arm").reindex(columns=ARMS)
        if pivot.isna().any().any():
            raise ValueError("unpaired dates")
        def utility(a):
            return 252*(np.log1p(a).mean(axis=0)-5*np.var(a, axis=0))
        observed = utility(pivot.to_numpy())
        blocks = [g.to_numpy() for _, g in pivot.groupby(level="year")]
        rng = np.random.default_rng(20261026)
        draws = np.empty((4000, len(ARMS)))
        for k in range(4000):
            sampled = []
            for block in blocks:
                starts = rng.integers(0, len(block), int(np.ceil(len(block)/20)))
                idx = np.concatenate([(s+np.arange(20)) % len(block) for s in starts])[:len(block)]
                sampled.append(block[idx])
            draws[k] = utility(np.concatenate(sampled))
        contrasts = []
        for a, b in PAIRS:
            ia, ib = ARMS.index(a), ARMS.index(b)
            delta = draws[:, ia]-draws[:, ib]
            contrasts.append({"arm": a, "reference": b, "annual_utility_difference": float(observed[ia]-observed[ib]),
                "pointwise_95_interval": np.quantile(delta, [.025, .975]).tolist(),
                "familywise_95_interval": np.quantile(delta, [.05/(2*len(PAIRS)), 1-.05/(2*len(PAIRS))]).tolist(),
                "conditional_on_posthoc_turnover_match": a == "B0-matched" or b == "B0-matched"})
        result[str(stress)] = contrasts
    return result


def audit_fees(case, d):
    """Independent Decimal fees plus cash/equity reconciliation; no outcomes edited."""
    rows, examples = [], []
    for arm in ("B0", "B0-cost", "B1", "B2"):
        for stress in (0, 10):
            fills = pd.read_parquet(case / f"{arm}-slip{stress}-fills.parquet")
            path = pd.read_parquet(case / f"{arm}-slip{stress}-path.parquet")
            datepos = d["dates"].get_indexer(pd.DatetimeIndex(fills.date))
            codepos = d["codes"].get_indexer(fills.code)
            spread = d["spread"][datepos, codepos]+stress
            stamp = np.where(pd.to_datetime(fills.date) < pd.Timestamp("2023-08-28"), 10., 5.)
            expected_comm = fills.notional.to_numpy()*3/10000
            expected_tax = fills.notional.to_numpy()*stamp*(fills.side == "SELL").to_numpy()/10000
            expected_slip = fills.notional.to_numpy()*spread/10000
            fee_error = float(np.max(np.abs(expected_comm+expected_tax+expected_slip-fills.total_cost.to_numpy()), initial=0))
            if fee_error > 1e-8:
                raise ValueError("fill fee mismatch")
            flow = np.where(fills.side == "SELL", fills.notional, -fills.notional)-fills.total_cost
            flows = pd.Series(np.asarray(flow), index=pd.DatetimeIndex(fills.date)).groupby(level=0).sum()
            fees = fills.groupby("date").total_cost.sum()
            cash_error = float(np.max(np.abs(path.cash.diff().fillna(0).to_numpy()-flows.reindex(path.date, fill_value=0).to_numpy())))
            day_fee_error = float(np.max(np.abs(path.total_cost.to_numpy()-fees.reindex(path.date, fill_value=0).to_numpy())))
            if max(cash_error, day_fee_error) > 1e-6:
                raise ValueError("cash or daily fee reconciliation failed")
            rows.append({"case": case.name, "arm": arm, "stress_bp": stress,
                "max_fill_fee_error_yuan": fee_error, "max_cash_flow_error_yuan": cash_error,
                "max_daily_fee_error_yuan": day_fee_error,
                "minimum_commission_shortfall_diagnostic_yuan": float((np.maximum(5.,expected_comm)-expected_comm)[fills.notional.to_numpy()>0].sum()),
                "total_cost_yuan": float(fills.total_cost.sum())})
            # Fixed samples: first buy/sell, and each side of the statutory tax cut.
            if stress == 0 and arm == "B0-cost":
                criteria = [("first_buy", (fills.side == "BUY") & (fills.notional > 0)),
                            ("first_sell", (fills.side == "SELL") & (fills.notional > 0))]
                if case.name.startswith("y2023"):
                    criteria += [("pre_tax_cut_sell", (fills.side == "SELL") & (fills.notional > 0) & (fills.date < pd.Timestamp("2023-08-28"))),
                                 ("post_tax_cut_sell", (fills.side == "SELL") & (fills.notional > 0) & (fills.date >= pd.Timestamp("2023-08-28")))]
                for label, mask in criteria:
                    selected = fills.loc[mask]
                    if selected.empty:
                        continue
                    row = selected.iloc[-1] if label == "pre_tax_cut_sell" else selected.iloc[0]
                    j = fills.index.get_loc(row.name)
                    notional = Decimal(str(row.notional))
                    comm = notional*Decimal(3)/Decimal(10000)
                    tax_bp = 0 if row.side == "BUY" else (10 if row.date < pd.Timestamp("2023-08-28") else 5)
                    tax = notional*Decimal(tax_bp)/Decimal(10000)
                    slip = notional*Decimal(str(spread[j]))/Decimal(10000)
                    total = comm+tax+slip
                    if abs(float(total)-row.total_cost) > 1e-8:
                        raise ValueError("manual Decimal fee mismatch")
                    examples.append({"case":case.name, "example":label, "date":str(row.date.date()), "code":row.code,
                        "side":row.side, "notional_yuan":float(notional), "commission_bp":3,
                        "stamp_tax_bp":tax_bp, "half_spread_bp":float(spread[j]),
                        "manual_commission":str(comm), "manual_stamp_tax":str(tax), "manual_spread":str(slip),
                        "manual_total":str(total), "recorded_total":float(row.total_cost)})
    return rows, examples


def numpy_capped_weights(logits, ids):
    """Independent float64 water filling for an already selected basket."""
    z = logits[ids].astype(float)
    w = np.exp(z-z.max())
    fixed = np.zeros(len(ids), dtype=bool)
    for _ in range(len(ids)):
        free = ~fixed
        proposed = np.zeros(len(ids))
        proposed[free] = w[free]/w[free].sum()*(.95-.1*fixed.sum())
        newly = free & (proposed > .1)
        if not newly.any():
            proposed[fixed] = .1
            return proposed
        fixed |= newly
    raise ValueError("water filling did not converge")


def audit_head(case, pred, cal, d, test):
    targets_json = json.loads((case / "B0-cost-targets.json").read_text())
    forecast = r.calibrated(pred, cal)
    previous = np.zeros(len(d["codes"]), dtype="float32")
    holdings = pd.read_parquet(case / "B0-cost-slip0-holdings.parquet")
    held_by_date = holdings[holdings.market_value > .01].groupby("date").code.agg(set).to_dict()
    audit, examples = [], []
    for i in range(0, len(test), 5):
        t = test[i]; day = d["dates"][t]
        valid = d["u"][t] & np.isfinite(forecast[i]) & np.isfinite(d["spread"][t])
        score = np.asarray(forecast[i], dtype="float32")
        spread = np.nan_to_num(d["spread"][t], nan=100).astype("float32")
        penalty = (np.float32(.0003)+spread/np.float32(10000)+np.float32(.0005))*(previous <= 1e-8)
        value = score*np.float32(5)-penalty
        scale = max(float(np.std(value[valid], ddof=0, dtype=np.float32)), 1e-5)*.2
        value[~valid] = -1e9
        # Use the original head's top-k tie order, then independently audit math.
        ids = torch.topk(torch.from_numpy(value), 20).indices.numpy()
        expected = np.zeros(len(previous)); expected[ids] = numpy_capped_weights(value/scale, ids)
        actual = np.zeros(len(previous))
        for code, w in targets_json[str(day.date())].items():
            actual[d["codes"].get_loc(code)] = w
        error = float(np.max(np.abs(expected-actual)))
        if error > 2e-5:
            raise ValueError(f"head formula mismatch {case.name} {day.date()}: {error}")
        known_held = held_by_date.get(day, set())
        intended = {d["codes"][j] for j in np.flatnonzero(previous > 1e-8)}
        forecast_std = float(np.std(score[valid]*5))
        audit.append({"case":case.name, "date":str(day.date()), "weight_formula_max_error":error,
            "forecast_5d_cross_section_std_bp":forecast_std*10000,
            "median_new_name_penalty_bp":float(np.median((.0003+spread[valid]/10000+.0005)*10000)),
            "penalty_to_forecast_std":float(np.median(penalty[valid])/max(forecast_std,1e-12)),
            "effective_names":float(actual.sum()**2/np.dot(actual,actual)), "max_weight":float(actual.max()),
            "intended_but_not_materially_held":len(intended-known_held),
            "materially_held_but_not_intended":len(known_held-intended),
            "fixed_half_stamp_penalty_bp":5.,
            "half_execution_date_sell_tax_bp":5. if day < pd.Timestamp("2023-08-28") else 2.5})
        if (case.name, i) in (("y2021-s0", 0), ("y2021-s0", 5), ("y2022-s0", 0), ("y2023-s0", 0)):
            for j in ids[:5]:
                examples.append({"case":case.name, "date":str(day.date()), "code":str(d["codes"][j]),
                    "raw_rank_prediction":float(pred[i,j,0]), "validation_return_per_score_slope":cal["slopes"][0],
                    "forecast_daily_return":float(score[j]), "forecast_5d_return_bp":float(score[j]*5*10000),
                    "previous_intended_weight":float(previous[j]), "entry_penalty_bp":float(penalty[j]*10000),
                    "net_selection_value_bp":float(value[j]*10000), "manual_weight":float(expected[j]),
                    "recorded_weight":float(actual[j])})
        previous = actual.astype("float32")
    return audit, examples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    started, cpu = time.perf_counter(), time.process_time()
    r.write(args.output / "registration.json", {"status":"frozen_before_diagnostic_outcomes",
        "scope":"posthoc_diagnostic_not_new_training_or_independent_confirmation", "alphas":ALPHAS,
        "matching_endpoint":"case_mean_realized_daily_one_way_turnover_only", "target":"saved_B2_path",
        "match_tolerance_absolute":.002, "tie_break":"minimum_abs_gap_then_larger_alpha",
        "matching_uses_full_outer_fold_turnover":True, "not_a_deployable_parameter_selection":True,
        "arms":ARMS, "pairs":PAIRS, "blocks":20, "draws":4000, "risk_penalty":5,
        "source_result_sha256":r.digest(args.source / "result.json"),
        "runner_sha256":r.digest(Path(r.__file__)), "script_sha256":r.digest(Path(__file__)),
        "registered_at":pd.Timestamp.now(tz="UTC"), "new_fits":0, "read_2026_outcomes":False})
    torch.set_num_threads(2)
    d = r.load_data()
    frozen = json.loads((args.source / "data.json").read_text())
    if frozen["hashes"] != d["hashes"]:
        raise ValueError("source data identity changed")
    all_paths, matches, candidates, fee_checks, fee_examples, head_checks, head_examples = [], [], [], [], [], [], []
    for case in sorted(args.source.glob("y????-s?")):
        if not (case / "completed.json").exists():
            raise ValueError("incomplete source case")
        year, seed = int(case.name[1:5]), int(case.name[-1])
        out = args.output / case.name; out.mkdir()
        archive = np.load(case / "predictions.npz", allow_pickle=False)
        test = d["dates"].get_indexer(pd.DatetimeIndex(archive["dates"]))
        if (test < 0).any():
            raise ValueError("prediction dates outside development panel")
        pred = archive["B0"]
        cal = json.loads((case / "B0-cost-calibration.json").read_text())
        target_turnover = float(pd.read_parquet(case / "B2-slip0-path.parquet").one_way_turnover.iloc[1:].mean())
        best = None
        for alpha in ALPHAS:
            targets = smooth_targets(pred[...,0], d["u"], d["spread"], test, alpha)
            path, _, _ = replay(d, targets, test, 0)
            turnover = float(path.one_way_turnover.iloc[1:].mean())
            gap = abs(turnover-target_turnover)
            candidates.append({"case":case.name, "alpha":alpha, "actual_turnover":turnover,
                               "target_turnover":target_turnover, "absolute_gap":gap})
            if best is None or (gap, -alpha) < (best[0], -best[1]):
                best = gap, alpha, targets
        gap, alpha, targets = best
        matches.append({"case":case.name, "alpha":alpha, "target_turnover":target_turnover,
                        "absolute_gap":gap, "matched_within_tolerance":bool(gap <= .002)})
        selected = {"B0-matched":targets}
        for arm in ("tie-equal", "soft-no-penalty", "penalty-equal"):
            selected[arm] = head_targets(pred, cal, d, test, arm)
        for arm, targets in selected.items():
            r.write(out / f"{arm}-targets.json", {str(d["dates"][t].date()):dict(zip(d["codes"][w>0], w[w>0].tolist())) for t,w in targets.items()})
            for stress in (0, 10):
                path, fills, holdings = replay(d, targets, test, stress)
                path = path.assign(arm=arm, seed=seed, year=year, stress_bp=stress)
                path.to_parquet(out / f"{arm}-slip{stress}-path.parquet", index=False)
                fills.to_parquet(out / f"{arm}-slip{stress}-fills.parquet", index=False)
                all_paths.append(path.iloc[1:])
        for arm in ("B0", "B2", "B0-cost"):
            for stress in (0, 10):
                all_paths.append(pd.read_parquet(case / f"{arm}-slip{stress}-path.parquet").iloc[1:])
        rows, examples = audit_fees(case, d); fee_checks += rows; fee_examples += examples
        rows, examples = audit_head(case, pred, cal, d, test); head_checks += rows; head_examples += examples
        r.log(f"DIAG {case.name} alpha={alpha} turnover_gap={gap:.6f}")
    paths = pd.concat(all_paths, ignore_index=True)
    paths.to_parquet(args.output / "daily-paths.parquet", index=False)
    pd.DataFrame(matches).to_csv(args.output / "turnover-matches.csv", index=False)
    pd.DataFrame(candidates).to_csv(args.output / "turnover-candidates.csv", index=False)
    pd.DataFrame(fee_checks).to_csv(args.output / "fee-reconciliation.csv", index=False)
    pd.DataFrame(head_checks).to_csv(args.output / "head-audit.csv", index=False)
    r.write(args.output / "manual-fee-examples.json", fee_examples)
    r.write(args.output / "manual-head-examples.json", head_examples)
    estimates = []
    for (arm, stress), part in paths.groupby(["arm", "stress_bp"]):
        means = part.groupby(["year", "date"]).net_return.mean()
        ret = means.to_numpy(); eq = np.cumprod(1+ret)
        estimates.append({"arm":arm, "stress_bp":int(stress),
            "utility_ann":float(252*(np.log1p(ret).mean()-5*np.var(ret))),
            "cagr":float(eq[-1]**(252/len(ret))-1), "volatility_ann":float(ret.std()*np.sqrt(252)),
            "mean_turnover":float(part.one_way_turnover.mean()),
            "mean_exposure":float((part.holdings_value/part.equity).mean())})
    inference = paired(paths)
    result = {"state":"completed", "new_fits":0, "read_2026_outcomes":False,
        "estimates":estimates, "paired_inference":inference, "matches":matches,
        "all_matches_within_tolerance":all(v["matched_within_tolerance"] for v in matches),
        "fee_audit_max_error_yuan":max(v["max_fill_fee_error_yuan"] for v in fee_checks),
        "head_weight_max_error":max(v["weight_formula_max_error"] for v in head_checks),
        "head_effective_names_median":float(np.median([v["effective_names"] for v in head_checks])),
        "cpu_seconds":time.process_time()-cpu, "wall_seconds":time.perf_counter()-started,
        "limitations":["posthoc_turnover_matching_conditions_on_outer_turnover", "conditional_bootstrap_does_not_refit_match",
            "same_fractional_ledger_and_company_action_limitations_as_primary", "rank_tie_handling_is_separately_controlled",
            "different_per_stock_exposures_not_causally_matched", "similarity_is_not_statistical_equivalence"]}
    r.write(args.output / "result.json", result)
    pd.DataFrame(estimates).to_csv(args.output / "comparison.csv", index=False)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
