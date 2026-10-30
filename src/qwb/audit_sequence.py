"""Order of events: was each trade possible in the order the backtest assumed?

1. Funding between legs. A buy in the same minute as a sale, larger than the cash held at the
   previous close, could only be paid with that sale's proceeds. The backtest assumes the sale filled
   in full first; if it fills only in part, the buy is short of cash. Counted from fills and the account.
2. Signal after fill. When the daily scores carry the time they became available, a buy of a
   selected name at an earlier time on the same day used a signal that did not exist yet.
3. Code hint. Daily-frequency history whose end is the current moment, inside a function scheduled
   before 10:00, can include today's bar. Read from the code, never run: a hint to check by hand,
   not a finding.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict


def _clock(text):
    text = str(text or "").strip()
    if len(text) >= 16 and text[10] in " T":
        text = text[11:]
    return text[:8] if len(text) >= 5 and text[2] == ":" else ""


def funding(bundle):
    fills = [f for f in bundle.get("fills") or [] if _clock(f.get("time"))]
    snaps = bundle.get("snapshots") or []
    if not fills or not snaps:
        return None
    days = [s["date"] for s in snaps]
    cash = {s["date"]: s["cash"] for s in snaps}
    opening = (bundle.get("opening") or {}).get("cash")
    groups = defaultdict(list)
    for f in fills:
        groups[(f["date"], _clock(f["time"])[:5])].append(f)
    partial_sells = {(o["date"], o["code"]) for o in bundle.get("orders") or []
                     if o["side"] == "sell" and 0 < o["filled"] < o["quantity"]}
    rows = []
    for (day, clock), fs in sorted(groups.items()):
        sells = [f for f in fs if f["side"] == "sell"]
        buys = [f for f in fs if f["side"] == "buy"]
        if not sells or not buys:
            continue
        prior = max((d for d in days if d < day), default=None)
        held = cash[prior] if prior else opening
        if held is None:
            continue
        need = sum(f["quantity"] * f["price"] + f.get("fee", 0.) for f in buys)
        if need <= held:
            continue
        rows.append({"date": day, "time": clock, "buy": need, "cash_before": held,
                     "from_sale": need - held, "sold": sum(f["quantity"] * f["price"] for f in sells),
                     "sell_partial": any((day, f["code"]) in partial_sells for f in sells)})
    buys = sum(f["side"] == "buy" for f in fills)
    out = {"id": "funding", "same_minute_switches": sum(1 for fs in groups.values()
                                                       if {f["side"] for f in fs} == {"buy", "sell"}),
           "dependent": len(rows), "buy_fills": buys, "rows": rows,
           "partial_sell_days": sum(r["sell_partial"] for r in rows)}
    if not rows:
        out["verdict"] = "没有买单需要靠同一时刻卖出的钱才付得起"
    else:
        out["verdict"] = (f"{len(rows)} 次换仓的买入要靠同一时刻卖出的钱才付得起（最多差 {max(r['from_sale'] for r in rows):,.0f} 元）："
                          "回测默认卖单先全部成交、钱马上可用")
        if out["partial_sell_days"]:
            out["verdict"] += f"；其中 {out['partial_sell_days']} 次的卖单只成交了一部分"
    return out


def signal_after_fill(bundle):
    scores = [s for s in bundle.get("scores") or [] if s.get("selected") and _clock(s.get("available_at"))]
    if not scores:
        return None
    known = {(s["date"], s["code"]): _clock(s["available_at"]) for s in scores}
    rows = []
    for f in bundle.get("fills") or []:
        clock = _clock(f.get("time"))
        at = known.get((f["date"], f["code"]))
        if f["side"] == "buy" and clock and at and at > clock:
            rows.append({"date": f["date"], "code": f["code"], "fill_time": clock, "signal_time": at})
    same = sum(1 for f in bundle.get("fills") or [] if f["side"] == "buy"
               and known.get((f["date"], f["code"])) and known[(f["date"], f["code"])][:5] == _clock(f.get("time"))[:5])
    return {"id": "signal_after_fill", "checked": len(known), "late": len(rows), "same_minute": same, "rows": rows[:20],
            "verdict": (f"{len(rows)} 笔买入的成交时间早于它所依据的打分可用时间：用了下单时还不存在的信号" if rows else
                        "没有买入早于它所依据的打分可用时间")
                       + (f"；另有 {same} 笔打分和成交在同一分钟，需确认打分没有用到这一分钟的价格" if same else "")}


OPEN_BEFORE = "10:00"
_SCHEDULE = re.compile(r"run_(?:daily|weekly|monthly)\s*\(\s*(\w+)[^)]*?(?:time\s*=\s*)?['\"](\d{1,2}):(\d{2})['\"]")


def code_hints(bundle):
    """Daily history ending now inside an open-time function: a hint, never a finding."""
    from .code_parse import parse
    source = (bundle.get("code") or {}).get("source") or ""
    if not source.strip():
        return None
    from .audit_premise import _code_texts
    text = _code_texts(source)[-1]
    tree, _ = parse(text)
    if tree is None:
        return None
    early = {m.group(1) for m in _SCHEDULE.finditer(text) if f"{int(m.group(2)):02d}:{m.group(3)}" < OPEN_BEFORE}
    guarded = bool(re.search(r"avoid_future_data['\"]?\s*,\s*True", text))
    hints = []
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in early:
        fn = functions.get(name)
        if not fn:
            continue
        reach = {name} | {c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                          and c.func.id in functions}
        for part in reach:
            for call in ast.walk(functions[part]):
                if not (isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) in
                        ("get_price", "history", "get_bars", "history_bars")):
                    continue
                kw = {k.arg: ast.unparse(k.value) for k in call.keywords if k.arg}
                freq = kw.get("frequency", kw.get("unit", "'daily'")).strip("'\"")
                ends_now = "current_dt" in kw.get("end_date", "") or kw.get("include_now", "") == "True" \
                    or kw.get("end_dt", "").endswith("current_dt")
                if freq in ("daily", "1d") and ends_now:
                    hints.append({"line": call.lineno, "function": part, "code": ast.unparse(call)[:160]})
    return {"id": "code_hints", "early_functions": sorted(early), "guarded": guarded, "hints": hints[:10],
            "verdict": (f"{len(hints)} 处在 {OPEN_BEFORE} 前运行的函数里读日线、截止时间是当前时刻，可能读到当天的数据，需人工核"
                        + ("（代码开启了防未来函数，平台可能已拦截）" if guarded else "") if hints else
                        "没有在开盘时段读取截止到当前时刻的日线")}


def analyze(bundle, accounting, thresholds=None):
    parts = {"funding": funding(bundle) if accounting.get("status") == "verified" else None,
             "signal_after_fill": signal_after_fill(bundle), "code_hints": code_hints(bundle)}
    parts = {k: v for k, v in parts.items() if v}
    if not parts:
        return None
    flagged = ((parts.get("signal_after_fill") or {}).get("late", 0) > 0
               or (parts.get("funding") or {}).get("partial_sell_days", 0) > 0)
    return {"id": "sequence", "status": "flagged" if flagged else "noted", **parts,
            "scope": "只看成交、账户、打分时间和代码文字；卖单是否先成交、信号何时算出，以平台撮合和日志为准"}
