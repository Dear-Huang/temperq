"""Exchange-traded instruments the audit accepts, and the rules that differ by kind.

Stocks pay sell-side stamp tax and trade in 0.01 yuan ticks. Exchange-traded funds,
LOFs, REITs and exchange bonds (including convertibles) pay no stamp tax and trade in
0.001 yuan ticks. Futures and options use margin and are out of scope.
"""
from __future__ import annotations

import re

from .strategy import stock_code

FUND_PREFIX = ("15", "16", "18", "50", "51", "52", "56", "58")
BOND_PREFIX = ("10", "11", "12", "13")
FUTURES = re.compile(r"^[A-Z]{1,2}\d{3,4}(\.(XSGE|XZCE|XDCE|CCFX|XINE|GFEX|SHF|DCE|CZC|CFE|INE|GFE))?$|^[A-Z]{1,2}(8888|9999)\b")


def security_code(value):
    """Normalize to NNNNNN.SH/.SZ; stocks keep the existing board checks."""
    code = str(value).strip().upper()
    if re.fullmatch(r"(SH|SZ)\.\d{6}", code):
        code = code[3:] + "." + code[:2]
    code = code.replace(".XSHG", ".SH").replace(".XSHE", ".SZ")
    if FUTURES.match(code):
        raise ValueError(f"{code} 是期货或期权合约：审计只覆盖 A 股股票、ETF/基金和可转债，期货暂不支持")
    if re.fullmatch(r"\d{6}", code) and code.startswith(FUND_PREFIX + BOND_PREFIX):
        code += ".SH" if code.startswith(("5", "10", "11", "13")) else ".SZ"
    match = re.fullmatch(r"(\d{6})\.(SH|SZ)", code)
    if match and match[1].startswith(FUND_PREFIX + BOND_PREFIX):
        return code
    if re.fullmatch(r"000\d{3}\.SH|399\d{3}\.SZ", code):
        raise ValueError(f"{code} 是指数，不能直接买卖：回测的成交或持仓里出现了指数，说明回测本身有问题，"
                         "常见原因是策略把择时用的指数代码当成了交易标的。请先修正策略再审计")
    return stock_code(code)


# ETFs that may be bought and sold on the same day (T+0): cross-border, gold/commodity, bond and money funds.
# Prefixes are the common ranges; anything else is treated as T+1 like a stock ETF.
T0_FUND_PREFIX = ("159920", "159941", "159934", "159937", "159980", "159981", "159985", "159812", "159866",
                  "511", "513", "518", "159001", "159003", "159005")


def same_day_resale(code):
    """True for instruments that may be sold the same day they are bought (T+0); False for T+1; None if unknown."""
    kind = asset_class(code)
    if kind == "bond":
        return True
    if kind == "stock":
        return False
    digits = code[:6]
    if digits.startswith(T0_FUND_PREFIX):
        return True
    if digits.startswith(("510", "512", "515", "516", "517", "159")):
        return False          # equity ETFs: T+1
    return None


def asset_class(code):
    digits = code[:6]
    if digits.startswith(FUND_PREFIX):
        return "fund"
    if digits.startswith(BOND_PREFIX):
        return "bond"
    return "stock"


def tick_yuan(code):
    return .01 if asset_class(code) == "stock" else .001


def pays_stamp_tax(code):
    return asset_class(code) == "stock"
