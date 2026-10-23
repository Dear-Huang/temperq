"""Minimal paper-account history and holdings, using existing workbench styles."""

from __future__ import annotations

import html
import math

import pandas as pd
from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from . import daily_data, daily_operation, experiments, paper, paper_auto
from .strategy_ui import reason_text


def esc(value):
    return html.escape(str(value))


STATES = {"WAITING_DATA": ("等待数据", "pending"), "WAITING_CALENDAR": ("等待日历", "pending"),
          "RUNNING": ("运行中", "active"), "BLOCKED": ("运行受阻", "blocked")}
SCOPES = {"historical_replay": "历史演示", "late_reconstruction": "晚到补记", "forward_paper": "当日前向"}


def statistics(value):
    records = value["sessions"]
    cash = records[-1]["state"]["cash"] if records else value["config"]["cash"]
    equity = records[-1]["equity"] if records else cash if not value["config"]["holdings"] else None
    change = records[-1]["equity"] / records[0]["equity"] - 1 if len(records) > 1 else None
    peak, drawdown = 0, 0
    for record in records:
        peak = max(peak, record["equity"])
        if peak:
            drawdown = min(drawdown, record["equity"] / peak - 1)
    return {"equity": equity, "cash": cash, "change": change,
            "drawdown": drawdown if len(records) > 1 else None,
            "fees": sum((r["audit"] or {}).get("total_cost", 0) for r in records),
            "forward_days": sum(r["scope"] == "forward_paper" for r in records)}


def metric(label, value, note=""):
    return f'<div class="metric"><span>{esc(label)}</span><strong>{esc(value)}</strong><small>{esc(note)}</small></div>'


def equity_chart(records, *, compact=False):
    if len(records) < 2:
        label = "等待首个完整收盘记录" if not records else "已有 1 日记录，等待下个交易日"
        return f'<div class="chart-empty{" compact" if compact else ""}"><span>{label}</span></div>'
    values = [r["equity"] / records[0]["equity"] for r in records]
    if not all(math.isfinite(v) for v in values):
        return '<p class="warn">权益记录异常，暂不绘制曲线</p>'
    low, high = min(values), max(values)
    padding = max((high-low) * .12, .0005)
    bottom, top = low-padding, high+padding
    points = ' '.join(f'{50 + 660*i/(len(values)-1):.1f},{145-125*(v-bottom)/(top-bottom):.1f}' for i, v in enumerate(values))
    labels = '' if compact else f'<div class="chart-scale"><span>{high:.4f}</span><span>{low:.4f}</span></div>'
    return (f'<div class="chart-plot">{labels}<svg class="equity-chart{" compact" if compact else ""}" viewBox="0 0 720 160" preserveAspectRatio="none" '
            'role="img" aria-label="账户净值曲线，首个记录日为 1">'
            '<line x1="50" y1="145" x2="710" y2="145" stroke="var(--line)"/>'
            f'<polyline points="{points}" fill="none" stroke="var(--acc)" stroke-width="2" '
            'vector-effect="non-scaling-stroke"/></svg></div>'
            f'<div class="chart-dates"><span>{esc(records[0]["day"])}</span><span>{esc(records[-1]["day"])}</span></div>')


