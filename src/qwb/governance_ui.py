"""Local mandate editor and historical governance comparison, backed by CPU jobs."""
from __future__ import annotations

import html
import json
from pathlib import Path
import uuid

import pandas as pd
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import daily_data, experiments, governance
from .strategy_ui import reason_text

LABELS = {"rules_baseline": "规则基线（无排名缓冲、无单票截断）", "fixed": "你的规则 · 固定仓位",
          "ewma": "你的规则 · EWMA 风险控制", "ml": "你的规则 · ML 风险控制",
          "ml_matched_fixed": "同平均仓位 · 固定仓位（事后解释）"}
REASONS = {"BUY_TOP_N": "进入买入排名", "HOLD_IN_BAND": "排名仍在继续持有范围",
           "REDUCED_MANDATE": "受总仓位上限或最低现金约束",
           "SELL_OUT_OF_BAND": "离开继续持有范围或名额不足", "CAPPED_MAX_WEIGHT": "单票上限截断，余款留现金",
           "FIXED_EXPOSURE": "按固定仓位上限", "EWMA_RISK": "按近期风险缩放仓位",
           "ML_RISK": "按 ML 预测风险缩放仓位", "ML_WARMUP_EWMA": "ML 成熟样本不足，暂用 EWMA"}


def esc(value):
    return html.escape(str(value))


