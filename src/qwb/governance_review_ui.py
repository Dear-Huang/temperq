"""Governance-first UI and typed API, using the same read-only planning service."""
from __future__ import annotations

import csv
import html
import io

import pandas as pd
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import governance_review as gr, review_store as store, strategy

MAX_BYTES = 1_000_000
STATUS = {"conditional": "成交条件待复核", "normal": "参考时点正常", "buy_blocked": "已知禁买", "sell_blocked": "已知禁卖", "suspended": "停牌"}
RULES = {"max_weight": "单票上限", "min_cash": "现金底线", "max_holdings": "持仓数量", "turnover_cap": "换手预算", "sellable_quantity": "可卖数量", "trade_blocked": "交易受阻", "buy_lot": "买入数量规则", "sell_lot": "卖出数量规则", 'original_target_direction': '朝原目标的整手调整边界'}


def esc(value):
    return html.escape(str(value), quote=True)


def money(value):
    return f"{value:,.2f}"


def pct(value):
    return f"{value:.2%}"


def csv_text(rows, columns):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
    def cell(value):
        # Order IDs and contract references are user text; keep them literal
        # when a CSV is opened in a spreadsheet. Evidence JSON stays unchanged.
        if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n'))):
            return "'" + value
        return value
    writer.writeheader()
    writer.writerows({key: cell(value) for key, value in row.items() if key in columns} for row in rows)
    return stream.getvalue()


def parse_csv(raw, columns, *, optional=()):
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("单张表不能超过 1MB")
    reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
    names = reader.fieldnames or []
    if len(names) != len(set(names)) or set(names) - set(columns) - set(optional) or set(columns) - set(names):
        raise ValueError("CSV 列应为 " + ",".join(columns) + ("；可选列 " + ",".join(optional) if optional else ""))
    rows = list(reader)
    if len(rows) > 100 or any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError("CSV 最多 100 行，每行列数须一致")
    return rows


def sample_input():
    # One example that exercises every part of the plan: a new name to buy, a name that
    # dropped out, a locked position, a buy-blocked target and small trades where the
    # minimum commission matters.
    return {"schema_version": "governance_review_input_v1", "name": "合成示例 · 换仓、锁仓与禁买",
            "observed_at": "2026-10-26T15:30:00+08:00", "signal_available_at": "2026-10-26T15:00:00+08:00",
            "execution_date": "2026-10-27", "signal_version": "synthetic-v2", "signal_kind": "target_weight",
            "cash": 30000.,
            "forecasts": [{"code": "600000.SH", "value": .22}, {"code": "000001.SZ", "value": .10},
                          {"code": "601318.SH", "value": .22}, {"code": "000858.SZ", "value": .20},
                          {"code": "600036.SH", "value": .205}],
            "holdings": [{"code": "600000.SH", "quantity": 3000, "sellable_quantity": 3000},
                         {"code": "000001.SZ", "quantity": 2000, "sellable_quantity": 0},
                         {"code": "600036.SH", "quantity": 600, "sellable_quantity": 600},
                         {"code": "002415.SZ", "quantity": 800, "sellable_quantity": 800}],
            "quotes": [{"code": "600000.SH", "price": 10., "status": "normal"},
                       {"code": "000001.SZ", "price": 12., "status": "normal"},
                       {"code": "601318.SH", "price": 50., "status": "normal"},
                       {"code": "000858.SZ", "price": 130., "status": "buy_blocked"},
                       {"code": "600036.SH", "price": 35., "status": "normal"},
                       {"code": "002415.SZ", "price": 30., "status": "normal"}],
            "rules": {"max_holdings": 5, "max_weight": .25, "min_cash": .03, "turnover_cap": .8,
                      "deviation_tolerance": .02, "commission_bp": 3., "minimum_commission": 5.,
                      "slippage_bp": 0., "preference": "fees"}}


TIME_FIELDS = ("observed_at", "signal_available_at")


