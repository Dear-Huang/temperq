"""Read-only daily operating terminal over the existing frozen paper ledger."""
from __future__ import annotations

import html
import json

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import experiments as ex, policy, strategy_workspace as ws
from .governance_ui import REASONS
from .strategy_ui import reason_text


SCOPES = {"forward_paper": "按时纸面", "late_reconstruction": "迟到重建", "historical_replay": "历史演示"}


def snapshot(state):
    """Describe recorded account state only; never replay, download or infer orders."""
    spec, runtime, records = state['spec'], state['runtime'], state['records']
    series, peak = [], 0.
    counts = dict.fromkeys(SCOPES, 0)
    costs = dict.fromkeys(('commission', 'stamp_tax', 'struct_spread', 'slippage'), 0.)
    fills, notices = [], []
    for record in records:
        path = record['path']; equity = path['equity']; peak = max(peak, equity)
        counts[record['scope']] = counts.get(record['scope'], 0) + 1
        series.append({'day': record['day'], 'equity': equity, 'cash': path['cash'],
            'exposure': path['exposure'], 'drawdown': equity/peak-1,
            'net_return': path['net_return'], 'cost': path['total_cost'], 'scope': record['scope']})
        for fill in record['fills']:
            for key in costs: costs[key] += fill.get(key, 0.)
            fills.append({**{key: fill.get(key) for key in ('asset_id', 'side', 'quantity', 'requested_quantity',
                'open_price', 'notional', 'total_cost', 'reason', 'unfilled_quantity')},
                'day': record['day'], 'scope': record['scope'], 'reason_text': reason_text(fill['reason'])})
        for violation in record['constraint_violations']:
            notices.append({'day': record['day'], **violation})
    last = records[-1] if records else None
    latest_decision = next((r['decision'] for r in reversed(records) if r.get('decision')), None)
    equity = last['path']['equity'] if last else None
    previous = {p['asset_id']: p for p in records[-2]['state']['positions']} if len(records)>1 else {}
    holdings = []
    for position in last['state']['positions'] if last else []:
        code = position['asset_id']; prior = previous.get(code)
        holdings.append({'code': code, 'quantity': position['quantity'], 'sellable': position['sellable_quantity'],
            'mark': position['mark'], 'mark_time': position['mark_time'],
            'value': position['quantity']*position['mark'], 'weight': position['quantity']*position['mark']/equity,
            'mark_change': position['mark']/prior['mark']-1 if prior else None})
    holdings.sort(key=lambda row: (-row['value'], row['code']))
    actual = {p['code']: p['weight'] for p in holdings}
    targets = []
    decision = None
    if latest_decision:
        d = latest_decision
        decision = {'day': d['date'], 'execution_date': d['execution_date'], 'exposure': d['target_exposure'],
            'risk_reason': REASONS.get(d['risk_reason'], d['risk_reason']),
            'reasons': [REASONS.get(reason, reason) for reason in dict.fromkeys(r['reason'] for r in d['reasons'])],
            'execution_recorded': any(r['day']==d['execution_date'] for r in records)}
        for code in sorted(set(actual)|set(d['weights'])):
            targets.append({'code': code, 'actual': actual.get(code, 0.), 'target': d['weights'].get(code, 0.),
                'difference': d['weights'].get(code, 0.)-actual.get(code, 0.)})
        targets.sort(key=lambda r: (-abs(r['difference']), r['code']))
    cfg = spec['resolved']['policy']; params = cfg['parameters']
    health = dict(state['health'])
    if not runtime:
        next_action = '先在策略设置页完成历史检验，再开启纸面观察。'
    elif health['state']=='BLOCKED':
        next_action = '运行受阻。先检查下方原因；修复后手动检查，不会填补虚构记录。'
    elif not records:
        next_action = '账户已启用，等待第一份完整收盘记录。可检查行情，或开启每日检查。'
    elif last['constraint_violations']:
        next_action = '最近记录存在实际约束偏离，请查看风险窗口和成交受阻原因。'
    else:
        next_action = '先看最后记账日和最新目标；日频策略无需盯盘，下一次检查会保留原有记录。'
    return {'schema': 'deployment_snapshot_v1', 'generated_at': ex.now(), 'name': spec['name'],
        'sid': spec['sid'], 'vid': spec['vid'], 'runtime': runtime,
        'health': health, 'next_action': next_action, 'scope_counts': counts,
        'historical_demo': bool(runtime and runtime['scope']=='historical_replay'),
        'series': series, 'holdings': holdings, 'targets': targets, 'decision': decision,
        'fills': list(reversed(fills[-100:])), 'fill_count': len(fills), 'violations': notices[-100:],
        'latest_violations': last['constraint_violations'] if last else [],
        'costs': costs, 'total_cost': sum(r['path']['total_cost'] for r in records),
        'metrics': {'equity': equity, 'cash': last['path']['cash'] if last else None,
            'last_day': last['day'] if last else None, 'record_count': len(records),
            'return_intervals': max(0, len(records)-1),
            'total_return': equity/series[0]['equity']-1 if series else None,
            'daily_return': series[-1]['net_return'] if len(series)>1 else None,
            'daily_pnl': equity-series[-2]['equity'] if len(series)>1 else None,
            'max_drawdown': min((r['drawdown'] for r in series), default=None),
            'exposure': last['path']['exposure'] if last else None},
        'policy': {'preset': policy.PRESETS[spec['resolved']['preset']]['name'], 'mode': cfg['mode'],
            'cost_model': cfg.get('cost_model', 'commission_only'), 'parameters': params},
        'model': {'kind': spec['signal'].get('kind', 'local_model'),
            'version': spec['signal']['model_hash'], 'data_end': spec['signal']['final_model_data_end']},
        'spec_hash': spec['spec_hash']}


