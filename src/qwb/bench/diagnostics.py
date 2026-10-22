"""Descriptive slices and overlapping label-cohort economics, never an account backtest."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import metrics
from .signal import evaluation_mask
from .statistics import block_summary, conclusion


def mean_finite(values):
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    return float(x.mean()) if len(x) else None


def _summary(values, protocol, *, block=None):
    settings = dict(protocol["statistics"])
    if block is not None:
        settings["block_days"] = block
    return block_summary(values, **settings)[0]


def build_context(panel: dict, protocol: dict) -> dict:
    a = protocol["assessment"]
    amount = panel["amount"].where(panel["amount"] > 0).rolling(
        a["liquidity_window"], min_periods=15).mean()
    # Cross-sectional buckets use only history through the decision's close.
    liquidity = amount.rank(axis=1, pct=True)
    close = panel["close"] * panel["adj_factor"]
    returns = close / close.shift(1) - 1
    market = returns.mean(axis=1).rolling(a["regime_window"], min_periods=a["regime_window"]).mean()
    return {"liquidity_pct": liquidity.to_numpy(), "market_trailing_mean": market.to_numpy(),
            "codes": panel["close"].columns.tolist()}


def evaluate(dates, labels, universe, predictions, references, protocol, path, context):
    selected, eligible, active = evaluation_mask(dates, labels, universe, references, protocol)
    days = dates[selected]
    pred, y = predictions[selected], labels[selected]
    ic = path["ic__candidate"].to_numpy()
    slices = []
    # Keep unselected dates as NaN on the original grid so gaps never become adjacent observations.
    for kind, periods in (("year", days.year.astype(str)),
                          ("quarter", days.to_period("Q").astype(str))):
        for name in sorted(set(periods)):
            rows = periods == name
            stats = _summary(np.where(rows, ic, np.nan), protocol)
            slices.append({"dimension": kind, "name": name, **stats,
                           "interpretation": "descriptive_not_a_separate_confirmatory_test"})
    liquidity = context["liquidity_pct"][selected]
    slice_paths = {}
    for name, mask in (("low", liquidity <= 1 / 3),
                       ("middle", (liquidity > 1 / 3) & (liquidity <= 2 / 3)),
                       ("high", liquidity > 2 / 3)):
        valid = eligible & mask
        values = metrics.rank_ic(np.where(valid, pred, np.nan), np.where(valid, y, np.nan))
        slice_paths[f"ic_liquidity__{name}"] = values
        slices.append({"dimension": "liquidity", "name": name, **_summary(values, protocol),
                       "interpretation": "trailing_amount_tercile_descriptive"})
    market = context["market_trailing_mean"][selected]
    for name, rows in (("trailing_market_positive", market > 0),
                       ("trailing_market_nonpositive", market <= 0)):
        values = np.where(rows, ic, np.nan)
        slices.append({"dimension": "market_history", "name": name,
                       **_summary(values, protocol), "interpretation": "past_20d_cross_section_mean_return_sign"})
    top = np.full(len(days), np.nan)
    excess = top.copy()
    bottom = top.copy()
    selected_illiquid = top.copy()
    universe_illiquid = top.copy()
    distinct_ratio = top.copy()
    quintiles = np.full((len(days), 5), np.nan)
    for t in np.flatnonzero(active):
        names = np.flatnonzero(eligible[t] & np.isfinite(pred[t]))
        if len(names) < 20:
            continue
        scores = pred[t, names]
        distinct_ratio[t] = len(np.unique(scores)) / len(scores)
        if np.ptp(scores) == 0:
            continue
        order = names[np.argsort(scores, kind="mergesort")]
        k = max(1, int(np.floor(len(names) * protocol["assessment"]["top_share"])))
        head, tail = order[-k:], order[:k]
        top[t], bottom[t] = y[t, head].mean(), y[t, tail].mean()
        excess[t] = top[t] - y[t, eligible[t]].mean()
        known = np.isfinite(liquidity[t, head])
        if known.all():
            selected_illiquid[t] = float((liquidity[t, head] <= 1 / 3).mean())
        base_known = np.isfinite(liquidity[t, eligible[t]])
        if base_known.all():
            universe_illiquid[t] = float((liquidity[t, eligible[t]] <= 1 / 3).mean())
        for bucket, members in enumerate(np.array_split(order, 5)):
            quintiles[t, bucket] = y[t, members].mean()
    gross = _summary(excess, protocol)
    scenarios = []
    for cost in protocol["assessment"]["cost_hurdles_bp"]:
        s = _summary(excess - cost / 10000, protocol)
        scenarios.append({"incremental_round_trip_bp": cost,
                          "mean_residual_bp": None if s["mean"] is None else s["mean"] * 10000,
                          "ci95_residual_bp": None if s["ci95"] is None else [v * 10000 for v in s["ci95"]],
                          "diagnostic_conclusion": conclusion(s)})
    sensitivity = []
    for block in protocol["assessment"]["sensitivity_blocks"]:
        s = _summary(ic, protocol, block=block)
        sensitivity.append({"block_days": block, **s,
                            "conclusion": conclusion(s, protocol["signal_threshold"])})
    positions = np.flatnonzero(selected)
    calendar_mature = positions + int(protocol["task"]["label"]["end"]["day"]) < len(dates)
    mature_population = universe[selected] & calendar_mature[:, None]
    missing_labels = mature_population & ~np.isfinite(y)
    missing_pred = eligible & ~np.isfinite(pred)
    missing_indices = np.flatnonzero(missing_pred.sum(axis=1))[:10]
    gap_dates, gap_codes = np.nonzero(missing_labels)
    codes = context.get("codes")
    weak_dates = np.flatnonzero(np.isfinite(ic))
    weak_dates = weak_dates[np.argsort(ic[weak_dates])[:5]]
    cohort_paths = pd.DataFrame({"date": days, "top_gross_label_return": top,
                                 "bottom_gross_label_return": bottom,
                                 "top_minus_universe_gross_label_return": excess,
                                 "top_low_liquidity_share": selected_illiquid,
                                 "universe_low_liquidity_share": universe_illiquid,
                                 "prediction_distinct_ratio": distinct_ratio,
                                 **slice_paths,
                                 **{f"quintile_{i + 1}_gross_label_return": quintiles[:, i]
                                    for i in range(5)}})
    return {
        "slices": slices, "block_sensitivity": sensitivity,
        "cohort_economics": {
            "status": "DIAGNOSTIC_ONLY", "horizon_days": protocol["task"]["target"]["horizon_days"],
            "n": gross["n"], "top_share": protocol["assessment"]["top_share"],
            "mean_top_gross_bp": None if mean_finite(top) is None else 10000 * mean_finite(top),
            "mean_top_minus_universe_gross_bp": None if gross["mean"] is None else 10000 * gross["mean"],
            "ci95_top_minus_universe_gross_bp": None if gross["ci95"] is None else [v * 10000 for v in gross["ci95"]],
            "mean_top_low_liquidity_share": mean_finite(selected_illiquid),
            "mean_universe_low_liquidity_share": mean_finite(universe_illiquid),
            "quintile_mean_gross_bp": [None if mean_finite(quintiles[:, i]) is None else
                                       10000 * mean_finite(quintiles[:, i]) for i in range(5)],
            "cost_scenarios": scenarios, "account_score_eligible": False,
            "limitations": ["overlapping_forward_label_cohorts_not_a_self_financing_account",
                            "universe_comparator_is_frictionless",
                            "cost_is_hypothetical_incremental_round_trip_not_estimated_total_cost",
                            "no_fills_cash_locked_positions_delisting_impact_or_capacity",
                            "equal_prediction_ties_use_fixed_code_order",
                            "do_not_annualize_cohort_means_or_treat_them_as_net_returns"]},
        "data_gaps": {"mature_universe_pairs": int(mature_population.sum()),
                      "missing_mature_label_pairs": int(missing_labels.sum()),
                      "missing_mature_label_dates": int(missing_labels.any(axis=1).sum()),
                      "missing_mature_label_examples": [
                          {"date": str(days[t].date()), "code": str(codes[j]) if codes is not None else str(j)}
                          for t, j in zip(gap_dates[:20], gap_codes[:20])],
                      "missing_prediction_dates": [{"date": str(days[t].date()),
                                                    "missing_pairs": int(missing_pred[t].sum())}
                                                   for t in missing_indices]},
        "worst_ic_dates": [{"date": str(days[t].date()), "ic": float(ic[t])} for t in weak_dates],
        "mean_prediction_distinct_ratio": mean_finite(distinct_ratio),
        "scope": "exploratory_diagnostics_not_causal_explanations_or_additional_confirmatory_tests",
    }, cohort_paths
