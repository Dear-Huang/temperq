"""Was the fill price realistic? Three checks on where and how much each fill traded.

1. Position in the day's range. Each fill price is placed in that day's high-low range (0 = low,
   1 = high). A strategy that buys near the low and sells near the high far more often than chance
   is the footprint of a signal that saw the day's path, or of an engine that filled at an unreachable
   price. Compared with the uniform distribution: mean position and the share in the best tenth.
2. Worst price soon after. Each buy is restated at the highest high, each sell at the lowest low,
   of the minutes up to WINDOW minutes after the fill. Quantities and fees fixed. This is a
   pessimistic floor for execution, not an estimate: if the result no longer holds here, it rests on
   ideal fills.
3. Share of the fill's own minute volume (already in fill_timing; repeated here as the capacity side).

None of this proves an order could have filled: queue position and depth need tick or L2 data.
"""
from __future__ import annotations

import math

WINDOW = 10          # minutes after the fill for the worst-price floor
BEST_TENTH = .1


def _minutes_after(hhmm, n, own=True):
    """The fill's minute (if own) and the n minutes after it, skipping lunch and stopping at the close."""
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    out = []
    for k in range(0 if own else 1, n + 1):
        t = h * 60 + m + k
        if 11 * 60 + 30 < t < 13 * 60 + 1:      # lunch break: jump to the afternoon session
            t += 90
        if t > 15 * 60:
            break
        out.append(f"{t // 60:02d}:{t % 60:02d}")
    return out


