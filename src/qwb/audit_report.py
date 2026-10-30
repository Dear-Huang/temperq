"""The exportable audit report: one self-contained HTML file for a reader who was not at the screen.

It only arranges what the audit already computed; nothing is recalculated here except the
summary return statistics, which come from the reconciled account equity (and, when the export
has it, the platform's own benchmark net value). The structure follows a professional audit
report: cover, conclusions, the strategy's premise, return and risk, attribution, costs,
findings with evidence grades, scope and limitations, appendix. Prints to PDF from the browser.
"""
from __future__ import annotations

import math
import statistics
from datetime import date

from . import audit_actions, audit_attribution, audit_verdicts
from .audit_rules import EVIDENCE
from .governance_review_ui import esc

SEVERITY = {"adjust": 0, "evidence": 1, "pass": 2}
STATE_WORDS = {"adjust": "需要调整", "evidence": "缺乏证据", "pass": "基本通过"}
CARD_WORDS = {"act": "建议处理", "review": "建议核对", "hold": "暂不改"}
PLATFORMS = {"bigquant_v1": "BigQuant", "joinquant_v1": "聚宽", "ricequant_v1": "米筐", "generic_v1": "通用格式",
             "code_only_v1": "仅代码"}
CHECK_WORDS = {"consistent": "一致", "review": "需核对", "missing": "未提供", "inconsistent": "不一致"}


def _yuan(v):
    return "—" if v is None else f"¥{(0. if abs(v) < .5 else v):,.0f}"


def _pct(v, digits=1):
    return "—" if v is None else f"{v:.{digits}%}"


def starting_capital(bundle):
    """Capital before the first trade: declared opening cash, else the platform's own net value, else the code."""
    opening = bundle.get("opening") or {}
    if opening.get("kind") == "empty" and opening.get("cash"):
        return opening["cash"], "声明的期初现金"
    snaps = bundle.get("snapshots") or []
    returns = {r["date"]: r["strategy_pct"] for r in bundle.get("platform_returns") or [] if r.get("strategy_pct") is not None}
    if snaps and snaps[0]["date"] in returns and returns[snaps[0]["date"]] > -100:
        return snaps[0]["equity"] / (1 + returns[snaps[0]["date"]] / 100), "平台净值反推的初始资金"
    values = {c["value"] for c in (bundle.get("code") or {}).get("candidates", [])
              if c["key"] == "capital_base" and isinstance(c["value"], (int, float)) and c["value"] > 0}
    if len(values) == 1:
        return float(values.pop()), "代码中的初始资金（未确认）"
    return None, "首日收盘权益（未计首日盈亏）"


def performance(bundle):
    """Return and risk from the account equity; benchmark from the platform's own net value when exported."""
    snaps = bundle.get("snapshots") or []
    if len(snaps) < 2:
        return None
    first, last = date.fromisoformat(snaps[0]["date"]), date.fromisoformat(snaps[-1]["date"])
    years = max((last - first).days / 365.25, 1 / 365.25)
    base, basis = starting_capital(bundle)
    # Measured from the capital before the first trade, as platforms report it; the first day's result counts.
    equity = ([base] if base else []) + [s["equity"] for s in snaps]
    daily = [b / a - 1 for a, b in zip(equity, equity[1:]) if a > 0]
    total = equity[-1] / equity[0] - 1
    out = {"start": snaps[0]["date"], "end": snaps[-1]["date"], "years": years, "days": len(snaps),
           "start_equity": equity[0], "end_equity": equity[-1], "total": total, "basis": basis,
           "annual": (1 + total) ** (1 / years) - 1 if total > -1 else None,
           # Platforms such as RiceQuant annualise over 252 trading days per year instead of calendar time.
           "annual_252": (1 + total) ** (252 / len(snaps)) - 1 if total > -1 else None,
           "volatility": statistics.stdev(daily) * math.sqrt(252) if len(daily) > 1 else None}
    out["sharpe"] = (statistics.mean(daily) / statistics.stdev(daily) * math.sqrt(252)
                     if len(daily) > 1 and statistics.stdev(daily) else None)
    peak, worst = equity[0], 0.
    for e in equity:
        peak = max(peak, e)
        worst = max(worst, 1 - e / peak if peak else 0)
    out["max_drawdown"] = worst
    out["calmar"] = out["annual"] / worst if out["annual"] is not None and worst else None
    bench = [r for r in bundle.get("platform_returns") or [] if r.get("benchmark_pct") is not None]
    if len(bench) >= 2:
        nav = {r["date"]: 1 + r["benchmark_pct"] / 100 for r in bench}
        days = [s["date"] for s in snaps if s["date"] in nav]
        if len(days) >= 2:
            b_start = 1. if base else nav[days[0]]
            b_total = nav[days[-1]] / b_start - 1
            out["benchmark_total"] = b_total
            out["benchmark_annual"] = (1 + b_total) ** (1 / years) - 1 if b_total > -1 else None
            eq = {s["date"]: s["equity"] for s in snaps}
            s_total = eq[days[-1]] / (base or eq[days[0]]) - 1
            out["excess_total"] = s_total - b_total
            if out["annual"] is not None and out["benchmark_annual"] is not None:
                out["excess_annual"] = out["annual"] - out["benchmark_annual"]
            sd = [eq[b] / eq[a] - 1 - (nav[b] / nav[a] - 1) for a, b in zip(days, days[1:])]
            if len(sd) > 1 and statistics.stdev(sd):
                out["tracking_error"] = statistics.stdev(sd) * math.sqrt(252)
                out["information_ratio"] = statistics.mean(sd) / statistics.stdev(sd) * math.sqrt(252)
    years_ret = {}
    for s in snaps:
        y = s["date"][:4]
        years_ret.setdefault(y, [s["equity"], s["equity"]])[1] = s["equity"]
    prev = None
    rows = []
    for y, (a, b) in sorted(years_ret.items()):
        start = prev if prev is not None else (equity[0] if base else a)
        rows.append({"year": y, "return": b / start - 1 if start else None, "partial": prev is None or y == out["end"][:4]})
        prev = b
    out["by_year"] = rows
    return out


