"""Material readiness, literal parameter provenance and pre-audit consistency."""
from __future__ import annotations

from .securities import pays_stamp_tax, tick_yuan

from collections import Counter

PLATFORMS = {'bigquant_v1': 'BigQuant', 'joinquant_v1': '聚宽', 'ricequant_v1': '米筐', 'generic_v1': '其他', 'code_only_v1': '未识别平台'}


def full_account_available(bundle):
    return bool(bundle['snapshots'] and bundle['positions']
                and not bundle.get('quality', {}).get('fills_inferred')
                and all(p.get('price') is not None and p.get('market_value') is not None
                        for p in bundle['positions']))


def parameter_candidates(bundle):
    candidates = bundle.get('code', {}).get('candidates', [])
    definitions = (
        ('initial_capital', '初始资金', ('capital_base',), None, 1),
        ('commission_bp', '买入佣金（bp）', ('buy_cost', 'open_commission'), 3., 10000),
        ('minimum_commission', '最低佣金（元）', ('min_cost', 'min_commission'), 5., 1),
        ('max_weight', '单票上限候选', ('max_cash_per_instrument',), 1., 1),
        ('holding_count', '选股/持仓数候选', ('stock_count', 'hold_count', 'hold_num', 'n_hold'), None, 1),
        ('holding_days', '代码 hold_days（用途待核）', ('hold_days',), None, 1),
        ('rebalance_days', '调仓间隔候选', ('rebalance_period_days',), None, 1),
        ('buy_price', '买入价格设置', ('order_price_field_buy',), None, 1),
        ('sell_price', '卖出价格设置', ('order_price_field_sell',), None, 1),
    )
    result = []
    for key, label, names, default, scale in definitions:
        matches = [c for c in candidates if c['key'] in names and c['value'] != '']
        values = {str(c['value']) for c in matches}
        value = matches[0]['value'] if len(values) == 1 else default
        source = 'code_literal' if len(values) == 1 else 'conflicting_literals' if matches else 'platform_default' if default is not None else 'missing'
        if len(values) == 1 and value is not None and isinstance(value, (int, float)):
            value *= scale
            if key == 'commission_bp':
                value = round(value, 10)
        if key == 'max_weight' and (not isinstance(value, (int, float)) or not 0 < value <= 1):
            value, source = default, 'needs_confirmation'
        result.append({'key': key, 'label': label, 'value': value, 'source': source,
                       'evidence': matches, 'confirmed': False})
    return result


def available_dates(bundle):
    # Trade dates alone cannot establish daily account coverage or a market regime.
    return sorted({r['date'] for key in ('snapshots', 'positions', 'fills') for r in bundle[key]})


