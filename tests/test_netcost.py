import numpy as np
import pandas as pd
import pytest

from qwb import netcost, rules

CARD = {"label": {"start": {"day": 1, "price": "open"}, "end": {"day": 2, "price": "open"}}}


def panel(n_days=8, n_codes=10, up_hit=None):
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    codes = [f"{i:06d}.SZ" for i in range(n_codes)]
    one = pd.DataFrame(10.0, index=dates, columns=codes)
    P = {k: one.copy() for k in ("open", "high", "low", "close")}
    P["amount"] = one * 1e8
    P["adj_factor"] = one * 0 + 1
    P["up_limit"] = one * 1.1
    P["down_limit"] = one * 0.9
    if up_hit is not None:                       # (row, col): open at the up limit
        P["open"].iloc[up_hit] = P["up_limit"].iloc[up_hit]
    return P


def flat_spread(P, bp):
    return pd.DataFrame(bp, index=P["close"].index, columns=P["close"].columns)


def test_cost_turnover_and_stamp_on_hand_case():
    P = panel()
    T, N = P["close"].shape
    U = np.ones((T, N), bool)
    y = np.zeros((T, N))
    sig = np.tile(np.arange(N, dtype=float), (T, 1))
    sig[1::2] = sig[1::2][:, ::-1]               # top name alternates every rebalance
    out = netcost.evaluate(P, CARD, {"s": sig}, U, y, [2024], top=0.1, commission_bp=3,
                           half_spread=flat_spread(P, 10.0))
    v = out["signals"]["s"]
    assert out["hold_days"] == 1 and out["rebalances"] == T - 2
    stamp = rules.stamp_tax_sell_bp("2024-01-03")
    # first period: buy 1.0 -> 13bp; later: sell 1.0 + buy 1.0 -> 26bp + stamp
    per = np.array([13.0] + [26.0 + stamp] * (out["rebalances"] - 1)) / 1e4
    assert v["cost_ann"] == pytest.approx(per.mean() * 252)
    assert v["gross_excess_ann"] == pytest.approx(0.0)
    assert v["net_excess_ann"] == pytest.approx(-per.mean() * 252)


def test_limit_up_name_is_not_bought():
    P = panel(up_hit=(1, 9))                     # day-1 open of the top name at the up limit
    T, N = P["close"].shape
    U = np.ones((T, N), bool)
    y = np.zeros((T, N))
    y[0, 9] = 0.10                               # it would have paid 10%
    sig = np.tile(np.arange(N, dtype=float), (T, 1))
    out = netcost.evaluate(P, CARD, {"s": sig}, U, y, [2024], top=0.1, commission_bp=0,
                           half_spread=flat_spread(P, 0.0))
    assert out["signals"]["s"]["gross_ann"] == pytest.approx(0.0)


def test_stamp_duty_dates():
    assert rules.stamp_tax_sell_bp("2023-08-25") == 10
    assert rules.stamp_tax_sell_bp("2023-08-28") == 5
    with pytest.raises(ValueError):
        rules.stamp_tax_sell_bp("2001-01-01")


def test_struct_half_spread_uses_previous_month():
    pytest.importorskip("ashare_tcost")
    P = panel(n_days=50)
    P["close"].iloc[30:] = 2.0                   # price drops in February
    hs = netcost.struct_half_spread_bp(P)
    jan, feb = hs.index.month == 1, hs.index.month == 2
    assert hs[jan].isna().all().all()            # no prior month yet
    # flat January price 10 -> zero volatility, so the tick floor binds: 100/10 = 10bp,
    # half-spread 5bp. February's own price (2) would give 25bp.
    assert hs[feb].iloc[0, 0] == pytest.approx(5.0)