def local_input(value):
    """Show a stored timestamp in a datetime-local box (Beijing time, minutes)."""
    try:
        stamp = pd.Timestamp(value)
    except (ValueError, TypeError):
        return str(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("Asia/Shanghai")
    return stamp.tz_convert("Asia/Shanghai").strftime("%Y-%m-%dT%H:%M")


def from_local_input(value, label):
    """Read a datetime-local value as Beijing time; explicit offsets are kept."""
    try:
        stamp = pd.Timestamp(str(value).strip())
    except (ValueError, TypeError):
        raise ValueError(f"{label}格式无效，请选择日期和时间") from None
    if pd.isna(stamp):
        raise ValueError(f"请填写{label}")
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("Asia/Shanghai")
    return stamp.isoformat()


def form_values(value=None):
    today = pd.Timestamp.now(tz="Asia/Shanghai").normalize()
    execution = today + pd.offsets.BDay(1)
    value = value or {"name": "我的治理诊断", "observed_at": str(today + pd.Timedelta(hours=15, minutes=30)),
                      "signal_available_at": str(today + pd.Timedelta(hours=15)), "execution_date": str(execution.date()),
                      "signal_version": "v1", "signal_kind": "target_weight", "cash": "", "rules": {}}
    rules = gr.ReviewRules(**value.get("rules", {})).model_dump()
    out = {k: str(v) for k, v in value.items() if k not in {"rules", "forecasts", "holdings", "quotes"}}
    for key in TIME_FIELDS:
        if out.get(key):
            out[key] = local_input(out[key])
    for k, v in rules.items():
        out[k] = str(v * 100 if k in {"max_weight", "min_cash", "turnover_cap", "deviation_tolerance"} else v)
    out["signals"] = csv_text(value.get("forecasts", []), ["code", "value"]) if value.get("forecasts") else ""
    out["holdings_csv"] = csv_text(value.get("holdings", []), ["code", "quantity", "sellable_quantity"]) if value.get("holdings") else ""
    out["quotes_csv"] = csv_text(value.get("quotes", []), ["code", "price", "status"]) if value.get("quotes") else ""
    return out


async def input_form(form, values=None):
    if values is None:
        values = {k: str(v) for k, v in form.items() if not hasattr(v, "read") and k != "token"}
    for text, file in (("signals", "signals_file"), ("holdings_csv", "holdings_file"), ("quotes_csv", "quotes_file")):
        upload = form.get(file)
        if upload and getattr(upload, "filename", ""):
            if values.get(text, "").strip():
                raise ValueError("同一张表请选择粘贴或上传其中一种，避免覆盖已有内容")
            raw = await upload.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("单个文件不能超过 1MB")
            values[text] = raw.decode("utf-8-sig")
    if values.get("account_clear") != "yes":
        raise ValueError("请先核对挂单、冻结资金和公司行动；本轮只处理已核清的账户快照")
    signals = parse_csv(values.get("signals", ""), ["code", "value"])
    quotes = parse_csv(values.get("quotes_csv", ""), ["code", "price"], optional=["status"])
    holdings = strategy.read_holdings(values.get("holdings_csv", "").encode("utf-8"))
    rules = {k: float(values.get(k, default)) for k, default in gr.ReviewRules().model_dump().items() if k != "preference"}
    rules["max_holdings"] = int(values["max_holdings"])
    for key in ("max_weight", "min_cash", "turnover_cap", "deviation_tolerance"):
        rules[key] /= 100
    rules["preference"] = values.get("preference", "fees")
    value = {k: values[k] for k in ("name", "observed_at", "signal_available_at", "execution_date", "signal_version", "signal_kind")}
    value["observed_at"] = from_local_input(value["observed_at"], "账户与价格参考时点")
    value["signal_available_at"] = from_local_input(value["signal_available_at"], "信号实际可用时点")
    value.update(cash=float(values["cash"]), forecasts=signals, holdings=holdings, quotes=quotes, rules=rules)
    return gr.ReviewInput.model_validate(value), values


def editor(token, values=None, *, error="", sample=False):
    v = values or form_values()
    def field(name, label, typ="number", **attrs):
        extra = " ".join(f'{k.replace("_", "-")}="{esc(a)}"' for k, a in attrs.items())
        return f'<label>{label}<input name="{name}" type="{typ}" value="{esc(v.get(name, ""))}" {extra} required></label>'
    body = '<link rel="stylesheet" href="/static/governance-review.css"><div class="review-heading"><div><p class="review-kicker">TemperQ / 治理工作台</p><h1>新建治理诊断</h1></div><a href="/review">诊断记录</a></div>'
    if sample:
        body += '<p class="box warn" role="status">合成示例，价格与持仓均非真实账户记录。</p>'
    if error:
        body += f'<p class="box bad" role="alert">{esc(error)}</p>'
    body += f'<form method="post" action="/review" enctype="multipart/form-data" class="review-form"><input type="hidden" name="token" value="{token}">'
    body += '<section><h2>01 · 策略与参考时点</h2><div class="review-fields">'
    body += field("name", "诊断名称", "text", maxlength=80) + field("signal_version", "信号版本", "text", maxlength=100)
    body += field("observed_at", "账户与价格参考时点（北京时间）", "datetime-local") + field("signal_available_at", "信号实际可用时点（北京时间）", "datetime-local") + field("execution_date", "拟执行交易日", "date") + field("cash", "可用现金（元）", min=0, step=".01")
    body += '</div></section><section><h2>02 · 预测信息与账户</h2><fieldset class="review-modes"><legend>预测信息类型</legend>'
    for key, label in (("target_weight", "目标权重"), ("score", "分数（越大越优）"), ("rank", "排名（越小越优）")):
        body += f'<label><input type="radio" name="signal_kind" value="{key}"'+(' checked' if v.get('signal_kind') == key else '')+f'>{label}</label>'
    body += '</fieldset><div class="review-input-tables">'
    for name, file, label, template, placeholder in (("signals", "signals_file", "预测输出", "signals", "code,value\n600000.SH,0.2"), ("holdings_csv", "holdings_file", "现有持仓（空仓可留空）", "holdings", "code,quantity,sellable_quantity\n600000.SH,1000,800"), ("quotes_csv", "quotes_file", "参考价格与交易状态", "quotes", "code,price,status\n600000.SH,10.5,conditional")):
        body += f'<div><label for="{name}">{label}</label><textarea id="{name}" name="{name}" rows="7" placeholder="{esc(placeholder)}">{esc(v.get(name, ""))}</textarea><div class="review-file"><input type="file" name="{file}" accept=".csv" aria-label="上传{label}"><a href="/review/templates/{template}.csv">模板</a></div></div>'
    body += '</div></section><section><h2>03 · 硬约束与优化偏好</h2><div class="review-fields">'
    for args in (("max_holdings", "最多持仓只数", 1, 100, 1), ("max_weight", "单票上限（%）", .01, 100, .01), ("min_cash", "最低现金（%）", 0, 99.99, .01), ("turnover_cap", "本次买卖合计 / 权益上限（%）", 0, 200, .01), ("deviation_tolerance", "可接受的额外目标偏离（百分点）", 0, 25, .01)):
        name, label, mini, maxi, step = args
        body += field(name, label, min=mini, max=maxi, step=step)
    body += '<label>优化优先级<select name="preference">'+''.join(f'<option value="{k}"'+(' selected' if v.get('preference') == k else '')+f'>{label}</option>' for k, label in (("fees", "容忍范围内减少费用"), ("tracking", "尽量贴近原目标")))+'</select></label></div>'
    body += '<details class="review-fees"><summary>费用设置</summary><div class="review-fields">'
    for k, label in (("commission_bp", "佣金（bp）"), ("minimum_commission", "每笔最低佣金（元）"), ("slippage_bp", "额外滑点假设（bp）")):
        body += field(k, label, min=0, step=.01)
    body += '</div></details><label class="review-check"><input type="checkbox" name="account_clear" value="yes" required>已核清挂单、冻结资金及公司行动，以上为可用于本次诊断的账户快照</label></section>'
    body += '<div class="review-submit"><button class="primary" type="submit">计算诊断与调整方案</button><a href="/review/new?example=1">载入合成示例</a></div></form><script src="/static/governance-review.js" defer></script>'
    return body


def report_body(review_id, original, result, token, *, selection_id="", selected_candidate=None):
    chosen = next((c for c in result["candidates"] if c["id"] == result["recommended_id"]), None)
    hold = result["candidates"][0]
    tracking = next((c for c in result["candidates"] if c["id"] == "tracking"), None)
    def confirmation(candidate, label, primary=False):
        cls = "primary" if primary else "secondary"
        return f'<form method="post" action="/review/{review_id}/select"><input type="hidden" name="token" value="{token}"><input type="hidden" name="candidate_id" value="{candidate["id"]}"><input type="hidden" name="result_digest" value="{result["result_digest"]}"><button class="{cls}" type="submit">{label}</button></form>'
    body = '<link rel="stylesheet" href="/static/governance-review.css">'
    body += f'<div class="review-heading"><div><p class="review-kicker">治理诊断 / {esc(original["signal_version"])}</p><h1>{esc(result["name"])}</h1></div><a href="/review/new?from_review={review_id}">修改输入再比较</a></div>'
    body += f'<p class="review-meta">参考时点 {esc(original["observed_at"])} · 拟执行 {esc(original["execution_date"])} · 参考权益 ¥{money(result["equity"])}</p>'
    body += '<nav class="review-section-nav" aria-label="账户方案章节"><a href="#recommendation">推荐调整</a>'
    if result.get('tolerance_comparison'):
        body += '<a href="#tolerance-comparison">费用与偏离</a>'
    body += '<a href="#plan-comparison">全部方案</a>'
    if chosen:
        body += '<a href="#plan-quantities">股数与动作</a>'
    body += '<a href="#review-evidence">计算依据</a></nav>'
    if selection_id:
        label = '：'+esc(selected_candidate['label']) if selected_candidate else ''
        body += f'<p class="notice" role="status">方案已保存{label}。未向账户或券商发送指令。<a href="/review/{review_id}/plans/{selection_id}.csv">下载已确认计划</a></p>'
    body += '<section class="review-recommendation" id="recommendation"><h2>推荐调整</h2>'
    if chosen:
        body += f'<h3>{esc(chosen["label"])}</h3><p>{esc(result["recommendation_reason"])}</p>'
        base = tracking or hold
        metrics = [("预计费用", "¥"+money(chosen["estimated_cost"]), "对比贴近目标方案节省 ¥"+money(base["estimated_cost"]-chosen["estimated_cost"])), ("目标偏离", pct(chosen["target_deviation"]), "当前持仓 "+pct(hold["target_deviation"])), ("本次换手", pct(chosen["turnover"]), "买卖金额合计 / 当前权益"), ("计划剩余现金", "¥"+money(chosen["cash_after"]), "现金占比 "+pct(chosen["cash_weight"]))]
        body += '<div class="review-metrics">'+''.join(f'<div><span>{a}</span><strong>{b}</strong><small>{c}</small></div>' for a,b,c in metrics)+'</div>'
        body += '<p class="review-note">参考价格下的计划测算；拟执行日的价格、交易状态和费用变化后须重新核对。</p>'
        body += '<div class="review-proposal-action">'+confirmation(chosen, '确认并保存推荐方案', primary=True)+'</div>'
    else:
        body += '<h3>先处理当前约束冲突</h3><p>没有同时满足硬约束的候选。现有持仓继续保留，以下列出已知限制及待处理动作。</p>'
    for blocker in result.get("constraint_actions", []):
        body += f'<p class="warn">{esc(blocker.get("code", ""))} {esc(blocker["action"])}</p>'
        if "minimum_weight" in blocker:
            body += f'<p>保留仓位至少 {pct(blocker["minimum_weight"])}，单票上限 {pct(blocker["limit"])}</p>'
        if "minimum_turnover_lower_bound" in blocker:
            body += f'<p>所需换手下界 {pct(blocker["minimum_turnover_lower_bound"])}，本次预算 {pct(blocker["limit"])}</p>'
    body += '</section>'
    comparison = result.get('tolerance_comparison')
    if comparison:
        body += '<section class="review-section" id="tolerance-comparison"><div class="review-section-heading"><h2>接受多少偏离，能少付多少费用</h2>'
        body += f'<a href="/review/{review_id}/comparison.csv">下载比较 CSV</a></div>'
        body += f'<p class="mut">额外偏离上限 {original["rules"]["deviation_tolerance"] * 100:.2f} 个百分点；单票、现金、换手与可卖约束保持本次输入。</p>'
        if comparison['rows']:
            body += '<div class="tw"><table><thead><tr><th>额外偏离容忍度</th><th>预计费用</th><th>较贴近方案少付</th><th>实际目标偏离</th><th>换手</th><th>求解状态</th><th>方案</th></tr></thead><tbody>'
            for row in comparison['rows']:
                body += f'<tr><td>{row["additional_tolerance"] * 100:.2f} 个百分点</td>'
                candidate = next((c for c in result['candidates'] if c['id'] == row['candidate_id']), None)
                if candidate:
                    status = '达到求解容差' if row['solver']['optimal_within_tolerance'] else '可行候选 · 未证最优'
                    body += f'<td>¥{money(row["estimated_cost"])}</td><td>¥{money(row["cost_saved_vs_tracking"])}</td><td>{pct(row["target_deviation"])}</td><td>{pct(row["turnover"])}</td><td>{status}</td><td>'
                    if candidate['id'] == result['recommended_id']:
                        body += '本次推荐'
                    else:
                        body += confirmation(candidate, '保存此档方案')
                    body += '</td>'
                else:
                    status = '模型不可行' if row['status'] == 'infeasible' else '未取得通过核对的解'
                    body += f'<td colspan="6">{status}</td>'
                body += '</tr>'
            body += '</tbody></table></div>'
        else:
            body += '<p class="notice">尚无通过核对的贴近目标方案，无法建立比较基准。请先处理约束冲突。</p>'
        body += '<p class="mut">以本次贴近目标方案为基准，抽取容忍范围内的三个档位（相同档位合并）。费用差为参考价格下的预计差额，不是收益提升；目标偏离包含股票与现金。每档独立核对，未通过的档位保留状态。</p></section>'
    repair = (chosen or hold).get('target_repair')
    if repair and result.get('repair_comparison', {}).get('has_blocks'):
        body += '<section class="review-section" id="target-repair"><h2>目标受阻后，还能调整多少</h2>'
        body += f'<p>按{ "推荐方案" if chosen else "保持持仓" }的费用后权益计算，股票偏离中至少 <b>{pct(repair["blocked_stock_gap_weight"])}</b>（¥{money(repair["blocked_stock_gap_value"])}）受当前交易限制约束；其余股票与现金偏离 {pct(repair["other_deviation_weight"])}，包括整手、费用与预算等影响，未认定均可消除。</p>'
        body += f'<p>当前必须保留的敞口 ¥{money(repair["retained_value"])}；朝原目标的计划调整额 ¥{money(repair["completed_toward_target_value"])} / 请求额 ¥{money(repair["requested_trade_value"])}。完成额是参考价格计划，不是成交或收益。</p>'
        body += '<div class="tw"><table><tr><th>证券</th><th>限制原因</th><th>数量范围</th><th>必须保留市值</th><th>股票偏离下界</th></tr>'
        for row in repair['rows']:
            bounds = f'{row["minimum_quantity"]}–{row["maximum_quantity"]} 股' if row['maximum_quantity'] is not None else f'至少 {row["minimum_quantity"]} 股；买入另受资金与仓位上限约束'
            body += f'<tr><td>{esc(row["code"])}</td><td>{esc("；".join(row["reasons"]))}</td><td>{esc(bounds)}</td><td>¥{money(row["retained_value"])}</td><td>¥{money(row["blocked_stock_gap_value"])}</td></tr>'
        body += '</table></div><p>'+esc(result['repair_comparison']['authorization'])+'</p>'
        if result['repair_comparison']['target_only_status'] not in (0, 1):
            body += '<p class="warn">仅朝原目标调整未找到满足全部硬约束的方案；未自动扩大调整范围或放松预算。</p>'
        body += '<p class="mut">'+esc(repair['meaning'])+'</p></section>'
    body += '<section class="review-section"><h2>逐环节诊断</h2><div class="review-findings">'
    for finding in result["findings"]:
        badge = {"plan": "已计算", "blocked": "待处理", "limited": "部分可计算", "conditional": "条件待复核", "needs_history": "可补充历史复验"}[finding["status"]]
        body += f'<article><header><h3>{esc(finding["title"])}</h3><span>{badge}</span></header><p>{esc(finding["action"])}</p>'
        if "current" in finding:
            unit = finding["unit"]
            fmt = money if unit == "cny" else pct
            prefix = '¥' if unit == 'cny' else ''
            labels = ('预算上限', '计划使用') if unit == 'gross_turnover' else ('贴近目标方案', '推荐方案') if unit == 'cny' else ('现有持仓', '推荐方案')
            body += f'<div class="review-change"><span>{labels[0]}</span>{prefix}{fmt(finding["current"])} <span>{labels[1]}</span>{prefix}{fmt(finding["proposed"])}</div>'
        if finding.get("locked"):
            body += '<p>'+ '；'.join(f'{esc(r["code"])} 锁定 {r["quantity"]} 股' for r in finding["locked"])+'</p>'
        body += '</article>'
    body += '</div></section><section class="review-section" id="plan-comparison"><h2>方案对照</h2><div class="tw"><table><thead><tr><th>方案</th><th>预计费用</th><th>目标偏离</th><th>换手</th><th>现金</th><th>约束</th><th></th></tr></thead><tbody>'
    def plan_key(candidate):
        return tuple((row["code"], row["proposed_quantity"]) for row in candidate["rows"])
    first_with_plan = {}
    for candidate in result["candidates"]:
        first_with_plan.setdefault(plan_key(candidate), candidate)
    recommended_key = plan_key(chosen) if chosen else None
    for candidate in result["candidates"]:
        label = esc(candidate["label"]) + (' · 推荐' if candidate["id"] == result["recommended_id"] else '')
        twin = first_with_plan[plan_key(candidate)]
        same_as_recommended = chosen is not None and candidate["id"] != chosen["id"] and plan_key(candidate) == recommended_key
        if same_as_recommended:
            label += '<br><small class="mut">股数与推荐方案相同</small>'
        elif twin["id"] != candidate["id"]:
            label += f'<br><small class="mut">股数与「{esc(twin["label"])}」相同</small>'
        status = "满足参考约束" if candidate["feasible"] else "；".join(sorted({RULES.get(v["rule"], v["rule"]) for v in candidate["violations"]}))
        body += f'<tr><td>{label}</td><td>¥{money(candidate["estimated_cost"])}</td><td>{pct(candidate["target_deviation"])}</td><td>{pct(candidate["turnover"])}</td><td>¥{money(candidate["cash_after"])}</td><td>{status}</td><td>'
        duplicate = twin["id"] != candidate["id"] or same_as_recommended
        if candidate["feasible"] and candidate["id"] != result["recommended_id"] and not duplicate:
            body += confirmation(candidate, '改用这个方案')
        body += '</td></tr>'
    body += '</tbody></table></div></section>'
    if chosen:
        body += '<section class="review-section" id="plan-quantities"><h2>推荐方案 · 股数与动作</h2><div class="tw"><table><thead><tr><th>股票</th><th>当前 / 可卖</th><th>参考价</th><th>目标股数</th><th>调整股数</th><th>预计费用</th></tr></thead><tbody>'
        for row in chosen["rows"]:
            body += f'<tr><td>{esc(row["code"])}</td><td>{row["current_quantity"]} / {row["sellable_quantity"]}</td><td>{money(row["price"])}</td><td>{row["proposed_quantity"]}</td><td>{row["delta"]:+d}</td><td>{money(row["estimated_cost"])}</td></tr>'
        body += '</tbody></table></div></section>'
    body += '<details class="review-section" id="review-evidence"><summary>输入核对与计算依据</summary>'
    for notice in result["input_notices"]:
        body += f'<p class="warn">{esc(notice)}</p>'
    body += '<p>目标偏离为股票及现金权重相对原目标的绝对偏差之和；不等于预计收益损失。印花税按拟执行日期计算，滑点为输入假设。当前未模拟未来收益、公司行动或实际成交。</p>'
    body += '<p>求解状态：'+esc('；'.join({0: '达到所列模型求解容差', 1: '达到时间限制，仅保留通过独立核对的可行候选', 2: '所列约束模型不可行', 3: '模型无界', 4: '求解未完成'}.get(a["status"], '求解状态未知') for a in result["solver_attempts"]))+'</p>'
    body += '<p>依据：A 股冻结数量及费用规则；HiGHS 整数规划；目标偏离与费用的显式权衡。本次账户计划不等同于论文参数在该账户上通过历史复验。</p>'
    body += f'<p><a href="/review/{review_id}/evidence.json">导出输入与诊断证据</a></p></details><p><a href="/review">返回诊断记录</a></p>'
    return body


def register(app, workdir, *, page, guard, token):
    def failure(exc, values=None):
        # Validation messages contain submitted data; return only escaped local text.
        response = page("修改诊断输入", editor(token, values, error=str(exc)))
        response.status_code = 400
        return response

    def load(review_id):
        try:
            return store.read(workdir, review_id)
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(404, "诊断记录不存在或完整性核对失败") from None

    @app.get("/review")
    def index():
        body = '<link rel="stylesheet" href="/static/governance-review.css"><div class="review-heading"><div><p class="review-kicker">TemperQ</p><h1>治理工作台</h1></div><a class="btn primary" href="/review/new">新建治理诊断</a></div>'
        records = store.history(workdir)
        if records:
            body += '<section class="review-section"><h2>诊断记录</h2><div class="review-history">'
            for r in records:
                try:
                    _, _, res = store.read(workdir, r["id"])
                    chosen = next((c for c in res["candidates"] if c["id"] == res["recommended_id"]), None)
                    outcome = "先处理约束冲突" if chosen is None else (
                        f'{esc(chosen["label"])} · {chosen.get("order_count", 0)} 笔调整 · 预计费用 ¥{money(chosen["estimated_cost"])}')
                except (ValueError, OSError, KeyError):
                    outcome = "记录无法读取"
                when = pd.Timestamp(r["created_at"]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
                body += f'<a href="/review/{r["id"]}"><strong>{esc(r["name"])}</strong><span>{outcome}</span><span>{when} · 查看方案 →</span></a>'
            body += '</div></section>'
        else:
            body += '<section class="review-empty"><h2>还没有诊断记录</h2><div class="review-steps"><span>01 · 预测信息</span><span>02 · 持仓与规则</span><span>03 · 调整方案</span></div><a href="/review/new?example=1">载入合成示例</a></section>'
        body += '<section class="review-section"><h2>策略与资源</h2><div class="review-resources"><a href="/strategy-audits">导入平台回测，查看归因与治理审计 →</a><a href="/data">行情数据 →</a></div></section>'
        return page("治理工作台", body)

    @app.get("/review/new")
    def new(example: bool = False, from_review: str = ""):
        original = load(from_review)[1] if from_review else sample_input() if example else None
        return page("新建治理诊断", editor(token, form_values(original), sample=example))

    @app.get("/review/templates/{name}.csv")
    def template(name: str):
        columns = {"signals": ["code", "value"], "holdings": ["code", "quantity", "sellable_quantity"], "quotes": ["code", "price", "status"]}
        if name not in columns:
            raise HTTPException(404)
        return Response("\ufeff" + csv_text([], columns[name]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})

    @app.post("/review")
    async def create(req: Request):
        form = await req.form(max_files=3, max_fields=40, max_part_size=MAX_BYTES)
        guard(req, form.get("token"))
        values = {k: str(v) for k, v in form.items() if not hasattr(v, "read") and k != "token"}
        try:
            case, values = await input_form(form, values)
            result = await run_in_threadpool(gr.review, case)
            review_id = store.save(workdir, case, result)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return failure(exc, values)
        return RedirectResponse("/review/" + review_id, 303)

    @app.post("/api/governance-reviews")
    async def api_create(req: Request):
        guard(req, req.headers.get("x-temperq-token"))
        chunks, size = [], 0
        async for chunk in req.stream():
            size += len(chunk)
            if size > MAX_BYTES:
                raise HTTPException(413, "诊断请求不能超过 1MB")
            chunks.append(chunk)
        try:
            case = gr.ReviewInput.model_validate_json(b"".join(chunks))
            result = await run_in_threadpool(gr.review, case)
            identity = store.save(workdir, case, result)
            return JSONResponse({"id": identity, "url": "/review/"+identity, "result": result}, status_code=201)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/review/{review_id}")
    def show(review_id: str, selection: str = ""):
        _, original, result = load(review_id)
        selected_candidate = None
        if selection:
            try:
                _, selected_candidate = store.selection(workdir, review_id, selection)
            except (ValueError, OSError, KeyError, StopIteration):
                raise HTTPException(404, "方案确认记录不存在") from None
        return page("治理诊断", report_body(review_id, original, result, token, selection_id=selection, selected_candidate=selected_candidate))

    @app.post("/review/{review_id}/select")
    async def select(review_id: str, req: Request):
        form = await req.form(); guard(req, form.get("token"))
        load(review_id)
        try:
            record = store.select(workdir, review_id, str(form.get("candidate_id", "")), str(form.get("result_digest", "")))
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from None
        return RedirectResponse(f'/review/{review_id}?selection={record["id"]}', 303)

    @app.get("/review/{review_id}/evidence.json")
    def evidence(review_id: str):
        record, original, result = load(review_id)
        return JSONResponse({"record": record, "input": original, "result": result}, headers={"Content-Disposition": 'attachment; filename="governance-evidence.json"'})

    @app.get("/review/{review_id}/comparison.csv")
    def comparison_csv(review_id: str):
        _, _, result = load(review_id)
        comparison = result.get('tolerance_comparison')
        if comparison is None:
            raise HTTPException(404, '此历史报告未计算容忍度比较，可修改输入后重新诊断')
        rows = [{**row, 'solver_status': row['solver']['status'], 'solver_gap': row['solver'].get('gap'),
                 'optimal_within_tolerance': row['solver']['optimal_within_tolerance']} for row in comparison['rows']]
        cols = ['additional_tolerance', 'deviation_bound', 'candidate_id', 'status', 'estimated_cost',
                'cost_saved_vs_tracking', 'target_deviation', 'deviation_added_vs_tracking', 'turnover',
                'cash_after', 'solver_status', 'solver_gap', 'optimal_within_tolerance']
        return Response('\ufeff' + csv_text(rows, cols), media_type='text/csv',
                        headers={'Content-Disposition': 'attachment; filename="tolerance-comparison.csv"'})

    @app.get("/review/{review_id}/plans/{selection_id}.csv")
    def plan(review_id: str, selection_id: str):
        load(review_id)
        try:
            _, candidate = store.selection(workdir, review_id, selection_id)
        except (ValueError, OSError, KeyError, StopIteration):
            raise HTTPException(404, "请先确认并保存候选方案") from None
        cols = ["code", "current_quantity", "sellable_quantity", "price", "proposed_quantity", "delta", "estimated_cost", "quote_status"]
        return Response("\ufeff" + csv_text(candidate["rows"], cols), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="reference-price-plan.csv"'})
