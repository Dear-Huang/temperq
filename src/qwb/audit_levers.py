"""Locate the lines in a strategy that set the knobs an audit card talks about.

Only read with the AST; uploaded code is never run. Each lever records the line,
the current text and a suggested edit written for that platform. A lever is a
pointer for the user to change by hand, not a patch: generated code lives in
audit_code and is limited to BigQuant.
"""
from __future__ import annotations

import ast
import re

STAMP_TAX_CUT = "2023-08-28"   # sell-side stamp tax fell from 10bp to 5bp on this date
HOLDING_NAMES = {"stock_count", "buy_stock_count", "stock_num", "MAX_HOLDING_NUM", "hold_num", "hold_count", "n_hold",
                 "stocknum", "num_stocks", "choicenum", "max_number", "stockCount"}
INTERVAL_NAMES = {"REBALANCE_INTERVAL", "rebalance_interval", "hold_days"}
# Names seen across a few hundred shared strategies; matched case-insensitively.
HOLDING_PATTERN = re.compile(r"^(max_)?(hold_|buy_)?(stock_?)?(num|nums|count|number)(_stocks?)?$|^max_hold(_stock)?(_?nums?)?$"
                             r"|^max_(hold_)?stock_?(num|nums|count)$|^max_hold_stocknum$")
INTERVAL_PATTERN = re.compile(r"(refresh|rebalance|reblance|adjust|tiaocang|change|shift)_?(rate|days?|period|interval|freq)?$"
                              r"|^(hold|holding)_?days$|^(period|interval)(_days)?$", re.I)
NOT_A_COUNT = re.compile(r"day|date|price|threshold|proportion|ratio|rate|zt|limit|min_|percent|pct", re.I)
TRADING_NAME = re.compile(r"trade|adjust|rebalance|reblance|buy|sell|order|market_open|tiaocang", re.I)
ORDER_CALLS = {"order", "order_target", "order_value", "order_target_value", "order_shares", "order_percent",
               "order_target_percent", "order_lots"}


def _tree(source):
    from .code_parse import parse
    return parse(source)[0]


def _name(node):
    """Bare name of an assignment target: x, g.x, context.x or context.options["x"]."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return ""


def _literal(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, RecursionError, MemoryError):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _trading_functions(tree):
    """Functions that place orders, directly or through functions they call (any depth in this file)."""
    calls = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls[fn.name] = {_call_name(c) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    trading = {name for name, used in calls.items() if used & ORDER_CALLS}
    changed = True
    while changed:
        grown = {name for name, used in calls.items() if used & trading} - trading
        trading |= grown
        changed = bool(grown)
    return trading


def _call_name(node):
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def locate(bundle):
    """Return {lever: [{line, code, value?, ...}]} found in the uploaded strategy code."""
    source = (bundle.get("code") or {}).get("source") or ""
    tree = _tree(source)
    if tree is None or not source.strip():
        return {}
    lines = source.splitlines()
    text = lambda n: lines[n - 1].strip()[:160] if 0 < n <= len(lines) else ""
    found = {}
    trading = _trading_functions(tree)
    defined = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def add(kind, line, **extra):
        found.setdefault(kind, []).append({"line": line, "code": text(line), **extra})

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = _name(node.targets[0])
            value = _literal(node.value)
            if isinstance(value, int) and (name in HOLDING_NAMES or (HOLDING_PATTERN.match(name.lower())
                                                                       and not NOT_A_COUNT.search(name))) and 1 <= value <= 300:
                add("holding_count", node.lineno, name=name, value=value)
            elif isinstance(value, int) and (name in INTERVAL_NAMES or INTERVAL_PATTERN.search(name)) and 1 <= value <= 120:
                add("rebalance", node.lineno, name=name, value=value)
            elif name == "FILTERS" and isinstance(node.value, ast.List):
                add("price_filter", node.lineno, name=name)
            elif name == "target_position_value" and value is None:
                add("order_size", node.lineno, name=name)
            elif (name == "value" and isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Div)
                  and "cash" in ast.unparse(node.value.left)):
                add("order_size", node.lineno, name=name)
            elif (isinstance(node.value, ast.Call) and _call_name(node.value) in {"filter_paused_stock", "filter_st_stock",
                  "filter_limitdown_stock", "filter_limitup_stock"}):
                add("price_filter", node.lineno, name=_call_name(node.value), after=True,
                    target=ast.unparse(node.targets[0]))
        elif isinstance(node, ast.Call):
            call = _call_name(node)
            keywords = {k.arg: k for k in node.keywords if k.arg}
            if call in {"PerOrder", "OrderCost", "PerTrade"}:
                values = {k: _literal(v.value) for k, v in keywords.items()}
                # JoinQuant's set_order_cost(..., type='fund') does not apply to stock trades.
                parent = next((p for p in ast.walk(tree) if isinstance(p, ast.Call) and node in p.args), None)
                scope = next((k.value.value for k in (parent.keywords if parent else [])
                              if k.arg == "type" and isinstance(k.value, ast.Constant)), None)
                add("fee_setting", node.lineno, call=call, values=values, scope=scope)
            for key in ("rebalance_period_days",):
                if key in keywords and isinstance(_literal(keywords[key].value), int):
                    add("rebalance", keywords[key].lineno, name=key, value=_literal(keywords[key].value))
            if "expr_filters" in keywords:
                add("price_filter", keywords["expr_filters"].lineno, name="expr_filters")
            if call in {"run_daily", "run_weekly", "run_monthly"} and node.args and isinstance(node.args[0], ast.Name):
                fn = node.args[0].id
                # Defined here: trust what it does. Defined elsewhere: fall back to its name.
                if fn in trading or (fn not in defined and TRADING_NAME.search(fn)):
                    add("rebalance", node.lineno, name=call, value={"run_daily": 1, "run_weekly": 5, "run_monthly": 20}[call],
                        function=fn)
            if (call == "order_value" and len(node.args) == 2 and isinstance(node.args[1], ast.Name)):
                add("order_size", node.lineno, name=node.args[1].id)
    for items in found.values():
        items.sort(key=lambda item: item["line"])
    return found


def _period(bundle):
    days = sorted({row["date"] for key in ("snapshots", "fills", "positions") for row in bundle.get(key, []) if row.get("date")})
    return (days[0], days[-1]) if days else (None, None)


def _set_value(code, old, new):
    """Swap the assigned value only: after '=' or inside the keyword's quotes, never inside a name."""
    quote = "(?:" + '"' * 3 + "|" + "'" * 3 + "|" + '"' + "|" + "'" + ")?"
    pattern = re.compile(r"(=\s*" + quote + r"\s*)" + re.escape(str(old)) + r"(?!\d)")
    swapped, n = pattern.subn(lambda m: m.group(1) + str(new), code, count=1)
    return swapped if n else f"{code}  # 改为 {new}"


