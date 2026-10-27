"""Reproduce the reference fit and out-of-sample check with this library.

Needs the private L2 monthly panel and daily panel (not distributed):
    python -X utf8 scripts/reproduce_paper.py <l2_cost_monthly.parquet> <daily_clean.parquet>

Sample rules follow the paper's recomputation: L2 coverage >= 80%, >= 10 L2
days, spread > 0, main/ChiNext/STAR codes, sub-tick filter n_ticks >= 0.98.
Default: previous-month inputs, fit target months through 2023-12 (unbound
outcomes only); test target months 2024-01..2026-06. --design same_month selects
the historical contemporaneous design explicitly. Metrics are compared with
archived paper results, not assumed to match a different supplied dataset.
"""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ashare_tcost as tc  # noqa: E402

OK_PREFIX = ("600", "601", "603", "605", "688", "689", "000", "001", "002", "003", "300", "301")
REFERENCES = {
    "lagged": {"beta": [10.4316, -0.3758, 0.4983, 0.1689], "rho": 0.904, "top_overlap": 0.856,
               "inv_price_rho": 0.718, "amihud_rho": 0.509, "cs_rho": -0.023},
    "same_month": {"beta": [10.8731, -0.3867, 0.5668, 0.1720], "rho": 0.913, "top_overlap": 0.880,
                   "inv_price_rho": 0.717, "amihud_rho": 0.520, "cs_rho": -0.007},
}


def prepare_panel(L, M, design="lagged"):
    if design not in REFERENCES:
        raise ValueError("unknown validation design")
    S = L.merge(M, on=["code", "ym"], how="inner", validate="one_to_one")
    S["target_n_ticks"] = S.spread_bp * S.price / 100.0
    S = S[S.target_n_ticks >= 0.98].copy()
    if design == "same_month":
        return S
    # Match both eligible target t and source t-1, following the paper's sample.
    features = tc.lag_monthly_features(S.drop(columns=["spread_bp", "l2_coverage", "l2_days", "target_n_ticks"]))
    targets = S[["code", "ym", "spread_bp", "target_n_ticks"]]
    return targets.merge(features, on=["code", "ym"], how="inner", validate="one_to_one")


def main(l2_path: str, daily_path: str, design="lagged") -> int:
    L = pd.read_parquet(l2_path, columns=["ym", "code", "spread_bp", "l2_coverage", "l2_days"])
    L = L[(L.l2_coverage >= 0.8) & (L.l2_days >= 10) & (L.spread_bp > 0)
          & L.code.str[:3].isin(OK_PREFIX)]
    D = pq.read_table(daily_path, columns=["date", "code", "high", "low", "close", "close_raw",
                                           "amount", "ret"]).to_pandas()
    D = D[D.date >= "2020-11-01"].copy()
    D["code"] = D.code.astype(str).str[:6]
    D = D[D.code.str[:3].isin(OK_PREFIX)]
    M = tc.monthly_features(D)
    S = prepare_panel(L, M, design)

    train = S[(S.ym >= "2021-01") & (S.ym <= "2023-12")]
    beta = tc.fit_struct(train, target_n_ticks="target_n_ticks")
    S["struct_bp"] = tc.struct_spread(S, beta)
    test = S[(S.ym >= "2024-01") & (S.ym <= "2026-06")]
    reference = REFERENCES[design]
    res = {"design": design, "feature_lag_months": 1 if design == "lagged" else 0,
           "beta": beta.round(4).tolist(), "train_unbound_rows": int((train.target_n_ticks > 1.3).sum()),
           "test_stock_months": int(len(test)), "test_months": int(test.ym.nunique()),
           "reference_status": "archived_paper_metrics_not_assumed_reproduced",
           "imputation": "source_month_cross_section_only; historical research used pooled volatility median"}
    for name, col in [("struct", "struct_bp"), ("inv_price", "inv_price"),
                      ("amihud", "amihud"), ("cs", "cs_bp"), ("neg_log_amount", "neg_log_amount")]:
        res[name] = tc.rank_agreement(test, col)
    res["paper"] = reference
    res["match"] = {
        "beta_max_abs_diff": float(np.max(np.abs(np.array(reference["beta"]) - beta))),
        "rho_diff": round(res["struct"]["rho"] - reference["rho"], 4),
        "overlap_diff": round(res["struct"]["top_overlap"] - reference["top_overlap"], 4),
    }
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("l2_path")
    parser.add_argument("daily_path")
    parser.add_argument("--design", choices=tuple(REFERENCES), default="lagged")
    args = parser.parse_args()
    sys.exit(main(args.l2_path, args.daily_path, args.design))
