"""Compare a cost estimate with measured spreads, month by month.

Two numbers per month: Spearman correlation, and overlap of the top 10% most
expensive names (the set a cost screen would exclude). Reported as the mean
over months.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_NAMES = 100


def rank_agreement(df: pd.DataFrame, estimate: str, truth: str = "spread_bp",
                   month: str = "ym", code: str = "code", top: float = 0.10) -> dict:
    rho, ov = [], []
    for _, g in df.groupby(month):
        g = g.dropna(subset=[estimate, truth])
        if len(g) < MIN_NAMES:
            continue
        rho.append(g[truth].rank().corr(g[estimate].rank()))
        k = max(1, int(len(g) * top))
        ov.append(len(set(g.nlargest(k, truth)[code]) & set(g.nlargest(k, estimate)[code])) / k)
    return {"months": len(rho), "rho": float(np.mean(rho)) if rho else np.nan,
            "top_overlap": float(np.mean(ov)) if ov else np.nan}
