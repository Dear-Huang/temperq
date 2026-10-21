"""Market rule packs (SPEC §3): configuration with effective windows."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

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
