"""Monthly features and the common daily-data spread proxies, for comparison.

Input `daily` columns (one row per stock-day):
    code, date, high, low, close, amount, ret, close_raw
high/low/close may be adjusted; close_raw is the unadjusted close in yuan
(used for the tick floor); amount in yuan; ret is the decimal daily return.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ROLL = 21
MIN_OBS = 10


def corwin_schultz(daily: pd.DataFrame) -> pd.Series:
    """Two-day Corwin-Schultz ending on each row, available after its close.

    Input rows must be chronological within each security. The first observation
    per security is unavailable; no following-day prices enter an estimate.
    Values are in bp, with negative estimates clipped to zero.
    """
    d = daily
    g = d.groupby("code", sort=False)
    h0, l0, c0 = g["high"].shift(1), g["low"].shift(1), g["close"].shift(1)
    h1, l1 = d["high"], d["low"]
    up, dn = (l1 > c0).fillna(False), (h1 < c0).fillna(False)
    ah1 = np.where(up, h1 - (l1 - c0), np.where(dn, h1 + (c0 - h1), h1))
    al1 = np.where(up, l1 - (l1 - c0), np.where(dn, l1 + (c0 - h1), l1))
    with np.errstate(divide="ignore", invalid="ignore"):
        beta = np.log(h0 / l0) ** 2 + np.log(ah1 / al1) ** 2
        gam = np.log(np.maximum(h0, ah1) / np.minimum(l0, al1)) ** 2
        k = 3 - 2 * np.sqrt(2)
        alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / k - np.sqrt(gam / k)
        cs = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    return (pd.Series(cs, index=d.index).clip(lower=0) * 1e4).rename("cs_bp")


def _rolling_mean(daily, s):
    return s.groupby(daily["code"], sort=False).transform(
        lambda x: x.rolling(ROLL, min_periods=MIN_OBS).mean())


def amihud(daily):
    with np.errstate(divide="ignore", invalid="ignore"):
        return _rolling_mean(daily, daily["ret"].abs() / daily["amount"]).rename("amihud")


def zero_share(daily):
    return _rolling_mean(daily, (daily["ret"].abs() < 1e-8).astype(float)).rename("zeros")


def vov(daily):
    with np.errstate(divide="ignore", invalid="ignore"):
        x = np.log(daily["high"] / daily["low"]) / np.sqrt(daily["amount"])
    return _rolling_mean(daily, x).rename("vov")


def monthly_features(daily: pd.DataFrame) -> pd.DataFrame:
    """Stock-month frame with STRUCT inputs (amount, vol, price) and comparison proxies."""
    need = {"code", "date", "high", "low", "close", "amount", "ret", "close_raw"}
    missing = sorted(need - set(daily.columns))
    if missing:
        raise ValueError(f"daily frame lacks columns {missing}")
    d = daily[(daily["amount"] > 0) & (daily["high"] > 0) & (daily["low"] > 0)].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    d["ym"] = d["date"].dt.strftime("%Y-%m")
    d["cs_bp"] = corwin_schultz(d)
    d["vol21"] = d.groupby("code", sort=False)["ret"].transform(
        lambda x: x.rolling(ROLL, min_periods=MIN_OBS).std())
    d["amihud"], d["zeros"], d["vov"] = amihud(d), zero_share(d), vov(d)
    m = d.groupby(["code", "ym"]).agg(
        amount=("amount", "mean"), price=("close_raw", "mean"), vol=("vol21", "last"),
        cs_bp=("cs_bp", "mean"), amihud=("amihud", "mean"), zeros=("zeros", "mean"),
        vov=("vov", "mean")).reset_index()
    # Impute only from the same source month's cross-section. A global median
    # would let later months affect the inputs available for an early listing.
    median = m.groupby("ym")["vol"].transform("median")
    m["vol"] = m["vol"].fillna(median).clip(lower=1e-4)
    m["inv_price"] = 1 / m["price"]
    m["neg_log_amount"] = -np.log(m["amount"])
    return m
