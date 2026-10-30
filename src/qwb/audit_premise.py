"""Did the strategy's premise hold during the backtest?

A score-based strategy rests on a premise such as "stocks with a higher 20-day momentum go on to
beat the others over the next week". The premise is identified first:

- the user's own sentence and direction, when given;
- otherwise the factor read from the strategy code (AST and text only, never run), and the
  direction read from the code's sort and from the strategy's own choices.

With the strategy's own scores and the candidates' closing prices it is then checked directly,
without re-running anything:

- 对不对: each signal date, rank correlation (Spearman IC) between the score and the return to
  the next signal date across all candidates, not only the ones bought; mean, t, share positive;
- 对多少: top-third minus bottom-third return per period, the selected names against the
  equal-weight pool, and how much of the backtest return the pool alone would have made;
- 哪段对: the same statistics by calendar year, in rising and falling markets, and in the
  development and validation segments when the audit has a partition.

Returns run from the close of one signal date to the close of the next, so they never contain
the data the score was computed from. All of this describes the backtest period only: it is
in-sample evidence (E2), not a forecast.
"""
from __future__ import annotations

import ast
import math
import re
import statistics
from collections import defaultdict

DIRECTIONS = {"higher": "分数越高", "lower": "分数越低"}

# Factor families recognised in strategy code. First match per family, in file order.
FACTORS = (
    ("momentum", "动量（过去一段时间涨得多）",
     r"momentum|\bmom\b|\bMOM\(|polyfit|slope|linregress|pct_change\(\s*\d|\[-1\]\s*/\s*\w+\[0\]|\broc\b|涨幅|动量"
     r"|close\w*\s*\[\s*-\d+[^\]]*\][^\n]*/"),                      # a price ratio over a window is a return
    ("reversal", "反转（过去一段时间跌得多）", r"reversal|反转"),
    ("low_vol", "低波动", r"\.std\(|np\.std|volatility|\batr\b|波动"),
    ("model", "模型预测分数", r"predict|ranker|\bmodel\b"),
    ("value", "估值（市盈率、市净率等）", r"pe_ratio|pb_ratio|ps_ratio|pcf_ratio|\bpe\b|\bpb\b|book_to_market|市盈|市净"),
    ("growth", "成长（利润或收入增速）", r"growth_rate|_growth\b|yoy|增长率|增速"),
    ("size", "市值", r"market_cap|circulating_market_cap|a_share_market_val|市值"),
    ("dividend", "股息率", r"dividend_yield|dividend_ratio|股息"),
    ("turnover", "换手率或成交额", r"turnover_ratio|turnover|换手"),
    ("quality", "盈利质量（ROE、ROA 等）", r"\broe\b|\broa\b|gross_profit_margin|净资产收益"),
    ("trend_fit", "趋势拟合度（R²）", r"r_squared|r2\b|判定系数"),
)
# Keyword (ascending=False) and dict-config ('ascending': False) forms.
SORT_HIGH = re.compile(r"reverse\s*=\s*True|['\"]?ascending['\"]?\s*[=:]\s*False|\.desc\(\)")
SORT_LOW = re.compile(r"['\"]?ascending['\"]?\s*[=:]\s*True|\.asc\(\)")
# Window lengths are statements (g.m_days = 25, N=21), not keyword arguments such as timedelta(days=250).
SCORE_SET = re.compile(r"""['"]?score['"]?\s*(?:=(?!=)|:)""")
LOOKBACK = re.compile(r"^\s*(?:\w+\.)?(lookback|LOOKBACK|m_days|n_days|momentum_days|mom_days|period|window|N)\s*=\s*(\d+)\s*$")
WINDOWED = {"momentum", "reversal", "low_vol", "trend_fit", "trend_momentum"}


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.] * len(values)
    i = 0
    while i < len(order):                        # average ranks for ties
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def _spearman(x, y):
    rx, ry = _ranks(x), _ranks(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy) if sx and sy else None


