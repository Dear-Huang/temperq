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
    return stock_code(code)


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
