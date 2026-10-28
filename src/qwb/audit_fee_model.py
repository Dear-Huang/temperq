"""Fees in four groups that are never mixed (docs/specs/AUDIT_EVIDENCE_SPEC.md R3).

- declared: the fee model in the strategy code or engine config (read, never run);
- charged: the fees in the export, with their source;
- broker: the user's contract terms, confirmed or not;
- scenarios: fees recomputed at assumed commission rates.

Fees divided by traded amount is not treated as a rate: minimums, charging units, taxes
and partial fills all move that ratio, so it is shown only as a descriptive number.
"""
from __future__ import annotations

import ast
from datetime import date

from . import rules as tax_rules
from .code_parse import parse
from .securities import asset_class

ROUNDING = .011          # exports round each fee to 0.01 yuan
SCENARIO_BP = (1., 2., 3.)
CLASS_NAMES = {"stock": "股票", "fund": "ETF/基金", "bond": "债券"}


def _number(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, RecursionError, MemoryError):
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _name(call):
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _engine_config(tree):
    """RQAlpha `__config__` literal, if the uploaded file is an engine entry."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__config__"):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, RecursionError, MemoryError):
                return None, node.lineno
            return value if isinstance(value, dict) else None, node.lineno
    return None, None


def declared(bundle):
    """The fee model per asset class, with what is missing to make it complete."""
    tree, _ = parse((bundle.get("code") or {}).get("source") or "")
    models, missing, sources = {}, {}, []
    engine = {"bigquant_v1": "BigQuant", "joinquant_v1": "聚宽", "ricequant_v1": "米筐/RQAlpha"}.get(bundle.get("adapter"), "未知平台")
    if tree is None:
        return {"status": "none", "models": {}, "missing": {}, "sources": [], "engine": engine,
                "note": "代码无法解析，读不到费用设置"}
    config, line = _engine_config(tree)
    cost = ((config or {}).get("mod") or {}).get("sys_transaction_cost") if config else None
    if cost:
        engine = "RQAlpha（导出未给引擎版本）"
        etf = ((cost.get("etf_commission") or {}).get("default") or {})
        if isinstance(etf.get("commission_rate"), (int, float)):
            rate = float(etf["commission_rate"])
            models["fund"] = {"commission_buy": rate, "commission_sell": rate,
                              "minimum": float(etf.get("min_commission") or 0.), "tax_buy": 0., "tax_sell": 0.,
                              "minimum_applies_to": "commission", "line": line,
                              "source": "配置 sys_transaction_cost.etf_commission"}
            sources.append({"line": line, "key": "sys_transaction_cost.etf_commission"})
        if "stock_commission_multiplier" in cost:
            missing["stock"] = ("只定位到 stock_commission_multiplier；倍数要和引擎版本、基础费率表一起才能换算成费率，"
                                "导出里没有这两项")
            sources.append({"line": line, "key": "sys_transaction_cost.stock_commission_multiplier"})
    else:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = _name(node)
            if call == "set_order_cost" and node.args and isinstance(node.args[0], ast.Call) and _name(node.args[0]) == "OrderCost":
                kw = {k.arg: _number(k.value) for k in node.args[0].keywords if k.arg}
                scope = next((k.value.value for k in node.keywords if k.arg == "type" and isinstance(k.value, ast.Constant)),
                             node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else "stock")
                target = {"stock": "stock", "fund": "fund", "mmf": "fund", "bond": "bond"}.get(scope)
                if target is None:
                    continue
                # OrderCost fields default to 0 on JoinQuant.
                models[target] = {"commission_buy": kw.get("open_commission") or 0., "commission_sell": kw.get("close_commission") or 0.,
                                  "minimum": kw.get("min_commission") or 0., "tax_buy": kw.get("open_tax") or 0.,
                                  "tax_sell": kw.get("close_tax") or 0., "minimum_applies_to": "commission",
                                  "line": node.lineno, "source": f"set_order_cost(..., type='{scope}')"}
                sources.append({"line": node.lineno, "key": f"set_order_cost type={scope}"})
            elif call in {"PerOrder", "PerTrade"}:
                kw = {k.arg: _number(k.value) for k in node.keywords if k.arg}
                if kw.get("buy_cost") is None or kw.get("sell_cost") is None:
                    continue
                # Sell cost includes stamp tax; the minimum applies to the whole charge.
                model = {"commission_buy": kw["buy_cost"], "commission_sell": kw["sell_cost"], "minimum": kw.get("min_cost") or 0.,
                         "tax_buy": 0., "tax_sell": 0., "minimum_applies_to": "total", "line": node.lineno,
                         "source": f"{call}(buy_cost, sell_cost, min_cost)", "tax_inside_sell_cost": True}
                for cls in ("stock", "fund", "bond"):
                    models[cls] = model
                sources.append({"line": node.lineno, "key": call})
    traded = sorted({asset_class(f["code"]) for f in bundle.get("fills", [])})
    for cls in traded:
        if cls not in models and cls not in missing:
            missing[cls] = "代码里没有这一类的费用设置，回测用平台默认费率（默认值未知）"
    status = ("none" if not models and not sources else
              "complete" if traded and all(cls in models for cls in traded) else "incomplete")
    return {"status": status, "models": models, "missing": missing, "sources": sources, "engine": engine,
            "traded_classes": traded}


def _declared_fee(model, fill):
    buy = fill["side"] == "buy"
    commission = fill["notional"] * model["commission_buy" if buy else "commission_sell"]
    tax = fill["notional"] * model["tax_buy" if buy else "tax_sell"]
    if model["minimum_applies_to"] == "total":
        return max(model["minimum"], commission + tax)
    return max(model["minimum"], commission) + tax


def self_check(bundle, model):
    """Did the backtest charge what its own declared model says? Facts only."""
    priced, gap, mismatched, unpriced = 0, 0., 0, 0
    recomputed = reported = 0.
    for fill in bundle.get("fills", []):
        m = model["models"].get(asset_class(fill["code"]))
        if m is None or fill.get("fee") is None:
            unpriced += 1
            continue
        expected = _declared_fee(m, fill)
        priced += 1
        recomputed += expected
        reported += fill["fee"]
        if abs(fill["fee"] - expected) > ROUNDING:
            mismatched += 1
    if not priced:
        return {"status": "unchecked", "priced_fills": 0, "unpriced_fills": unpriced}
    gap = reported - recomputed
    tolerance = ROUNDING * priced
    return {"status": "consistent" if abs(gap) <= tolerance and not mismatched else "inconsistent",
            "recomputed": recomputed, "reported": reported, "gap": gap, "mismatched_fills": mismatched,
            "priced_fills": priced, "unpriced_fills": unpriced}


def stamp_check(bundle, model):
    """Declared stock sell tax against the rate in force on each trade date (a fact)."""
    m = model["models"].get("stock")
    sells = [f for f in bundle.get("fills", []) if f["side"] == "sell" and asset_class(f["code"]) == "stock"]
    if m is None or not sells:
        return None
    declared_bp = 1e4 * ((m["commission_sell"] - m["commission_buy"]) if m.get("tax_inside_sell_cost") else m["tax_sell"])
    wrong, rule_bps = 0, set()
    for f in sells:
        try:
            rule_bp = tax_rules.stamp_tax_sell_bp(date.fromisoformat(f["date"]))
        except ValueError:
            continue
        rule_bps.add(rule_bp)
        if abs(declared_bp - rule_bp) > 2:   # 0.02%: well above rounding, below one rate step
            wrong += 1
    return {"declared_bp": declared_bp, "rule_bp": sorted(rule_bps), "mismatched_sells": wrong, "sells": len(sells),
            "line": m.get("line")}


def analyze(bundle, context, settings):
    from .audit_diagnostics import fee_analysis
    model = declared(bundle)
    fills = bundle.get("fills", [])
    reported = sum(f["fee"] for f in fills if f.get("fee") is not None)
    buys = [f for f in fills if f["side"] == "buy" and f.get("fee") is not None]
    buy_notional = sum(f["notional"] for f in buys)
    account = context["accounting"]
    minimum = settings["minimum_commission"]
    rates = sorted(set(SCENARIO_BP) | {float(settings["commission_bp"])})
    scenarios = []
    for bp in rates:
        fees = fee_analysis(bundle, account, {"commission_bp": bp, "minimum_commission": minimum,
                                              "max_weight": settings["max_weight"]})
        scenarios.append({"commission_bp": bp, "minimum_commission": minimum, "fee": fees["modeled_fee"]})
    confirmed = bool(settings.get("broker_confirmed"))
    broker = {"confirmed": confirmed, "commission_bp": float(settings["commission_bp"]), "minimum_commission": minimum}
    broker_check = None
    if confirmed:
        modeled = next(s["fee"] for s in scenarios if s["commission_bp"] == broker["commission_bp"])
        broker_check = {"modeled": modeled, "reported": reported, "gap": reported - modeled}
    return {
        "id": "fee_model", "declared": model, "self_check": self_check(bundle, model),
        "stamp_check": stamp_check(bundle, model),
        "charged": {"reported": reported, "fills": len(fills),
                    "sources": [s["name"] for s in bundle.get("sources", []) if s["kind"] == "fills"],
                    "fee_per_buy_amount_bp": buy_notional and sum(f["fee"] for f in buys) / buy_notional * 1e4,
                    "note": "买入费用 ÷ 买入金额，含最低佣金和其他费用的影响，只作描述，不当作费率"},
        "broker": broker, "broker_check": broker_check, "scenarios": scenarios,
    }