def register(app, workdir, *, page, guard, token):
    @app.get("/paper")
    def index():
        accounts = []
        for path in sorted((workdir / "runs" / "paper").glob("*/strategy.json")):
            name = path.parent.name
            value = paper.inspect(workdir, name)
            accounts.append((name, value))
        accounts.sort(key=lambda pair: (pair[1]["config"]["scope"] != "forward_paper", pair[0]))
        cards = []
        for name, value in accounts:
            stats = statistics(value)
            state, tone = STATES.get(value["status"]["state"], (value["status"]["state"], "pending"))
            records = value["sessions"]
            scope = "前向模拟" if value["config"]["scope"] == "forward_paper" else "历史演示"
            position_count = len(records[-1]["state"]["positions"]) if records else len(value["config"]["holdings"])
            cards.append(f'<article class="account-card"><div class="card-heading"><span class="scope-label">{scope}</span>'
                f'<span class="status-badge {tone}">{esc(state)}</span></div>'
                f'<h3><a href="/paper/{esc(name)}">{esc(name)}</a></h3>'
                '<div class="metrics compact-metrics">' +
                metric("账户权益（元）", f'{stats["equity"]:,.2f}' if stats["equity"] is not None else "待估值") +
                metric("记录期收益", f'{stats["change"]:+.2%}' if stats["change"] is not None else "暂无", f'{len(records)} 个记录日') +
                '</div>' + equity_chart(records, compact=True) +
                f'<div class="card-meta"><span>持仓 {position_count} 只</span><span>当日前向 {stats["forward_days"]} 日</span></div>'
                f'<p class="mut account-message">{esc(value["status"]["message"])}</p>'
                f'<a class="card-link" href="/paper/{esc(name)}">查看账户</a></article>')
        content = '<div class="account-grid">' + ''.join(cards) + '</div>' if cards else (
            '<div class="empty-state"><h3>还没有模拟账户</h3><a class="btn" href="/models">选择模型</a></div>')
        return page("纸面交易", '<div class="page-heading"><div><h1>纸面交易</h1>'
                    f'<p class="mut">{len(accounts)} 个账户 · 前向模拟与历史演示分别记录</p></div>'
                    '<div class="actions"><a href="/paper/data">更新日线数据</a><a class="btn" href="/models">选择模型</a></div></div>'
                    '<p class="notice">模拟费用仅含佣金和印花税，未含价差与冲击。历史演示和晚到补记不算前向成绩。</p>' + content)

    @app.get("/paper/data")
    def data_page():
        return page("更新日线数据", '<h2>更新日线数据</h2>'
                    '<form method="post" enctype="multipart/form-data">'
                    f'<input type="hidden" name="token" value="{token}">'
                    '<label class="f">数据名称 <input name="name" required pattern="[A-Za-z0-9_-]{1,40}"></label>'
                    '<label class="f">行情 CSV <input name="bars" type="file" accept=".csv" required></label>'
                    '<label class="f">交易日历 CSV <input name="calendar" type="file" accept=".csv" required></label>'
                    '<button>保存数据更新</button></form>')

    @app.post("/paper/data")
    async def update_data(req: Request):
        form = await req.form()
        guard(req, form.get("token"))
        try:
            name = str(form.get("name", ""))
            if not paper.NAME.fullmatch(name):
                raise ValueError("数据名称只能用字母、数字、下划线和连字符")
            bars = await form["bars"].read(50_000_001)
            calendar = await form["calendar"].read(1_000_001)
            if len(bars) > 50_000_000 or len(calendar) > 1_000_000:
                raise ValueError("行情不超过 50 MB，日历不超过 1 MB")
            value = daily_data.update(workdir / "data" / name, bars, calendar)
            return page("数据更新已保存", f'<h2>数据更新已保存</h2><p>新增 {value["added_rows"]} 行</p>'
                        '<p><a href="/">训练模型</a> · <a href="/paper">返回模拟账户</a></p>')
        except (ValueError, OSError, KeyError, AttributeError) as exc:
            return page("数据未更新", f'<p class="bad">{esc(exc)}</p>')

    @app.get("/paper/{name}")
    def dashboard(name: str):
        try:
            value = paper.inspect(workdir, name)
        except (ValueError, OSError) as exc:
            return page("模拟账户", f'<p>{esc(exc)}</p>')
        records = value["sessions"]
        stats = statistics(value)
        rows = ''.join(f'<tr><td>{esc(r["day"])}</td><td>{r["equity"]:,.2f}</td>'
                       f'<td>{r["state"]["cash"]:,.2f}</td><td>{(r["audit"] or {}).get("total_cost", 0):,.2f}</td>'
                       f'<td>{esc(SCOPES.get(r["scope"], r["scope"]))}</td></tr>' for r in records[-60:])
        positions = records[-1]["state"]["positions"] if records else [dict(p, asset_id=p["code"], mark=None) for p in value["config"]["holdings"]]
        held = ''.join(f'<tr><td>{esc(p["asset_id"])}</td><td>{p["quantity"]}</td>'
                       f'<td>{p["sellable_quantity"]}</td><td>{format(p["mark"], ",.2f") if p["mark"] is not None else "待估值"}</td></tr>' for p in positions)
        submissions = [dict(day=r["day"], **f) for r in records[-10:] for f in (r["audit"] or {}).get("fills", [])]
        fills = [f for f in submissions if f["quantity"] > 0]
        rejected = [f for f in submissions if not f["quantity"]]
        trades = ''.join(f'<tr><td>{esc(f["day"])}</td><td>{esc(f["asset_id"])}</td>'
                         f'<td>{"买入" if f["side"] == "BUY" else "卖出"}</td><td>{f["quantity"]}</td><td>{esc(reason_text(f["reason"]))}</td></tr>'
                         for f in fills)
        chart = equity_chart(records)
        rejections = ''
        if rejected:
            rejected_rows = ''.join(f'<tr><td>{esc(f["day"])}</td><td>{esc(f["asset_id"])}</td>'
                                   f'<td>{esc(reason_text(f["reason"]))}</td></tr>' for f in rejected)
            rejections = (f'<details><summary>未成交申报（{len(rejected)} 条）</summary>'
                       '<div class="tw"><table><tr><th>日期</th><th>代码</th><th>原因</th></tr>'
                       f'{rejected_rows}</table></div></details>')
        status = value["status"]
        tracking = daily_operation.coverage(workdir, name)
        operation = tracking["operation"]
        operation_time = (pd.Timestamp(operation["attempted_at"]).tz_convert("Asia/Shanghai")
                          .strftime("%Y-%m-%d %H:%M")) if operation else ""
        operations = (f'<p>最近每日任务：{esc(operation_time)} · '
                      f'{esc(operation.get("message", ""))}</p>') if operation else ''
        if operation and operation.get("data_end"):
            operations += f'<p>已核验行情截至 {esc(operation["data_end"])}</p>'
        if not tracking.get("calendar_available", True):
            operations += '<p class="warn">交易日历未覆盖检查日期，漏记列表可能不完整。</p>'
        gaps = ''.join(f'<tr><td>{esc(r["day"])}</td><td>{esc(r["record_type"])}</td>'
                       f'<td>{esc(r["reason"])}</td></tr>' for r in tracking["missed"])
        if gaps:
            operations += ('<h3>未及时记录的交易日</h3><div class="tw"><table><tr><th>日期</th>'
                           f'<th>记录</th><th>原因</th></tr>{gaps}</table></div>')
        state, tone = STATES.get(status["state"], (status["state"], "pending"))
        automatic = paper_auto.settings(workdir, name)
        automation = (f'<form method="post" action="/paper/{esc(name)}/automatic">'
                      f'<input type="hidden" name="token" value="{token}">'
                      f'<label class="row"><input type="checkbox" name="enabled" value="yes" {"checked" if automatic["enabled"] else ""}>每日自动记账</label>'
                      '<button class="secondary">保存</button></form>'
                      '<p class="mut">每天 17:30 后检查一次，工作台须保持运行；关闭或注销期间不记账。已有 API 每日任务的账户先更新行情，其他账户需持续导入新数据。</p>'
                      f'<p>{esc(automatic.get("attempted_at", "尚未自动运行"))} · {esc(automatic.get("message", ""))}</p>')
        source_path = paper.location(workdir, name) / "workflow.json"
        previous = (f'<a href="/workflow/{esc(experiments.read(source_path)["run"])}">返回模型评价</a>'
                    if source_path.exists() else '<a href="/models">返回模型评价</a>')
        next_step = (f'<section class="data-section"><h2>下一步：模拟持仓</h2>'
                     f'<p>已记录 {len(records)} 日，其中当日前向 {stats["forward_days"]} 日。'
                     '这些记录尚不能证明未来收益；可先用真实仓位查看参考建议。</p>'
                     f'<a class="btn" href="/holdings/{esc(name)}">输入真实仓位 · 模拟建议</a></section>'
                     if value["config"]["scope"] == "forward_paper" else '')
        scope = "前向模拟" if value["config"]["scope"] == "forward_paper" else "历史演示"
        empty_row = lambda columns, label: f'<tr><td colspan="{columns}" class="empty-cell">{label}</td></tr>'
        summary = '<div class="metrics account-metrics">' + \
            metric("账户权益（元）", f'{stats["equity"]:,.2f}' if stats["equity"] is not None else "待估值", f'现金 {stats["cash"]:,.2f}') + \
            metric("记录期收益", f'{stats["change"]:+.2%}' if stats["change"] is not None else "暂无", "相对首个记录日") + \
            metric("最大回撤", f'{stats["drawdown"]:.2%}' if stats["drawdown"] is not None else "暂无", "记录期内") + \
            metric("累计费用（元）", f'{stats["fees"]:,.2f}', "佣金与印花税") + '</div>'
        return page(f"纸面交易 {name}", '<p class="breadcrumb"><a href="/paper">全部账户</a> / 账户详情</p>' + previous +
                    f'<div class="page-heading"><div><span class="scope-label">{scope}</span><h1>{esc(name)}</h1></div>'
                    f'<div class="actions"><a href="/paper/{esc(name)}/history.csv">下载账户历史</a>'
                    f'<form method="post" action="/paper/{esc(name)}/tick">'
                    f'<input type="hidden" name="token" value="{token}"><button>更新记账</button></form></div></div>'
                    f'<p><span class="status-badge {tone}">{esc(state)}</span> {esc(status["message"])}</p>'
                    '<p class="notice">日线开盘价模拟，只扣佣金和印花税，未计价差与冲击；历史补记单独标识。</p>'
                    f'{summary}<section class="data-section"><div class="section-heading"><h2>账户净值</h2>'
                    f'<span class="mut">{len(records)} 个记录日 · 当日前向 {stats["forward_days"]} 日</span></div>{chart}</section>'
                    '<div class="account-detail-grid"><section class="data-section"><h2>当前持仓</h2>'
                    '<div class="tw"><table><tr><th>代码</th><th>持仓</th><th>可卖</th><th>参考价</th></tr>'
                    f'{held or empty_row(4, "尚无已记账持仓")}</table></div></section>'
                    '<section class="data-section"><h2>近期模拟成交</h2><div class="tw"><table><tr><th>日期</th><th>代码</th><th>方向</th>'
                    f'<th>成交股数</th><th>状态</th></tr>{trades or empty_row(5, "暂无模拟成交")}</table></div>{rejections}</section></div>'
                    '<section class="data-section"><h2>每日账户记录</h2><div class="tw"><table><tr><th>日期</th><th>权益</th><th>现金</th>'
                    f'<th>费用</th><th>记录类型</th></tr>{rows or empty_row(5, "尚无记录，等待完整收盘数据")}</table></div></section>'
                    f'<section class="data-section"><h2>自动运行</h2>{automation}</section>'
                    f'<details open class="operation-details"><summary>任务状态与记录完整性</summary>{operations or "尚无每日任务记录"}</details>'
                    f'<details><summary>冻结协议</summary><code>{esc(value["config"]["protocol_hash"])}</code></details>' + next_step)

    @app.post("/paper/{name}/automatic")
    async def automatic_setting(req: Request, name: str):
        form = await req.form()
        guard(req, form.get("token"))
        try:
            paper_auto.configure(workdir, name, form.get("enabled") == "yes")
        except (ValueError, OSError) as exc:
            return page("自动记账未启用", f'<p>{esc(exc)}</p>')
        return RedirectResponse(f"/paper/{name}", 303)

    @app.post("/paper/{name}/tick")
    async def tick(req: Request, name: str):
        form = await req.form()
        guard(req, form.get("token"))
        try:
            root = paper.location(workdir, name)
            if (root / "operation.json").exists():
                config = paper.inspect(workdir, name)["config"]
                daily_operation.run(workdir, name, config["data"]["dir"])
            else:
                paper.tick(workdir, name)
        except (ValueError, OSError, KeyError) as exc:
            return page("记账已停止", f'<p>{esc(exc)}</p>')
        return RedirectResponse(f"/paper/{name}", 303)

    @app.get("/paper/{name}/history.csv")
    def history(name: str):
        value = paper.inspect(workdir, name)
        frame = pd.DataFrame([{k: r[k] for k in ("day", "equity", "daily_return", "scope", "protocol_hash")}
                              for r in value["sessions"]])
        return Response(frame.to_csv(index=False).encode("utf-8-sig"), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="paper-history.csv"'})