def holding(levers, platform):
    """Edits that lengthen holding: rebalance less often."""
    out = []
    for lever in levers.get("rebalance", []):
        if lever["name"] == "run_daily":
            fn = lever["function"]
            out.append({**lever, "change": f"run_weekly({fn}, weekday=1, time='14:40', reference_security='000300.XSHG')",
                        "note": "每天调仓改为每周一调仓（聚宽 run_weekly）"})
        elif lever["name"] == "run_weekly":
            fn = lever["function"]
            out.append({**lever, "change": f"run_monthly({fn}, monthday=1, time='9:30', reference_security='000300.XSHG')",
                        "note": "每周调仓改为每月第一个交易日调仓"})
        elif lever["name"] == "run_monthly":
            continue
        else:
            new = max(5, lever["value"] * 2)
            if new == lever["value"]:
                continue
            out.append({**lever, "change": _set_value(lever["code"], lever["value"], new),
                        "note": f"{lever['name']} 从 {lever['value']} 改为 {new}，先试这一档，用效果验证比较"})
    return out


def price_floor(levers, platform):
    """Edits that drop stocks priced below 5 yuan."""
    out = []
    for lever in levers.get("price_filter", []):
        if lever["name"] == "expr_filters":
            out.append({**lever, "change": "在 expr_filters 里加一行：close >= 5", "note": "BigQuant 表达式过滤；确认 close 是不复权收盘价"})
        elif lever["name"] == "FILTERS":
            out.append({**lever, "change": "在 FILTERS 里加一条收盘价大于 5 元的过滤", "note": "米筐策略生成器的过滤列表；具体字段名以平台文档为准"})
        elif lever.get("after"):
            name = lever["target"]
            out.append({**lever, "change": f"下一行加：{name} = [s for s in {name} if get_current_data()[s].last_price >= 5]",
                        "note": "放在现有过滤之后，沿用你代码里的变量名"})
            break
    return out


