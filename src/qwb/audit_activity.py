"""Did the strategy actually work as it traded? Exits that failed, and stretches when it held nothing.

1. Failed exits. A sell order that filled nothing (or only part), after which the name was still held at
   that day's close. When this repeats, an intraday strategy turns into an overnight one, or a planned
   exit becomes a long hold. Counted from the order records and the closing positions; the strategy's
   intent is not inferred, only that a sell was placed and did not complete.
2. Idle stretches. Days on which the account held nothing at the close, the longest unbroken run, and
   the average cash share. A low drawdown that comes from long idle stretches says the strategy was not
   invested, not that it managed risk. Whether idling was intended needs the strategy's own targets.
"""
from __future__ import annotations

from collections import defaultdict

LONG_IDLE = 20          # trading days


def analyze(bundle, accounting, thresholds=None):
    if accounting.get("status") != "verified":
        return None
    snaps = bundle.get("snapshots") or []
    if not snaps:
        return None
    days = [s["date"] for s in snaps]
    held = defaultdict(dict)
    for p in bundle.get("positions") or []:
        if p.get("quantity"):
            held[p["date"]][p["code"]] = p["quantity"]
    out = {"id": "activity", "days": len(days),
           "scope": "只按委托记录与收盘持仓判断；策略是否本意如此，需要策略的目标或退出规则才能确认"}

    # 1. failed exits
    failed = []
    for o in bundle.get("orders") or []:
        if o.get("side") != "sell" or o.get("filled", 0) >= o.get("quantity", 0):
            continue
        left = held.get(o["date"], {}).get(o["code"], 0)
        if left > 0:
            failed.append({"date": o["date"], "time": o.get("time", ""), "code": o["code"], "ordered": o["quantity"],
                           "filled": o.get("filled", 0), "held_at_close": left, "reason": o.get("reason") or ""})
    sells = [o for o in bundle.get("orders") or [] if o.get("side") == "sell"]
    if sells:
        by_day = {o["date"] for o in failed}
        run = best = 0
        for d in days:
            run = run + 1 if d in by_day else 0
            best = max(best, run)
        reasons = defaultdict(int)
        for o in failed:
            reasons[o["reason"] or "未说明"] += 1
        out["exits"] = {"sell_orders": len(sells), "failed": len(failed), "failed_days": len(by_day),
                        "longest_run": best, "reasons": dict(reasons), "examples": failed[:5],
                        "share": len(failed) / len(sells)}
        if failed:
            top = max(reasons, key=reasons.get)
            out["exits"]["verdict"] = (f"{len(sells)} 笔卖单里 {len(failed)} 笔没成交完、收盘仍持有（最长连续 {best} 个交易日），"
                                       f"主要原因：{top}。卖不掉会让日内策略变成过夜持仓、计划退出变成长期持有")
        else:
            out["exits"]["verdict"] = f"{len(sells)} 笔卖单都在当天成交完，或收盘时已不再持有"

    # 2. idle stretches
    idle = [d for d in days if not held.get(d)]
    run = best = 0
    start = best_start = best_end = None
    for d in days:
        if not held.get(d):
            run += 1
            start = start or d
            if run > best:
                best, best_start, best_end = run, start, d
        else:
            run, start = 0, None
    cash = [s["cash"] / s["equity"] for s in snaps if s.get("equity")]
    out["idle"] = {"idle_days": len(idle), "share": len(idle) / len(days), "longest": best,
                   "longest_from": best_start, "longest_to": best_end,
                   "mean_cash_share": sum(cash) / len(cash) if cash else None}
    if best >= LONG_IDLE:
        out["idle"]["verdict"] = (f"{len(days)} 个交易日里 {len(idle)} 天收盘没有任何持仓，最长连续 {best} 个交易日"
                                  f"（{best_start} 至 {best_end}），平均 {out['idle']['mean_cash_share']:.0%} 的权益是现金：低回撤可能来自策略长期没有投资，"
                                  "不等于控制了风险；要确认是不是本意，需要策略每天的目标持仓")
    else:
        out["idle"]["verdict"] = f"{len(idle)} 天收盘没有持仓，最长连续 {best} 个交易日"
    out["status"] = ("flagged" if (out.get("exits") or {}).get("failed", 0) >= 3 or best >= LONG_IDLE else "normal")
    return out
