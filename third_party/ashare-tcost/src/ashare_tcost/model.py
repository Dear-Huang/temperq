"""STRUCT quoted-spread model.

    spread_bp = max(1e4 * tick / price,
                    exp(b0 + b1 log(amount) + b2 log(vol) + b3 log(price)))

amount: mean daily traded value in the month, yuan
vol:    21-day standard deviation of daily returns (decimal), last value in the month
price:  mean raw (unadjusted) close in the month, yuan

The liquidity part is fitted only on stock-months where the measured spread is
above 1.3 ticks, i.e. where the tick floor does not bind; the floor itself has
no free parameter.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TICK_YUAN = 0.01
UNBOUND_TICKS = 1.3

# Fitted on L2 relative quoted spreads, 2021-01..2023-12, unbound stock-months
# (Wrong Ruler v2 recomputation, same-month features). Order: const, log amount,
# log vol, log price.
DEFAULT_BETA = np.array([10.8731, -0.3867, 0.5668, 0.1720])


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
    """STRUCT estimate in bp for each row of a stock-month frame."""
    X = _design(df[amount], df[vol], df[price])
    with np.errstate(over="ignore", invalid="ignore"):
        liq = np.exp(X @ np.asarray(beta, dtype=float))
    out = np.maximum(tick_floor_bp(df[price]), liq)
    bad = ~np.isfinite(X).all(axis=1)
    out[bad] = np.nan
    return pd.Series(out, index=df.index, name="struct_bp")


def fit_struct(df: pd.DataFrame, spread_bp="spread_bp", amount="amount", vol="vol",
               price="price") -> np.ndarray:
    """Refit the liquidity part on your own measured spreads.

    Uses only rows above UNBOUND_TICKS ticks, as in the reference fit.
    """
    n_ticks = df[spread_bp] * df[price] / (1e4 * TICK_YUAN)
    d = df[(n_ticks > UNBOUND_TICKS) & (df[spread_bp] > 0)]
    X = _design(d[amount], d[vol], d[price])
    keep = np.isfinite(X).all(axis=1)
    beta, *_ = np.linalg.lstsq(X[keep], np.log(d[spread_bp].to_numpy()[keep]), rcond=None)
    return beta
