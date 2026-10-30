"""Versioned chronological eligibility and an internal comparison partition.

This creates no optimized strategy, validation result or claim of unseen data.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
import math

from .audit_catalog import STAGES
from .audit_input import available_dates
from .experiments import object_digest

DEFAULT_POLICY = {'validation_fraction': .25, 'minimum_triggers': 20, 'purge_days': 20}


def policy(values=None):
    out = dict(DEFAULT_POLICY)
    if values:
        if set(values) - set(out):
            raise ValueError('不支持的审计周期设置')
        out.update(values)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in out.values()):
        raise ValueError('审计周期设置须为有限数字')
    if not .1 <= out['validation_fraction'] <= .5:
        raise ValueError('验证比例须在10%至50%之间')
    if int(out['minimum_triggers']) != out['minimum_triggers'] or not 20 <= out['minimum_triggers'] <= 10000:
        raise ValueError('规则触发门槛可提高，不能低于20次')
    if int(out['purge_days']) != out['purge_days'] or not 1 <= out['purge_days'] <= 252:
        raise ValueError('边界隔离须为1至252个观测交易日期')
    out['minimum_triggers'], out['purge_days'] = int(out['minimum_triggers']), int(out['purge_days'])
    return out


def months_after(day, months):
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def covers_months(start, end, count):
    return end >= months_after(start, count) - timedelta(days=1)


def partition(bundle, settings=None):
    settings = policy(settings)
    dates = available_dates(bundle)
    result = {'version': 'audit_internal_partition_v1', 'settings': settings,
        'input_digest': object_digest(bundle), 'status': 'insufficient_period',
        'development_dates': [], 'purged_dates': [], 'validation_dates': [],
        'development_start': None, 'development_end': None, 'validation_start': None, 'validation_end': None,
        'minimum_validation_months': 3, 'minimum_development_months': 3,
        'meaning': '历史内部验证段，不能认证未见；已查看过的回测尾段不变成新的独立留出。',
        'optimization_performed': False, 'validation_evaluations': 0}
    if len(dates) < 2:
        result['reason'] = '没有足够的历史日期可切分。'
    else:
        start, end = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        if not covers_months(start, end, 6):
            result['reason'] = '建议段与验证段各需至少3个月；当前历史不足6个月。'
        else:
            nominal = start + timedelta(days=int(((end - start).days + 1) * (1 - settings['validation_fraction'])))
            latest = months_after(end + timedelta(days=1), -3)
            target = min(nominal, latest)
            starts = [d for d in dates if date.fromisoformat(d) >= target]
            cut = starts[0] if starts else dates[-1]
            if date.fromisoformat(cut) > latest:
                cut = max(d for d in dates if date.fromisoformat(d) <= latest)
            before = [d for d in dates if d < cut]
            n = settings['purge_days']
            development = before[:-n] if len(before) > n else []
            purged = before[-n:]
            validation = [d for d in dates if d >= cut]
            if development and covers_months(start, date.fromisoformat(development[-1]), 3):
                result.update(status='planned', development_dates=development, purged_dates=purged,
                    validation_dates=validation, development_start=development[0], development_end=development[-1],
                    validation_start=validation[0], validation_end=validation[-1],
                    realized_validation_fraction=(end - date.fromisoformat(cut)).days / max(1, (end-start).days),
                    reason='按时间切分并隔离边界；这里只登记分段，没有运行调优或验证比较。')
            else:
                result['reason'] = '留出3个月并隔离边界后，建议段不足3个月；保留账目检查，不输出对比结论。'
    result['plan_digest'] = object_digest(result)
    return result


def assess(bundle, settings=None):
    settings = policy(settings)
    dates = available_dates(bundle)
    if not dates:
        band, label, max_months = 'static', '仅代码静态检查', 0
    else:
        start, end = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        if not covers_months(start, end, 3):
            band, label, max_months = 'under_3m', '不足3个月', 0
        elif not covers_months(start, end, 12):
            band, label, max_months = '3m_to_1y', '3个月至1年：规则效果仅作描述', 3
        elif not covers_months(start, end, 24):
            band, label, max_months = '1y_to_2y', '1至2年：满足部分效果检验的长度条件', 12
        else:
            band, label, max_months = 'over_2y', '2年以上：仍需逐项满足材料与算法条件', 24
    kinds = {s['kind'] for s in bundle['sources']}
    stages = []
    for stage in STAGES:
        key = stage['id']
        minimum = 0 if key in {'costs', 'execution', 'account_integrity', 'decision_governance'} else 3 if key in {'attribution', 'risk_exposure', 'rebalance'} else 12
        missing = []
        if key == 'execution' and 'logs' not in kinds:
            missing.append('运行日志')
        if key in {'signal_contract', 'signal_age', 'portfolio_mapping', 'universe'} and not bundle.get('scores'):
            missing.append('每日打分数据')
        if key in {'account_integrity', 'attribution', 'risk_exposure'} and not bundle['snapshots']:
            missing.append('账户汇总与完整持仓')
        if not dates:
            state, message = 'static_only', '仅代码检查，不作历史效果结论'
        elif missing:
            state, message = 'missing_inputs', '未审：缺'+ '、'.join(missing)
        elif minimum > max_months:
            state, message = 'sample_too_short', '样本太短；可查看已算出的账目事实，不下规则效果结论'
        elif max_months < 12 and key not in {'costs', 'execution', 'account_integrity', 'decision_governance'}:
            state, message = 'descriptive_only', '黄灯：只展示历史统计，不判规则有效'
        else:
            state, message = 'length_eligible', '长度条件允许继续检验；不代表材料齐全、算法已运行或效果成立'
        if bundle.get('quality', {}).get('fills_inferred') and key in {'execution', 'costs', 'attribution', 'account_integrity'}:
            if missing:
                message += '；另仅有持仓净变动推算，不能认证真实成交。'
            else:
                state, message = 'approximate', '近似：交易由持仓净变动推算，真实成交价、费用和公司行动需补充'
        stages.append({'id': key, 'title': stage['title'], 'state': state, 'message': message,
                       'minimum_months': minimum, 'missing': missing})
    down = None
    if len(bundle['snapshots']) >= 2:
        peak, draw = 0., 0.
        for snapshot in bundle['snapshots']:
            equity = snapshot['equity']
            peak = max(peak, equity)
            if peak > 0:
                draw = max(draw, 1 - equity / peak)
        down = draw
    return {'version': 'audit_period_eligibility_v1', 'band': band, 'label': label,
        'observed_dates': len(dates), 'actual_start': dates[0] if dates else None,
        'actual_end': dates[-1] if dates else None, 'settings': settings,
        'rules_effect_warning': '不足1年不出规则有效/无效结论。20次触发只是最低门槛，仍需独立日期、区间和多重比较控制。',
        'regime': {'status': 'unverified', 'account_snapshot_max_drawdown': down,
                   'meaning': '策略账户回撤不能证明样本覆盖市场下跌；需要同期间基准行情另行核对。'},
        'stages': stages, 'partition': partition(bundle, settings),
        'non_default_settings': {k: v for k, v in settings.items() if v != DEFAULT_POLICY[k]}}


def rule_gate(events, settings=None):
    settings = policy(settings)
    dates = {str(e['date'] if isinstance(e, dict) else e) for e in events}
    allowed = len(events) >= settings['minimum_triggers'] and len(dates) >= settings['minimum_triggers']
    return {'triggers': len(events), 'distinct_dates': len(dates), 'can_test_effect': allowed,
            'effect_conclusion': None, 'meaning': '允许检验不等于有效；同日多只股票或拆单不算独立日期。'}


def development_bundle(bundle, plan):
    """Only expose development rows to future optimizers; never infer missing targets."""
    check = {k: v for k, v in plan.items() if k != 'plan_digest'}
    if object_digest(check) != plan.get('plan_digest') or plan['input_digest'] != object_digest(bundle):
        raise ValueError('分段计划或输入摘要不一致')
    if plan['status'] != 'planned':
        raise ValueError('当前历史不足以建立建议段和验证段')
    allowed = set(plan['development_dates'])
    out = deepcopy(bundle)
    for key in ('fills', 'positions', 'snapshots', 'actions', 'scores', 'quote_observations', 'minute_prices', 'day_ranges'):
        out[key] = [r for r in bundle.get(key, []) if r['date'] in allowed]
    out['logs'] = [r for r in bundle['logs'] if r.get('时间', '')[:10] in allowed]
    # Full-period external attribution is not a development-only observation.
    out['external_attribution'] = []
    out['partition_context'] = {'plan_digest': plan['plan_digest'], 'scope': 'development_only'}
    return out