# Platform summary names for the same quantity, across RiceQuant and JoinQuant-style exports.
PLATFORM_METRICS = (
    ("total", "区间收益", ("累计收益率", "回测收益")),
    ("annual", "年化收益", ("年化收益率", "回测年化收益")),
    ("max_drawdown", "最大回撤", ("最大回撤",)),
    ("benchmark_total", "基准收益", ("基准累计收益率", "基准收益")),
    ("sharpe", "夏普比率", ("夏普比率", "夏普率")),
)


def _number(text):
    text = (text or "").strip().replace(",", "")
    try:
        return float(text[:-1]) / 100 if text.endswith("%") else float(text)
    except ValueError:
        return None


def platform_check(bundle, perf):
    """The platform's own headline figures beside the audit's, recomputed from the account."""
    summary = bundle.get("platform_summary") or {}
    if not summary or not perf:
        return []
    rows = []
    for key, label, names in PLATFORM_METRICS:
        value = next((_number(summary[n]) for n in names if n in summary), None)
        ours = perf.get(key)
        if value is None or ours is None:
            continue
        ratio = key != "sharpe"
        gap = ours - value
        agree = abs(gap) <= (.001 if ratio else .05)
        note = "一致" if agree else ("平台扣除了无风险利率，审计按 0 计" if key == "sharpe" else "不一致，需核对材料是否来自同一次回测")
        if key == "annual" and not agree and perf.get("annual_252") is not None and abs(perf["annual_252"] - value) <= .001:
            agree, note = True, f"一致：平台按每年 252 个交易日年化（{perf['annual_252']:.2%}），本报告按日历年"
        rows.append({"label": label, "platform": value, "audit": ours, "ratio": ratio, "agree": agree, "note": note})
    return rows


def scope(result):
    """Which checks ran, which could not run for missing materials, and which the engine does not have yet.

    Read from the engine's own coverage table; the report never claims a check it did not run.
    """
    done, blocked, absent = [], [], []
    for stage in result.get("coverage") or []:
        for method in stage.get("algorithms") or []:
            row = {"stage": stage["title"], "check": method["name"], "missing": method.get("missing") or []}
            if method["implementation"] in ("connected", "available") and method["inputs"] == "ready":
                done.append(row)
            elif method["implementation"] in ("connected", "available"):
                blocked.append(row)                    # the engine can do it; the materials do not allow it
            else:
                absent.append(row)                     # not built yet, whatever the materials
    return {"done": done, "blocked": blocked, "absent": absent}


def claim_check(result, perf):
    """A claimed return beside the audited one. The claim is the user's entry, never inferred."""
    claim = (result.get("settings") or {}).get("claim")
    if not claim or not perf:
        return None
    rows = []
    for key, label in (("total", "累计收益"), ("annual", "年化收益")):
        if isinstance(claim.get(key), (int, float)) and perf.get(key) is not None:
            rows.append({"label": label, "claimed": claim[key], "audited": perf[key], "gap": perf[key] - claim[key]})
    if not rows:
        return None
    worst = min(rows, key=lambda r: r["gap"])
    if all(abs(r["gap"]) <= .01 for r in rows):
        verdict = "复算收益与声称收益一致（相差不超过 1 个百分点）"
    elif worst["gap"] < 0:
        verdict = f"复算{worst['label']}比声称低 {-worst['gap']:.1%}：同一份材料复现不出声称的收益"
    else:
        verdict = f"复算{worst['label']}比声称高 {worst['gap']:.1%}"
    return {"source": claim.get("source", ""), "rows": rows, "verdict": verdict,
            "note": "声称收益常用不同区间、不同费率或未扣滑点；不一致时先核对区间和费用设置"}


# Professional terms stay in the report; each gets a short hint the first time it appears.
GLOSSARY = {
    "IC": "信息系数：每期按分数排序和随后收益排序的相关程度，+1 完全一致，0 无关",
    "t": "t 值：均值除以它的标准误；绝对值越大越不像运气，常用 2 为门槛",
    "检验力": "样本能测出真实效果的把握；不足时，没测出不等于不存在",
    "最大回撤": "从净值高点到其后低点的最大跌幅",
    "夏普比率": "年化收益除以年化波动；本报告无风险利率按 0 计",
    "信息比率": "相对基准的年化超额收益除以超额收益的波动",
    "卡玛比率": "年化收益除以最大回撤",
    "HHI": "赫芬达尔指数：各持仓权重平方和，越大越集中",
    "FIFO": "先进先出：卖出时先配对最早买入的那一批，用来算每笔持有期",
    "右删失": "期末仍未卖出的持仓，持有期还没结束，只能算到期末",
    "STRUCT": "按最小报价单位估计的买卖价差下限情景，不是实测成本",
    "bp": "基点，0.01%",
    "样本内": "结果来自用来设计或调参的同一段历史，不代表以后",
}


def term(word, label=None):
    """A term with its hint on hover and in print."""
    return f'<abbr title="{esc(GLOSSARY[word])}">{esc(label or word)}</abbr>'


