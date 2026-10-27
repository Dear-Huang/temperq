"""An opt-in historical timing experiment inside the rules stage."""

from __future__ import annotations

from dataclasses import asdict, replace
import html
from io import BytesIO
from pathlib import Path
import uuid

import pandas as pd
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import experiments as ex, governance_timing as timing, strategy_workspace as ws

REASONS = {
    'SCHEDULED': '到达常规调仓日', 'WAIT_PERIOD': '等待常规调仓日',
    'WAIT_SIGNAL': '等待当日新信号', 'EXTRA_LIMIT': '本周额外次数已用完',
    'COOLDOWN': '仍在冷却期', 'UNCALIBRATED_SIGNAL': '排名尚未标定为预期收益',
    'HORIZON_MISMATCH': '预测期限与比较期限不一致',
    'INCOMPLETE_RETURN_FORECAST': '新旧持仓缺少完整收益预测',
    'NO_EXECUTABLE_CHANGE': '整手及费用约束下没有可执行变化',
    'QUOTE_INFEASIBLE': '预计无法完整执行本次调整',
    'DIRECTION_NOT_AUTHORIZED': '超出允许的买卖方向',
    'TURNOVER_BUDGET': '超过本次交易预算',
    'INSUFFICIENT_ESTIMATED_SURPLUS': '预期增量未覆盖成本及门槛',
    'EXTRA_AUTHORIZED': '满足额外调整授权',
}


def esc(value):
    return html.escape(str(value), quote=True)