def order_floor(levers, platform, threshold):
    """Edits that skip buys too small to cover the minimum commission."""
    amount = f"{threshold:.0f}"
    out = []
    for lever in levers.get("order_size", []):
        if platform == "bigquant_v1" and lever["name"] != "value":
            out.append({**lever, "change": f"if {lever['name']} >= {amount}: {lever['code']}",
                        "note": "BigQuant 可在下方『按审计发现修改代码』直接生成"})
        elif lever["name"] == "value":
            out.append({**lever, "change": f"下一行加：if value < {amount}: return", "note": "每只可买金额低于拐点时不买"})
        elif lever["name"] == "target_position_value":
            out.append({**lever, "change": f"下单前加：if context.target_position_value < {amount}: return",
                        "note": "每只目标金额低于拐点时不买"})
    return out


def buy_rate_bp(bundle):
    """Fee charged per traded amount on buys, which carry no stamp tax: the commission actually applied."""
    buys = [f for f in bundle.get("fills", []) if f["side"] == "buy" and f.get("fee") is not None]
    notional = sum(f["notional"] for f in buys)
    return sum(f["fee"] for f in buys) / notional * 10000 if notional else None


def fees(levers, bundle, more, commission_bp=None):
    """Edits that make the backtest fee setting match the commission and stamp tax actually in force."""
    start, end = _period(bundle)
    out = []
    charged = buy_rate_bp(bundle)
    # Minimum commission lifts small buys, so only a clear excess points at the rate itself.
    commission_high = charged is not None and commission_bp and charged > commission_bp * 1.5 + 1
    for lever in levers.get("fee_setting", []):
        values = lever["values"]
        if lever.get("scope") not in (None, "stock"):
            change = lever["code"].replace(f"type='{lever['scope']}'", "type='stock'", 1)
            note = (f"这行设置的是 type='{lever['scope']}'，按聚宽文档只作用于该品种，股票成交用的是平台默认费率；"
                    "默认值与这行相同时结果看不出差别，但改了这行也不会生效")
            tax = values.get("close_tax")
            if more and tax is not None and tax > .0007 and end and end >= STAMP_TAX_CUT:
                change = change.replace(f"close_tax={tax}", "close_tax=0.0005", 1)
                note += (f"；同时把印花税改为 {STAMP_TAX_CUT} 起的 5bp" +
                         ("（回测跨越这一天，改后之前的部分偏乐观）" if start and start < STAMP_TAX_CUT else ""))
            out.append({**lever, "change": change, "note": note})
            continue
        if lever["call"] in {"PerOrder", "PerTrade"} and values.get("sell_cost") is not None and values.get("buy_cost") is not None:
            stamp = values["sell_cost"] - values["buy_cost"]
            if more and stamp > .0007 and start and start >= STAMP_TAX_CUT:
                new = round(values["buy_cost"] + .0005, 6)
                out.append({**lever, "change": lever["code"].replace(f"sell_cost={values['sell_cost']}", f"sell_cost={new}", 1),
                            "note": f"卖出含 10bp 印花税；{STAMP_TAX_CUT} 起为 5bp，本回测全在此后"})
        elif lever["call"] == "OrderCost" and values.get("close_tax") is not None and more and values["close_tax"] > .0007 and end and end >= STAMP_TAX_CUT:
            spans = start and start < STAMP_TAX_CUT
            out.append({**lever, "change": lever["code"].replace(f"close_tax={values['close_tax']}", "close_tax=0.0005", 1),
                        "note": (f"回测跨越 {STAMP_TAX_CUT}，单一税率无法两段都对；改后 {STAMP_TAX_CUT} 前偏乐观，只用于看近期" if spans
                                 else f"{STAMP_TAX_CUT} 起印花税为 5bp")})
    if not out and more and not levers.get("fee_setting"):
        where = {"ricequant_v1": "米筐回测页的『交易设置』", "bigquant_v1": "BigQuant 回测模块的费用参数",
                 "joinquant_v1": "代码里加 set_order_cost(..., type='stock')"}.get(bundle.get("adapter"), "回测平台的费用设置")
        if commission_high:
            out.append({"line": None, "code": "", "change": f"在{where}里把佣金率设为你券商的实际费率（你确认的是 {commission_bp:g}bp）",
                        "note": f"代码里没有费用设置；回测买入实收约 {charged:.1f}bp（买入不含印花税），高于确认佣金，是平台默认佣金偏高"})
        elif end and end >= STAMP_TAX_CUT:
            out.append({"line": None, "code": "", "change": f"在{where}里把卖出印花税设为 5bp（{STAMP_TAX_CUT} 起）",
                        "note": "代码里没有费用设置，回测用的是平台默认费率"})
    return out