def answers(bundle, result, perf, premise, cards):
    """The three questions a client asks, answered only from what the engine computed."""
    modules = result["modules"]
    account = (modules.get("accounting") or {}).get("status")
    out = []
    # 1. Can the return be trusted?
    trust, notes = "evidence", []
    if account != "verified":
        trust = "adjust"
        notes.append("没有任何成交" if not bundle.get("fills") else "账目没有闭合，收益是暂算")
    else:
        notes.append("成交、持仓、现金逐日对得上")
    checks = platform_check(bundle, perf) if perf else []
    if checks:
        bad = [c for c in checks if not c["agree"] and c["label"] != "夏普比率"]
        notes.append("与平台自报的收益和回撤一致" if not bad else "与平台自报数字不一致：" + "、".join(c["label"] for c in bad))
        trust = "adjust" if bad else trust
    claim = claim_check(result, perf)
    if claim:
        notes.append(claim["verdict"])
        trust = "adjust" if "复现不出" in claim["verdict"] else trust
    timing = modules.get("fill_timing") or {}
    if timing.get("status") in ("sensitive", "stable"):
        notes.append(timing["verdict"])
        trust = "adjust" if timing["status"] == "sensitive" else trust
    elif timing.get("open_share"):
        notes.append(f"{timing['open_share']:.0%} 的成交在开盘几分钟内，但没有分钟价，没检验换个时点成交会怎样")
    tick = (modules.get("tick_analysis") or {}).get("accounted_intervals") or {}
    if tick.get("profit_covers_scenario") is False:
        notes.append("扣掉最小价位的买卖价差情景后不再盈利")
        trust = "adjust"
    if account == "verified" and trust == "evidence" and checks and not timing.get("status") == "sensitive":
        trust = "pass"
    ret = (f"区间收益 {_pct(perf['total'])}，最大回撤 {_pct(perf['max_drawdown'])}"
           + (f"，同期基准 {_pct(perf['benchmark_total'])}" if perf.get("benchmark_total") is not None else "")) if perf else "无法计算收益"
    out.append({"q": "收益能信吗", "tone": trust, "lead": ret, "notes": notes})
    # 2. Was the idea right?
    if not premise:
        out.append({"q": "你的想法对吗", "tone": "evidence", "lead": "没有识别出策略依据的因子，也没有每日打分，本次没检验",
                    "notes": ["补每日打分和候选池收盘价后可检验（见第七节）"]})
    else:
        tone = {"held": "pass", "reversed": "adjust"}.get(premise["status"], "evidence")
        out.append({"q": "你的想法对吗", "tone": tone, "lead": f"前提：{premise['premise']}",
                    "notes": [premise["verdict"]]})
    # 3. What to change (at most three, most urgent first)
    picks = [c for c in cards if c["state"] in ("act", "review")][:3]
    out.append({"q": "该改什么", "tone": "adjust" if any(c["state"] == "act" for c in picks) else ("evidence" if picks else "pass"),
                "lead": f"{len(picks)} 条建议" if picks else "本次没有需要改的",
                "notes": [c["headline"] + (f"（{'原策略' if c['edits'][0].get('embedded') else ''}第 {c['edits'][0]['line']} 行）"
                                           if c.get("edits") and c["edits"][0].get("line") else "") for c in picks]})
    return out


def headline(bundle, result, perf, premise, cards, verdicts):
    """Three to five sentences a reader can act on, most important first."""
    lines = []
    account = result["modules"].get("accounting") or {}
    if account.get("status") == "verified":
        lines.append(("pass", "账目闭合：成交、持仓、现金与权益逐日核对一致，以下数字可以复算。"))
    elif not bundle.get("fills"):
        lines.append(("adjust", "回测期间没有任何成交：策略从未买入，收益为零不是策略结果。"
                                "先查运行日志里的下单失败或信号条件，修正后重新回测再审计。"))
    else:
        lines.append(("adjust", "账目未闭合：以下盈亏与收益为暂算，须先修复材料再引用。"))
    if premise and premise.get("status") not in (None, "needs_prices", "needs_scores"):
        tone = {"held": "pass", "reversed": "adjust"}.get(premise["status"], "evidence")
        lines.append((tone, f"策略前提「{premise['premise']}」：{premise['verdict']}。"))
    elif premise:
        lines.append(("evidence", f"策略前提「{premise['premise']}」未能检验：{premise['verdict']}。"))
    if perf:
        line = f"区间收益 {_pct(perf['total'])}（年化 {_pct(perf['annual'])}），最大回撤 {_pct(perf['max_drawdown'])}"
        if perf.get("excess_total") is not None:
            line += f"；同期基准 {_pct(perf['benchmark_total'])}，超额 {perf['excess_total']:+.1%}"
        lines.append(("evidence", line + "。收益未扣除价差情景，且为样本内回测。"))
    act = [c for c in cards if c["state"] == "act"]
    adjust = [v for v in verdicts if v["state"] == "adjust"]
    if act or adjust:
        names = "、".join([c["title"] for c in act] + [v["title"] for v in adjust])
        lines.append(("adjust", f"需要处理：{names}。"))
    review = [c for c in cards if c["state"] == "review"]
    if review:
        lines.append(("evidence", "建议核对：" + "、".join(c["title"] for c in review) + "。"))
    return lines[:6]


