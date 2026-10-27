"""Show the audit is worth something: restate a backtest under realistic costs, then compare versions.

Two layers:

1. Restated original: the same fills, re-costed with the confirmed commission, the stamp tax
   that applied on each trade date, and the one-tick spread scenario. Nothing is re-run.
2. Original vs optimized: the user re-runs the optimized code on the original platform and
   uploads the new export. Both versions are restated the same way and compared over the
   full period and over the internal validation segment, which the audit did not tune on.

A same-period backtest is still in-sample for the strategy's own design. The page says so and
lets "keep the original" be the outcome.
"""
from __future__ import annotations

from collections import defaultdict


def _key(fill):
    ref = fill.get("ref") or {}
    return (ref.get("source"), ref.get("row"), fill["date"], fill["code"], fill["side"])


def restate(result):
    """Daily realistic P&L for one audited backtest, or None when the audit lacks the inputs."""
    modules = result.get("modules", {})
    account, fees, tick = modules.get("accounting"), modules.get("fee_analysis"), modules.get("tick_analysis")
    if result.get("mode", "full") != "full" or not account or not account.get("daily"):
        return None
    modeled = {_key(f): f.get("modeled_fee") for f in (fees or {}).get("fills", [])}
    fee_gap, spread, counted = defaultdict(float), defaultdict(float), defaultdict(int)
    unpriced = 0
    for fill in (tick or {}).get("fills", []):
        if not fill.get("accounted_interval"):
            continue  # baseline-day fills are outside the P&L bridge
        day = fill["date"]
        counted[day] += 1
        spread[day] += fill.get("tick_scenario_cost") or 0.
        model = modeled.get(_key(fill))
        if model is None:
            unpriced += 1
        else:
            fee_gap[day] += fill["fee"] - model   # positive: the backtest charged too much
    baseline = account["baseline_equity"]
    equity = {"reported": baseline, "realistic": baseline}
    peak = dict(equity)
    worst = {"reported": 0., "realistic": 0.}
    days = []
    for row in account["daily"]:
        d = row["date"]
        realistic = row["net_pnl"] + fee_gap[d] - spread[d]
        equity["reported"] += row["net_pnl"]; equity["realistic"] += realistic
        for k in equity:
            peak[k] = max(peak[k], equity[k])
            worst[k] = min(worst[k], equity[k] / peak[k] - 1)
        days.append({"date": d, "reported": row["net_pnl"], "fee_gap": fee_gap[d], "spread": spread[d],
                     "realistic": realistic, "fills": counted[d], "turnover": row.get("gross_turnover") or 0.})
    return {"baseline": baseline, "start": days[0]["date"] if days else None, "end": days[-1]["date"] if days else None,
            "days": days, "unpriced_fills": unpriced, "drawdown": worst}


def totals(restated, start=None, end=None):
    rows = [r for r in restated["days"] if (start is None or r["date"] >= start) and (end is None or r["date"] <= end)]
    out = {k: sum(r[k] for r in rows) for k in ("reported", "fee_gap", "spread", "realistic", "fills", "turnover")}
    out["sessions"] = len(rows)
    return out


def windows(result):
    part = (result.get("period_review") or {}).get("partition") or {}
    if part.get("status") != "planned":
        return None
    return {"development": (part["development_start"], part["development_end"]),
            "validation": (part["validation_start"], part["validation_end"])}


def compare(original, optimized):
    """Restate both versions with one method and judge only on the realistic figures."""
    a, b = restate(original), restate(optimized)
    if not a or not b:
        return None
    start, end = max(a["start"], b["start"]), min(a["end"], b["end"])
    if start > end:
        return {"comparable": False, "reason": "两次回测的日期没有重叠，无法比较；请用相同区间重新回测。"}
    span = windows(original)
    rows = {"full": (totals(a, start, end), totals(b, start, end))}
    if span:
        v0 = max(span["validation"][0], start)
        if v0 <= end:
            rows["validation"] = (totals(a, v0, end), totals(b, v0, end))
    same_period = a["start"] == b["start"] and a["end"] == b["end"]
    full_gain = rows["full"][1]["realistic"] - rows["full"][0]["realistic"]
    val = rows.get("validation")
    val_gain = val[1]["realistic"] - val[0]["realistic"] if val else None
    if full_gain > 0 and (val_gain is None or val_gain > 0):
        verdict, state = "按更真实的成本，优化版好于原版", "better"
        if val_gain is None:
            verdict += "；但没有可用的验证段，结论只基于同一段历史"
    elif full_gain > 0:
        verdict, state = "优化版只在前段更好，验证段没有赢过原版；建议先保留原版", "mixed"
    else:
        verdict, state = "按更真实的成本，优化版没有好于原版；建议保留原版", "worse"
    return {"comparable": True, "start": start, "end": end, "same_period": same_period,
            "rows": rows, "full_gain": full_gain, "validation_gain": val_gain,
            "drawdown": (a["drawdown"]["realistic"], b["drawdown"]["realistic"]),
            "verdict": verdict, "state": state}
