"""Two changes tried alone and together: does the gain of one depend on the other?

Four audited runs of the same strategy over the same period: base, change A only, change B only, both.
Each is restated by the same method as the two-version comparison (audit_verification). The margin of a
change is reported twice, without and with the other change; the interaction is
x11 - x10 - x01 + x00. When the two margins of a change differ in sign, the two suggestions cannot be
added up. Same-period re-runs are in-sample (E4); one history is not a distribution.
"""
from __future__ import annotations

from .audit_verification import restate, totals


def analyze(base, only_a, only_b, both, labels=("A", "B")):
    runs = [base, only_a, only_b, both]
    restated = [restate(r) for r in runs]
    if any(r is None for r in restated):
        return {"comparable": False, "reason": "四次回测里有账目未闭合或材料不全的，无法比较"}
    costs = {((r.get("settings") or {}).get("commission_bp"), (r.get("settings") or {}).get("minimum_commission")) for r in runs}
    if len(costs) > 1:
        return {"comparable": False, "reason": "四次审计的费用情景不同，不能同口径比较；用相同的费用参数重跑"}
    start, end = max(r["start"] for r in restated), min(r["end"] for r in restated)
    if start > end:
        return {"comparable": False, "reason": "四次回测的日期没有重叠"}
    capital = restated[0]["baseline"]
    out = {"comparable": True, "start": start, "end": end, "capital": capital, "labels": list(labels),
           "evidence": "E4", "scope": "同区间复跑，样本内；四格只是一段历史，不是收益分布；不代表以后叠加也这样"}
    for key, word in (("reported", "回测原值"), ("realistic", "按审计费率和价差情景重算")):
        x00, x10, x01, x11 = (totals(r, start, end)[key] for r in restated)
        a, b = labels
        rows = [
            {"change": a, "given": f"不加{b}", "gain": x10 - x00},
            {"change": a, "given": f"已加{b}", "gain": x11 - x01},
            {"change": b, "given": f"不加{a}", "gain": x01 - x00},
            {"change": b, "given": f"已加{a}", "gain": x11 - x10},
        ]
        for r in rows:
            r["gain_pp"] = r["gain"] / capital * 100 if capital else None
        flips = [c for c in (a, b) if (lambda g: g[0] * g[1] < 0)([r["gain"] for r in rows if r["change"] == c])]
        out[key] = {"label": word, "margins": rows, "interaction": x11 - x10 - x01 + x00,
                    "combined": x11 - x00, "sum_of_singles": (x10 - x00) + (x01 - x00), "flips": flips}
    r = out["realistic"]
    if r["flips"]:
        out["verdict"] = (f"{'、'.join(r['flips'])}单独做和与另一项一起做，效果方向相反：两条建议不能直接叠加；"
                          f"一起做比各自效果相加差 {-r['interaction']:,.0f} 元" if r["interaction"] < 0 else
                          f"{'、'.join(r['flips'])}单独做和与另一项一起做，效果方向相反：两条建议不能直接叠加")
    elif abs(r["interaction"]) >= .1 * max(abs(r["sum_of_singles"]), 1.):
        out["verdict"] = f"方向不变，但一起做比各自效果相加{'少' if r['interaction'] < 0 else '多'} {abs(r['interaction']):,.0f} 元：叠加效果要按一起做的那次算"
    else:
        out["verdict"] = "两项改动大致可以相加：一起做的效果接近各自效果之和"
    out["verdict"] += "（同区间复跑，样本内）"
    return out
