"""Target weights against what the account actually held.

With the strategy's own target weights (date, code, target_weight), each closing position's actual
weight is compared with its latest target. Reported per name: mean target, mean actual, the days the
gap exceeded the tolerance, and how many of those gaps were no larger than one board lot (100 shares)
at that day's price, which a lot-sized account cannot close. A gap within one lot is a granularity
limit, not a strategy error. Daily closing weights only; whether an order was due that day is not inferred.
"""
from __future__ import annotations

from collections import defaultdict

LOT = 100


def analyze(bundle, accounting, thresholds=None):
    targets = bundle.get("target_weights") or []
    if not targets or accounting.get("status") != "verified":
        return None
    tolerance = (thresholds or {}).get("T-ALLOC-GAP", .15)
    snaps = {s["date"]: s for s in bundle.get("snapshots") or []}
    days = sorted(snaps)
    held = defaultdict(dict)
    for p in bundle.get("positions") or []:
        held[p["date"]][p["code"]] = p
    by_date = defaultdict(dict)
    for t in targets:
        by_date[t["date"]][t["code"]] = t["target_weight"]
    plan_days = sorted(by_date)
    rows = defaultdict(lambda: {"target": [], "actual": [], "over": 0, "within_lot": 0, "lot_share": []})
    current, k = {}, 0
    for d in days:
        while k < len(plan_days) and plan_days[k] <= d:
            current = by_date[plan_days[k]]
            k += 1
        if not current:
            continue
        equity = snaps[d]["equity"]
        if not equity:
            continue
        for code, w in current.items():
            p = held[d].get(code)
            actual = (p["market_value"] / equity) if p else 0.
            r = rows[code]
            r["target"].append(w)
            r["actual"].append(actual)
            price = (p or {}).get("price")
            lot = LOT * price / equity if price else None
            if lot:
                r["lot_share"].append(lot)
            if w and abs(actual - w) / w > tolerance:
                r["over"] += 1
                if lot and abs(actual - w) <= lot:
                    r["within_lot"] += 1
    if not rows:
        return None
    names = []
    for code, r in rows.items():
        n = len(r["target"])
        names.append({"code": code, "days": n, "target": sum(r["target"]) / n, "actual": sum(r["actual"]) / n,
                      "over_days": r["over"], "within_lot_days": r["within_lot"],
                      "lot_share": sum(r["lot_share"]) / len(r["lot_share"]) if r["lot_share"] else None})
    names.sort(key=lambda x: -x["target"])
    worst = max(names, key=lambda x: abs(x["actual"] - x["target"]))
    cash = [s["cash"] / s["equity"] for s in snaps.values() if s.get("equity")]
    out = {"id": "allocation", "names": names, "tolerance": tolerance, "worst": worst,
           "mean_cash_share": sum(cash) / len(cash) if cash else None,
           "scope": "按收盘持仓与最近一次目标权重比较；一手以内的偏差是交易单位所限，不算策略错误；不推断当天是否该下单"}
    # The largest target matters most; report it first, then any name with many gap days.
    lead = max(names, key=lambda x: x["target"])
    flagged = [x for x in names if x["over_days"] and x["over_days"] >= .05 * x["days"]]
    big = bool(flagged) or (lead["target"] and abs(lead["actual"] - lead["target"]) / lead["target"] > tolerance)
    def line(x):
        lot = f"一手约占权益 {x['lot_share']:.0%}" if x.get("lot_share") else ""
        return (f"{x['code']} 目标 {x['target']:.0%}，实际日均 {x['actual']:.1%}；{x['over_days']} 个收盘日偏离超过目标的 {tolerance:.0%}"
                + (f"，其中 {x['within_lot_days']} 天缺口不超过一手（{lot}，账户规模下凑不齐整手）" if x["over_days"] else ""))
    out["status"] = "gap" if big else "close"
    out["verdict"] = ("；".join(line(x) for x in ([lead] + [x for x in flagged if x is not lead])[:3]) if big else
                      f"各标的收盘权重偏离目标超过 {tolerance:.0%} 的天数都不到 5%")
    out["worst"] = lead
    return out
