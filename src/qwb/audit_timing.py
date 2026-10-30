"""Fill-time sensitivity: does the result survive filling a little later?

Each fill is moved later by 1, 5, 10, 30 and 60 minutes; quantity and fee stay fixed, and the price
becomes the close of the bar at that later minute. The baseline is the close of the fill's own minute
(not the fill price), so the backtest's own slippage stays out of the comparison. A fill is only ever
moved later, never earlier, and never past the close: a 14:59 sale cannot be "moved" to the morning,
so fills too close to the close are left out and the coverage says how much was left out.

Nothing is re-run: the strategy's decisions stay as they were, so this is a price-sensitivity scenario
(E3), not a delayed replay. A delayed replay needs the platform: freeze the signal and re-run.

The 09:31 bar's open is shown for fills at 09:31 only, as the price near the opening auction; only an
order submitted before the auction closed could have filled there.
"""
from __future__ import annotations

DELAYS = (1, 5, 10, 30, 60)
SEARCH = 2                 # minutes to look ahead when the exact bar is missing (no trade that minute)
OPEN_BY = "09:35"          # fills at or before this minute count as "at the open"
MIN_COVERAGE = .5          # a delay must cover this share of traded value to be judged
# Comparison times the supplement script fetches besides each fill's own window.
TIMES = ("09:31", "09:35", "09:45", "10:00", "10:30", "11:00", "13:30", "14:00", "14:30", "14:50")


def hhmm(text):
    text = str(text or "").strip()
    if len(text) >= 16 and text[10] in " T":
        text = text[11:]
    return text[:5] if len(text) >= 5 and text[2] == ":" else ""


def shift(clock, minutes):
    """HH:MM `minutes` later in trading time, skipping the lunch break; None past the close."""
    h, m = int(clock[:2]), int(clock[3:5])
    start = h * 60 + m
    t = start + minutes
    if start <= 11 * 60 + 30 < t:
        t += 90
    if t > 15 * 60:
        return None
    return f"{t // 60:02d}:{t % 60:02d}"