def review(bundle):
    kinds = {s['kind'] for s in bundle['sources']}
    dates = available_dates(bundle)
    code = bundle.get('code', {})
    inferred = bundle.get('quality', {}).get('fills_inferred', False)
    parameters = parameter_candidates(bundle)
    checks = []

    def check(key, title, status, detail, action):
        checks.append({'key': key, 'title': title, 'status': status, 'detail': detail, 'action': action})

    check('code', '策略代码', 'consistent' if 'code' in kinds else 'blocked',
          '已保留原文，只做静态解析。' if 'code' in kinds else '缺少策略代码，不能核对策略意图。',
          '核对参数候选，代码不会运行。' if 'code' in kinds else '上传 .py/.txt 或粘贴代码后再审计。')
    if code.get('status') == 'unparsed':
        check('syntax', '代码解析', 'review', '代码无法静态解析，不能读取设置或生成安全补丁。', '检查 Python 语法；交易费用仍可独立核对。')
    check('fills', '回测交易', 'approximate' if inferred else 'consistent' if bundle['fills'] else 'missing',
          bundle.get('quality', {}).get('inference_note', '') if inferred else f"识别 {len(bundle['fills'])} 笔成交。",
          '建议补原始成交和公司行动；推算净变动不能审真实费用。' if inferred else '核对是否同一次回测的完整导出。')
    if bundle['fills'] and not inferred:
        from .audit_fee_units import charging_units
        from .portfolio.reference import Fees
        units = charging_units(bundle['fills'], Fees(0., 0., 0.))
        confirmed = sum(u['confirmed'] for u in units)
        check('fee_units', '委托归属与最低佣金单位', 'consistent' if confirmed == len(units) else 'missing',
              f'{len(bundle["fills"])}笔成交，{len(units)}个收费单位；{confirmed}个有合同/交割单依据且满足完整性要求。',
              '补委托编号、成交编号、佣金计费单位、计费依据。按委托时仅最终全部成交已导出才确认完整；未知留空，不影响先做情景分析。')
    check('holdings', '持仓与账户', 'consistent' if full_account_available(bundle) else 'approximate' if bundle['positions'] else 'missing',
          f"{len(bundle['snapshots'])} 个账户快照、{len(bundle['positions'])} 条持仓。",
          '账户资料完整时可逐股对账；缺持仓/行情时不虚构回撤或期末市值。')
    check('logs', '运行日志', 'consistent' if 'logs' in kinds else 'missing',
          f"识别 {len(bundle['logs'])} 条日志。" if 'logs' in kinds else '成交可行性与执行偏离：未审，缺日志。',
          '日志计数不是订单计数；需匹配订单生命周期才可判断漏单。')
    if 'logs' in kinds:
        check('orders_vs_fills', '日志委托与成交对应', 'review',
              f"日志 {len(bundle['logs'])} 行、成交 {len(bundle['fills'])} 笔；尚无统一订单编号与完整生命周期映射。",
              '不能把日志行数当委托数或把差额当漏单；补订单编号、提交、撤单与成交回报后核对。')
    orders = bundle.get('orders') or []
    if orders:
        failed = [o for o in orders if o['status'] in {'cancelled', 'rejected'} and o['filled'] < o['quantity']]
        why = Counter(o['reason'] or '未说明' for o in failed)
        check('orders', '委托与成交', 'consistent',
              f"识别 {len(orders)} 笔委托，{len(failed)} 笔未全部成交" + ("：" + "、".join(f"{k} {v} 笔" for k, v in why.most_common()) if why else "。"),
              '未成交的委托不产生费用，但会让持仓偏离信号；原因见『排名一变就换仓』卡片。')
    check('scores', '每日打分', 'consistent' if bundle.get('scores') else 'missing',
          f"识别 {len(bundle.get('scores', []))} 条打分。" if bundle.get('scores') else '信号寿命、构建方式对比、规则暴露需要每日打分数据。',
          '提供 date, code, score，可补 available_at；有打分不代表对应算法已运行。')
    if dates:
        starts = [c for c in code.get('candidates', []) if c['key'] in ('start_date', 'start')
                  and isinstance(c['value'], str) and len(c['value']) == 10]
        ends = [c for c in code.get('candidates', []) if c['key'] in ('end_date', 'end')
                and isinstance(c['value'], str) and len(c['value']) == 10]
        intended_start = min((c['value'] for c in starts), default=None)
        intended_end = max((c['value'] for c in ends), default=None)
        mismatch = (intended_start and dates[0] > intended_start) or (intended_end and dates[-1] < intended_end)
        check('period', '代码区间与实际文件', 'review' if mismatch else 'consistent' if intended_start and intended_end else 'missing',
              f"实际 {dates[0]} 至 {dates[-1]}；代码区间 {intended_start or '未读出'} 至 {intended_end or '未读出'}。",
              '区间可能是数据抽取窗口或回测设置；确认截取范围，必要时重新导出完整期间。' if mismatch else '以文件的实际覆盖期间开展审计。')
    capital = next(p['value'] for p in parameters if p['key'] == 'initial_capital')
    if bundle['snapshots']:
        check('opening', '初始资金与首日权益', 'review' if bundle['opening']['kind'] != 'empty' else 'consistent',
              f"代码初始资金 {capital if capital is not None else '未读出'}；首日收盘权益 {bundle['snapshots'][0]['equity']:.2f}。",
              '首日可有交易盈亏，不能把这两个数不相等直接判成资金错误；未声明空仓期初时从首日收盘之后归因。')
        negative_cash = sum(s['cash'] < 0 for s in bundle['snapshots'])
        if negative_cash:
            check('negative_cash', '负现金记录', 'review', f'{negative_cash} 个账户快照的现金为负，保留原值。',
                  '核对平台费用、结算或融资假设；不自动把负现金改成零，也不直接用于只做多现金账户计划。')
    price_options = [c for c in code.get('candidates', []) if c['key'] == 'use_real_price']
    check('price_basis', '价格口径', 'missing',
          '持仓和成交表自身不足以认证不复权成交价；未用外部行情核对比例。',
          '上传原平台价格设置或对应日期不复权日线；不得直接改股数/金额使账目对齐。')
    returns = bundle.get('platform_returns') or []
    equity = {s['date']: s['equity'] for s in bundle['snapshots']}
    paired = [(r['strategy_pct'], equity[r['date']]) for r in returns if r['date'] in equity]
    if paired:
        base = paired[0][1] / (1 + paired[0][0] / 100)
        worst = max(abs((e / base - 1) * 100 - pct) for pct, e in paired)
        check('platform_returns', '平台收益表与账户', 'consistent' if worst <= .05 else 'review',
              f'平台报告的累计收益与按持仓、现金还原的账户逐日对照 {len(paired)} 天，最大差 {worst:.2f} 个百分点。',
              '一致说明导出完整、属于同一次回测。' if worst <= .05 else '差异较大：确认收益表与交易、持仓来自同一次回测。')
    if bundle['snapshots']:
        first, last = bundle['snapshots'][0]['date'], bundle['snapshots'][-1]['date']
        outside = sorted({r['date'] for key in ('fills', 'positions') for r in bundle[key]
                          if not first <= r['date'] <= last})
        if outside:
            check('coverage', '交易、持仓与账户区间', 'review',
                  f'账户记录覆盖 {first} 至 {last}；另有 {len(outside)} 天的交易或持仓在此之外（{outside[0]} 起），这些天不进入对账和归因。',
                  '常见原因是某个文件只导出了一部分，或几份文件不是同一次回测；请重新完整导出。')
    rounded = bundle.get('quality', {}).get('rounded_price_fills')
    if rounded:
        check('rounded_prices', '成交价四舍五入', 'consistent',
              f'交易详情里的成交价是含滑点后四舍五入到 3–4 位小数的；{rounded} 笔成交按账户实际收付的金额入账，差额在价格末位的四舍五入以内。',
              '无需处理；如要逐笔精确，请导出未四舍五入的成交价。')
    receivable = sum(1 for s in bundle['snapshots'] if s.get('receivable_dividend'))
    if receivable:
        check('receivable_dividend', '应收股息', 'consistent',
              f'账户详情有 {receivable} 天含已宣告未到账的股息，计入账户现金；到账当天只是从应收转为现金，不算盈亏。',
              '无需处理。')
    if bundle.get('scores'):
        priced = len({p['code'] for p in bundle.get('prices', [])})
        scored = len({s['code'] for s in bundle['scores']})
        check('prices', '候选池收盘价', 'consistent' if priced else 'missing',
              f'识别 {priced} 只标的的收盘价，打分覆盖 {scored} 只。' if priced else '有打分但没有候选池收盘价，策略前提只能记录、不能检验。',
              '用于在盈亏归因里检验策略前提：分数高的标的随后是否真的涨得更多。')
    ranked = [s for s in bundle.get('scores', []) if s.get('rank') is not None]
    if ranked:
        days = len({s['date'] for s in ranked})
        flagged = any(s.get('selected') is not None for s in ranked)
        check('ranking', '策略每日排名', 'consistent',
              f'识别 {days} 天、{len(ranked)} 条排名' + ('，含策略选中的目标。' if flagged else '；未标出选中目标，按每天第 1 名当作目标。'),
              '排名用于核对持仓是否跟随信号、统计换仓频率；它是信号排序，不是成交清单。')
    inferred_actions = bundle.get('quality', {}).get('inferred_actions')
    if inferred_actions:
        check('corporate_actions', '分红与送转（由账户推断）', 'review',
              f"原平台未导出公司行动记录；按账户变化推断现金分红 {inferred_actions['cash_dividend']} 条、送转 {inferred_actions['bonus_shares']} 条。",
              '分红金额按前一日市值分摊到当日持仓，是估计；送转按无成交时股数增加、价格同步下跌识别。有平台公司行动记录时以原记录为准。')
    if full_account_available(bundle):
        from .strategy_audit import accounting
        account = accounting(bundle)
        count = sum(r['failures'] for r in account['summary'] if r['key'] != 'prior_inventory_for_sales')
        check('inventory_bridge', '成交、库存与现金一致性', 'consistent' if count == 0 else 'review',
              f"{count} 项库存、现金、估值或盈亏桥异常。", '缺公司行动时补日志和事件记录，不自动调整费用或持仓。')
    mode = 'static' if not dates else 'partial' if not full_account_available(bundle) else 'full'
    return {'version': 'audit_input_review_v1', 'platform': PLATFORMS.get(bundle['adapter'], '其他'),
        'mode': mode, 'can_analyze': 'code' in kinds,
        'financial_ready': 'code' in kinds and bool(bundle['fills']), 'fills_inferred': inferred,
        'parameters': parameters, 'checks': checks,
        'actual_start': dates[0] if dates else None, 'actual_end': dates[-1] if dates else None,
        'source_summary': [{'kind': s['kind'], 'name': s['name'], 'rows': s['rows'],
                            'encoding': s.get('encoding', 'historical_unspecified')} for s in bundle['sources']],
        'style': bundle.get('metadata', {}).get('style', {'value': '', 'source': 'user_confirmation_required'}),
        'export_guides': {'bigquant': '导出同次回测的代码、交易、持仓和运行日志。',
                          'joinquant': '持仓导出格式已适配；交易和日志入口尚未经真实账号核实。'}}