def register(app, workdir, *, page, guard, token, submit):
    def get(sid, vid):
        try: return ws.inspect(workdir, sid, vid)
        except (ValueError, OSError, KeyError) as exc: raise HTTPException(409, str(exc)) from exc

    @app.get('/strategies/{sid}/{vid}/dashboard/snapshot.json')
    def data(sid: str, vid: str):
        return JSONResponse(snapshot(get(sid, vid)), headers={'Cache-Control': 'no-store'})

    @app.get('/strategies/{sid}/{vid}/dashboard')
    def terminal(sid: str, vid: str):
        state = snapshot(get(sid, vid)); base = f'/strategies/{sid}/{vid}'
        body = MARKUP.replace('__BASE__', base).replace('__TOKEN__', token)
        body = body.replace('__NAME__', html.escape(state['name'], quote=True)).replace('__VERSION__', vid[:8])
        body += '<script type="application/json" id="deployment-data">'+json.dumps(state, ensure_ascii=False).replace('<', '\\u003c')+'</script>'
        response = page('部署看板 · '+state['name'], body)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/strategies/{sid}/{vid}/dashboard/actions/{action}')
    async def action(req: Request, sid: str, vid: str, action: str):
        form = await req.form(); guard(req, form.get('token'))
        if action not in {'tick', 'automation'}: raise HTTPException(404)
        state = get(sid, vid)
        if not state['runtime']: raise HTTPException(409, '请先在策略设置页开启纸面观察')
        try:
            ws.revision(workdir, sid, vid)
            if action=='automation':
                ws.automation(workdir, sid, vid, form.get('enabled')=='yes')
                return RedirectResponse(f'/strategies/{sid}/{vid}/dashboard', 303)
            jid = submit('evaluate', {'action': 'strategy_tick', 'sid': sid, 'vid': vid,
                'ui': {'what': 'strategy', 'name': '更新行情与纸面记账', 'sid': sid, 'vid': vid, 'return_to': 'dashboard'}})
            return RedirectResponse('/job/'+jid, 303)
        except (ValueError, OSError, KeyError) as exc: raise HTTPException(409, str(exc)) from exc


