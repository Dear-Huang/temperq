"""Slippage the backtest already charged, read from the strategy code (never run).

Fill prices in a backtest with slippage already include it, so the tick-spread scenario
must only add what the backtest left out. Each model is converted to a cost per side in bp:

- JoinQuant set_slippage: FixedSlippage(x) is a spread of x yuan, half on each side;
  PriceRelatedSlippage(r) a spread of r of price, half each side; StepRelatedSlippage(n) a
  spread of n ticks, half each side.
- RiceQuant/RQAlpha config: PriceRatioSlippage(r) moves each side by r of price;
  TickSizeSlippage(n) by n ticks each side. A FixedSpread-style model is a spread, half each side.

An unknown model is reported, not guessed: the scenario then stays as computed and says so.
"""
from __future__ import annotations

import ast

from .code_parse import parse
from .securities import asset_class, tick_yuan

SPREAD_YUAN = {"FixedSlippage", "FixedSpreadSlippage"}
SPREAD_RATIO = {"PriceRelatedSlippage"}
SPREAD_TICKS = {"StepRelatedSlippage"}
SIDE_RATIO = {"PriceRatioSlippage"}
SIDE_TICKS = {"TickSizeSlippage"}
KNOWN = SPREAD_YUAN | SPREAD_RATIO | SPREAD_TICKS | SIDE_RATIO | SIDE_TICKS


def _name(call):
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _number(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, RecursionError, MemoryError):
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def declared(bundle):
    """Return {'settings': [...], 'unknown': [...]} or None when the code sets no slippage."""
    tree, _ = parse((bundle.get("code") or {}).get("source") or "")
    if tree is None:
        return None
    settings, unknown = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node) == "set_slippage" and node.args:
            inner = node.args[0]
            scope = next((k.value.value for k in node.keywords if k.arg == "type" and isinstance(k.value, ast.Constant)), None)
            model = _name(inner) if isinstance(inner, ast.Call) else ""
            value = _number(inner.args[0]) if isinstance(inner, ast.Call) and inner.args else None
            entry = {"model": model, "value": value, "scope": scope, "line": node.lineno}
            (settings if model in KNOWN and value is not None else unknown).append(entry)
        elif isinstance(node, ast.Dict):
            keys = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "slippage" not in keys:
                continue
            value = _number(keys["slippage"])
            model_node = keys.get("slippage_model")
            model = model_node.value.rsplit(".", 1)[-1] if isinstance(model_node, ast.Constant) and isinstance(model_node.value, str) else "PriceRatioSlippage"
            entry = {"model": model, "value": value, "scope": None, "line": keys["slippage"].lineno}
            (settings if model in KNOWN and value is not None else unknown).append(entry)
    if not settings and not unknown:
        return None
    return {"settings": sorted(settings, key=lambda s: s["line"]), "unknown": unknown}


def per_side_bp(slippage, code, price):
    """Cost per side in bp the backtest already charged on this fill, or None if unknown."""
    if not slippage or slippage["unknown"] or price <= 0:
        return None if slippage and slippage["unknown"] else 0.
    kind = asset_class(code)
    matching = [s for s in slippage["settings"] if s["scope"] in (None, kind, "fund" if kind == "bond" else kind)]
    if not matching:
        return 0.
    s = matching[-1]  # JoinQuant applies the last call for a scope
    model, value, tick = s["model"], s["value"], tick_yuan(code)
    if model in SPREAD_YUAN:
        return 1e4 * value / 2 / price
    if model in SPREAD_RATIO:
        return 1e4 * value / 2
    if model in SPREAD_TICKS:
        return 1e4 * value * tick / 2 / price
    if model in SIDE_RATIO:
        return 1e4 * value
    return 1e4 * value * tick / price  # SIDE_TICKS


def describe(slippage):
    if not slippage:
        return "代码里没有设置滑点"
    if slippage["unknown"]:
        names = "、".join(sorted({u["model"] or "未识别写法" for u in slippage["unknown"]}))
        return f"代码设置了滑点（{names}），但无法换算，价差情景未扣除这部分，可能重复计算"
    parts = [f"第 {s['line']} 行 {s['model']}({s['value']:g})" + (f" type='{s['scope']}'" if s["scope"] else "")
             for s in slippage["settings"]]
    if all(s["value"] == 0 for s in slippage["settings"]):
        return "代码把滑点设为 0（" + "；".join(parts) + "），成交价不含价差"
    return "原回测已计滑点：" + "；".join(parts)
