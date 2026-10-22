"""Automatic look-ahead check (guardrail: "used future data").

Prefix invariance: anything known at day t must come out the same whether or
not the data after t exists. Features and the universe are recomputed on the
panel truncated at t and compared with the full-panel values for row t. A
centred window, a backfill, a whole-sample normalisation or a universe filter
that peeks ahead all break this.

Labels are excluded on purpose (they are future returns by definition); their
timing is enforced by the task card and the label-maturity filter instead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import daily


def check_days(dates: pd.DatetimeIndex, years: list[int], n: int = 3) -> list[int]:
    cand = np.flatnonzero(np.isin(dates.year, years))
    if len(cand) == 0:
        cand = np.arange(min(60, len(dates) - 1), len(dates))
    pick = cand[np.linspace(0, len(cand) - 1, min(n, len(cand))).astype(int)]
    return sorted(set(int(t) for t in pick))


def prefix_invariance(P: dict, cfg: dict, st: pd.DataFrame | None = None,
                      days: list[int] | None = None, F: dict | None = None,
                      U: np.ndarray | None = None, atol: float = 1e-6,
                      members: pd.DataFrame | None = None) -> dict:
    dates = P["close"].index
    days = days if days is not None else check_days(dates, cfg["final_test_years"])
    F = F if F is not None else daily.features(P)
    U = np.asarray(U if U is not None else daily.universe(P, cfg, st, members))
    violations = []
    for t in days:
        Pt = {k: v.iloc[:t + 1] for k, v in P.items()}
        Ft = daily.features(Pt)
        Ut = np.asarray(daily.universe(Pt, cfg, None if st is None else st.iloc[:t + 1],
                                     None if members is None else members.iloc[:t + 1]))
        for k in daily.feature_names(F):
            a, b = F[k].iloc[t].to_numpy(dtype=float), Ft[k].iloc[t].to_numpy(dtype=float)
            diff = ~np.isclose(a, b, atol=atol, equal_nan=True)
            if diff.any():
                violations.append({"date": str(dates[t].date()), "item": k,
                                   "cells": int(diff.sum())})
        diff = U[t] != Ut[t]
        if diff.any():
            violations.append({"date": str(dates[t].date()), "item": "universe",
                               "cells": int(diff.sum())})
    return {"days": [str(dates[t].date()) for t in days],
            "items": len(daily.feature_names(F)) + 1, "violations": violations,
            "ok": not violations}
