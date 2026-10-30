"""Does the return belong to the strategy as declared? Two accounting splits of the actual path.

1. P&L by target status. Each security-day's P&L is grouped by whether the name was held at the previous
   close and, if so, whether it was inside the previous day's stated target. A large share earned by
   holdings already outside the target says the return came partly from a path the strategy did not
   intend (an exit that failed, a state that drifted). This is an accounting split of what happened, not
   "illegitimate profit": it is not subtracted from anything and says nothing about a fixed version.

2. Overnight exposure. Positions still held at the close, and the gap from that close to the next open
   (previous shares x (open - previous close)). For a strategy that says it closes out daily, this is the
   risk it carried without declaring it. The latest exit time is the user's to declare; without it only
   the facts are reported.

Security-day P&L = previous shares x (close - previous close) + sum(signed fill shares x (close - fill
price)) - fees, which equals the engine's per-name daily contribution when there are no corporate actions.
"""
from __future__ import annotations

from collections import defaultdict


def _targets(bundle):
    """{date: set(codes)} from scores with an explicit selection, or None when no target is stated."""
    flagged, chosen = set(), defaultdict(set)
    for s in bundle.get("scores") or []:
        if s.get("selected") is None:
            continue
        flagged.add(s["date"])
        if s["selected"]:
            chosen[s["date"]].add(s["code"])
    return ({d: chosen[d] for d in flagged} if flagged else None)


def analyze(bundle, accounting, thresholds=None):
    if accounting.get("status") != "verified":
        return None
    snaps = bundle.get("snapshots") or []
    days = [s["date"] for s in snaps]
    if len(days) < 2:
        return None
    held = defaultdict(dict)
    for p in bundle.get("positions") or []:
        if p.get("quantity"):
            held[p["date"]][p["code"]] = p
    contrib = defaultdict(float)
    for c in accounting.get("contributions") or []:
        contrib[(c["date"], c["code"])] += c["net_pnl"]
    out = {"id": "fulfilment",
           "scope": "按实际路径的会计分组，不是因果结论：不能从收益里扣掉，也不能据此预测修复后的收益"}

    # 1. P&L by whether the previous close was inside the stated target
    targets = _targets(bundle)
    if targets:
        groups = {"inside": 0., "outside": 0., "fresh": 0.}
        current = set()
        plan = sorted(targets)
        k = 0
        for prev, day in zip(days, days[1:]):
            while k < len(plan) and plan[k] <= prev:
                current = targets[plan[k]]
                k += 1
            before = held.get(prev, {})
            for (d, code), pnl in ((key, v) for key, v in contrib.items() if key[0] == day):
                if code not in before:
                    groups["fresh"] += pnl
                elif code in current:
                    groups["inside"] += pnl
                else:
                    groups["outside"] += pnl
        total = sum(groups.values())
        out["by_target"] = {**groups, "total": total,
                            "outside_share": groups["outside"] / total if total > 0 else None}
        share = out["by_target"]["outside_share"]
        if share is not None and abs(groups["outside"]) >= .1 * abs(total):
            out["by_target"]["verdict"] = (f"净盈亏 {total:,.0f} 元里，{groups['outside']:,.0f} 元（{share:.0%}）来自前一日收盘已在策略目标之外的持仓："
                                           "这部分收益不是按声明的规则持有得来的；它是实际路径的会计分组，不能直接扣掉，"
                                           "修正状态管理后收益会变成多少要重跑才知道")
        else:
            out["by_target"]["verdict"] = (f"净盈亏 {total:,.0f} 元里，目标外持仓贡献 {groups['outside']:,.0f} 元，不到一成")

    # 2. overnight exposure
    opens = {}
    for m in bundle.get("minute_prices") or []:
        if m.get("open") and m["time"] in ("09:31", "09:30"):
            opens[(m["date"], m["code"])] = m["open"]
    nights = [d for d in days[:-1] if held.get(d)]
    gap, measured = 0., 0
    for prev, day in zip(days, days[1:]):
        for code, p in held.get(prev, {}).items():
            o = opens.get((day, code))
            if o and p.get("price"):
                gap += p["quantity"] * (o - p["price"])
                measured += 1
    exposure = [sum(p.get("market_value") or 0 for p in held[d].values()) / s["equity"]
                for d, s in zip(days, snaps) if held.get(d) and s.get("equity")]
    out["overnight"] = {"nights": len(nights), "days": len(days), "measured": measured,
                        "gap_pnl": gap if measured else None,
                        "mean_exposure": sum(exposure) / len(exposure) if exposure else None}
    if measured:
        out["overnight"]["verdict"] = (f"{len(days)} 个交易日里 {len(nights)} 天收盘仍有持仓，平均占权益 {out['overnight']['mean_exposure']:.0%}；"
                                       f"这些持仓从收盘到次日开盘的盈亏合计 {gap:+,.0f} 元。如果策略声明日内清仓，这部分就是没有声明的隔夜风险")
    else:
        out["overnight"]["verdict"] = (f"{len(nights)} 天收盘仍有持仓；没有次日开盘价，未算隔夜盈亏")
    out["status"] = "flagged" if (out.get("by_target") or {}).get("outside_share") and \
        abs(out["by_target"]["outside"]) >= .1 * abs(out["by_target"]["total"]) else "normal"
    return out
