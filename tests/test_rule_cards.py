"""Shared rule format, causality, validation and non-mutating tool behavior."""
from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from qwb import rule_cards as rc
from qwb.cli import main
from qwb.experience_rules import evaluate, RuleSyntaxError


@pytest.fixture
def panel():
    dates = pd.bdate_range('2024-01-01', periods=85)
    prices = pd.DataFrame({'000001.SZ': np.arange(10., 95.), '600000.SH': np.arange(30., 115.)}, index=dates)
    return {'close': prices, 'amount': prices * 1e6, 'adj_factor': prices * 0 + 1}


@pytest.mark.parametrize('doc', rc.catalog(), ids=lambda doc: doc['id'])
def test_rule_roundtrip_and_causal_score(doc, panel):
    copy = rc.loads(rc.dumps(doc))
    assert copy == doc
    before = rc.scorecard(doc).evaluate(panel)['scores']
    changed = {k: v.copy() for k, v in panel.items()}
    changed['close'].iloc[70:] *= 100
    after = rc.scorecard(copy).evaluate(changed)['scores']
    pd.testing.assert_frame_equal(before.iloc[:70], after.iloc[:70])


@pytest.mark.parametrize('change', [
    {'window': True}, {'window': 1.5}, {'window': -1}, {'window': 505},
    {'field': 'future_close'}, {'operator': '__import__'}, {'weight': float('nan')},
    {'required': 'false'}, {'origin': 'trusted_ai'}, {'expression': 'close'},
    {'description': '保证盈利'}, {'fee': 0}, {'value': 1},
])
def test_reject_unknown_and_out_of_range(change):
    doc = rc.template('trend_following_daily')
    doc['rules'][0].update(change)
    with pytest.raises(ValueError): rc.validate(doc)


def test_duplicate_json_and_false_source_are_handled():
    with pytest.raises(ValueError, match='重复'): rc.loads('{"schema":1,"schema":2}')
    doc = rc.loads(rc.dumps(rc.template('low_volatility_daily')), imported=True)
    assert all(r['origin'] == 'imported' for r in doc['rules'])
    with pytest.raises(ValueError): rc.loads('[' * 2000 + ']' * 2000)


def test_unknown_values_never_become_false_or_zero(panel):
    for expr in ('above(close, mean(close, 20))', 'cross_up(close, mean(close, 20))'):
        assert evaluate(expr, panel).iloc[0].isna().all()
    score = rc.scorecard(rc.template('trend_following_daily')).evaluate(panel)
    assert score['scores'].iloc[0].isna().all()
    assert not score['valid'].iloc[0].any()
    panel['close'].iloc[75, 0] = np.nan
    score = rc.scorecard(rc.template('trend_following_daily')).evaluate(panel)
    assert pd.isna(score['scores'].iloc[75, 0])


@pytest.mark.parametrize('expression', ['close**1000000', 'mean(close, 1.5)', 'mean(close, 999999)', 'rank(close, 1)', 'close.iloc[0]'])
def test_expression_resource_and_syntax_limits(panel, expression):
    with pytest.raises(RuleSyntaxError): evaluate(expression, panel)


def test_cli_is_json_and_read_only(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path/'rules.json'
    path.write_text(rc.dumps(rc.template('low_volatility_daily')), encoding='utf-8')
    original = path.read_bytes()
    assert main(['rule', 'validate', str(path), '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['valid'] and result['required_fields'] == ['adj_factor', 'amount', 'close']
    assert path.read_bytes() == original and list(tmp_path.iterdir()) == [path]
    path.write_text('{"fee":0}', encoding='utf-8')
    assert main(['rule', 'validate', str(path), '--json']) == 1
    assert not json.loads(capsys.readouterr().out)['valid']
