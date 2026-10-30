"""Partial entries: a buy that filled only in part, followed by days with no further order for that name.

Holding the name is not the same as completing the order. From the order records (ordered vs filled
quantity), the account (cash share) and the fills, each partial buy is followed until the next order or
fill in that name, or until the position is gone. Only what the records show is counted; whether the
strategy meant to top up is not inferred.
"""
from __future__ import annotations

from collections import defaultdict

SCOPE = "只按委托记录的下单数与成交数判断；持有该标的不等于完成了原买单；不推断策略是否本该补单"


def analyze(bundle, accounting, thresholds=None):
    orders = bundle.get("orders") or []
    if not orders or accounting.get("status") != "verified":
        return None
    t = thresholds or {}
    long_days = t.get("T-PARTIAL-DAYS", 5)
    snaps = bundle.get("snapshots") or []
    days = [s["date"] for s in snaps]
    cash = {s["date"]: s["cash"] / s["equity"] for s in snaps if s["equity"]}
    held = defaultdict(dict)
    for p in bundle.get("positions") or []:
        held[p["date"]][p["code"]] = p["quantity"]
    touched = defaultdict(set)                 # (code) -> dates with any order or fill
    for o in orders:
        touched[o["code"]].add(o["date"])
    for f in bundle.get("fills") or []:
        touched[f["code"]].add(f["date"])
    partial = [o for o in orders if o["side"] == "buy" and 0 < o["filled"] < o["quantity"]]
    episodes = []
    for o in partial:
        after = [d for d in days if d > o["date"]]
        run = []
        for d in after:
            if d in touched[o["code"]] or not held[d].get(o["code"]):
                break
            run.append(d)
        share = [cash[d] for d in run if d in cash]
        episodes.append({"date": o["date"], "code": o["code"], "ordered": o["quantity"], "filled": o["filled"],
                         "fill_share": o["filled"] / o["quantity"], "days_without_order": len(run),
                         "end": run[-1] if run else o["date"], "mean_cash_share": sum(share) / len(share) if share else None,
                         "reason": o.get("reason") or ""})
    if not partial:
        return {"id": "partial_entry", "status": "none", "orders": len(orders), "partial_buys": 0, "episodes": [],
                "verdict": "没有部分成交的买单", "scope": SCOPE}
    long = [e for e in episodes if e["days_without_order"] >= long_days]
    worst = max(episodes, key=lambda e: e["days_without_order"])
    return {"id": "partial_entry", "status": "found" if long else "short", "orders": len(orders),
            "partial_buys": len(partial), "long_episodes": len(long), "long_days": long_days,
            "worst": worst, "episodes": episodes,
            "verdict": (f"{len(partial)} 笔买单只成交了一部分，其中 {len(long)} 笔之后连续 {long_days} 个交易日以上没有补单；"
                        f"最长 {worst['days_without_order']} 天（{worst['code']}，{worst['date']} 起，成交 {worst['fill_share']:.0%}），"
                        + (f"期间平均 {worst['mean_cash_share']:.0%} 的权益是现金" if worst["mean_cash_share"] is not None else "")
                        if long else f"{len(partial)} 笔买单只成交了一部分，之后都在 {long_days} 个交易日内补单或换仓"),
            "scope": SCOPE}
