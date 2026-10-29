"""What the daily ranking adds: did holdings follow the signal, and how often did it churn.

Needs the strategy's own daily ranking (rank per candidate, which ones it selected) and a
reconciled account. It only describes what happened and counts trades a buffer rule would
have skipped; the P&L of a changed rule needs a re-run on the platform.
"""
from __future__ import annotations

from collections import defaultdict



def analyze(bundle, context, thresholds=None):
    from .audit_rules import defaults
    t = {**defaults(), **(thresholds or {})}
    buffer_rank, whipsaw_days, short_share = t["T-BUFFER-RANK"], t["T-WHIPSAW-DAYS"], t["T-CASH-SHORT"]
    ranked = [s for s in bundle.get("scores", []) if s.get("rank") is not None]
    account = context.get("accounting") or {}
    if not ranked or account.get("status") != "verified":
        return None
    ranks = defaultdict(dict)
    chosen = defaultdict(set)
    for s in ranked:
        ranks[s["date"]][s["code"]] = s["rank"]
        if s.get("selected"):
            chosen[s["date"]].add(s["code"])
    days = sorted(d for d in ranks if d >= account["daily"][0]["date"]) if account.get("daily") else []
    signal_time = {}
    for s in ranked:
        signal_time.setdefault(s["date"], s.get("available_at") or "")
    if len(days) < 20:
        return None
    for d in days:
        if not chosen[d]:  # no explicit flag: the top-ranked name is the target
            chosen[d] = {min(ranks[d], key=ranks[d].get)}
    held = defaultdict(set)
    for p in bundle["positions"]:
        held[p["date"]].add(p["code"])
    traded = defaultdict(set)
    for f in bundle["fills"]:
        traded[f["date"]].add(f["code"])

    failed = defaultdict(list)
    for o in bundle.get("orders") or []:
        if o["status"] in {"cancelled", "rejected"} and o["filled"] < o["quantity"]:
            failed[o["date"]].append(o)

    # 1. Did the account hold what the signal chose?
    missed, retained = [], []
    for prev, d in zip([None] + days, days):
        want = chosen[d]
        if want <= held[d]:
            continue
        changed = prev is not None and chosen[prev] != want
        orders = failed.get(d, [])
        platform = orders[-1]["message"] if orders and orders[-1].get("message") else ""
        held_ranks = [ranks[d][c] for c in held[d] if c in ranks[d]]
        if (not orders and not traded[d] and held[d] and len(held_ranks) == len(held[d])
                and max(held_ranks) <= len(want) + 1):
            # Holding a name just behind the top with no order that day looks like the strategy's own retention
            # rule (keep while still near the top), not an execution gap. Reported apart from misses.
            retained.append({"date": d, "wanted": sorted(want), "held": sorted(held[d])})
            continue
        if orders:
            reason = "委托未成交：" + (orders[-1]["reason"] or "未说明")
        elif changed or traded[d]:
            reason = "换仓当天未成交"
        else:
            reason = "策略当天未下单"
        missed.append({"date": d, "wanted": sorted(want), "held": sorted(held[d]), "reason": reason,
                       "platform_message": platform, "order_time": orders[-1].get("time", "") if orders else ""})

    # 2. How often the top name changed, and how many changes a buffer would skip.
    switches = sum(chosen[a] != chosen[b] for a, b in zip(days, days[1:]))
    target_size = max(1, round(sum(len(chosen[d]) for d in days) / len(days)))
    keep, buffered = None, 0
    for d in days:
        if keep is None or any(ranks[d].get(c, 1e9) > target_size + buffer_rank - 1 for c in keep):
            if keep is not None and keep != chosen[d]:
                buffered += 1
            keep = chosen[d]
    avoided = max(0, switches - buffered)

    # 3. Round trips, from the FIFO lots, to price a switch.
    lots = (context.get("holding_analysis") or {}).get("closed") or []
    cost = [l["fees"] + l["tick_scenario_cost"] for l in lots]
    per_switch = sum(cost) / len(cost) if cost else 0.
    index = {d: i for i, d in enumerate(days)}
    whipsaw = []
    for lot in lots:
        i = index.get(lot["exit_date"])
        if i is None:
            continue
        later = days[i + 1:i + 1 + whipsaw_days]
        if any(lot["code"] in chosen[d] for d in later):
            whipsaw.append(lot)
    short = [o for o in bundle.get("orders") or [] if o.get("required_cash") and o.get("available_cash") is not None
             and 0 < o["required_cash"] - o["available_cash"] <= short_share * o["required_cash"]]
    short_days = {o["date"] for o in short}
    return {
        "id": "signal_mapping", "days": len(days), "followed_days": len(days) - len(missed) - len(retained),
        "retained_days": len(retained),
        "retained_note": "没有下单、继续持有排名紧随第一之后的标的：像是策略自带的保留规则，不算执行失败；"
                         "只有排名、没有策略的最终目标时，审计无法区分保留规则和漏单",
        "cash_short_orders": len(short), "cash_short_days": sum(m["date"] in short_days for m in missed),
        "cash_short_gap": max((o["required_cash"] - o["available_cash"] for o in short), default=0.),
        "cash_short_examples": [{"date": o["date"], "code": o["code"], "available": o["available_cash"],
                                 "required": o["required_cash"]} for o in short[:3]],
        "missed": missed, "missed_reasons": dict(_count(m["reason"] for m in missed)),
        "switches": switches, "buffer_rank": target_size + buffer_rank - 1, "buffered_switches": buffered,
        "closed_lots": len(lots), "missed_groups": _groups(missed, signal_time),
        "avoided_switches": avoided, "cost_per_round_trip": per_switch, "avoided_cost": avoided * per_switch,
        "whipsaw_lots": len(whipsaw), "whipsaw_cost": sum(l["fees"] + l["tick_scenario_cost"] for l in whipsaw),
        "whipsaw_net": sum(l["net_after_tick_scenario"] for l in whipsaw),
        "average_hold_days": len(days) / (switches + 1),
        "scope": "按策略自己的每日排名描述实际发生的换仓；保留带只数少换的次数，收益影响需在原平台重跑",
    }


def _groups(missed, signal_time):
    """Facts only: which code, at what signal time, with what platform reason. No cause is inferred."""
    groups = defaultdict(list)
    for m in missed:
        key = ("、".join(m["wanted"]), m["order_time"] or signal_time.get(m["date"], ""), m["reason"])
        groups[key].append(m)
    out = []
    for (code, clock, reason), rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        messages = sorted({r["platform_message"] for r in rows if r["platform_message"]})
        out.append({"code": code, "time": clock, "reason": reason, "days": len(rows),
                    "first": rows[0]["date"], "last": rows[-1]["date"], "dates": [r["date"] for r in rows],
                    "platform_messages": messages[:3]})
    return out


def _count(items):
    out = defaultdict(int)
    for item in items:
        out[item] += 1
    return out
