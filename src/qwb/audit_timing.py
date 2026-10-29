"""Fill-time sensitivity: how much does the result depend on the minute the orders filled?

Backtests often fill every order at one minute (9:31 for a 9:30 schedule). Holding each fill's
quantity and fee fixed, the close of the fill's own minute is compared with the close at other minutes
of the same day, from minute prices the user supplies. The difference is money the chosen minute is
worth. Using the fill minute's close as the baseline (not the fill price) keeps the backtest's own
slippage out of the comparison; slippage is audited separately.
Nothing is re-run: the strategy's decisions stay as they were, so this is a scenario (E3), not a replay.

With a volume column, each fill is also compared with its own minute's volume: a fill that is a large
share of that minute could not have filled at that price.
"""
from __future__ import annotations

TIMES = ("09:31", "09:35", "09:45", "10:00", "10:30", "11:00", "13:30", "14:00", "14:30", "14:50")
OPEN_BY = "09:35"          # fills at or before this minute count as "at the open"
MIN_COVERAGE = .9          # a comparison minute must cover this share of traded value to be judged


def hhmm(text):
    text = str(text or "").strip()
    if len(text) >= 16 and text[10] in " T":
        text = text[11:]
    return text[:5] if len(text) >= 5 and text[2] == ":" else ""


def analyze(bundle, accounting, thresholds=None):
    fills = bundle.get("fills") or []
    if not fills:
        return None
    t = thresholds or {}
    share_bar = t.get("T-TIMING-SHARE", .25)
    timed = [f for f in fills if hhmm(f.get("time"))]
    total = sum(f["quantity"] * f["price"] for f in fills)
    at_open = [f for f in timed if hhmm(f["time"]) <= OPEN_BY]
    open_value = sum(f["quantity"] * f["price"] for f in at_open)
    base = {"id": "fill_timing", "fills": len(fills), "timed_fills": len(timed), "open_fills": len(at_open),
            "open_share": open_value / total if total else None, "open_by": OPEN_BY,
            "scope": "固定原成交股数与费用，以成交所在分钟的收盘价为基准换成其他时点；回测自带的滑点不计入对比；不重跑策略决策（情景，样本内）",
            "thresholds": {"share": share_bar, "coverage": MIN_COVERAGE}}
    minutes = bundle.get("minute_prices") or []
    if not timed:
        return {**base, "status": "no_fill_times", "verdict": "成交记录没有成交时间，无法判断按哪一分钟成交"}
    if not minutes:
        return {**base, "status": "needs_minutes",
                "verdict": "没有成交日的分钟价，无法检验换个时点成交结果会差多少", "missing": "minute_prices"}
    bars = {(m["date"], m["code"], m["time"]): m for m in minutes}
    net = sum(d["net_pnl"] for d in accounting.get("daily") or []) if accounting.get("status") == "verified" else None
    rows = []
    for label, minute, field in [(x, x, "close") for x in TIMES] + [("竞价（09:31 开盘价）", "09:31", "open")]:
        gain, value, n = 0., 0., 0
        for f in timed:
            bar = bars.get((f["date"], f["code"], minute))
            own = bars.get((f["date"], f["code"], hhmm(f["time"])))
            price = bar and bar.get(field)
            reference = own and own.get("close")
            if not price or not reference:
                continue
            sign = 1 if f["side"] == "buy" else -1
            gain += (reference - price) * f["quantity"] * sign     # positive: filling at this minute would have paid off
            value += f["quantity"] * f["price"]
            n += 1
        if not n:
            continue
        timed_value = sum(f["quantity"] * f["price"] for f in timed)
        rows.append({"label": label, "minute": minute, "field": field, "fills": n, "gain": gain,
                     "coverage": value / timed_value if timed_value else None, "gain_bp": gain / value * 1e4,
                     "share_of_net": gain / abs(net) if net else None})
    judged = [r for r in rows if (r["coverage"] or 0) >= MIN_COVERAGE]
    volume = []
    for f in timed:
        bar = bars.get((f["date"], f["code"], hhmm(f["time"])))
        if bar and bar.get("volume"):
            volume.append(f["quantity"] / bar["volume"])
    out = {**base, "rows": rows, "net_pnl": net,
           "volume_checked": len(volume), "max_volume_share": max(volume) if volume else None}
    if not judged:
        return {**out, "status": "insufficient_minutes",
                "verdict": f"分钟价覆盖的成交额都不足 {MIN_COVERAGE:.0%}，不下结论"}
    low, high = min(r["gain"] for r in judged), max(r["gain"] for r in judged)
    out.update(low=low, high=high, worse=sum(r["gain"] < 0 for r in judged), better=sum(r["gain"] > 0 for r in judged),
               judged=len(judged))
    swing = max(abs(low), abs(high))
    if net is None:
        out.update(status="unscaled", verdict="账目未闭合，只给出金额，不和净盈亏比较")
    elif net == 0 or swing >= share_bar * abs(net):
        out.update(status="sensitive",
                   verdict=f"成交时点挪动，盈亏变化 {low:+,.0f} 到 {high:+,.0f} 元，达到净盈亏的 {swing / abs(net) if net else float('inf'):.0%}：结论依赖成交时点")
    else:
        out.update(status="stable",
                   verdict=f"成交时点挪动，盈亏变化不超过净盈亏的 {swing / abs(net):.0%}：成交时点不改变结论")
    moved = [r for r in judged if abs(r["gain"]) > .5]       # the fill's own minute compares to itself at zero
    if moved and all(r["gain"] < 0 for r in moved):
        out["open_flatters"] = True        # every other minute was worse than the minute used
    return out