STYLE = """
:root{--ink:#1d2430;--mut:#5b6575;--line:#d9dee6;--bg:#ffffff;--soft:#f4f6f9;--pass:#1f7a4d;--evidence:#9a6400;--adjust:#b3261e;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--ink:#e6e9ee;--mut:#a3acb9;--line:#353c48;--bg:#14181e;--soft:#1c222b;--pass:#5cc58f;--evidence:#e0b45a;--adjust:#ff8a80;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:920px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--ink)}
h3{font-size:15px;margin:20px 0 8px}p{margin:6px 0}.mut{color:var(--mut);font-size:13px}
.cover{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:8px}
.kicker{color:var(--mut);font-size:12px;letter-spacing:.08em;margin:0 0 6px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:6px 20px;margin-top:12px;font-size:13px}
.meta span{color:var(--mut)}
.tw{overflow-x:auto;margin:8px 0}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--soft);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:10px 0}
.kpis div{background:var(--soft);border-radius:6px;padding:10px 12px}.kpis span{display:block;color:var(--mut);font-size:12px}
.kpis strong{font-size:18px;font-variant-numeric:tabular-nums}
ol.findings{padding-left:20px}ol.findings li{margin:6px 0}
.tag{display:inline-block;font-size:12px;border:1px solid currentColor;border-radius:4px;padding:0 6px;margin-right:6px;white-space:nowrap}
.pass{color:var(--pass)}.evidence{color:var(--evidence)}.adjust{color:var(--adjust)}
.card{border:1px solid var(--line);border-radius:6px;padding:12px 14px;margin:10px 0;break-inside:avoid}
.answers{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:18px 0}
.answer{border:1px solid var(--line);border-left:4px solid currentColor;border-radius:6px;padding:12px 14px}
.answer h3{margin:0 0 6px;display:flex;justify-content:space-between;align-items:center;color:var(--ink)}
.answer .lead{font-weight:600;color:var(--ink)}.answer ul{margin:6px 0 0;padding-left:18px;color:var(--ink)}
abbr{text-decoration:underline dotted;cursor:help}
dl.glossary{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px}dl.glossary dt{font-weight:600}dl.glossary dd{margin:0}
.card h3{margin:0 0 6px}.neg{color:var(--adjust)}
footer{margin-top:40px;border-top:1px solid var(--line);padding-top:10px;color:var(--mut);font-size:12px}
@media print{body{font-size:12px}main{padding:0;max-width:none}h2{break-after:avoid}.card,table{break-inside:avoid}a{color:inherit;text-decoration:none}}
"""


