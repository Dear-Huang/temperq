"""Minimal paper-account history and holdings, using existing workbench styles."""

from __future__ import annotations

import html

import pandas as pd
from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from . import daily_data, daily_operation, paper
from .strategy_ui import reason_text


def esc(value):
    return html.escape(str(value))


def register(app, workdir, *, page, guard, token):
    @app.get("/paper")
    def index():
        links = []
        for path in sorted((workdir / "runs" / "paper").glob("*/strategy.json")):
            name = path.parent.name
            links.append(f'<li><a href="/paper/{esc(name)}">{esc(name)}</a></li>')
        return page("模拟账户", '<h2>模拟账户</h2><ul>' + ''.join(links) + '</ul>'
                    '<p><a href="/models">选择模型</a> · <a href="/paper/data">更新日线数据</a></p>')

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
        rows = ''.join(f'<tr><td>{esc(r["day"])}</td><td>{r["equity"]:,.2f}</td>'
                       f'<td>{r["state"]["cash"]:,.2f}</td><td>{(r["audit"] or {}).get("total_cost", 0):,.2f}</td>'
                       f'<td>{esc({"historical_replay": "历史演示", "late_reconstruction": "晚到补记", "forward_paper": "当日前向"}.get(r["scope"], r["scope"]))}</td></tr>' for r in records[-60:])
        positions = (records[-1]["state"]["positions"] if records else [])
        held = ''.join(f'<tr><td>{esc(p["asset_id"])}</td><td>{p["quantity"]}</td>'
                       f'<td>{p["sellable_quantity"]}</td><td>{p["mark"]:,.2f}</td></tr>' for p in positions)
        fills = [dict(day=r["day"], **f) for r in records[-10:] for f in (r["audit"] or {}).get("fills", [])]
        trades = ''.join(f'<tr><td>{esc(f["day"])}</td><td>{esc(f["asset_id"])}</td>'
                         f'<td>{"买入" if f["side"] == "BUY" else "卖出"}</td><td>{f["quantity"]}</td><td>{esc(reason_text(f["reason"]))}</td></tr>'
                         for f in fills)
        chart = ''
        if len(records) > 1:
            values = [r["equity"] for r in records]
            low, high = min(values), max(values)
            points = ' '.join(f'{10 + 700 * i / (len(values)-1):.1f},{150 - 130 * (v-low)/max(high-low,1):.1f}'
                              for i, v in enumerate(values))
            chart = f'<svg viewBox="0 0 720 170" role="img" aria-label="模拟账户权益曲线" style="width:100%;height:170px">' \
                    f'<polyline points="{points}" fill="none" stroke="var(--acc)" stroke-width="2"/></svg>'
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
        return page(f"模拟账户 {name}", f'<p><a href="/paper">全部账户</a></p><h2>{esc(name)}</h2>'
                    f'<p>{esc({"WAITING_DATA": "等待数据", "WAITING_CALENDAR": "等待交易日历", "RUNNING": "运行中", "BLOCKED": "已停止"}.get(status["state"], status["state"]))} · {esc(status["message"])}</p>'
                    '<p class="warn">日线开盘价模拟，只扣佣金和印花税，未计价差与冲击；历史补记单独标识。</p>'
                    f'<form method="post" action="/paper/{esc(name)}/tick">'
                    f'<input type="hidden" name="token" value="{token}"><button>更新记账</button></form>'
                    f'<p><a href="/paper/{esc(name)}/history.csv">下载账户历史</a></p>{operations}{chart}'
                    '<h3>账户权益</h3><div class="tw"><table><tr><th>日期</th><th>权益</th><th>现金</th>'
                    f'<th>费用</th><th>记录类型</th></tr>{rows}</table></div>'
                    '<h3>当前持仓</h3><div class="tw"><table><tr><th>代码</th><th>持仓</th><th>可卖</th>'
                    f'<th>估值</th></tr>{held}</table></div>'
                    '<h3>近期模拟成交</h3><div class="tw"><table><tr><th>日期</th><th>代码</th><th>方向</th>'
                    f'<th>成交股数</th><th>状态</th></tr>{trades}</table></div>'
                    f'<p class="mut">协议 <code>{esc(value["config"]["protocol_hash"])}</code></p>')

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
