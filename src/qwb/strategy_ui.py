"""Account CSV preview routes for the local workbench."""

from __future__ import annotations

import html
import uuid
from pathlib import Path

import pandas as pd
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from . import experiments, strategy


def esc(value):
    return html.escape(str(value))


def reason_text(value):
    return {"FILLED": "数量满足", "LOT_ROUNDED": "已按数量规则调整", "HOLD": "持有不变",
            "T1_LOCKED": "T+1 不可卖", "INSUFFICIENT_CASH": "现金不足",
            "INSUFFICIENT_CASH_FOR_FEES": "现金不足以支付费用", "LIMIT_UP": "涨停受阻",
            "LIMIT_DOWN": "跌停受阻", "SUSPENDED": "停牌", "MISSING_OPEN": "缺少开盘价",
            "MISSING_LIMITS": "缺少涨跌停价", "BELOW_MINIMUM_BUY": "不足最低买入股数",
            "BELOW_MINIMUM_SELL": "不足最低卖出股数"}.get(value, value)


def register(app, workdir, *, page, guard, token, locate):
    def result(advice_id):
        if not experiments.ID_RE.fullmatch(advice_id):
            raise HTTPException(404)
        path = workdir / "runs" / "advice" / f"{advice_id}.json"
        if not path.is_file():
            raise HTTPException(404)
        return experiments.read(path)

    @app.get("/models/{run}/account")
    def account_page(run: str):
        locate(run)
        return page("账户交易建议", f'<h2>{esc(run)} · 账户交易建议</h2>'
                    f'<form method="post" enctype="multipart/form-data">'
                    f'<input type="hidden" name="token" value="{token}">'
                    '<label class="f">可用现金（元） <input name="cash" type="number" min="0" '
                    'step="0.01" value="100000" required></label>'
                    '<label class="f">计划执行日 <input name="execution_day" type="date" required></label>'
                    '<label class="f">持仓 CSV <input name="file" type="file" accept=".csv"></label>'
                    '<p><a href="/account-template.csv">持仓模板</a></p>'
                    '<label class="row"><input type="checkbox" name="supported_only" value="yes" required>'
                    '已确认无待处理公司行动、北交所持仓或特殊订单</label>'
                    '<p><button>生成账户建议</button></p></form>')

    @app.get("/account-template.csv")
    def template():
        return Response(b"code,quantity,sellable_quantity\n000001.SZ,100,100\n", media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="holdings.csv"'})

    @app.post("/models/{run}/account")
    async def account_preview(req: Request, run: str):
        form = await req.form()
        guard(req, form.get("token"))
        source = locate(run)
        try:
            if form.get("supported_only") != "yes":
                raise ValueError("请先确认账户中没有暂不支持的公司行动、证券或订单")
            upload = form.get("file")
            raw = await upload.read(1_000_001) if hasattr(upload, "read") else b""
            if len(raw) > 1_000_000:
                raise ValueError("持仓 CSV 不得超过 1 MB")
            holdings = strategy.read_holdings(raw)
            value = strategy.preview(source, workdir, float(form.get("cash", "")), holdings,
                                     str(form.get("execution_day", "")))
            value.update(run_source=str(source), holdings=holdings)
            advice_id = uuid.uuid4().hex
            output = workdir / "runs" / "advice"
            output.mkdir(parents=True, exist_ok=True)
            experiments.write(output / f"{advice_id}.json", value)
            return RedirectResponse(f"/advice/{advice_id}", 303)
        except (ValueError, OSError) as exc:
            return page("账户建议未生成", f'<p class="bad">{esc(exc)}</p>'
                        f'<a href="/models/{esc(run)}/account">返回账户</a>')

    @app.get("/advice/{advice_id}")
    def advice_page(advice_id: str):
        value = result(advice_id)
        paper_link = (f'<p><a href="/holdings/{esc(value["paper_account"])}">修改真实仓位</a> · '
                      f'<a href="/paper/{esc(value["paper_account"])}">返回纸面交易</a></p>'
                      if value.get("paper_account") else '')
        rows = ''.join('<tr>' + ''.join(f'<td>{esc(cell)}</td>' for cell in
                       (row["code"], row["current_quantity"], row["sellable_quantity"],
                        f'{row["target_weight"]:.1%}', f'{row["reference_price"]:.2f}',
                        f'{row["suggested_delta"]:+d}', f'{row["estimated_cost"]:.2f}', reason_text(row["reason"]))) + '</tr>'
                       for row in value["rows"])
        body = '<h2>模拟持仓建议</h2>' + paper_link + (
                    f'<p>行情截止 {esc(value["date"])} · 计划执行 {esc(value["execution_day"])}</p>'
                    f'<p>账户参考权益 {value["equity"]:,.2f} 元 · 预估费用 {value["estimated_cost"]:,.2f} 元</p>'
                    '<p class="warn">参考价估算，尚未成交；仅扣佣金和印花税，未计价差冲击。'
                    '涨跌停、停牌及实际开盘价可能改变结果。</p>'
                    f'<p><a href="/advice/{advice_id}/orders.csv">下载建议 CSV</a> · '
                    f'<a href="/advice/{advice_id}/protocol.json">冻结配置</a></p>'
                    '<div class="tw"><table><tr><th>代码</th><th>持仓</th><th>可卖</th><th>目标权重</th>'
                    f'<th>参考价</th><th>建议增减</th><th>预估费用</th><th>状态</th></tr>{rows}</table></div>'
                    f'<p class="mut">配置 <code>{esc(value["protocol_hash"])}</code></p>')
        if not value.get("paper_account"):
            body += (f'<form method="post" action="/advice/{advice_id}/paper">'
                    f'<input type="hidden" name="token" value="{token}">'
                    '<label class="f">模拟账户名称 <input name="name" required pattern="[A-Za-z0-9_-]{1,40}"></label>'
                    '<button>启用前向模拟账户</button></form>')
        return page("模拟持仓建议", body)

    @app.get("/advice/{advice_id}/orders.csv")
    def download(advice_id: str):
        value = result(advice_id)
        frame = pd.DataFrame(value["rows"])
        frame["protocol_hash"] = value["protocol_hash"]
        frame["decision_date"] = value["date"]
        return Response(frame.to_csv(index=False).encode("utf-8-sig"), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="account-advice.csv"'})

    @app.get("/advice/{advice_id}/protocol.json")
    def frozen_protocol(advice_id: str):
        value = result(advice_id)
        return {"protocol_hash": value["protocol_hash"], "model_hash": value["model_hash"],
                "recipe": value["recipe"]}

    @app.post("/advice/{advice_id}/paper")
    async def start_paper(req: Request, advice_id: str):
        from . import paper
        form = await req.form()
        guard(req, form.get("token"))
        value = result(advice_id)
        try:
            if value.get("paper_account"):
                raise ValueError("此建议已绑定纸面账户，请返回原账户查看")
            name = str(form.get("name", ""))
            paper.initialize(workdir, name, Path(value["run_source"]), value["cash"], value["holdings"])
            return RedirectResponse(f"/paper/{name}", 303)
        except (ValueError, OSError) as exc:
            return page("未启用模拟账户", f'<p class="bad">{esc(exc)}</p>')