def render(record, bundle, result):
    modules = result["modules"]
    attr = audit_attribution.build(result) or {}
    perf = performance(bundle)
    premise = modules.get("premise")
    cards = audit_actions.cards(result, bundle)
    verdicts = sorted(audit_verdicts.conclusions(bundle, result), key=lambda v: SEVERITY[v["state"]])
    lines = headline(bundle, result, perf, premise, cards, verdicts)
    platform = PLATFORMS.get(bundle.get("adapter", ""), bundle.get("adapter", ""))
    snaps = bundle.get("snapshots") or []
    period = f'{snaps[0]["date"]} 至 {snaps[-1]["date"]}' if snaps else "—"
    s = result.get("settings") or {}
    fee_basis = (f'佣金 {s.get("commission_bp", "—")}bp、最低 {s.get("minimum_commission", "—")} 元'
                 + ("（券商合同费率已确认）" if s.get("broker_confirmed") else "（未确认合同费率，按情景）"))

    h = [f'<title>{esc(bundle["name"])} · 策略审计报告</title><style>{STYLE}</style><main>']
    # Cover
    h.append('<header class="cover"><p class="kicker">TEMPERQ · 策略回测审计与诊断报告</p>'
             f'<h1>{esc(bundle["name"])}</h1><div class="meta">'
             f'<div><span>回测平台</span><br>{esc(platform)}</div><div><span>审计区间</span><br>{esc(period)}</div>'
             f'<div><span>费用口径</span><br>{esc(fee_basis)}</div>'
             f'<div><span>报告生成</span><br>{esc((record.get("created_at") or "")[:19].replace("T", " ") or "—")}</div>'
             f'<div><span>输入摘要</span><br><code>{esc(result["input_digest"][7:19])}</code></div>'
             f'<div><span>结果摘要</span><br><code>{esc(result["result_digest"][7:19])}</code></div></div></header>')

    # First screen: the three questions a client asks
    h.append('<section class="answers">')
    for a in answers(bundle, result, perf, premise, cards):
        h.append(f'<div class="answer {a["tone"]}"><h3>{a["q"]}<span class="tag {a["tone"]}">{STATE_WORDS[a["tone"]]}</span></h3>'
                 f'<p class="lead">{esc(a["lead"])}</p>'
                 + ('<ul>' + "".join(f'<li>{esc(n)}</li>' for n in a["notes"]) + '</ul>' if a["notes"] else '') + '</div>')
    h.append('</section>')
    claim = claim_check(result, perf)
    if claim:
        h.append('<div class="tw"><table><tr><th>声称收益对照</th><th class="n">声称</th><th class="n">复算</th><th class="n">差</th></tr>'
                 + "".join(f'<tr><td>{r["label"]}</td><td class="n">{_pct(r["claimed"])}</td><td class="n">{_pct(r["audited"])}</td>'
                           f'<td class="n{" neg" if r["gap"] < 0 else ""}">{r["gap"]:+.1%}</td></tr>' for r in claim["rows"])
                 + '</table></div>' + (f'<p class="mut">出处：{esc(claim["source"])}。' if claim["source"] else '<p class="mut">')
                 + f'{esc(claim["note"])}。</p>')

    # 1. Conclusions
    h.append('<h2>一、结论摘要</h2><ol class="findings">')
    for tone, text in lines:
        h.append(f'<li><span class="tag {tone}">{STATE_WORDS[tone]}</span>{esc(text)}</li>')
    h.append('</ol><p class="mut">结论只覆盖本次导入的材料与所列口径；回测期间的结果是样本内描述，不代表未来表现，也不构成投资建议。</p>')

    # 2. Premise
    h.append('<h2>二、策略前提检验</h2>')
    if not premise:
        h.append('<p>未能识别策略依据的因子，也没有导入每日打分，本次未检验策略前提。补充每日打分（覆盖全部候选）与候选池收盘价后可检验。</p>')
    else:
        h.append(f'<p>前提（{esc(premise["source"])}）：<strong>{esc(premise["premise"])}</strong></p>')
        code = premise.get("code") or {}
        if code.get("factors") and not premise.get("stated"):
            h.append('<p class="mut">识别依据：' + "；".join(f'第 {f["line"]} 行 <code>{esc(f["code"])}</code>' for f in code["factors"][:3])
                     + ("（代码内嵌的原策略）" if code.get("embedded") else "") + '</p>')
        tone = {"held": "pass", "reversed": "adjust"}.get(premise["status"], "evidence")
        h.append(f'<p><span class="tag {tone}">结论</span>{esc(premise["verdict"])}</p>')
        o = premise.get("overall")
        if o and o.get("mean") is not None:
            bar = premise["thresholds"]["t"]
            sig = lambda v: v is not None and abs(v) >= bar
            h.append('<div class="tw"><table><tr><th>问题</th><th>指标</th><th class="n">数值</th><th>读法</th></tr>')
            h.append(f'<tr><td>对不对</td><td>秩相关 {term("IC")} 均值（{term("t")}）</td><td class="n">{o["mean"]:+.3f}（{o["t"]:.2f}）</td>'
                     f'<td>{o["periods"]} 期中 {o["positive_share"]:.0%} 为正；|t| ≥ {bar:g} 才算显著</td></tr>')
            if premise.get("spread_mean") is not None:
                ann = (f'，约年化 {premise["spread_annual"]:+.1%}' if sig(premise.get("spread_t")) and premise.get("spread_annual") is not None
                       else "；不显著，不折年化")
                h.append(f'<tr><td>对多少</td><td>前 1/3 减后 1/3（每期，{term("t")}）</td><td class="n">{premise["spread_mean"]:+.2%}（{premise["spread_t"]:.2f}）</td>'
                         f'<td>不计费用{ann}</td></tr>')
            if premise.get("selected_excess_mean") is not None:
                h.append(f'<tr><td>选股</td><td>选中减候选池（每期，t）</td><td class="n">{premise["selected_excess_mean"]:+.2%}（{premise["selected_excess_t"]:.2f}）</td>'
                         f'<td>{premise["selected_beat_share"]:.0%} 的周期跑赢候选池</td></tr>')
            if premise.get("pool_compound") is not None:
                h.append(f'<tr><td>收益来源</td><td>候选池等权 / 选中 累计</td><td class="n">{premise["pool_compound"]:+.1%} / {premise["selected_compound"]:+.1%}</td>'
                         '<td>不计费用；候选池涨幅是不靠前提也能拿到的部分</td></tr>')
            h.append('</table></div><h3>哪段时间对</h3><div class="tw"><table><tr><th>时间段</th><th class="n">周期</th><th class="n">IC 均值</th><th class="n">t</th><th class="n">前1/3减后1/3</th><th>判断</th></tr>')
            words = {"held": "成立", "reversed": "反了", "unclear": "不显著", "too_few": "期数太少"}
            for title, rows in (("按年份", premise.get("by_year") or []), ("按市场涨跌", premise.get("by_market") or []),
                                ("开发 / 验证", [premise["segments"][k] for k in ("development", "validation") if k in (premise.get("segments") or {})])):
                if rows:
                    h.append(f'<tr><th colspan="6">{title}</th></tr>')
                for r in rows:
                    h.append(f'<tr><td>{esc(r["label"])}</td><td class="n">{r["periods"]}</td>'
                             f'<td class="n">{"—" if r["mean"] is None else format(r["mean"], "+.3f")}</td>'
                             f'<td class="n">{"—" if r["t"] is None else format(r["t"], ".2f")}</td>'
                             f'<td class="n">{"—" if r.get("spread_mean") is None else format(r["spread_mean"], "+.2%")}</td>'
                             f'<td>{words.get(r["status"], "")}</td></tr>')
            h.append(f'</table></div><p class="mut">收益区间：{esc(premise["return_window"])}。{esc(premise["scope"])}。分段越细期数越少，单段结果容易受运气影响。</p>')

    # 3. Return and risk
    h.append('<h2>三、收益与风险</h2>')
    if perf:
        k = [("区间收益", _pct(perf["total"])), ("年化收益", _pct(perf["annual"])), ("年化波动", _pct(perf["volatility"])),
             ("夏普比率", "—" if perf["sharpe"] is None else f'{perf["sharpe"]:.2f}'), ("最大回撤", _pct(perf["max_drawdown"])),
             ("卡玛比率", "—" if perf["calmar"] is None else f'{perf["calmar"]:.2f}')]
        if perf.get("benchmark_total") is not None:
            k += [("基准收益", _pct(perf["benchmark_total"])), ("超额收益", f'{perf["excess_total"]:+.1%}'),
                  ("信息比率", "—" if perf.get("information_ratio") is None else f'{perf["information_ratio"]:.2f}')]
        h.append('<div class="kpis">' + "".join(f'<div><span>{term(a) if a in GLOSSARY else a}</span><strong>{b}</strong></div>'
                                                for a, b in k) + '</div>')
        h.append('<div class="tw"><table><tr><th>年份</th><th class="n">收益</th><th>说明</th></tr>' + "".join(
            f'<tr><td>{r["year"]}</td><td class="n">{_pct(r["return"])}</td><td>{"不足一年" if r["partial"] else ""}</td></tr>' for r in perf["by_year"])
            + '</table></div>')
        dd = (modules.get("risk_analysis") or {}).get("max_drawdown") or {}
        if dd.get("peak_date"):
            names = "、".join(f'{x["code"]} {_yuan(x["net_pnl"])}' for x in sorted(dd.get("stocks") or [], key=lambda x: x["net_pnl"])[:3])
            h.append(f'<p>最大回撤从 {dd["peak_date"]} 到 {dd["trough_date"]}，权益减少 {_yuan(dd["loss"])}'
                     + (f'，{dd["recovery_date"]} 收复' if dd.get("recovery_date") else '，期末尚未收复') + f'。主要来自：{esc(names)}。</p>')
        h.append(f'<p class="mut">起点：{esc(perf["basis"])} {_yuan(perf["start_equity"])}。按账户每日权益计算，无风险利率取 0；基准来自平台导出的基准净值'
                 + ('' if perf.get("benchmark_total") is not None else '（本次未导出，未作基准比较）') + '。</p>')
        check = platform_check(bundle, perf)
        if check:
            fmt = lambda r, v: _pct(v, 2) if r["ratio"] else f"{v:.2f}"
            h.append('<h3>与平台自报数字对照</h3><div class="tw"><table><tr><th>指标</th><th class="n">平台</th><th class="n">审计复算</th><th>结论</th></tr>'
                     + "".join(f'<tr><td>{r["label"]}</td><td class="n">{fmt(r, r["platform"])}</td><td class="n">{fmt(r, r["audit"])}</td>'
                               f'<td class="{"pass" if r["agree"] else "evidence"}">{esc(r["note"])}</td></tr>' for r in check)
                     + '</table></div>')
    else:
        h.append('<p>账户快照不足两天，无法计算收益与风险。</p>')

    # 4. Attribution
    h.append('<h2>四、盈亏归因</h2>')
    if attr:
        h.append('<ul>' + "".join(f'<li>{esc(x)}</li>' for x in attr["summary"]) + '</ul>')
        h.append('<div class="tw"><table><tr><th>项目</th><th class="n">金额</th></tr>' + "".join(
            f'<tr><td>{esc(w["label"])}</td><td class="n{" neg" if w["value"] < 0 else ""}">{_yuan(w["value"])}</td></tr>' for w in attr["waterfall"]) + '</table></div>')
        conc = attr["concentration"]
        noun = conc.get("noun", "股票")
        rows = conc["top"] + conc["bottom"]
        h.append(f'<h3>按{noun}</h3><div class="tw"><table><tr><th>{noun}</th><th class="n">费用前</th><th class="n">费用</th><th class="n">净贡献</th></tr>' + "".join(
            f'<tr><td>{esc(r["code"])}</td><td class="n">{_yuan(r["gross_pnl"])}</td><td class="n">{_yuan(r["fees"])}</td><td class="n{" neg" if r["net_pnl"] < 0 else ""}">{_yuan(r["net_pnl"])}</td></tr>' for r in rows)
            + '</table></div>')
        if attr.get("by_holding"):
            h.append('<h3>按持有期（已平仓，先进先出）</h3><div class="tw"><table><tr><th>持有期</th><th class="n">笔数</th><th class="n">净盈亏</th><th class="n">扣价差情景后</th></tr>' + "".join(
                f'<tr><td>{esc(r["label"])}</td><td class="n">{r["matches"]}</td><td class="n">{_yuan(r["net"])}</td><td class="n">{_yuan(r["after_tick"])}</td></tr>' for r in attr["by_holding"])
                + '</table></div>')
    else:
        h.append('<p>账目未闭合，未出归因。</p>')

    # 5. Costs and execution
    h.append('<h2>五、成本与执行</h2>')
    fees = modules.get("fee_analysis") or {}
    tick = modules.get("tick_analysis") or {}
    rows = [("回测已扣交易费用", _yuan(attr.get("fees") if attr else fees.get("reported_fee")))]
    if fees.get("reported_fee") is not None and fees.get("modeled_fee") is not None:
        rows.append(("按审计口径复算的费用", _yuan(fees["modeled_fee"])))
    interval = tick.get("accounted_intervals") or {}
    if interval.get("tick_scenario_cost") is not None:
        rows.append(("最小价位半档价差情景（未在回测中扣除时）", _yuan(interval["tick_scenario_cost"])))
    fills = bundle.get("fills") or []
    if fills:
        rows.append(("成交笔数 / 有成交的交易日", f'{len(fills)} / {len({f["date"] for f in fills})}'))
        notional = sum(abs(f.get("quantity", 0) * f.get("price", 0)) for f in fills)
        if perf and perf["years"] and perf["start_equity"]:
            rows.append(("年化双边换手（成交额 / 期初权益）", f'{notional / perf["start_equity"] / perf["years"]:.1f} 倍'))
    h.append('<div class="tw"><table><tr><th>项目</th><th class="n">数值</th></tr>' + "".join(f'<tr><td>{a}</td><td class="n">{b}</td></tr>' for a, b in rows) + '</table></div>')
    h.append('<p class="mut">价差为按最小价位估算的情景，不是实测成本；原回测若已计滑点，需先分离再比较。逐笔费用见附件 fees.csv、tick-costs.csv。</p>')
    sequence = modules.get("sequence")
    if sequence:
        h.append('<h3>先后顺序</h3><ul>')
        for key, label in (("signal_after_fill", "信号与成交"), ("funding", "资金衔接"), ("code_hints", "代码提示")):
            part = sequence.get(key)
            if part:
                h.append(f'<li><strong>{label}</strong>：{esc(part["verdict"])}。</li>')
        h.append(f'</ul><p class="mut">{esc(sequence["scope"])}。代码提示只是提醒人工核对，不是结论。</p>')
    quality = modules.get("fill_quality")
    if quality and quality.get("status") in ("normal", "suspicious"):
        h.append('<h3>成交价是否拿得到</h3>')
        rng, floor = quality.get("range"), quality.get("floor")
        if rng:
            tone = "adjust" if rng["status"] == "suspicious" else "pass"
            h.append(f'<p><span class="tag {tone}">位置</span>{esc(rng["verdict"])}。</p>')
            h.append('<div class="tw"><table><tr><th>方向</th><th class="n">笔数</th><th class="n">在当天区间的平均位置</th>'
                     '<th class="n">落在最有利一成以内</th></tr>' + "".join(
                f'<tr><td>{"买入" if side == "buy" else "卖出"}</td><td class="n">{v["fills"]}</td>'
                f'<td class="n">{v["mean_position"]:.0%}</td><td class="n">{v["best_tenth_share"]:.0%}</td></tr>'
                for side, v in rng["sides"].items()) + '</table></div>'
                     '<p class="mut">位置 0% 是当天最低价，100% 是最高价。随机成交时平均约 50%，落在最有利一成以内约 10%。</p>')
        if floor:
            tone = "adjust" if floor.get("net_after") is not None and floor["net_after"] <= 0 < (floor.get("net_pnl") or 0) else "evidence"
            h.append(f'<p><span class="tag {tone}">最差价下限</span>{esc(floor["verdict"])}。</p>')
        h.append(f'<p class="mut">{esc(quality["scope"])}。</p>')
    partial = modules.get("partial_entry")
    if partial:
        h.append('<h3>部分成交与补单</h3>')
        h.append(f'<p><span class="tag {"evidence" if partial["status"] == "found" else "pass"}">结论</span>{esc(partial["verdict"])}。</p>')
        long = sorted((e for e in partial.get("episodes", []) if e["days_without_order"] >= partial.get("long_days", 5)),
                      key=lambda e: -e["days_without_order"])[:5]
        if long:
            h.append('<div class="tw"><table><tr><th>日期</th><th>标的</th><th class="n">下单 / 成交</th><th class="n">未补单交易日</th>'
                     '<th class="n">期间现金占比</th><th>平台原因</th></tr>' + "".join(
                f'<tr><td>{e["date"]}</td><td>{esc(e["code"])}</td><td class="n">{e["ordered"]:,} / {e["filled"]:,}</td>'
                f'<td class="n">{e["days_without_order"]}</td><td class="n">{_pct(e["mean_cash_share"], 0)}</td><td>{esc(e["reason"])}</td></tr>'
                for e in long) + '</table></div>')
        if partial.get("scope"):
            h.append(f'<p class="mut">{esc(partial["scope"])}。</p>')
    timing = modules.get("fill_timing")
    if timing:
        h.append('<h3>成交时点敏感性</h3>')
        if timing.get("open_share") is not None:
            h.append(f'<p>{timing["timed_fills"]} 笔有成交时间，{timing["open_fills"]} 笔在 {timing["open_by"]} 以前成交，'
                     f'占成交额 {timing["open_share"]:.0%}。</p>')
        tone = {"sensitive": "evidence", "stable": "pass"}.get(timing["status"], "evidence")
        h.append(f'<p><span class="tag {tone}">结论</span>{esc(timing["verdict"])}</p>')
        if timing.get("rows"):
            h.append('<div class="tw"><table><tr><th>换到这个时点成交</th><th class="n">笔数</th><th class="n">盈亏变化</th>'
                     '<th class="n">每边（bp）</th><th class="n">成交额覆盖</th></tr>' + "".join(
                f'<tr><td>{esc(r["label"])}</td><td class="n">{r["fills"]}</td><td class="n{" neg" if r["gain"] < 0 else ""}">{_yuan(r["gain"])}</td>'
                f'<td class="n">{r["gain_bp"]:+.1f}</td><td class="n">{_pct(r["coverage"], 0)}</td></tr>' for r in timing["rows"]) + '</table></div>')
        if timing.get("max_volume_share") is not None:
            h.append(f'<p>单笔成交最多占当分钟成交量的 {timing["max_volume_share"]:.0%}（核对 {timing["volume_checked"]} 笔）。</p>')
        h.append(f'<p class="mut">{esc(timing["scope"])}。正数表示换到该时点成交会更好。</p>')

    # 6. Findings
    h.append('<h2>六、发现与建议</h2>')
    if not cards:
        h.append('<p>本次没有可计算的建议。</p>')
    for i, c in enumerate(cards, 1):
        tone = {"act": "adjust", "review": "evidence", "hold": "pass"}[c["state"]]
        h.append(f'<div class="card"><h3>{i}. {esc(c["title"])} <span class="tag {tone}">{CARD_WORDS[c["state"]]}</span>'
                 f'<span class="tag">{esc(EVIDENCE.get(c["evidence"], c["evidence"]))}</span></h3>'
                 f'<p><strong>{esc(c["headline"])}</strong></p><p>{esc(c.get("finding", ""))}</p>')
        for edit in (c.get("edits") or [])[:2]:
            if edit.get("line"):
                h.append(f'<p class="mut">代码位置：{"原策略" if edit.get("embedded") else ""}第 {edit["line"]} 行 '
                         f'<code>{esc(edit.get("code", ""))}</code>' + (f' → {esc(edit["change"])}' if edit.get("change") else '') + '</p>')
        if c.get("options"):
            h.append('<p>可选改法：</p><ul>' + "".join(f'<li>{esc(o["label"])}：{esc(o.get("effect", ""))}</li>' for o in c["options"]) + '</ul>')
        trade = [(k, c.get(k)) for k in ("saving", "sacrifice", "net") if c.get(k)]
        if trade:
            labels = {"saving": "省下", "sacrifice": "可能放弃", "net": "净影响"}
            h.append('<ul class="mut">' + "".join(f'<li>{labels[k]}：{esc(v)}</li>' for k, v in trade) + '</ul>')
        if c.get("conditions"):
            h.append('<p class="mut">前提条件：' + "；".join(esc(x if isinstance(x, str) else x.get("text", "")) for x in c["conditions"]) + '</p>')
        if c.get("overturn"):
            h.append(f'<p class="mut">什么情况下不成立：{esc(c["overturn"])}</p>')
        if c.get("rules"):
            h.append('<p class="mut">依据：' + "；".join(f'{esc(r["id"])} {esc(r["text"])}' for r in c["rules"]) + '</p>')
        h.append('</div>')

    # 7. Scope and limitations
    sc = scope(result)
    h.append(f'<h2>七、审计范围与限制</h2><p>本次共列 {len(sc["done"]) + len(sc["blocked"]) + len(sc["absent"])} 项检查：'
             f'<span class="pass">已审 {len(sc["done"])} 项</span>，<span class="evidence">缺材料未审 {len(sc["blocked"])} 项</span>，'
             f'<span class="mut">本工具尚不具备 {len(sc["absent"])} 项</span>。没有审到的项目不代表没有问题。</p>')
    h.append('<h3>已审</h3><div class="tw"><table><tr><th>环节</th><th>检查</th></tr>'
             + "".join(f'<tr><td>{esc(r["stage"])}</td><td>{esc(r["check"])}</td></tr>' for r in sc["done"]) + '</table></div>')
    if sc["blocked"]:
        h.append('<h3>缺材料未审（补齐后可审）</h3><div class="tw"><table><tr><th>环节</th><th>检查</th><th>需要补充</th></tr>'
                 + "".join(f'<tr><td>{esc(r["stage"])}</td><td>{esc(r["check"])}</td><td>{esc("；".join(r["missing"]) or "确认参数")}</td></tr>'
                           for r in sc["blocked"]) + '</table></div>')
    if sc["absent"]:
        h.append(f'<p class="mut">另有 {len(sc["absent"])} 项检查本工具尚不具备，补材料也审不了，列在附录 C。</p>')
    h.append('<h3>十五个环节的结论</h3><div class="tw"><table><tr><th>环节</th><th>结论</th><th>理由</th></tr>')
    for v in verdicts:
        h.append(f'<tr><td>{esc(v["title"])}</td><td class="{v["state"]}">{STATE_WORDS[v["state"]]}</td><td>{esc(v["reason"])}</td></tr>')
    h.append('</table></div><h3>限制</h3><ul>'
             '<li>只读材料、不运行策略代码；代码参数是静态识别的候选，未确认生效。</li>'
             '<li>所有结果都来自回测期间（样本内）；内部验证段是历史尾段，不能当作未见数据。</li>'
             '<li>价差与冲击成本是情景估计，不是实盘成交成本；未覆盖容量与盘口深度。</li>'
             '<li>未提供的材料（如运行日志、委托号、行业与风格数据）对应的环节只能标为缺乏证据。</li></ul>')

    # Appendix
    h.append('<h2>附录 A · 材料清单与核对</h2><div class="tw"><table><tr><th>文件</th><th>识别类型</th><th class="n">行数</th><th>SHA-256（前 12 位）</th></tr>')
    for src in bundle.get("sources") or []:
        h.append(f'<tr><td>{esc(src["name"])}</td><td>{esc(src["kind"])}</td><td class="n">{src.get("rows", "")}</td><td><code>{esc((src.get("sha256") or "")[:12])}</code></td></tr>')
    h.append('</table></div><div class="tw"><table><tr><th>核对项</th><th>结果</th><th>说明</th></tr>')
    for c in (result.get("input_review") or {}).get("checks") or []:
        h.append(f'<tr><td>{esc(c["title"])}</td><td>{esc(CHECK_WORDS.get(c["status"], c["status"]))}</td><td>{esc(c["detail"])}</td></tr>')
    h.append('</table></div><h2>附录 B · 证据等级</h2><ul>' + "".join(f'<li><strong>{k}</strong>：{esc(v)}</li>' for k, v in EVIDENCE.items()) + '</ul>')
    if sc["absent"]:
        h.append('<h2>附录 C · 本工具尚不具备的检查</h2><div class="tw"><table><tr><th>环节</th><th>检查</th><th>所需材料</th></tr>'
                 + "".join(f'<tr><td>{esc(r["stage"])}</td><td>{esc(r["check"])}</td><td>{esc("；".join(r["missing"]))}</td></tr>'
                           for r in sc["absent"]) + '</table></div>')
    h.append('<h2>附录 D · 术语</h2><dl class="glossary">' + "".join(f'<dt>{esc(k)}</dt><dd>{esc(v)}</dd>' for k, v in GLOSSARY.items()) + '</dl>')
    h.append(f'<footer>TemperQ 本地生成，数据不离开本机。审计版本 {esc(result["schema_version"])}；输入 {esc(result["input_digest"])}；结果 {esc(result["result_digest"])}。'
             '可下载 evidence.json 与各 CSV 复算。本报告不构成投资建议。</footer></main>')
    return "".join(h)