def analyze(bundle, accounting, thresholds=None):
    from .audit_timing import hhmm
    fills = [f for f in bundle.get("fills") or [] if f.get("quantity") and f.get("price")]
    if not fills:
        return None
    t = thresholds or {}
    best_bar = t.get("T-FILL-BEST-SHARE", .25)
    ranges = {(r["date"], r["code"]): r for r in bundle.get("day_ranges") or []}
    minutes = {(m["date"], m["code"], m["time"]): m for m in bundle.get("minute_prices") or []}
    net = sum(d["net_pnl"] for d in accounting.get("daily") or []) if accounting.get("status") == "verified" else None
    out = {"id": "fill_quality", "fills": len(fills), "window": WINDOW,
           "scope": "只看成交价相对当天价格区间和成交后几分钟的位置；排队位置和盘口深度需要逐笔数据，本项不审",
           "thresholds": {"best_share": best_bar}}

    # 1. position in the day's range
    from . import audit_slippage
    slippage = audit_slippage.declared(bundle)
    placed = []
    for f in fills:
        r = ranges.get((f["date"], f["code"]))
        if not r or r["high"] <= r["low"]:
            continue
        side_bp = audit_slippage.per_side_bp(slippage, f["code"], f["price"]) or 0.
        slip = f["price"] * side_bp / 1e4 + 1e-6
        pos = (f["price"] - r["low"]) / (r["high"] - r["low"])
        beyond = f["price"] < r["low"] - slip or f["price"] > r["high"] + slip
        good = 1 - pos if f["side"] == "buy" else pos          # 1 = best possible price of the day
        placed.append({"side": f["side"], "pos": min(max(pos, 0.), 1.), "good": min(max(good, 0.), 1.),
                       "outside": beyond})
    if placed:
        n = len(placed)
        good_mean = sum(p["good"] for p in placed) / n
        best_share = sum(p["good"] >= 1 - BEST_TENTH for p in placed) / n
        # Under no skill, "good" is uniform on [0, 1]: mean 0.5 with sd 1/sqrt(12n); best tenth binomial(n, 0.1).
        z_mean = (good_mean - .5) / (1 / math.sqrt(12 * n))
        z_best = (best_share - BEST_TENTH) / math.sqrt(BEST_TENTH * (1 - BEST_TENTH) / n)
        sides = {}
        for side in ("buy", "sell"):
            g = [p for p in placed if p["side"] == side]
            if g:
                sides[side] = {"fills": len(g), "mean_position": sum(p["pos"] for p in g) / len(g),
                               "best_tenth_share": sum(p["good"] >= 1 - BEST_TENTH for p in g) / len(g)}
        suspicious = (best_share >= best_bar and z_best >= 3) or z_mean >= 3
        out["range"] = {"fills": n, "coverage": n / len(fills), "good_mean": good_mean, "best_share": best_share,
                        "z_mean": z_mean, "z_best": z_best, "sides": sides, "outside": sum(p["outside"] for p in placed),
                        "status": "suspicious" if suspicious else "normal",
                        "verdict": (f"{best_share:.0%} 的成交落在当天最有利的一成价格以内（随机约 10%），成交价偏向当天最好的一侧"
                                    f"明显超过随机：疑似信号用了当天走势，或回测按不可能拿到的价格成交" if suspicious else
                                    f"成交价在当天区间的位置接近随机（落在最有利一成以内的占 {best_share:.0%}，随机约 10%），"
                                    f"没有专挑当天低点买、高点卖的迹象")}
        if out["range"]["outside"]:
            out["range"]["verdict"] += (f"；另有 {out['range']['outside']} 笔成交价超出当天最高最低价加声明滑点的范围，"
                                        "需核对价格口径（复权）或撮合方式")

    # 2. worst price in the minutes after the fill
    worse, value, used, truncated = 0., 0., 0, 0
    for f in fills:
        clock = hhmm(f.get("time"))
        if not clock:
            continue
        # Bars are labelled by their end, so the fill's own bar may have traded its high or low before the fill;
        # only the bars after it count. A fill too near the close has no full window and is left out.
        after = _minutes_after(clock, WINDOW, own=False)
        if len(after) < WINDOW:
            truncated += 1
            continue
        bars = [minutes.get((f["date"], f["code"], m)) for m in after]
        bars = [b for b in bars if b and (b.get("high") if f["side"] == "buy" else b.get("low"))]
        if not bars:
            continue
        if f["side"] == "buy":
            worst = max(b["high"] for b in bars)
            worse += (f["price"] - worst) * f["quantity"]       # negative: paying more
        else:
            worst = min(b["low"] for b in bars)
            worse += (worst - f["price"]) * f["quantity"]       # negative: receiving less
        value += f["quantity"] * f["price"]
        used += 1
    if used:
        total = sum(f["quantity"] * f["price"] for f in fills)
        coverage = value / total if total else 0.
        floor = {"fills": used, "coverage": coverage, "change": worse, "change_bp": worse / value * 1e4,
                 "net_pnl": net, "net_after": net + worse if net is not None else None,
                 "truncated": truncated, "window": WINDOW}
        if net is None:
            floor["verdict"] = f"按成交后 {WINDOW} 分钟内最差价重算，盈亏变化 {worse:,.0f} 元（账目未闭合，不和净盈亏比较）"
        elif coverage < .9:
            floor["verdict"] = (f"只有 {coverage:.0%} 的成交额有成交后完整 {WINDOW} 分钟的分钟价"
                                + (f"（{truncated} 笔离收盘不足 {WINDOW} 分钟）" if truncated else "") + "，最差价下限不完整，不下结论")
        elif net + worse <= 0 < net:
            floor["verdict"] = (f"按成交后 {WINDOW} 分钟内最差价重算，盈亏少 {-worse:,.0f} 元，净盈亏从 {net:,.0f} 元变为 {net + worse:,.0f} 元："
                                "收益依赖较好的成交价；这是每笔都取最差价的压力情景，不能单独证明实盘成交不了")
        else:
            floor["verdict"] = (f"按成交后 {WINDOW} 分钟内最差价重算，盈亏少 {-worse:,.0f} 元"
                                f"（净盈亏的 {-worse / abs(net):.0%}），仍为 {net + worse:,.0f} 元")
        out["floor"] = floor

    if "range" not in out and "floor" not in out:
        return {**out, "status": "needs_prices",
                "verdict": "没有成交日的全天最高最低价或成交后分钟价，无法检验成交价是否现实",
                "missing": ["成交日全天区间（date, code, high, low）", "成交日分钟价含 high、low"]}
    floor = out.get("floor") or {}
    out["status"] = ("suspicious" if (out.get("range") or {}).get("status") == "suspicious"
                     or floor.get("coverage", 0) >= .9 and floor.get("net_after") is not None and floor["net_after"] <= 0 < (net or 0)
                     else "normal")
    return out
