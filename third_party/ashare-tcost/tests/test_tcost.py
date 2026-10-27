import numpy as np
import pandas as pd
import pytest

import ashare_tcost as tc


def test_tick_floor_binds_for_cheap_liquid_stock():
    # 2-yuan, very liquid stock: floor 50 bp beats the liquidity part
    df = pd.DataFrame({"amount": [5e9], "vol": [0.02], "price": [2.0]})
    assert tc.struct_spread(df).iloc[0] == pytest.approx(50.0)


def test_liquidity_part_for_expensive_illiquid_stock():
    df = pd.DataFrame({"amount": [5e6], "vol": [0.04], "price": [200.0]})
    s = tc.struct_spread(df).iloc[0]
    assert s > tc.tick_floor_bp([200.0])[0]          # floor 0.5 bp does not bind
    b = tc.DEFAULT_BETA
    expect = np.exp(b[0] + b[1] * np.log(5e6) + b[2] * np.log(0.04) + b[3] * np.log(200.0))
    assert s == pytest.approx(expect)


def test_default_calibration_uses_paper_lagged_coefficients_without_an_implicit_shift():
    frame = pd.DataFrame({'amount': [5e6], 'vol': [.04], 'price': [200.]}, index=['target'])
    expected = np.exp(10.4316 - .3758*np.log(5e6) + .4983*np.log(.04) + .1689*np.log(200.))
    result = tc.struct_spread(frame)
    assert result.index.tolist() == ['target']
    assert result.iloc[0] == pytest.approx(expected)
    assert tc.struct_spread(frame, tc.SAME_MONTH_BETA).iloc[0] != pytest.approx(expected)
    # A coefficients-only update cannot change the independent one-tick component.
    assert tc.tick_floor_bp([2., 5., 10.]).tolist() == [50., 20., 10.]


def test_calendar_alignment_has_no_gap_fill_or_second_lag_and_preserves_input():
    frame = pd.DataFrame({'code': ['a', 'a', 'b'], 'ym': ['2024-01', '2024-03', '2023-12'],
                          'amount': [1e8]*3, 'price': [2., 4., 10.], 'vol': [.02]*3})
    original = frame.copy(deep=True)
    aligned = tc.lag_monthly_features(frame)
    assert aligned.ym.tolist() == ['2024-02', '2024-04', '2024-01']
    assert aligned.feature_ym.tolist() == ['2024-01', '2024-03', '2023-12']
    assert aligned.price.tolist() == [2., 4., 10.]
    pd.testing.assert_frame_equal(frame, original)
    with pytest.raises(ValueError, match='second lag'):
        tc.lag_monthly_features(aligned)
    with pytest.raises(ValueError, match='duplicate'):
        tc.lag_monthly_features(pd.concat([frame, frame.iloc[:1]]))
    with pytest.raises(ValueError, match='YYYY-MM'):
        tc.lag_monthly_features(frame.assign(ym='2024-13'))


def test_lagged_refit_uses_target_tick_filter_instead_of_lagged_price():
    rng = np.random.default_rng(10)
    n = 500
    frame = pd.DataFrame({'amount': np.exp(rng.uniform(15, 22, n)),
                          'vol': np.exp(rng.uniform(-4, -2, n)),
                          'price': np.exp(rng.uniform(3, 6, n)), 'target_ticks': 2.})
    true = np.array([9., -.4, .5, .2])
    X = np.column_stack([np.ones(n), np.log(frame.amount), np.log(frame.vol), np.log(frame.price)])
    frame['spread_bp'] = np.exp(X @ true)
    outliers = frame.iloc[:100].copy()
    outliers['price'] *= 100
    outliers['spread_bp'] *= 100
    outliers['target_ticks'] = 1.
    mixed = pd.concat([frame, outliers])  # deliberately retains duplicate row indices
    result = tc.fit_struct(mixed, target_n_ticks='target_ticks')
    np.testing.assert_allclose(result, true, atol=1e-10)
    with pytest.raises(ValueError, match='full-rank'):
        tc.fit_struct(frame.assign(target_ticks=1.), target_n_ticks='target_ticks')


def test_monotone_in_amount_and_bad_inputs_are_nan():
    df = pd.DataFrame({"amount": [1e7, 1e8, 1e9, 0.0], "vol": [0.03] * 4,
                       "price": [50.0, 50.0, 50.0, 50.0]})
    s = tc.struct_spread(df)
    assert s.iloc[0] > s.iloc[1] > s.iloc[2] and np.isnan(s.iloc[3])


