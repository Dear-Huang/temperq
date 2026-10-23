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
