"""Supplementary, explicitly post-primary robustness inference for native features."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from report_training_comparison import utility
from run_training_comparison import digest, write
import numpy as np
import pandas as pd


def summarize(root):
    control = root / "native10-control"
    cases = sorted(control.glob("y????-s?"))
    if len(cases) != 9 or not all((p / "completed.json").exists() for p in cases):
        raise ValueError("all nine native-feature fits must finish")
    original = pd.read_parquet(root / "all-daily-paths.parquet")
    pieces = []
    for case in cases:
        for stress in (0, 10):
            path = pd.read_parquet(case / f"B0-slip{stress}-path.parquet").iloc[1:].copy()
            path["arm"] = "B0-native10"
            pieces.append(path)
    paths = pd.concat([original, *pieces], ignore_index=True)
    paths.to_parquet(control / "paired-paths.parquet", index=False)
    arms = sorted(paths.arm.unique())
    estimates, inference = [], {}
    for stress in (0, 10):
        rows = paths[paths.stress_bp == stress]
        counts = rows.groupby(["year", "date", "arm"]).seed.nunique()
        if not counts.eq(3).all():
            raise ValueError("incomplete seed/date coverage")
        pivot = rows.groupby(["year", "date", "arm"]).net_return.mean().unstack("arm").reindex(columns=arms)
        if pivot.isna().any().any():
            raise ValueError("unpaired control dates")
        values = pivot.to_numpy()
        observed = utility(values)
        for i, arm in enumerate(arms):
            part = rows[rows.arm == arm]
            r = values[:, i]
            eq = np.cumprod(1+r)
            estimates.append({"arm": arm, "stress_bp": stress, "utility_ann": float(observed[i]),
                "cagr": float(eq[-1]**(252/len(r))-1), "volatility_ann": float(r.std()*np.sqrt(252)),
                "max_drawdown": float((eq/np.maximum.accumulate(np.r_[1,eq])[1:]-1).min()),
                "mean_exposure": float((part.holdings_value/part.equity).mean()),
                "mean_daily_one_way_turnover": float(part.one_way_turnover.mean())})
        blocks = [g.to_numpy() for _, g in pivot.groupby(level="year")]
        rng = np.random.default_rng(20261026)
        draws = np.empty((4000, len(arms)))
        for b in range(4000):
            samples = []
            for block in blocks:
                starts = rng.integers(0, len(block), size=int(np.ceil(len(block)/20)))
                idx = np.concatenate([(s+np.arange(20)) % len(block) for s in starts])[:len(block)]
                samples.append(block[idx])
            draws[b] = utility(np.concatenate(samples))
        reference = arms.index("B0-native10")
        contrasts = []
        for i, arm in enumerate(arms):
            if i == reference:
                continue
            lo, hi = np.quantile(draws[:,i]-draws[:,reference], [.05/14, 1-.05/14])
            contrasts.append({"arm": arm, "reference": "B0-native10",
                "annual_utility_difference": float(observed[i]-observed[reference]),
                "familywise_95_interval": [float(lo), float(hi)], "supported_positive": bool(lo > 0)})
        inference[str(stress)] = contrasts
    pd.DataFrame(estimates).to_csv(control / "comparison.csv", index=False)
    cpu = sum(json.loads((p / "completed.json").read_text())["cpu_seconds"] for p in cases)
    result = {"scope": "supplementary_post_primary_robustness_not_independent_confirmation",
        "purpose": "guard_against_tree_input_expansion_weakening_the_benchmark",
        "estimates": estimates, "paired_inference": inference, "cpu_seconds": cpu,
        "qualified_economic_winner": None, "script_sha256": digest(Path(__file__)),
        "caveats": ["same_fractional_ledger_limitations", "no_fresh_holdout", "no_new_hyperparameter_search",
                    "same_eligibility_and_mature_label_masks_as_primary", "ten_native_plus_six_common_market_features"]}
    write(control / "result.json", result)
    lines = ["# Original-feature tree robustness control", "",
        "This supplementary check was added after seeing primary point estimates, to test whether expanding the feature set weakened the tree benchmark.",
        "Same three years, three seeds, selection grid, maturity/eligibility masks, and standard B0 equal-weight portfolio. Ten native features plus the six common market summaries.",
        "Original primary comparisons are unchanged. This is not fresh confirmation.", "",
        "| Arm | Annual utility | CAGR | Annual volatility |", "| --- | ---: | ---: | ---: |"]
    for r in sorted([v for v in estimates if v["stress_bp"] == 0], key=lambda v:v["utility_ann"], reverse=True):
        lines.append(f"| {r['arm']} | {r['utility_ann']:.2%} | {r['cagr']:.2%} | {r['volatility_ann']:.2%} |")
    lines += ["", "Seven comparisons to native-feature B0, separately Bonferroni corrected. Annual utility differences are not CAGR differences.",
        "", "| Comparison | Difference | Familywise 95% interval |", "| --- | ---: | ---: |"]
    for r in inference["0"]:
        lo, hi = r["familywise_95_interval"]
        lines.append(f"| {r['arm']} minus B0-native10 | {r['annual_utility_difference']:.2%} | [{lo:.2%}, {hi:.2%}] |")
    lines += ["", f"Additional case CPU time: {cpu/3600:.3f} hours.",
        "The same fractional-account and reused-history limitations prevent an economic qualification claim."]
    (control / "report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    summarize(parser.parse_args().output)