def _summary(ics):
    if len(ics) < 2:
        return {"periods": len(ics), "mean": statistics.mean(ics) if ics else None, "t": None, "positive_share": None}
    mean, sd = statistics.mean(ics), statistics.stdev(ics)
    return {"periods": len(ics), "mean": mean, "t": mean / sd * math.sqrt(len(ics)) if sd else None,
            "positive_share": sum(i > 0 for i in ics) / len(ics)}


def _code_texts(source):
    """The uploaded code plus any strategy embedded as a SOURCE string (local RQAlpha bridges)."""
    texts = [source]
    from .code_parse import parse
    tree = parse(source)[0]
    if tree is not None:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id.upper() == "SOURCE" and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str) and "\n" in node.value.value):
                texts.append(node.value.value)
    return texts


def from_code(bundle):
    """Factor families, sort direction and look-back window read from the code; candidates for the user to confirm."""
    source = (bundle.get("code") or {}).get("source") or ""
    if not source.strip():
        return None
    texts = _code_texts(source)
    text = texts[-1]                              # the embedded original when there is one
    from .code_parse import parse
    tree = parse(text)[0]
    prose = set()                                 # docstrings and other bare strings describe, they do not compute
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                prose.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    code_lines = [(i, ln.split("#")[0]) for i, ln in enumerate(text.splitlines(), 1)
                  if i not in prose and ln.split("#")[0].strip()]
    factors = []
    for key, name, pattern in FACTORS:
        rx = re.compile(pattern, re.I)
        hit = next(((i, ln) for i, ln in code_lines if rx.search(ln)), None)
        if hit:
            factors.append({"key": key, "name": name, "line": hit[0], "code": hit[1].strip()[:160]})
    high = next(((i, ln) for i, ln in code_lines if SORT_HIGH.search(ln)), None)
    low = next(((i, ln) for i, ln in code_lines if SORT_LOW.search(ln)), None)
    direction = sort_line = None
    if high and not low:
        direction, sort_line = "higher", high
    elif low and not high:
        direction, sort_line = "lower", low
    if sort_line and factors:
        # The factor on (or nearest to) the line that orders the candidates is the one the strategy acts on.
        factors.sort(key=lambda f: abs(f["line"] - sort_line[0]))
    # A factor written into the score itself wins, and a leading minus (score = -market_cap) reverses it.
    for f in factors:
        rx = re.compile(next(p for k, _, p in FACTORS if k == f["key"]), re.I)
        for i, ln in code_lines:
            m = SCORE_SET.search(ln)
            if m and rx.search(ln[m.end():]):
                f.update(line=i, code=ln.strip()[:160], in_score=True, negated=ln[m.end():].lstrip().startswith("-"))
                break
    factors.sort(key=lambda f: not f.get("in_score"))
    keys = [f["key"] for f in factors]
    if "momentum" in keys and "trend_fit" in keys and abs(keys.index("momentum") - keys.index("trend_fit")) <= 1:
        # The common ETF rotation score: annualised slope of log price times R² of the fit.
        fit = next(f for f in factors if f["key"] == "trend_fit")
        factors = [{**fit, "key": "trend_momentum", "name": "趋势动量（年化涨幅×拟合度 R²）"}] + \
                  [f for f in factors if f["key"] not in ("momentum", "trend_fit")]
    window = next((int(m.group(2)) for _, ln in code_lines for m in [LOOKBACK.match(ln)] if m), None)
    if not factors or factors[0]["key"] not in WINDOWED:
        window = None
    if not factors and direction is None:
        return None
    return {"factors": factors, "direction": direction,
            "sort": {"line": sort_line[0], "code": sort_line[1].strip()[:160]} if sort_line else None,
            "lookback": window, "embedded": len(texts) > 1,
            "note": "从代码文字识别，未运行代码；请在导入确认页核对或改写前提"}


