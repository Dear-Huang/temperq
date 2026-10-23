"""Platform trap set: one clean case and one planted mistake per pair.

Each pair runs an existing qwb guard twice. A pair passes when the guard
flags the planted mistake (caught) and stays quiet on the clean case (no
false alarm). The expected answers come from how the case is built, not from
the guard being tested.

This measures the platform, not a model. A new platform version must catch at
least what the frozen version caught, with zero false alarms on clean cases.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from .. import daily, folds, leakcheck, ledger
from . import account

CFG = dict(daily.DEFAULTS, min_age_days=20, final_test_years=[2024])


def synthetic_panel(n_days=120, n_codes=30, seed=0, dies_after=None):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    codes = [f"{i:06d}.SZ" for i in range(n_codes)]
    close = pd.DataFrame(10 * np.exp(np.cumsum(rng.normal(0, 0.02, (n_days, n_codes)), 0)),
                         index=dates, columns=codes)
    if dies_after is not None:                       # last 5 stocks stop trading (delisted)
        close.iloc[dies_after:, -5:] = np.nan
    return {"open": close * 1.001, "high": close * 1.01, "low": close * 0.99, "close": close,
            "amount": close * 0 + 1e7 * (1 + np.arange(n_codes)), "adj_factor": close * 0 + 1,
            "up_limit": close * 1.1}


# ------------------------------------------------------------------ the pairs

def _prefix(P, days, patch_target=None, leaky=None):
    if patch_target is None:
        return not leakcheck.prefix_invariance(P, CFG, days=days)["ok"]
    with patch.object(daily, patch_target, leaky):
        return not leakcheck.prefix_invariance(P, CFG, days=days)["ok"]


def centred_window(planted):
    real = daily.raw_features
    def leaky(P):
        f = real(P)
        f["vol20"] = f["r1"].rolling(21, center=True, min_periods=5).std()
        return f
    return _prefix(synthetic_panel(), [60, 90], "raw_features" if planted else None, leaky)


def backfill(planted):
    real = daily.raw_features
    def leaky(P):
        f = real(P)
        f["r60"] = f["r60"].bfill()
        return f
    return _prefix(synthetic_panel(), [30], "raw_features" if planted else None, leaky)


def whole_sample_normalisation(planted):
    real = daily.raw_features
    def leaky(P):
        f = real(P)
        f["r5"] = f["r5"] - f["r5"].mean()           # per-stock mean over the full sample
        return f
    return _prefix(synthetic_panel(), [60, 90], "raw_features" if planted else None, leaky)


def survivorship(planted):
    real = daily.universe
    def leaky(P, cfg, st=None, members=None):
        alive_at_end = P["close"].iloc[-1].notna()
        return real(P, cfg, st, members) & alive_at_end
    return _prefix(synthetic_panel(dies_after=100), [60, 90],
                   "universe" if planted else None, leaky)


def immature_labels(planted):
    dates = pd.Series(pd.bdate_range("2024-01-01", periods=60))
    end = (dates + pd.Timedelta(days=7)).dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    cutoff = pd.Timestamp(dates.iloc[-1]).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
    window = (dates.iloc[0], dates.iloc[-1])
    mask, _ = folds.build_fold(0, dates, end, window, cutoff, "snap", "v")
    if planted:                                      # train on every row in the window
        mask = pd.Series(True, index=dates.index)
    return bool((mask & ~folds.mature_mask(end, cutoff)).any())


def limit_up_buy(planted):
    price = np.array([[10.0], [10.0], [10.0]])
    dates = pd.bdate_range("2024-01-01", periods=3)
    f = lambda x: pd.DataFrame(x, index=dates, columns=["A"])
    up = price * (1.0 if planted else 1.1)           # planted: opens at the limit-up price
    P = {"open": f(price), "close": f(price), "adj_factor": f(np.ones_like(price)),
         "up_limit": f(up), "down_limit": f(price * 0.9)}
    _, fills, _ = account.replay(P, np.ones(price.shape, bool), np.zeros(price.shape), {0: [1.0]},
                                 start=0, end=2, initial_cash=100, commission_bp=0)
    return bool((fills["reason"] == "LIMIT_UP").any())


def repeated_test_views(planted):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ledger.jsonl"
        if planted:                                  # four earlier looks at the same test years
            for i, ic in enumerate([0.03, 0.04, 0.02, 0.05]):
                ledger.append(p, {"task": "t", "snapshot": "s", "config_hash": f"c{i}",
                                  "final_test_years": [2024], "final_ic": ic})
        h = ledger.history(p, "t", "s", [2024], "new", 0.06)
    return h["views"] > 1


PAIRS = {
    "look_ahead/centred_window": centred_window,
    "look_ahead/backfill": backfill,
    "look_ahead/whole_sample_normalisation": whole_sample_normalisation,
    "universe/survivorship": survivorship,
    "labels/immature_in_training": immature_labels,
    "execution/limit_up_buy": limit_up_buy,
    "reporting/repeated_test_views": repeated_test_views,
}


def run() -> dict:
    rows = []
    for name, case in PAIRS.items():
        caught, false_alarm = case(True), case(False)
        rows.append({"pair": name, "caught": caught, "false_alarm": false_alarm,
                     "pass": caught and not false_alarm})
    n = len(rows)
    return {"pairs": rows, "n": n,
            "catch_rate": sum(r["caught"] for r in rows) / n,
            "false_alarm_rate": sum(r["false_alarm"] for r in rows) / n,
            "all_pass": all(r["pass"] for r in rows)}
