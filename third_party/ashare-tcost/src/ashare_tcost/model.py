"""STRUCT quoted-spread model.

    spread_bp = max(1e4 * tick / price,
                    exp(b0 + b1 log(amount) + b2 log(vol) + b3 log(price)))

amount: previous month's mean daily traded value, yuan
vol:    previous month's last 21-day return standard deviation (decimal)
price:  previous month's mean raw (unadjusted) close, yuan

The liquidity part is fitted only on stock-months where the measured spread is
above 1.3 ticks, i.e. where the tick floor does not bind; the floor itself has
no free parameter.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TICK_YUAN = 0.01
UNBOUND_TICKS = 1.3

# Target months 2021-02..2023-12; features from the immediately previous month.
# Unbound eligibility uses target-month measured spread and target-month price.
# Order: const, log amount, log vol, log price. Stored rounded paper coefficients.
CALIBRATION_ID = "struct_lagged_2021_2023_v1"
DEFAULT_BETA = np.array([10.4316, -0.3758, 0.4983, 0.1689])
# Explicit opt-in for historical same-month research; never selected implicitly.
SAME_MONTH_BETA = np.array([10.8731, -0.3867, 0.5668, 0.1720])


def tick_floor_bp(price) -> np.ndarray:
    """Spread floor implied by one tick, in bp of price."""
    p = np.asarray(price, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(p > 0, 1e4 * TICK_YUAN / p, np.nan)


def _design(amount, vol, price) -> np.ndarray:
    a, v, p = (np.asarray(x, dtype=float) for x in (amount, vol, price))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.column_stack([np.ones(len(a)), np.log(a), np.log(v), np.log(p)])


def struct_spread(df: pd.DataFrame, beta=DEFAULT_BETA, amount="amount", vol="vol",
                  price="price") -> pd.Series:
    """Full quoted spread in bp, evaluated row-wise with no implicit shifting.

    With the default calibration, pass month t-1 inputs for target month t.
    Use lag_monthly_features once, or align in the caller, but never both.
    """
    X = _design(df[amount], df[vol], df[price])
    with np.errstate(over="ignore", invalid="ignore"):
        liq = np.exp(X @ np.asarray(beta, dtype=float))
    out = np.maximum(tick_floor_bp(df[price]), liq)
    bad = ~np.isfinite(X).all(axis=1)
    out[bad] = np.nan
    return pd.Series(out, index=df.index, name="struct_bp")


def fit_struct(df: pd.DataFrame, spread_bp="spread_bp", amount="amount", vol="vol",
               price="price", *, target_n_ticks=None) -> np.ndarray:
    """Refit the liquidity part on your own measured spreads.

    Caller must align features and restrict the training target dates. For lagged
    fits pass the name of a target-month n_ticks column: eligibility is determined
    by the target month's measured spread and raw price, not the lagged price.
    Omitting target_n_ticks retains the same-month fitting convention.
    """
    n_ticks = (df[target_n_ticks] if target_n_ticks is not None else
               df[spread_bp] * df[price] / (1e4 * TICK_YUAN))
    eligible = (n_ticks > UNBOUND_TICKS) & (df[spread_bp] > 0)
    d = df[eligible]
    X = _design(d[amount], d[vol], d[price])
    y = np.log(d[spread_bp].to_numpy())
    keep = np.isfinite(X).all(axis=1) & np.isfinite(y) & np.isfinite(np.asarray(n_ticks)[np.asarray(eligible)])
    if keep.sum() < 4 or np.linalg.matrix_rank(X[keep]) < 4:
        raise ValueError("fit requires at least four finite, full-rank unbound training rows")
    beta, *_ = np.linalg.lstsq(X[keep], y[keep], rcond=None)
    return beta


def lag_monthly_features(monthly: pd.DataFrame) -> pd.DataFrame:
    """Relabel month m's features for target month m+1, with source-month evidence.

    Requires one row per code/month; does not fill missing calendar months.
    Rejects already aligned input to avoid an accidental second lag. Pass only
    source features, not contemporaneous target observations, to this helper.
    """
    need = {"code", "ym", "amount", "vol", "price"}
    if need - set(monthly):
        raise ValueError(f"monthly frame lacks columns {sorted(need - set(monthly))}")
    if "feature_ym" in monthly:
        raise ValueError("features are already aligned; do not apply a second lag")
    if monthly["code"].isna().any() or not monthly["ym"].astype("string").str.fullmatch(r"\d{4}-(0[1-9]|1[0-2])").fillna(False).all():
        raise ValueError("code must be present and ym must be YYYY-MM")
    if monthly.duplicated(["code", "ym"]).any():
        raise ValueError("duplicate code/month features")
    out = monthly.copy()
    out["feature_ym"] = out["ym"].astype(str)
    out["ym"] = (pd.PeriodIndex(out["feature_ym"], freq="M") + 1).astype(str)
    return out.sort_values(["code", "ym"]).reset_index(drop=True)