def infer_direction(scores):
    """From the strategy's own choices: did it select the higher or the lower scores?"""
    higher = lower = 0
    by_day = defaultdict(list)
    for s in scores:
        if s.get("selected") is not None:
            by_day[s["date"]].append(s)
    for rows in by_day.values():
        picked = [r["score"] for r in rows if r["selected"]]
        rest = [r["score"] for r in rows if not r["selected"]]
        if picked and rest:
            higher += min(picked) >= max(rest)
            lower += max(picked) <= min(rest)
    if not higher and not lower:
        return None
    return "higher" if higher >= lower else "lower"


def _judge(summary, t_bar):
    if summary["t"] is None or summary["mean"] is None:
        return "too_few"
    if summary["mean"] > 0 and summary["t"] >= t_bar:
        return "held"
    if summary["mean"] < 0 and summary["t"] <= -t_bar:
        return "reversed"
    return "unclear"


SEGMENT_WORDS = {"held": "成立", "reversed": "反了", "unclear": "不显著", "too_few": "期数太少"}


def _segment(rows, t_bar, label):
    s = _summary([r["ic"] for r in rows])
    spreads = [r["spread"] for r in rows if r["spread"] is not None]
    return {"label": label, **s, "status": _judge(s, t_bar),
            "spread_mean": statistics.mean(spreads) if spreads else None,
            "start": rows[0]["date"] if rows else None, "end": rows[-1]["next"] if rows else None}


MAX_MISSING = .02  # share of candidate-periods without a next price above which no conclusion is given
Z_POWER = .84      # one-sided normal quantile for 80% power (M-PREMISE-POWER)


def _power(rows, overall, t_bar, per_year):
    """How large an effect this sample could have detected, and how long a history the observed one would need.

    Minimum detectable mean IC at 80% power = (t_bar + 0.84) x sd(IC) / sqrt(periods). Small candidate pools
    make each period's IC noisy (its spread under no effect is about 1/sqrt(candidates - 1)), which is often
    why a premise cannot be tested at all.
    """
    n, mean, t = overall["periods"], overall["mean"], overall["t"]
    if n < 2 or mean is None or not t:
        return None
    sd = abs(mean) * math.sqrt(n) / abs(t)
    mde = (t_bar + Z_POWER) * sd / math.sqrt(n)
    pool = statistics.median(r["candidates"] for r in rows)
    needed = math.ceil(((t_bar + Z_POWER) * sd / abs(mean)) ** 2) if mean else None
    years = needed / per_year if needed and per_year else None
    text = (f"以每期约 {pool:.0f} 个候选、{n} 期计，IC 均值至少要达到 ±{mde:.3f} 才有八成把握测出（现为 {mean:+.3f}）")
    if needed and mean * (1 if mean > 0 else -1) > 0 and needed > n:
        text += f"；按现在的效果大小，约需 {needed:,} 期" + (f"（约 {years:.0f} 年）" if years else "") + "才能测出"
    if pool < 10:
        text += f"。候选池只有 {pool:.0f} 个，每期的排序比较很粗，这本身就让前提难以检验"
    return {"mde": mde, "sd": sd, "candidates": pool, "needed_periods": needed, "needed_years": years, "text": text}


def _premise_text(direction, code):
    factor = (code or {}).get("factors") or []
    if factor:
        name = factor[0]["name"].split("（")[0]
        window = f"过去{code['lookback']}日" if code.get("lookback") else ""
        high = (direction == "higher") != bool(factor[0].get("negated"))      # score = -factor reverses the factor
        return f"{window}{name}{'越高' if high else '越低'}的标的，到下一次调仓前涨得更多"
    return f"{DIRECTIONS[direction]}的标的，到下一次调仓前涨得更多"


