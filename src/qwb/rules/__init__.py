"""Market rule packs (SPEC §3): configuration with effective windows."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

HERE = Path(__file__).parent


@lru_cache(maxsize=None)
def load(market: str = "cn_a") -> dict:
    with open(HERE / f"{market}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def stamp_tax_sell_bp(day, pack: dict | None = None) -> float:
    """Sell-side stamp duty in bp on `day`; raises outside the rule pack's coverage."""
    pack = pack or load("cn_a")
    d = pd.Timestamp(day).normalize()
    for r in pack["stamp_tax_sell_bp"]:
        if "from" in r and d < pd.Timestamp(r["from"]):
            continue
        if "until" in r and d > pd.Timestamp(r["until"]):
            continue
        return float(r["bp"])
    raise ValueError(f"no stamp duty rule covers {d.date()}")


def cn_board_vec(codes) -> np.ndarray:
    s = pd.Series(codes, dtype="string").fillna("")
    return np.select([s.str.endswith(".BJ"), s.str.startswith(("688", "689")),
                      s.str.startswith("30")], ["bse", "star", "chinext"], "main")


def limit_prices_vec(codes, is_st, days, pre_close, pack: dict | None = None):
    """Vectorised limit_prices: (up, down) arrays in yuan, NaN where no rule applies."""
    pack = pack or load("cn_a")
    board = cn_board_vec(codes)
    st = np.asarray(is_st, dtype=bool)
    d = pd.to_datetime(pd.Series(days)).dt.normalize().to_numpy()
    pc = np.asarray(pre_close, dtype=float)
    pct = np.full(len(pc), np.nan)
    trunc = np.zeros(len(pc), dtype=bool)
    todo = np.ones(len(pc), dtype=bool)
    for r in pack["price_limits"]:                  # first matching rule wins
        m = todo & (board == r["board"])
        if "st" in r:
            m &= st == bool(r["st"])
        if "from" in r:
            m &= d >= np.datetime64(pd.Timestamp(r["from"]))
        if "until" in r:
            m &= d <= np.datetime64(pd.Timestamp(r["until"]))
        pct[m] = r["pct"]
        trunc[m] = r.get("rounding") == "truncate"
        todo &= ~m
    up_raw, dn_raw = pc * (1 + pct) * 100, pc * (1 - pct) * 100
    up = np.where(trunc, np.floor(up_raw + 1e-6), np.floor(up_raw + 0.5 + 1e-6)) / 100
    dn = np.where(trunc, np.ceil(dn_raw - 1e-6), np.floor(dn_raw + 0.5 + 1e-6)) / 100
    return up, dn


def cn_board(code: str) -> str:
    if code.endswith(".BJ"):
        return "bse"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith("30"):
        return "chinext"
    return "main"


def limit_pct(code: str, is_st: bool, day, pack: dict | None = None) -> float | None:
    """Daily price-limit fraction for a cn_a stock on `day`, or None if no rule."""
    r = limit_rule(code, is_st, day, pack)
    return None if r is None else float(r["pct"])


def limit_prices(code: str, is_st: bool, day, pre_close: float,
                 pack: dict | None = None) -> tuple[float, float] | None:
    """(up, down) limit prices in yuan, to the 0.01 tick."""
    r = limit_rule(code, is_st, day, pack)
    if r is None:
        return None
    up, dn = pre_close * (1 + r["pct"]) * 100, pre_close * (1 - r["pct"]) * 100
    if r.get("rounding") == "truncate":
        return math.floor(up + 1e-6) / 100, math.ceil(dn - 1e-6) / 100
    return math.floor(up + 0.5 + 1e-6) / 100, math.floor(dn + 0.5 + 1e-6) / 100


def limit_rule(code: str, is_st: bool, day, pack: dict | None = None) -> dict | None:
    pack = pack or load("cn_a")
    d = pd.Timestamp(day).normalize()
    board = cn_board(code)
    for r in pack["price_limits"]:
        if r["board"] != board:
            continue
        if "st" in r and bool(r["st"]) != bool(is_st):
            continue
        if "from" in r and d < pd.Timestamp(r["from"]):
            continue
        if "until" in r and d > pd.Timestamp(r["until"]):
            continue
        return r
    return None