MARKUP = '''<link rel="stylesheet" href="/static/deployment.css">
<script src="/static/deployment.js" defer></script>
<div class="deployment-shell" data-base="__BASE__" data-layout="overview">
  <div class="desk-breadcrumb"><a href="/start">工作台</a><span>/</span><a href="__BASE__">策略设置与检验</a><span>/</span><b>部署看板</b><span class="desk-market">CN A-SHARES · DAILY</span></div>
  <header class="desk-heading"><div><p class="desk-eyebrow"><span class="desk-dot"></span> TEMPERQ / STRATEGY TERMINAL</p><h1>__NAME__</h1><p class="desk-subtitle">持续跟踪你的账户、纪律与每一次决定。</p></div>
    <div class="desk-heading-meta"><span id="desk-scope" class="desk-badge">纸面账户</span><span class="desk-version">POLICY __VERSION__</span><span id="desk-sync" role="status">正在读取记录</span></div></header>
  <div class="desk-toolbar"><div class="desk-segments" aria-label="看板布局"><button type="button" data-layout="overview" aria-pressed="true">总览</button><button type="button" data-layout="professional" aria-pressed="false">专业多窗</button></div>
    <div class="desk-tools"><button type="button" id="desk-refresh">刷新看板</button><button type="button" id="desk-windows" aria-expanded="false" aria-controls="desk-window-options">自定义窗口</button><button type="button" id="desk-fullscreen">全屏</button><a href="__BASE__/edit">修改并建立新版本 ↗</a></div></div>
  <div id="desk-window-options" class="desk-window-options" hidden><p>只改变本机显示，不改变策略与账户。</p><div id="desk-window-checks"></div><button type="button" id="desk-reset">恢复默认布局</button></div>
  <section class="desk-attention" id="desk-attention" aria-label="今天先看什么"><span class="desk-status-icon" aria-hidden="true">●</span><div><b id="desk-status">读取账户状态</b><p id="desk-action"></p><p id="desk-health-message"></p></div><a href="#desk-operations">运行控制 ↓</a></section>
  <div id="desk-connection" class="desk-connection" role="status" hidden></div>
  <section class="desk-metrics" aria-label="账户关键指标">
    <article><span>账户总权益 <small>CNY</small></span><strong id="metric-equity">—</strong><small id="metric-date">等待第一条记录</small></article>
    <article><span>最近一日盈亏</span><strong id="metric-daily">—</strong><small id="metric-daily-return">含已计交易费用</small></article>
    <article><span>账户路径累计</span><strong id="metric-return">—</strong><small id="metric-return-note">从首条记录起计算</small></article>
    <article><span>区间最大回撤</span><strong id="metric-drawdown">—</strong><small>按全部账户记录计算</small></article>
    <article><span>股票仓位 / 现金</span><strong id="metric-exposure">—</strong><small id="metric-cash">—</small></article>
    <article><span>按时纸面 / 总记录</span><strong id="metric-days">0 / 0</strong><small id="metric-evidence">尚待积累观察</small></article>
  </section>
  <div class="desk-grid">
    <section class="desk-panel desk-equity" data-window="equity"><div class="desk-panel-head"><div><span class="desk-panel-index">01</span><h2>账户净值</h2><small>ACCOUNT EQUITY</small></div><div class="desk-segments" aria-label="图表区间"><button type="button" data-range="20" aria-pressed="false">20 日</button><button type="button" data-range="60" aria-pressed="false">60 日</button><button type="button" data-range="all" aria-pressed="true">全部</button></div></div>
      <div class="desk-chart-meta"><span><i class="desk-key cyan"></i>扣已计费用后的账户路径</span><span id="desk-chart-readout" role="status">移动到曲线上查看记录</span></div><div id="desk-equity-chart" class="desk-chart" aria-label="账户净值图"></div><p class="desk-chart-note" id="desk-chart-note"></p></section>
    <section class="desk-panel desk-allocation" data-window="allocation"><div class="desk-panel-head"><div><span class="desk-panel-index">02</span><h2>资金分布</h2><small>ALLOCATION</small></div></div><div class="desk-allocation-body"><div id="desk-allocation-ring"></div><div id="desk-allocation-list"></div></div><p class="desk-panel-note">按最后记账日收盘估值；未成交目标不计作持仓。</p></section>
    <section class="desk-panel desk-holdings" data-window="holdings"><div class="desk-panel-head"><div><span class="desk-panel-index">03</span><h2>持仓监视</h2><small>POSITIONS</small></div><label class="desk-search"><span class="sr-only">筛选持仓代码</span><input id="desk-holding-search" type="search" placeholder="搜索股票代码" autocomplete="off"></label></div><div id="desk-holdings-table" class="desk-table-wrap"></div><p class="desk-panel-note">价格变化比较相邻记录的估值价，不是持仓浮盈；T+1 锁定数量以账本为准。</p></section>
    <section class="desk-panel desk-decision" data-window="decision"><div class="desk-panel-head"><div><span class="desk-panel-index">04</span><h2>最近一次决策</h2><small>DECISION DESK</small></div><a href="__BASE__/decisions">完整解释 ↗</a></div><div id="desk-decision-summary"></div><div id="desk-targets" class="desk-targets"></div><p class="desk-panel-note">最近目标与当前持仓对照，包含价格漂移；不是待执行订单，也不是实盘指令。</p></section>
    <section class="desk-panel desk-drawdown" data-window="drawdown"><div class="desk-panel-head"><div><span class="desk-panel-index">05</span><h2>回撤与仓位</h2><small>RISK MONITOR</small></div><span class="desk-tiny">跟随上方区间</span></div><div id="desk-drawdown-chart" class="desk-chart desk-small-chart"></div><div id="desk-risk-limits" class="desk-limit-list"></div></section>
    <section class="desk-panel desk-fills" data-window="fills"><div class="desk-panel-head"><div><span class="desk-panel-index">06</span><h2>模拟成交与受阻</h2><small>EXECUTION TAPE</small></div><span class="desk-tiny" id="desk-fill-count"></span></div><div id="desk-fills-table" class="desk-table-wrap"></div><p class="desk-panel-note">最多展示最近 100 条指令结果；零成交和部分成交均保留。</p></section>
    <section class="desk-panel desk-costs" data-window="costs"><div class="desk-panel-head"><div><span class="desk-panel-index">07</span><h2>费用拆解</h2><small>COST ANALYSIS</small></div></div><div class="desk-cost-total"><strong id="desk-cost-total">—</strong><span>CNY · 累计已计费用</span></div><div id="desk-cost-bars"></div><p class="desk-panel-note" id="desk-cost-note"></p></section>
    <section class="desk-panel desk-evidence" data-window="evidence"><div class="desk-panel-head"><div><span class="desk-panel-index">08</span><h2>记录质量</h2><small>EVIDENCE LOG</small></div></div><div id="desk-evidence-counts" class="desk-evidence-counts"></div><div id="desk-evidence-timeline" class="desk-evidence-timeline"></div><p class="desk-panel-note">迟到重建和历史演示分别标记，不计作按时前向。这里不拼接回测曲线。</p></section>
    <section class="desk-panel desk-operations" data-window="operations" id="desk-operations"><div class="desk-panel-head"><div><span class="desk-panel-index">09</span><h2>运行控制</h2><small>OPERATIONS</small></div><span id="desk-auto-state" class="desk-tiny"></span></div><div id="desk-operation-details" class="desk-operation-details"></div>
      <div class="desk-operation-actions"><form method="post" action="__BASE__/dashboard/actions/tick"><input type="hidden" name="token" value="__TOKEN__"><button id="desk-tick">检查行情并记账</button></form><form method="post" action="__BASE__/dashboard/actions/automation"><input type="hidden" name="token" value="__TOKEN__"><input type="hidden" name="enabled" id="desk-auto-value" value="yes"><button type="submit" id="desk-auto-button">开启每日检查</button></form><a href="__BASE__#activation" id="desk-activation" hidden>返回策略设置，开启观察 →</a></div>
      <p class="desk-panel-note">自动检查需工作台运行，17:30 后每日尝试一次。看板每 30 秒读取账本，刷新显示不会下载行情或执行记账。</p></section>
  </div>
  <footer class="desk-footer"><span><i class="desk-dot"></i> LOCAL WORKSPACE · 数据与持仓留在本机</span><div><a href="__BASE__/records.csv">导出运行记录</a><a href="__BASE__/policy-template.json">分享规则模板</a><a href="__BASE__">查看配置与历史检验</a></div><p>日频收盘记录 · 纸面运行，不连接券商。当前观察尚未证明收益优势；实际成交冲击未标定。</p></footer>
  <noscript><p class="desk-connection">看板交互需要 JavaScript。可<a href="__BASE__">返回策略页</a>读取记录与导出 CSV。</p></noscript>
</div>'''