def partial_result(bundle, settings):
    """Useful observed amounts and explicit scenarios without invented account paths."""
    from .portfolio.reference import Fees
    from datetime import date
    fees = Fees(settings['commission_bp'], settings['minimum_commission'], 0.)
    from .audit_fee_units import charging_units
    units = charging_units(bundle['fills'], fees)
    commissions = {i: u['modeled_commission'] * bundle['fills'][i]['notional'] / u['notional']
                   for u in units for i in u['indexes']}
    rows = []
    inferred = bundle.get('quality', {}).get('fills_inferred', False)
    for index, f in enumerate(bundle['fills']):
        try:
            cost = fees.costs(f['notional'], f['side'].upper(), date.fromisoformat(f['date']))
            if not pays_stamp_tax(f['code']):
                cost['stamp_tax'] = 0.0
            cost['commission'] = commissions[index]
            modeled = sum(cost.values())
        except ValueError:
            modeled = None
        rows.append({'date': f['date'], 'code': f['code'], 'quantity': f['quantity'],
            'notional_reference': f['notional'], 'reported_fee': None if inferred else f['fee'],
            'modeled_fee_scenario': modeled, 'tick_half_spread_bp': 5000 * tick_yuan(f['code']) / f['price'],
            'tick_cost_scenario': f['notional'] * 50 / f['price'] / 10000,
            'inferred': inferred})
    return {'id': 'partial_diagnostics', 'status': 'approximate' if inferred else 'observed_fills_only',
        'fills': rows, 'reported_fees': None if inferred else sum(f['fee'] for f in bundle['fills']),
        'modeled_fee_scenario': sum(r['modeled_fee_scenario'] for r in rows if r['modeled_fee_scenario'] is not None),
        'tick_cost_scenario': sum(r['tick_cost_scenario'] for r in rows),
        'net_pnl': None, 'drawdown': None,
        'meaning': '由持仓推算净变动，价格仅作参考；实际费用未知。' if inferred else '可核成交费用和价位情景；缺少完整账户路径，不报告精确归因或回撤。'}