def analyze(bundle, accounting, thresholds=None):
    fills = bundle.get("fills") or []
    if not fills:
        return None
    share_bar = (thresholds or {}).get("T-TIMING-SHARE", .25)
    timed = [f for f in fills if hhmm(f.get("time"))]
    value_of = lambda f: f["quantity"] * f["price"]
    total = sum(value_of(f) for f in fills)
    at_open = [f for f in timed if hhmm(f["time"]) <= OPEN_BY]
    base = {"id": "fill_timing", "fills": len(fills), "timed_fills": len(timed), "open_fills": len(at_open),
            "open_share": sum(value_of(f) for f in at_open) / total if total else None, "open_by": OPEN_BY,
            "delays": list(DELAYS),
            "scope": "每笔成交只往后挪（延后 1、5、10、30、60 分钟），股数和费用不变，以成交所在分钟的收盘价为基准；"
                     "收盘前来不及延后的成交不计入；回测自带的滑点不计入对比；不重跑策略决策（价格敏感性情景，样本内）",
            "thresholds": {"share": share_bar, "coverage": MIN_COVERAGE}}
    minutes = bundle.get("minute_prices") or []
    if not timed:
        return {**base, "status": "no_fill_times", "verdict": "成交记录没有成交时间，无法判断按哪一分钟成交"}
    if not minutes:
        return {**base, "status": "needs_minutes",
                "verdict": "没有成交日的分钟价，无法检验成交延后几分钟结果会差多少", "missing": "minute_prices"}
    bars = {(m["date"], m["code"], m["time"]): m for m in minutes}

    def bar_from(f, clock):
        for k in range(SEARCH + 1):
            c = shift(clock, k)
            if c is None:
                return None
            b = bars.get((f["date"], f["code"], c))
            if b and b.get("close"):
                return b
        return None

    net = sum(d["net_pnl"] for d in accounting.get("daily") or []) if accounting.get("status") == "verified" else None
    timed_value = sum(value_of(f) for f in timed)
    rows = []
    for delay in DELAYS:
        gain = value = 0.
        n = late = 0
        for f in timed:
            clock = hhmm(f["time"])
            own = bars.get((f["date"], f["code"], clock))
            target = shift(clock, delay)
            if target is None:
                late += 1
                continue
            later = bar_from(f, target)
            if not own or not own.get("close") or not later:
                continue
            sign = 1 if f["side"] == "buy" else -1
            gain += (own["close"] - later["close"]) * f["quantity"] * sign   # positive: filling later would have paid off
            value += value_of(f)
            n += 1
        if n:
            rows.append({"label": f"延后 {delay} 分钟", "delay": delay, "field": "close", "fills": n, "too_late": late,
                         "gain": gain, "coverage": value / timed_value if timed_value else None,
                         "gain_bp": gain / value * 1e4, "share_of_net": gain / abs(net) if net else None})
    gain = value = 0.
    n = 0
    for f in timed:
        if hhmm(f["time"]) != "09:31":
            continue
        own = bars.get((f["date"], f["code"], "09:31"))
        if own and own.get("close") and own.get("open"):
            sign = 1 if f["side"] == "buy" else -1
            gain += (own["close"] - own["open"]) * f["quantity"] * sign
            value += value_of(f)
            n += 1
    if n:
        rows.append({"label": "09:31 开盘价（仅限竞价截止前已提交的订单）", "delay": None, "field": "open", "fills": n,
                     "too_late": 0, "gain": gain, "coverage": value / timed_value if timed_value else None,
                     "gain_bp": gain / value * 1e4, "share_of_net": gain / abs(net) if net else None})
    volume = []
    for f in timed:
        bar = bars.get((f["date"], f["code"], hhmm(f["time"])))
        if bar and bar.get("volume"):
            volume.append(f["quantity"] / bar["volume"])
    out = {**base, "rows": rows, "net_pnl": net,
           "volume_checked": len(volume), "max_volume_share": max(volume) if volume else None}
    judged = [r for r in rows if r["delay"] and (r["coverage"] or 0) >= MIN_COVERAGE]
    if not judged:
        best = max((r["coverage"] or 0 for r in rows if r["delay"]), default=0.)
        return {**out, "status": "insufficient_minutes",
                "verdict": f"能往后挪的成交最多只占成交额 {best:.0%}（收盘前几分钟的成交来不及延后，或缺分钟价），不下结论"}
    low, high = min(r["gain"] for r in judged), max(r["gain"] for r in judged)
    reach = min(r["coverage"] for r in judged)
    out.update(low=low, high=high, judged=len(judged), judged_delays=[r["delay"] for r in judged],
               worse=sum(r["gain"] < 0 for r in judged), better=sum(r["gain"] > 0 for r in judged))
    span = (f"延后 {judged[0]['delay']} 到 {judged[-1]['delay']} 分钟成交，盈亏变化 {low:+,.0f} 到 {high:+,.0f} 元"
            f"（覆盖成交额 {reach:.0%} 以上）")
    swing = max(abs(low), abs(high))
    if net is None:
        out.update(status="unscaled", verdict=span + "；账目未闭合，不和净盈亏比较")
    elif net == 0 or swing >= share_bar * abs(net):
        out.update(status="sensitive", verdict=span + (f"，达到净盈亏的 {swing / abs(net):.0%}" if net else "") + "：结论依赖成交时点")
    else:
        out.update(status="stable", verdict=span + f"，不超过净盈亏的 {swing / abs(net):.0%}：固定原成交路径下，"
                   "成交稍晚不改变结论；这不代表执行稳健，现金、量限和后续持仓的变化要重跑才知道")
    if all(r["gain"] < -.5 for r in judged):
        out["open_flatters"] = True          # every later fill would have been worse than the minute used
    return out
