from copy import deepcopy

import pandas as pd
import pytest

from qwb import audit_period as period, strategy_audit as audit, strategy_imports as imp


def history(start='2021-01-04', end='2023-12-29'):
    bundle = imp.normalize([imp.SourceFile('strategy.py', b'capital_base=10000\n')])
    for day in pd.bdate_range(start, end).strftime('%Y-%m-%d'):
        bundle['snapshots'].append({'date': day, 'equity': 10000., 'cash': 9000.,
            'market_value': 1000., 'ref': {'source': 'positions', 'row': 2}})
        bundle['positions'].append({'date': day, 'code': '600000.SH', 'quantity': 100,
            'price': 10., 'market_value': 1000., 'ref': {'source': 'positions', 'row': 2}})
    return bundle


def test_partition_is_chronological_calendar_minimum_and_purges_labels():
    bundle = history()
    plan = period.partition(bundle)
    assert plan['status'] == 'planned'
    development, gap, validation = (set(plan[k]) for k in ('development_dates', 'purged_dates', 'validation_dates'))
    assert not development & gap and not gap & validation and not development & validation
    assert max(development) < min(gap) < max(gap) < min(validation)
    assert len(gap) == 20
    assert period.covers_months(pd.Timestamp(plan['validation_start']).date(), pd.Timestamp(plan['validation_end']).date(), 3)
    assert not plan['optimization_performed'] and plan['validation_evaluations'] == 0
    original = deepcopy(bundle)
    dev = period.development_bundle(bundle, plan)
    assert max(r['date'] for r in dev['snapshots']) == max(development)
    assert bundle == original
    tampered = deepcopy(plan); tampered['development_dates'].append(max(validation))
    with pytest.raises(ValueError, match='摘要'):
        period.development_bundle(bundle, tampered)


def test_eight_months_reserve_three_months_not_just_quarter_of_rows():
    report = period.assess(history('2024-05-07', '2024-12-31'))
    assert report['band'] == '3m_to_1y'
    plan = report['partition']
    assert plan['status'] == 'planned'
    assert plan['realized_validation_fraction'] > .25
    assert report['regime']['status'] == 'unverified'
    assert next(s for s in report['stages'] if s['id'] == 'rebalance')['state'] == 'descriptive_only'


def test_long_history_does_not_manufacture_materials_or_green_verdicts():
    report = period.assess(history())
    assert report['band'] == 'over_2y'
    for key in ('signal_age', 'universe', 'portfolio_mapping', 'execution'):
        row = next(s for s in report['stages'] if s['id'] == key)
        assert row['state'] == 'missing_inputs'
    assert all(s['state'] != 'effective' for s in report['stages'])


def test_short_history_and_static_code_never_get_fake_validation_windows():
    for bundle in (history('2024-01-02', '2024-05-31'), imp.normalize([imp.SourceFile('strategy.py', b'x=1')])):
        report = period.assess(bundle)
        assert report['partition']['status'] == 'insufficient_period'
        assert report['partition']['validation_dates'] == []
        with pytest.raises(ValueError, match='不足'):
            period.development_bundle(bundle, report['partition'])


def test_trigger_gate_counts_independent_dates_and_never_asserts_effect():
    same = period.rule_gate(['2024-01-02'] * 100)
    assert same['triggers'] == 100 and same['distinct_dates'] == 1
    assert not same['can_test_effect']
    independent = period.rule_gate(pd.bdate_range('2024-01-02', periods=20).strftime('%Y-%m-%d').tolist())
    assert independent['can_test_effect'] and independent['effect_conclusion'] is None


@pytest.mark.parametrize('config', [{'validation_fraction': float('nan')}, {'validation_fraction': .01},
    {'minimum_triggers': 2}, {'purge_days': 0}, {'purge_days': 1.5}])
def test_bad_advanced_policy_is_rejected(config):
    with pytest.raises(ValueError):
        period.policy(config)


def test_nondefault_policy_is_versioned_in_result_not_accounting():
    bundle = history()
    base = audit.analyze(bundle)
    changed = audit.analyze(bundle, policy={'validation_fraction': .3, 'minimum_triggers': 30, 'purge_days': 25})
    assert changed['period_review']['non_default_settings']['minimum_triggers'] == 30
    assert base['modules']['accounting'] == changed['modules']['accounting']
    assert base['result_digest'] != changed['result_digest']
    assert base['input_digest'] == changed['input_digest']
