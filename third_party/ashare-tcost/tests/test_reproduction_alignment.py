"""Small alignment fixtures, not empirical reproduction of the paper."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest


def script():
    path = Path(__file__).resolve().parents[1]/'scripts/reproduce_paper.py'
    spec = importlib.util.spec_from_file_location('cost_reproduction', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def panels():
    monthly = pd.DataFrame({'code': ['a']*3, 'ym': ['2021-01', '2021-02', '2021-04'],
        'amount': [1e7, 2e7, 3e7], 'price': [10., 100., 20.], 'vol': [.02, .03, .04]})
    truth = monthly[['code', 'ym']].copy()
    truth['spread_bp'] = [12., 1.5, 6.]
    truth['l2_coverage'] = .9
    truth['l2_days'] = 20
    return truth, monthly


def test_default_reproduction_aligns_previous_calendar_month_but_target_tick_eligibility():
    module = script()
    truth, features = panels()
    aligned = module.prepare_panel(truth, features)
    assert aligned.ym.tolist() == ['2021-02']  # April cannot use February as its source
    assert aligned.feature_ym.tolist() == ['2021-01']
    assert aligned.price.iloc[0] == 10.
    assert aligned.target_n_ticks.iloc[0] == pytest.approx(1.5)  # target price=100
    assert aligned.spread_bp.iloc[0] == 1.5
    # Missing eligible source L2 row is recorded as unmatched, never synthesized.
    assert module.prepare_panel(truth.iloc[1:], features).empty


def test_historical_same_month_reproduction_is_explicit():
    module = script()
    truth, features = panels()
    same = module.prepare_panel(truth, features, 'same_month')
    assert same.ym.tolist() == features.ym.tolist()
    assert 'feature_ym' not in same
    assert same.price.tolist() == features.price.tolist()
    with pytest.raises(ValueError, match='design'):
        module.prepare_panel(truth, features, 'unknown')