def register(app, workdir, *, page, guard, token):
    @app.get('/strategies/{sid}/{vid}/timing-template.csv')
    def template(sid: str, vid: str):
        root, spec = ws.revision(workdir, sid, vid, verify_implementation=False)
        frame = pd.read_parquet(root/'predictions.parquet')
        frame = frame[["code", "decision_time", "prediction"]].copy()
        # Never invent historical availability from the nominal decision time.
        frame["available_at"] = ""
        frame["data_asof"] = ""
        return Response('\ufeff'+frame.to_csv(index=False), media_type='text/csv',
                        headers={'Content-Disposition': 'attachment; filename="timing-signals.csv"'})

    @app.get('/strategies/{sid}/{vid}/timing')
    def editor(sid: str, vid: str):
        root, spec = ws.revision(workdir, sid, vid, verify_implementation=False)
        _, panel, _, _, _, _ = ws.inputs(workdir, root, spec)
        frame = pd.read_parquet(root/'predictions.parquet', columns=['decision_time'])
        dates = pd.to_datetime(frame.decision_time, utc=True).dt.tz_convert('Asia/Shanghai').dt.strftime('%Y-%m-%d')
        base = f'/strategies/{sid}/{vid}'
        body = f'<link rel="stylesheet" href="/static/governance-timing.css"><p><a href="{base}">← 账户规则</a></p><h1>调仓时机检验</h1><p class="mut">历史试验 · 当前纸面账户继续使用原规则</p>'
        body += '<details><summary>数据与检验边界</summary><p>使用本策略的资金、初始持仓和本地行情。普通排名不能换算成收益；周期外成本门槛仅接受已标定收益及期限。模板中的 available_at、data_asof 须按真实生成记录填写，导入不认证来源。结果暂不支持部署，也不证明增量收益。</p></details>'
        body += f'<form class="timing-form" method="post" enctype="multipart/form-data" action="{base}/timing"><input type="hidden" name="token" value="{token}">'
        body += f'<label class="f">历史信号 CSV<input type="file" name="signals" accept=".csv" required></label><p><a href="{base}/timing-template.csv">下载本策略的信号模板</a></p>'
        body += '<label class="f">分数含义<select name="score_kind"><option value="ranking">因子得分或排名</option><option value="expected_return">已标定预期收益（小数）</option></select></label>'
        body += '<label class="f">信号版本<input name="version" value="my-signal-v1" required></label>'
        body += '<label class="f timing-toggle"><input type="checkbox" name="allow_between" value="yes">允许周期外调整</label>'
        body += '<label class="f">常规周期（交易日）<input name="period_sessions" type="number" min="1" value="5" required></label>'
        body += f'<label class="f">开始<input name="start" type="date" value="{dates.min()}" required></label>'
        end = min(dates.max(), str(panel['close'].index[-3].date())) if len(panel['close']) >= 3 else dates.max()
        body += f'<label class="f">结束<input name="end" type="date" value="{end}" required></label>'
        body += '<details><summary>高级设置</summary>'
        for key, label, default, minimum in (
            ('evaluation_horizon', '比较期限（交易日）', 5, 1),
            ('horizon_sessions', '信号预测期限（交易日）', 5, 1),
            ('max_extra_per_week', '每个自然周最多额外调整次数', 1, 1),
            ('min_sessions_between_extra', '两次额外调整最少间隔（交易日）', 2, 1),
            ('extra_turnover_cap', '额外买卖总额 / 账户权益上限', .5, 0),
            ('cost_multiplier', '估计成本倍数', 1.5, 1),
            ('minimum_surplus_bp', '额外收益门槛（bp）', 5, 0)):
            body += f'<label class="f">{label}<input name="{key}" type="number" step="any" min="{minimum}" value="{default}" required></label>'
        body += '<label class="f">额外调整方向<select name="extra_direction"><option value="both">允许买卖</option><option value="reduce_only">只允许卖出</option><option value="add_only">只允许买入</option></select></label>'
        body += '<label class="f">校准版本<input name="calibration_id"></label><label class="f">校准数据截止时刻（含时区）<input name="calibration_end_at" placeholder="2023-12-29T15:30:00+08:00"></label>'
        body += '</details><button>比较调仓规则</button></form>'
        return page('调仓时机检验', body)

    @app.post('/strategies/{sid}/{vid}/timing')
    async def compare(req: Request, sid: str, vid: str):
        form = await req.form()
        guard(req, form.get('token'))
        attempt = None
        try:
            root, spec = ws.revision(workdir, sid, vid, verify_implementation=False)
            attempt = root/'timing-experiments'/uuid.uuid4().hex
            attempt.mkdir(parents=True)
            record = {'status': 'RUNNING', 'scope': 'historical_replay', 'created_at': ex.now(),
                      'spec_hash': spec['spec_hash'], 'engine_hash': ex.digest(Path(timing.__file__))}
            ex.write(attempt/'result.json', record)
            uploaded = form.get('signals')
            if uploaded is None or not getattr(uploaded, 'filename', ''):
                raise ValueError('请上传历史信号 CSV')
            raw = await uploaded.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('首版试验最多接收 1 MB 的信号 CSV')
            frame = pd.read_csv(BytesIO(raw), dtype={'code': str})
            if len(frame) > 10000:
                raise ValueError('首版试验最多接收 10000 行信号')
            _, panel, snapshot, _, _, _ = ws.inputs(workdir, root, spec)
            if len(panel['close'].loc[str(form['start']):str(form['end'])]) > 252:
                raise ValueError('首版交互试验最多 252 个交易日；更长区间请使用离线脚本')
            contract = {'version': str(form['version']), 'score_kind': str(form['score_kind']),
                        'horizon_sessions': int(form['horizon_sessions']),
                        'calibration_id': str(form.get('calibration_id', '')) or None,
                        'calibration_end_at': str(form.get('calibration_end_at', '')) or None}
            (attempt/'signals.csv').write_bytes(raw)
            ex.write(attempt/'signal-contract.json', contract)
            snapshots = timing.read_snapshots(frame, panel, contract)
            values = {key: int(form[key]) for key in ('period_sessions', 'evaluation_horizon',
                      'max_extra_per_week', 'min_sessions_between_extra')}
            values.update({key: float(form[key]) for key in ('extra_turnover_cap', 'cost_multiplier', 'minimum_surplus_bp')})
            values.update(allow_between=form.get('allow_between') == 'yes', extra_direction=str(form['extra_direction']))
            policy = timing.TimingPolicy(**values)
            cfg, mode = ws.config(spec)
            if mode != 'fixed':
                raise ValueError('首版时机试验仅支持固定仓位；波动目标策略请先建立固定仓位新版本')
            cost_model = spec['resolved']['policy'].get('cost_model', 'commission_only')
            arms = {}
            for name, selected in (('periodic', replace(policy, allow_between=False)), ('selected', policy)):
                arms[name] = await run_in_threadpool(timing.replay, panel, snapshots, cfg, selected,
                                          start=str(form['start']), end=str(form['end']), cash=spec['cash'],
                                          holdings=spec['holdings'], snapshot_id=snapshot, cost_model=cost_model)
                ex.write(attempt/(name+'.json'), arms[name])
            record.update(status='SUCCEEDED', policy=asdict(policy), signal_contract=contract,
                          signal_hash=ex.digest(attempt/'signals.csv'),
                          data_snapshot_id=snapshot, engine_hash=ex.digest(Path(timing.__file__)),
                          evidence='INSUFFICIENT', metrics={name: arm['metrics'] for name, arm in arms.items()})
            ex.write(attempt/'result.json', record)
            return RedirectResponse(f'/strategies/{sid}/{vid}/timing/{attempt.name}', 303)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            if attempt is not None:
                record.update(status='FAILED', error=str(exc))
                ex.write(attempt/'result.json', record)
            response = page('检验受阻', f'<h1>检验受阻</h1><p class="bad">{esc(exc)}</p><a href="/strategies/{sid}/{vid}/timing">返回修改</a>')
            response.status_code = 400
            return response

    @app.get('/strategies/{sid}/{vid}/timing/{attempt_id}')
    def result(sid: str, vid: str, attempt_id: str):
        if not ex.ID_RE.fullmatch(attempt_id):
            raise HTTPException(404, '无效试验编号')
        root, _ = ws.revision(workdir, sid, vid, verify_implementation=False)
        attempt = root/'timing-experiments'/attempt_id
        if not (attempt/'result.json').is_file():
            raise HTTPException(404, '试验不存在')
        record = ex.read(attempt/'result.json')
        body = f'<link rel="stylesheet" href="/static/governance-timing.css"><p><a href="/strategies/{sid}/{vid}/timing">← 调仓时机</a></p><h1>调仓时机对照</h1>'
        if record['status'] != 'SUCCEEDED':
            return page('调仓时机对照', body+f'<p>{esc(record.get("error", record["status"]))}</p>')
        body += '<p>历史回放 · 尚未证明收益优势</p><div class="tw"><table class="timing-metrics"><thead><tr><th>规则</th><th>净收益</th><th>费用</th><th>平均仓位</th><th>买卖总额 / 权益</th></tr></thead><tbody>'
        for key, label in (('periodic', '固定周期'), ('selected', '当前时机规则')):
            metrics = record['metrics'][key]
            body += f'<tr><td>{label}</td><td>{metrics["total_return"]:.2%}</td><td>{metrics["total_cost"]:.2f}</td><td>{metrics["mean_exposure"]:.2%}</td><td>{metrics["gross_traded_fraction"]:.2f}</td></tr>'
        body += '</tbody></table></div><details><summary>每日决定与原因</summary><div class="tw"><table class="timing-decisions"><tr><th>日期</th><th>决定</th><th>原因</th><th>预期增量 bp</th><th>估计成本 bp</th><th>所需增量 bp</th></tr>'
        arm = ex.read(attempt/'selected.json')
        for row in arm['checks']:
            quote = row.get('quote')
            estimates = ''.join(f'<td>{quote[key]:.2f}</td>' if quote else '<td>-</td>'
                                for key in ('estimated_gain_bp', 'cost_bp', 'required_gain_bp'))
            body += f'<tr><td>{esc(row["decision_at"][:10])}</td><td>{"生成调整目标" if row["authorized"] else "不新增调整"}</td><td title="{esc(row["reason"])}">{esc(REASONS.get(row["reason"], row["reason"]))}</td>{estimates}</tr>'
        body += '</table></div></details><details><summary>检验边界</summary><p>校准与信号来源为用户声明；预计费用不等于真实成交成本。尚无相同换手和平均仓位对照，不能把收益差归因为触发能力。未支持公司行动，不连接券商，不改变现有运行账户。</p></details>'
        return page('调仓时机对照', body)