def analyze(bundle, premise=None, partition=None, thresholds=None):
    scores = [s for s in bundle.get("scores", []) if s.get("score") is not None]
    code = from_code(bundle)
    if not scores and not code:
        return None
    t = thresholds or {}
    min_periods, t_bar = t.get("T-PREMISE-MIN-PERIODS", 20), t.get("T-PREMISE-T", 2.)
    premise = dict(premise or {})
    behaviour = infer_direction(scores) if scores else None
    direction = premise.get("direction") or behaviour or (code or {}).get("direction")
    if premise.get("direction"):
        source = "用户填写"
    elif premise.get("text"):
        source = "用户填写前提，方向" + ("从策略选股行为识别" if behaviour else "从代码排序识别" if direction else "未知")
    elif behaviour:
        source = "从代码和策略选股行为识别" if code and code.get("factors") else "从策略选股行为识别"
    elif code and code.get("factors"):
        source = "从代码识别" if direction else "从代码识别因子，方向未定（按分数越高处理）"
    else:
        source = "从代码识别" if direction else "未知"
    direction = direction or "higher"
    text = premise.get("text") or _premise_text(direction, code)
    base = {"id": "premise", "premise": text, "direction": direction, "source": source,
            "stated": bool(premise.get("text") or premise.get("direction")), "code": code,
            "behaviour_direction": behaviour,
            "conflict": bool(code and code.get("direction") and behaviour and code["direction"] != behaviour),
            "scope": "回测期间的事后描述（样本内），不代表以后仍然成立",
            "return_window": "信号日收盘到下一个信号日收盘；不含计算分数所用的数据"}
    if not scores:
        return {**base, "status": "needs_scores", "signal_days": 0,
                "verdict": "代码里识别出了策略依据的因子，但没有导入每日打分，只能记录前提、不能检验",
                "missing": "scores"}
    by_day = defaultdict(dict)
    selected = defaultdict(set)
    for s in scores:
        by_day[s["date"]][s["code"]] = s["score"]
        if s.get("selected"):
            selected[s["date"]].add(s["code"])
    prices = defaultdict(dict)
    for p in bundle.get("prices", []):
        prices[p["date"]][p["code"]] = p["close"]
    days = sorted(by_day)
    base["signal_days"] = len(days)
    widest = max((len(v) for v in by_day.values()), default=0)
    if widest < 3:
        return {**base, "status": "not_applicable", "candidates": widest,
                "verdict": f"每期最多只有 {widest} 个候选：前提检验比较的是同一天不同标的的排序，"
                           "不适用于单标的择时或阈值策略，本项不审；补交易日也不会改变这一点"}
    if not prices:
        return {**base, "status": "needs_prices",
                "verdict": "要检验这个前提，需要候选池每只标的的每日收盘价（不只是买入的那几只）",
                "missing": "candidate_prices"}
    sign = 1 if direction == "higher" else -1
    rows = []
    offered = observed = incomplete = 0
    dropped = []
    for a, b in zip(days, days[1:]):
        codes = [c for c in by_day[a] if c in prices.get(a, {}) and c in prices.get(b, {}) and prices[a][c] > 0]
        offered += len(by_day[a]); observed += len(codes)
        if len(codes) < len(by_day[a]):
            incomplete += 1
            dropped += [(a, c) for c in by_day[a] if c not in codes][:3]
        if len(codes) < 3:
            continue
        forward = {c: prices[b][c] / prices[a][c] - 1 for c in codes}
        ic = _spearman([sign * by_day[a][c] for c in codes], [forward[c] for c in codes])
        if ic is None:
            continue
        ordered = sorted(codes, key=lambda c: sign * by_day[a][c])
        third = len(ordered) // 3
        spread = (statistics.mean(forward[c] for c in ordered[-third:]) - statistics.mean(forward[c] for c in ordered[:third])
                  if third else None)
        chosen = [forward[c] for c in selected[a] if c in forward]
        best = ordered[-1]                                   # the name the premise ranks first
        rows.append({"date": a, "next": b, "ic": ic, "candidates": len(codes), "pool": statistics.mean(forward.values()),
                     "selected": statistics.mean(chosen) if chosen else None, "spread": spread,
                     "top": forward[best] - statistics.mean(forward.values())})
    overall = _summary([r["ic"] for r in rows])
    spreads = [r["spread"] for r in rows if r["spread"] is not None]
    picked = [r["selected"] - r["pool"] for r in rows if r["selected"] is not None]
    span_years = None
    if rows:
        from datetime import date
        span_years = (date.fromisoformat(rows[-1]["next"]) - date.fromisoformat(rows[0]["date"])).days / 365.25
    per_year = len(rows) / span_years if span_years else None

    segments = {}
    if partition and partition.get("status") == "planned":
        for key, start, end in (("development", partition["development_start"], partition["development_end"]),
                                ("validation", partition["validation_start"], partition["validation_end"])):
            segments[key] = _segment([r for r in rows if start <= r["date"] <= end], t_bar,
                                     "开发段" if key == "development" else "验证段")
    years = defaultdict(list)
    for r in rows:
        years[r["date"][:4]].append(r)
    by_year = [_segment(v, t_bar, k + "年") for k, v in sorted(years.items())]
    by_market = [_segment([r for r in rows if r["pool"] > 0], t_bar, "候选池整体上涨的周期"),
                 _segment([r for r in rows if r["pool"] <= 0], t_bar, "候选池整体下跌的周期")]

    compound = lambda xs: math.prod(1 + x for x in xs) - 1 if xs else None
    status = _judge(overall, t_bar) if overall["periods"] >= min_periods else "insufficient"
    if status == "too_few":
        status = "insufficient"
    status = {"unclear": "not_supported"}.get(status, status)
    missing_share = 1 - observed / offered if offered else 0.
    coverage = {"candidate_periods": offered, "with_next_price": observed, "missing_share": missing_share,
                "incomplete_periods": incomplete, "examples": [{"date": d, "code": c} for d, c in dropped[:10]]}
    if missing_share > MAX_MISSING:
        status = "incomplete_prices"
    verdict = {
        "incomplete_prices": (f"候选里有 {offered - observed:,} 个标的周期缺下一期价格（占 {missing_share:.0%}，"
                              f"{incomplete} 期不完整）：停牌或退市的标的被丢掉会让检验偏乐观，不下结论；"
                              "补齐候选池全部标的的后续价格后再检验"),
        "insufficient": f"只有 {overall['periods']} 个调仓周期，少于 {min_periods} 个，不能下结论",
        "held": "回测期间前提成立：分数和随后收益的排序一致，且统计上显著",
        "reversed": "回测期间前提反了：分数越符合前提的标的，随后反而涨得更少，且统计上显著",
        "not_supported": "回测期间前提没有得到支持：分数和随后收益的排序没有稳定关系",
    }[status]
    power = _power(rows, overall, t_bar, per_year)
    if status == "not_supported" and power and missing_share <= MAX_MISSING:
        verdict = ("回测期间没有测出前提的效果，这不等于前提错了：" + power["text"])
    val = segments.get("validation")
    if status == "held" and val and val["periods"] >= 2 and (val["mean"] or 0) <= 0:
        verdict += "；但验证段不成立，前段的效果没有延续"
    good = [s["label"] for s in by_year if s["status"] == "held"]
    bad = [s["label"] for s in by_year if s["status"] == "reversed"]
    pool_c, sel_c = compound([r["pool"] for r in rows]), compound([r["selected"] for r in rows if r["selected"] is not None])
    return {**base, "status": status, "verdict": verdict, "overall": overall, "segments": segments, "power": power,
            "price_coverage": coverage,
            "by_year": by_year, "by_market": by_market, "held_years": good, "reversed_years": bad,
            "spread_mean": statistics.mean(spreads) if spreads else None,
            "spread_t": _summary(spreads)["t"] if spreads else None,
            "spread_annual": statistics.mean(spreads) * per_year if spreads and per_year else None,
            "periods_per_year": per_year,
            "selected_excess_mean": statistics.mean(picked) if picked else None,
            "selected_beat_share": sum(x > 0 for x in picked) / len(picked) if picked else None,
            "selected_excess_t": _summary(picked)["t"] if picked else None,
            "top_excess_mean": statistics.mean(r["top"] for r in rows) if rows else None,
            "top_excess_t": _summary([r["top"] for r in rows])["t"] if len(rows) > 1 else None,
            "pool_compound": pool_c, "selected_compound": sel_c,
            "periods": rows, "thresholds": {"min_periods": min_periods, "t": t_bar}}