def register(app, workdir, *, page, guard, token, locate, submit, datasets):
    root = workdir / "runs" / "governance"

    def result_path(attempt):
        if not experiments.ID_RE.fullmatch(attempt):
            raise HTTPException(404)
        path = root / attempt
        if not (path / "result.json").is_file():
            raise HTTPException(404)
        return path

    @app.get("/governance")
    def editor():
        sources = sorted({p.parent.name for p in (workdir / "runs").glob("*/predictions.parquet")}
                         | {p.parent.name for p in (workdir / "runs/history").glob("*/predictions.parquet")})
        options = ''
        for source in sources:
            path = (workdir / "runs/history" / source if experiments.ID_RE.fullmatch(source)
                    else workdir / "runs" / source)
            if not (path / "final_model/meta.json").is_file():
                continue
            label = experiments.read(path / "run.json").get("name", source) if (path / "run.json").is_file() else source
            options += f'<option value="{esc(source)}">{esc(label)}</option>'
        ds = {k: v for k, v in datasets(workdir).items() if k != "_trial"}
        data_options = ''.join(f'<option value="{esc(k)}">{esc(k)}</option>' for k in ds)
        attempts = sorted(root.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        history = ''.join(f'<li><a href="/governance/{p.parent.name}">{esc(p.parent.name[:12])}</a> · '
                          f'{esc(experiments.read(p)["status"])}</li>' for p in attempts[:20])
        return page("治理实验", '<h1>同一个信号，你来定怎么用</h1>'
            '<p>设置持仓规则和风险目标，对照固定仓位、EWMA 与小型 ML 风险模型。'
            '首版固定每 5 个交易日决策、次日开盘执行。</p>'
            '<p class="warn">历史实验。公司行动、持仓行情缺口会停止回放；不能把回放写成前向成绩。</p>'
            '<form method="post" action="/governance">'
            f'<input type="hidden" name="token" value="{token}">'
            '<section class="step"><h2>1 · 固定信号和账户</h2>'
            f'<label class="f">训练预测集 <select name="source" required>{options}</select></label>'
            f'<label class="f">同一行情快照 <select name="dataset" required>{data_options}</select></label>'
            '<p class="mut">使用训练时导出的逐期预测，不用最终模型回填历史。'
            '外部预测可先用命令行导入。预测和行情的快照标识须一致。</p>'
            '<label class="f">回放开始 <input type="date" name="start" required></label>'
            '<label class="f">回放结束 <input type="date" name="end" required></label>'
            '<label class="f">初始现金 <input type="number" name="cash" min="1" value="100000" required></label>'
            '</section><section class="step"><h2>2 · 你的持仓规则</h2>'
            '<label class="f">最多持有（只） <input type="number" name="n_hold" min="1" max="500" value="10" required></label>'
            '<label class="f">排名不低于此名次就可继续持有 <input type="number" name="hold_until_rank" min="1" value="15" required></label>'
            '<label class="f">单票上限（%） <input type="number" name="max_weight" min="0.1" max="100" step="0.1" value="10" required></label>'
            '<p class="mut">超出单票上限的部分留作现金；不把余款分给其他股票。</p>'
            '</section><section class="step"><h2>3 · 给模型的授权</h2>'
            '<label class="f">股票总仓位上限（%） <input type="number" name="exposure_cap" min="0" max="100" value="100" required></label>'
            '<label class="f">最低现金（%） <input type="number" name="cash_floor" min="0" max="100" value="0" required></label>'
            '<label class="f">目标年化风险（%） <input type="number" name="target_volatility" min="0.1" max="200" step="0.1" value="15" required></label>'
            '<label class="f">每边滑点压力（bp） <input type="number" name="slippage_bp" min="0" max="1000" step="0.1" value="0" required></label>'
            '<p class="mut">风险目标采用日线平方收益代理；它是调仓依据，不是回撤或亏损保证。'
            '模型不能改动授权。首版不含止损、大盘择时和换手预算。</p></section>'
            '<button>记录本次尝试并运行对照</button></form>'
            f'<h2>本地尝试记录 · {len(attempts)} 次</h2><p class="mut">记录可用于自查，不能证明在别处没有尝试或本地记录未被删除。</p><ul>{history}</ul>')

    @app.post("/governance")
    async def launch(req: Request):
        form = await req.form()
        guard(req, form.get("token"))
        try:
            source = locate(str(form.get("source", "")))
            ds = datasets(workdir)[str(form.get("dataset", ""))]
            config = {key: int(form[key]) for key in ("n_hold", "hold_until_rank")}
            config.update({key: float(form[key])/100 for key in
                ("max_weight", "exposure_cap", "cash_floor", "target_volatility")})
            config["slippage_bp"] = float(form["slippage_bp"])
            governance.GovernanceConfig(**config)
            start, end = str(form["start"]), str(form["end"])
            if pd.Timestamp(start) > pd.Timestamp(end):
                raise ValueError("回放开始不能晚于结束")
            snapshot = daily_data.current(ds["path"])
            attempt = uuid.uuid4().hex
            jid = submit("evaluate", {"action": "governance", "predictions": str(source / "predictions.parquet"),
                "data_dir": str(snapshot), "output": str(root / attempt), "governance_config": config,
                "start": start, "end": end, "initial_cash": float(form["cash"]),
                "ui": {"what": "governance", "name": "治理规则与风险对照"}})
            return RedirectResponse(f"/job/{jid}", 303)
        except (ValueError, OSError, KeyError) as exc:
            return page("未启动治理实验", f'<p class="bad">{esc(exc)}</p><a href="/governance">返回修改</a>')

    @app.get("/governance/{attempt}")
    def report(attempt: str):
        path = result_path(attempt)
        result = experiments.read(path / "result.json")
        back = '<p><a href="/governance">← 修改授权并新开一次实验</a></p>'
        if result["status"] != "SUCCEEDED":
            return page("治理实验", back + f'<h1>{esc(result["status"])}</h1><p>{esc(result.get("error", "运行中"))}</p>')
        rows = ''
        for name, m in result["metrics"].items():
            q = '未评分' if m["risk_qlike"] is None else f'{m["risk_qlike"]:.4g} / {m["risk_labels"]} 次'
            rows += '<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in (LABELS[name], f'{m["total_return"]:.2%}',
                f'{m["max_drawdown"]:.2%}', f'{m["mean_exposure"]:.1%}', f'{m["total_cost"]:.2f}',
                f'{m["gross_traded_fraction"]:.2f}', f'{m["risk_target_error"]:.2%}', q,
                m["ml_warmup_decisions"], m["actual_constraint_violations"])) + '</tr>'
        # Native SVG keeps the report self-contained and works without a chart CDN.
        colors = {"rules_baseline": "#9097a4", "fixed": "#bb7834", "ewma": "#43866c", "ml": "#527ddd",
                  "ml_matched_fixed": "#9560b5"}
        available = list(result["metrics"])
        curves = {name: pd.read_csv(path / name / "path.csv").equity.to_numpy()/result["initial_cash"] for name in available}
        low, high = min(float(v.min()) for v in curves.values()), max(float(v.max()) for v in curves.values())
        span = max(high-low, .001)
        lines = ''.join('<polyline fill="none" stroke="'+colors[name]+'" stroke-width="2" points="' +
            ' '.join(f'{30+i*900/max(1,len(values)-1):.1f},{225-(v-low)*190/span:.1f}' for i,v in enumerate(values))+'"/>'
            for name, values in curves.items())
        axes = ''.join(f'<line x1="30" x2="930" y1="{y}" y2="{y}" stroke="#9097a4" opacity=".2"/>'
                       f'<text x="32" y="{y-5}" fill="#757d89" font-size="12">{value:.3f}</text>'
                       for value,y in [(high,35),((low+high)/2,130),(low,225)])
        axes += (f'<text x="30" y="250" fill="#757d89" font-size="12">{esc(result["start"])}</text>'
                 f'<text x="930" y="250" text-anchor="end" fill="#757d89" font-size="12">{esc(result["end"])}</text>')
        chart = f'<svg role="img" aria-label="历史净值对照，初始净值为一" viewBox="0 0 960 260" style="width:100%;height:auto"><title>历史净值对照，初始净值为一</title>{axes}{lines}</svg>'
        legend = ' · '.join(f'<span style="color:{colors[name]}">{esc(LABELS[name])}</span>' for name in available)
        diagnostic = result.get("diagnostics", {}).get("ml_matched_fixed")
        matching = '<p class="warn">旧版结果尚无相同平均仓位对照，请新建实验；不能据此归因 ML 效果。</p>'
        if diagnostic:
            status = "误差在容许范围内" if diagnostic["status"] == "MATCHED_WITHIN_TOLERANCE" else "未匹配成功，勿据此归因"
            matching = (f'<p>仓位匹配：ML 实际平均仓位 {diagnostic["reference_mean_exposure"]:.2%}；'
                f'固定仓位对照 {diagnostic["actual_mean_exposure"]:.2%}；差 {diagnostic["signed_match_error"]*100:+.3f} 个百分点，{status}。</p>'
                f'<p class="mut">用了 {len(diagnostic["trials"])} 次仅按仓位误差选择的校准，未按收益选优。'
                '第五组使用整个区间的信息，是事后解释对照，不是当时可执行的策略；相同平均仓位也不等于相同风险。</p>')
        if result["metrics"]["ml"]["sessions"] < 252:
            matching += '<p class="warn">回放不足一年，只作短窗口工程检查；请勿从少量风险标签判断模型优劣。更长样本仍需检验不确定性。</p>'
        return page("治理对照报告", back + '<h1>规则和风险控制改变了什么</h1>'
            f'<p>{esc(result["start"])} 至 {esc(result["end"])} · {result["elapsed_seconds"]:.2f} 秒 · 历史回放</p>'
            f'<p class="warn">{esc(result["conclusion"])}</p>{chart}<p>{legend}</p>{matching}'
            '<p class="mut">表格可左右滑动查看全部指标。</p>'
            '<div class="tw"><table style="min-width:1040px"><tr><th>方案</th><th>净收益</th><th>最大回撤</th><th>平均仓位</th><th>费用（元）</th>'
            '<th>累计成交额/权益</th><th>风险目标误差</th><th>风险预测损失/次数</th><th>ML 回退次数</th><th>实际超限天/项</th></tr>'
            f'{rows}</table></div><p class="mut">风险预测损失在同一前 N 名参考篮子上计算，越低越好；重叠的 5 日标签不独立。'
            '方案平均仓位不同，收益差不能直接归因于 ML 增益；报告未给出显著性结论。</p>'
            f'<p><a href="/governance/{attempt}/decisions">逐日决策与成交</a> · '
            f'<a href="/governance/{attempt}/result.json">下载配置与结果</a></p>'
            '<details><summary>本次冻结的规则</summary><pre>'+esc(json.dumps(result["config"], ensure_ascii=False, indent=2))+'</pre></details>'
            '<ul>'+''.join(f'<li>{esc(x)}</li>' for x in result["limitations"])+'</ul>')

    @app.get("/governance/{attempt}/decisions")
    def decisions(attempt: str, arm: str = "ml", day: str = ""):
        path = result_path(attempt)
        if arm not in LABELS or not (path / arm / "decisions.jsonl").is_file():
            raise HTTPException(404)
        records = [json.loads(line) for line in (path / arm / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        days = [record["date"] for record in records]
        chosen = day or (days[-1] if days else "")
        record = next((r for r in records if r["date"] == chosen), None)
        available = experiments.read(path / "result.json").get("metrics", {})
        controls = f'<form><select name="arm">'+''.join(f'<option value="{key}"'+(' selected' if key == arm else '')+f'>{esc(LABELS[key])}</option>' for key in available)+'</select> '
        controls += '<select name="day">'+''.join(f'<option'+(' selected' if d == chosen else '')+f'>{d}</option>' for d in days)+'</select> <button>查看当天</button></form>'
        body = f'<p><a href="/governance/{attempt}">← 返回对照报告</a></p><h1>决策回放</h1>'+controls
        if arm == "ml_matched_fixed":
            body += '<p class="warn">此为整段历史校准的固定仓位事后对照，不能用于前向交易。</p>'
        if record:
            body += f'<p>{esc(REASONS[record["risk_reason"]])} · 目标现金 {record["cash_target"]:.1%} · 计划执行 {record["execution_date"]}</p>'
            if "unconstrained_exposure" in record:
                body += (f'<p>风险控制原始仓位 {record["unconstrained_exposure"]:.1%} → '
                         f'授权范围内 {record["authorized_exposure"]:.1%} → '
                         f'单票限制后 {record["target_exposure"]:.1%}</p>')
            if record["forecast_variance"] is not None:
                body += f'<p>预测年化风险 {record["forecast_variance"]**.5:.1%} · 最近 ML 拟合 {esc(record["risk_fit_day"] or "未使用 ML")}</p>'
            body += '<div class="tw"><table><tr><th>股票</th><th>排名</th><th>目标权重</th><th>决策原因</th></tr>'+''.join(
                f'<tr><td>{esc(r.get("code", "组合"))}</td><td>{esc(r.get("rank", ""))}</td><td>{record["weights"].get(r.get("code"),0):.1%}</td>'
                f'<td>{esc(REASONS.get(r["reason"],r["reason"]))}</td></tr>' for r in record["reasons"])+'</table></div>'
            fills = [json.loads(line) for line in (path / arm / "fills.jsonl").read_text(encoding="utf-8").splitlines()]
            executed = [f for f in fills if f["date"] == record["execution_date"]]
            body += '<h2>次日实际模拟成交</h2>'
            if executed:
                body += '<div class="tw"><table><tr><th>股票</th><th>方向</th><th>申请股数</th><th>成交股数</th><th>价格</th><th>成交额</th><th>费用</th><th>状态</th></tr>'
                body += ''.join('<tr>'+''.join(f'<td>{esc(v)}</td>' for v in (
                    f["asset_id"], "买入" if f["side"] == "BUY" else "卖出", f["requested_quantity"], f["quantity"],
                    f'{f["open_price"]:.2f}' if f["open_price"] is not None else '缺失',
                    f'{f["notional"]:.2f}', f'{f["total_cost"]:.2f}', reason_text(f["reason"])))+'</tr>' for f in executed)
                body += '</table></div>'
            else:
                body += '<p>该次决策没有产生买卖。</p>'
        return page("决策回放", body)

    @app.get("/governance/{attempt}/result.json")
    def download(attempt: str):
        return FileResponse(result_path(attempt) / "result.json", filename="governance-result.json")
