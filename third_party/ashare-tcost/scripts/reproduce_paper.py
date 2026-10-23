"""Reproduce the reference fit and out-of-sample check with this library.

Needs the private L2 monthly panel and daily panel (not distributed):
    python -X utf8 scripts/reproduce_paper.py <l2_cost_monthly.parquet> <daily_clean.parquet>

Sample rules follow the paper's recomputation: L2 coverage >= 80%, >= 10 L2
days, spread > 0, main/ChiNext/STAR codes, sub-tick filter n_ticks >= 0.98.
Fit 2021-01..2023-12 (unbound only); test 2024-01..2026-06.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "src")
import ashare_tcost as tc  # noqa: E402

OK_PREFIX = ("600", "601", "603", "605", "688", "689", "000", "001", "002", "003", "300", "301")
PAPER = {"beta": [10.8731, -0.3867, 0.5668, 0.1720], "rho": 0.913, "top_overlap": 0.880,
         "inv_price_rho": 0.717, "amihud_rho": 0.520, "cs_rho": -0.007}


def main(l2_path: str, daily_path: str) -> int:
    L = pd.read_parquet(l2_path, columns=["ym", "code", "spread_bp", "l2_coverage", "l2_days"])
    L = L[(L.l2_coverage >= 0.8) & (L.l2_days >= 10) & (L.spread_bp > 0)
          & L.code.str[:3].isin(OK_PREFIX)]
    D = pq.read_table(daily_path, columns=["date", "code", "high", "low", "close", "close_raw",
                                           "amount", "ret"]).to_pandas()
    D = D[D.date >= "2020-11-01"].copy()
    D["code"] = D.code.astype(str).str[:6]
    D = D[D.code.str[:3].isin(OK_PREFIX)]
    M = tc.monthly_features(D)
    S = L.merge(M, on=["code", "ym"], how="inner")
    S["n_ticks"] = S.spread_bp * S.price / 100.0
    S = S[S.n_ticks >= 0.98].copy()

    beta = tc.fit_struct(S[S.ym <= "2023-12"])
    S["struct_bp"] = tc.struct_spread(S, beta)
    test = S[(S.ym >= "2024-01") & (S.ym <= "2026-06")]
    res = {"beta": beta.round(4).tolist(), "test_stock_months": int(len(test)),
           "test_months": int(test.ym.nunique())}
    for name, col in [("struct", "struct_bp"), ("inv_price", "inv_price"),
                      ("amihud", "amihud"), ("cs", "cs_bp"), ("neg_log_amount", "neg_log_amount")]:
        res[name] = tc.rank_agreement(test, col)
    res["paper"] = PAPER
    res["match"] = {
        "beta_max_abs_diff": float(np.max(np.abs(np.array(PAPER["beta"]) - beta))),
        "rho_diff": round(res["struct"]["rho"] - PAPER["rho"], 4),
        "overlap_diff": round(res["struct"]["top_overlap"] - PAPER["top_overlap"], 4),
    }
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
