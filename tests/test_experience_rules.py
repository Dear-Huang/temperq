"""Experience scorecards are safe, causal and explainable."""

import numpy as np
import pandas as pd
import pytest

from qwb.experience_rules import RuleCard, RuleSyntaxError, Scorecard, evaluate
from qwb.experience_rule_templates import catalog, get
from qwb.experience_playbooks import catalog as playbook_catalog, resolve as resolve_playbook


@pytest.fixture
def panel():
    dates = pd.bdate_range('2024-01-01', periods=80)
    codes = ['000001.SZ', '600000.SH']
    close = pd.DataFrame({codes[0]: np.arange(10., 90.), codes[1]: np.arange(20., 100.)}, index=dates)
    return {name: close.copy() * factor for name, factor in {
        'open': .99, 'high': 1.01, 'low': .98, 'close': 1., 'amount': 100000., 'volume': 1000., 'adj_factor': 1.
    }.items()}


def test_template_catalog_contains_four_daily_scorecards():
    assert set(catalog()) == {'trend_following_daily', 'short_reversal_daily', 'low_volatility_daily', 'volume_price_daily'}
    assert all(value['data'] == 'daily_ohlc_amount' for value in catalog().values())


def test_trend_card_evaluates_wide_panel_and_explains_rules(panel):
    result = get('trend_following_daily').evaluate(panel)
    assert set(result['rule_values']) == {'close_above_ma20', 'ma20_above_ma60', 'momentum_60'}
    assert result['scores'].shape == panel['close'].shape
    assert result['eligible'].iloc[-1].all()
    assert result['scope'].endswith('not_performance_claim')


def test_required_rule_is_an_explicit_eligibility_mask(panel):
    card = Scorecard('x', 'test', (RuleCard('r', 'r', 'above(close, mean(close, 20))', required=True),))
    result = card.evaluate(panel)
    assert result['eligible'].iloc[10].isna().sum() == 0
    assert result['eligible'].iloc[0].eq(False).all()


@pytest.mark.parametrize('expression', [
    '__import__("os").system("whoami")', 'close.iloc[0]', 'open("secret")',
    'above(close)', 'mean(close, 0)', 'unknown(close, 20)', 'close if close else open',
])
def test_expression_language_rejects_code_and_invalid_rules(panel, expression):
    with pytest.raises(RuleSyntaxError): evaluate(expression, panel)


def test_missing_data_is_an_explicit_error(panel):
    with pytest.raises(RuleSyntaxError):
        evaluate('above(amount, mean(amount, 20))', {k: v for k, v in panel.items() if k != 'amount'})


def test_future_rows_do_not_change_prior_rolling_value(panel):
    before = evaluate('mean(close, 20)', panel).iloc[30].copy()
    changed = {k: v.copy() for k, v in panel.items()}
    changed['close'].iloc[31:] *= 100
    after = evaluate('mean(close, 20)', changed).iloc[30]
    assert after.equals(before)


def test_rule_card_weights_are_explicit_and_nonzero(panel):
    with pytest.raises(ValueError): RuleCard('x', 'x', 'close', weight=0)
    with pytest.raises(ValueError): Scorecard('x', 'x', ())
    card = Scorecard('x', 'x', (RuleCard('a', 'a', 'ret(close, 5)', 2), RuleCard('b', 'b', 'rank(close)', -1)))
    assert card.evaluate(panel)['rules'][0]['weight'] == 2


def test_experience_playbooks_have_executable_policy_and_rule_reference():
    books = playbook_catalog()
    assert {'first_strategy', 'patient_owner', 'active_rebalancer'} <= set(books)
    assert all(value['rule_template'].endswith('_daily') for value in books.values())
    assert resolve_playbook('patient_owner')['preset'] == 'patient'