def test_fit_recovers_planted_beta_and_ignores_bound_rows():
    rng = np.random.default_rng(0)
    n = 20000
    df = pd.DataFrame({"amount": np.exp(rng.uniform(15, 22, n)),
                       "vol": np.exp(rng.uniform(-4.5, -2.5, n)),
                       "price": np.exp(rng.uniform(0.5, 5, n))})
    true = np.array([9.0, -0.4, 0.5, 0.2])
    X = np.column_stack([np.ones(n), np.log(df.amount), np.log(df.vol), np.log(df.price)])
    liq = np.exp(X @ true + rng.normal(0, 0.05, n))
    df["spread_bp"] = np.maximum(tc.tick_floor_bp(df.price), liq)
    beta = tc.fit_struct(df)
    assert np.allclose(beta, true, atol=0.02)


def _daily(n_days=60, codes=("000001", "600000")):
    rng = np.random.default_rng(1)
    rows = []
    for c in codes:
        px = 10.0
        for d in pd.bdate_range("2024-01-01", periods=n_days):
            r = rng.normal(0, 0.02)
            px *= 1 + r
            rows.append(dict(code=c, date=d, high=px * 1.01, low=px * 0.99, close=px,
                             close_raw=px, amount=1e8, ret=r))
    return pd.DataFrame(rows)


def test_monthly_features_shape_and_columns():
    m = tc.monthly_features(_daily())
    assert set(["code", "ym", "amount", "price", "vol", "cs_bp", "amihud", "zeros", "vov"]) <= set(m)
    assert m.groupby("code").ym.nunique().min() >= 2
    assert (m["vol"] > 0).all()
    assert tc.struct_spread(m).notna().all()


def test_corwin_schultz_is_available_at_pair_end_and_does_not_cross_securities():
    daily = pd.DataFrame({'code': ['a', 'a', 'b', 'b'],
                          'high': [11., 11., 21., 21.],
                          'low': [9., 9., 19., 19.],
                          'close': [10., 10., 20., 20.]})
    result = tc.corwin_schultz(daily)
    assert result.iloc[[0, 2]].isna().all()
    # Identical consecutive ranges have alpha=log(high/low).
    assert result.iloc[1] == pytest.approx(2000.)
    assert result.iloc[3] == pytest.approx(1000.)


def test_corwin_schultz_month_end_does_not_borrow_next_month_prices():
    daily = _daily()
    cutoff = pd.Timestamp('2024-01-31')
    prefix = daily[daily.date <= cutoff].copy()
    full = tc.corwin_schultz(daily)
    pd.testing.assert_series_equal(full.loc[prefix.index], tc.corwin_schultz(prefix))
    before = tc.monthly_features(daily)
    changed = daily.copy()
    changed.loc[changed.date > cutoff, ['high', 'low', 'close']] *= 3
    after = tc.monthly_features(changed)
    pd.testing.assert_frame_equal(before[before.ym == '2024-01'].reset_index(drop=True),
                                  after[after.ym == '2024-01'].reset_index(drop=True))


def test_volatility_imputation_does_not_borrow_from_future_months():
    daily = _daily()
    jan = daily.date < '2024-02-01'
    # Retain only two first-month observations for one newly appearing stock.
    a_jan = daily[(daily.code == '000001') & jan].index
    daily = daily.drop(a_jan[2:])
    before = tc.monthly_features(daily)
    changed = daily.copy()
    changed.loc[changed.date >= '2024-02-01', 'ret'] *= 100
    after = tc.monthly_features(changed)
    pd.testing.assert_frame_equal(before[before.ym == '2024-01'].reset_index(drop=True),
                                  after[after.ym == '2024-01'].reset_index(drop=True))
    # If nobody has source-month volatility yet, it remains unavailable.
    early = tc.monthly_features(_daily(n_days=2))
    assert early.vol.isna().all()


def test_missing_columns_raise():
    with pytest.raises(ValueError, match="close_raw"):
        tc.monthly_features(_daily().drop(columns="close_raw"))


def test_rank_agreement_perfect_and_skips_small_months():
    df = pd.DataFrame({"ym": ["2024-01"] * 200 + ["2024-02"] * 50,
                       "code": [f"c{i}" for i in range(250)],
                       "spread_bp": np.arange(250.0)})
    df["est"] = df["spread_bp"]
    r = tc.rank_agreement(df, "est")
    assert r["months"] == 1 and r["rho"] == pytest.approx(1.0) and r["top_overlap"] == 1.0
