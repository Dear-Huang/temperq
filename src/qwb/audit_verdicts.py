"""User-facing conclusions derived from completed checks, not algorithm tiers."""
from __future__ import annotations

from .audit_catalog import STAGES

LABELS = {'pass': '基本通过', 'evidence': '缺乏证据', 'adjust': '需要调整'}

SUMMARIES = {
    'signal_contract': '核对预测的含义、可用时点，以及转成目标持仓的规则。',
    'universe': '检查股票准入、停牌与涨跌停过滤，是否在当时可知且可交易。',
    'portfolio_mapping': '检查预测排名如何分配股数、权重和现金，是否符合原策略。',
    'constraint_tradeoffs': '检查单票上限、现金、整手和交易限制是否冲突。',
    'risk_exposure': '检查实际持仓的集中度、现金占用，以及尚未解释的风险暴露。',
    'rebalance': '检查调仓频率、小额来回交易和调整速度是否与目标一致。',
    'signal_age': '检查信号在持仓期间是否过期，调仓节奏是否跟得上。',
    'costs': '核对手续费和价差情景，检查交易收益能否覆盖摩擦。',
    'capacity': '检查账户规模和交易量是否超出可用流动性。',
    'completion': '检查建仓和退出还差多少，受阻后是否有完成安排。',
    'execution': '核对委托与成交，检查漏调、延迟、库存和交易制度约束。',
    'account_integrity': '核对持仓、现金、成交和分红送转，检查账目能否闭合。',
    'attribution': '检查盈亏能否分解到股票与费用，区分会计贡献和策略能力。',
    'validation': '检查回测覆盖、数据时点和验证安排，防止把调参结果当证明。',
    'decision_governance': '检查代码、参数、报告和人工调整是否有可追溯的版本记录。',
}


def conclusions(bundle, result):
    rows = {s['id']: {'id': s['id'], 'title': s['title'], 'summary': SUMMARIES[s['id']],
                      'state': 'evidence', 'label': LABELS['evidence'],
                      'reason': '现有材料或已完成检查不足以确认这一环节。'} for s in STAGES}

    def set_row(key, state, reason):
        rows[key].update(state=state, label=LABELS[state], reason=reason)

    modules = result.get('modules', {})
    account, gov = modules.get('accounting'), modules.get('governance')
    risk, fees, tick = (modules.get(k) for k in ('risk_analysis', 'fee_analysis', 'tick_analysis'))
    if account and account['daily']:
        failures = sum(r['failures'] for r in account['summary'])
        set_row('account_integrity', 'adjust' if failures else 'pass',
                f'已核对库存、现金、估值与盈亏桥，有{failures}条异常。' if failures else
                '导出范围内库存、现金、估值与盈亏桥闭合；不认证未提供的账户期间。')
        attr = modules['attribution']
        set_row('attribution', 'pass' if attr['status'] == 'verified' else 'adjust',
                '逐股贡献与费用已闭合到权益变动；是否来自策略能力还需基准与反事实。' if attr['status'] == 'verified'
                else '逐股贡献尚未闭合到账户，请先修复账目再判断盈亏来源。')
    if gov:
        breach = next(r for r in gov['items'] if r['key'] == 'concentration')
        if breach['observed']:
            set_row('constraint_tradeoffs', 'adjust',
                    f'{int(breach["observed"])}条收盘持仓超过已确认的{gov["settings"]["max_weight"]:.1%}单票上限，需核对构建与执行。')
            set_row('risk_exposure', 'adjust', rows['constraint_tradeoffs']['reason'])
        else:
            set_row('constraint_tradeoffs', 'evidence', '收盘单票上限未见超限；完整目标、现金和可交易约束仍需联查。')
        inventory = next(r for r in gov['items'] if r['key'] == 'inventory')
        if inventory['observed']:
            set_row('execution', 'adjust', f'{int(inventory["observed"])}条卖出不满足已知库存必要条件，需补公司行动或核对成交。')
        same = next((r for r in gov['items'] if r['key'] == 'same_day_resale'), None)
        if same and same['observed'] and not inventory['observed']:
            set_row('execution', 'evidence', f'{int(same["observed"])}次当日买入当日卖出，账目闭合；需确认这些标的可以 T+0'
                    '（跨境、黄金、债券、货币类 ETF 与可转债可以，股票和股票型 ETF 不行）。')
    if fees and fees['priced_fill_count']:
        if fees['mismatch_count']:
            set_row('costs', 'adjust', f'{fees["mismatch_count"]}个已确认收费单位的费用与确认费率及历史税率不一致；差额需核实，不能直接算作节省。')
        elif fees.get('unknown_basis_unit_count'):
            set_row('costs', 'evidence', f'{fees["unknown_basis_unit_count"]}个单位缺收费依据或订单完整性；已给费用情景，补委托号和交割单后可核验差额。')
        elif fees['unpriced_fill_count']:
            set_row('costs', 'evidence', f'{fees["unpriced_fill_count"]}笔成交不在已覆盖费率日期，费用核对不完整。')
        else:
            set_row('costs', 'pass', '已覆盖日期的成交费用与确认口径一致；实际滑点和冲击尚未认证。')
    if tick and account and account['status'] == 'verified':
        interval = tick['accounted_intervals']
        if interval['profit_covers_scenario'] is False and interval['fills']:
            set_row('costs', 'adjust', '原净盈亏不足以覆盖半档价差情景；应核验成交机制，并复验降摩擦方案，不能视为已实现成本。')
    if risk and rows['risk_exposure']['state'] != 'adjust':
        set_row('risk_exposure', 'evidence', '已算单票集中度、现金和回撤；缺基准、行业与风格材料，不能确认总体风险适当。')
    validation = result.get('period_review', {})
    if validation:
        set_row('validation', 'evidence', validation['label']+'；分段已登记，尚无候选在验证段的比较结果。')
    if result.get('confirmation', {}).get('same_run'):
        set_row('decision_governance', 'pass', '本次输入摘要、确认参数与报告版本可核对；后续人工例外与实际成交仍需另留痕。')
    if bundle.get('scores'):
        set_row('signal_contract', 'evidence', '已导入打分；信号语义、可用时点及到目标的映射尚未完整验证。')
    return list(rows.values())
