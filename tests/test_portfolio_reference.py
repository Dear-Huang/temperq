"""Hand-computed accounting paths with future execution restrictions."""

from dataclasses import replace

import pytest

from qwb.portfolio.contracts import AccountState, Position, SettlementLot
from qwb.portfolio.reference import ExecutionBar, Fees, execute_session
from test_portfolio_contracts import target

D0 = "2024-01-02T15:30:00+08:00"
O1 = "2024-01-03T09:30:00+08:00"
C1 = "2024-01-03T15:30:00+08:00"
O2 = "2024-01-04T09:30:00+08:00"
C2 = "2024-01-04T15:30:00+08:00"
O3 = "2024-01-05T09:30:00+08:00"


def bars(a=10, b=20, ac=11, bc=19):
    return (ExecutionBar("A", a, ac, 20, 5), ExecutionBar("B", b, bc, 40, 10))


def run(s, t, market=None, **kwargs):
    return execute_session(s, t, market or bars(), open_at=O1, close_at=C1, next_open_at=O2,
                           universe_id="known-assets", protocol_id="protocol-1", decision_assets=("A", "B"), **kwargs)


def test_exact_buy_fees_daily_pnl_and_t1_state():
    s = AccountState(D0, "fixture", 10000)
    t = target(s, weights=(("A", .5), ("B", .4)), cash_weight=.1)
    after, audit = run(s, t)
    assert after.cash == 990
    assert [p.quantity for p in after.positions] == [500, 200]
    assert [p.sellable_quantity for p in after.positions] == [0, 0]
    assert after.equity == 10290
    assert audit["total_cost"] == 10
    assert audit["overnight_pnl"] == 0
    assert audit["intraday_pnl"] == 300
    assert abs(audit["pnl_residual"]) < 1e-9
    assert abs(audit["self_financing_residual"]) < 1e-9
    assert audit["diagnostic_only"]
    sell = target(after, weights=(), cash_weight=1, execution_not_before=O2)
    final, audit2 = execute_session(after, sell, bars(a=12, b=18, ac=12, bc=18),
        open_at=O2, close_at=C2, next_open_at=O3, universe_id="known-assets", protocol_id="protocol-1",
        decision_assets=("A", "B"))
    assert not final.positions
    # 6000 + 3600 sold, minimum commissions 10, current sell stamp duty 4.8.
    assert final.cash == pytest.approx(10575.2)
    assert audit2["overnight_pnl"] == 300


def test_actual_drift_not_previous_target_drives_next_trade():
    s = AccountState(D0, "fixture", 10000, (Position("A", 100, 100, 10, D0),))
    after, audit = run(s, target(s, weights=(("A", .5), ("B", .4)), cash_weight=.1),
                       bars(a=12, b=20, ac=13, bc=18))
    assert audit["equity_open"] == 11200
    assert audit["overnight_pnl"] == 200
    assert audit["fills"][0]["quantity"] == 300
    assert after.equity == 11190


@pytest.mark.parametrize("change,reason", [({"suspended": True}, "SUSPENDED"),
    ({"open": 20}, "LIMIT_UP"), ({"upper_limit": None}, "MISSING_LIMITS"),
    ({"max_quantity": 0}, "CAPACITY")])
def test_execution_restrictions_do_not_change_prior_decision(change, reason):
    s = AccountState(D0, "fixture", 10000)
    t = target(s, weights=(("A", .8),), cash_weight=.2)
    before = t.to_dict()
    after, audit = run(s, t, (replace(bars()[0], **change), bars()[1]))
    assert t.to_dict() == before
    assert after.positions == ()
    assert audit["fills"][0]["reason"] == reason


def test_locked_or_missing_holdings_never_disappear():
    p = Position("A", 100, 0, 10, D0, (SettlementLot(100, O2),))
    s = AccountState(D0, "fixture", 1000, (p,))
    after, audit = run(s, target(s, weights=(), cash_weight=1))
    assert after.positions[0].quantity == 100
    assert audit["fills"][0]["reason"] == "T1_LOCKED"
    no_price = (replace(bars()[0], open=None, close=None), bars()[1])
    stale, audit = run(s, target(s, weights=(), cash_weight=1), no_price)
    assert stale.positions[0].quantity == 100 and stale.positions[0].mark == 10
    assert len(audit["notices"]) == 2
    with pytest.raises(ValueError, match="retain"):
        run(s, target(s), (bars()[1],))


def test_lots_and_minimum_fees_cannot_create_negative_cash():
    s = AccountState(D0, "fixture", 1000)
    after, audit = run(s, target(s, weights=(("A", 1.0),), cash_weight=0))
    assert after.positions == () and after.cash == 1000
    assert audit["fills"][0]["reason"] == "INSUFFICIENT_CASH"
    after, audit = run(replace(s, cash=1005), target(replace(s, cash=1005), weights=(("A", 1.0),), cash_weight=0))
    assert after.positions[0].quantity == 100 and after.cash == 0


def test_no_new_target_keeps_existing_position_and_marks_market_movement():
    s = AccountState(D0, "fixture", 1000, (Position("A", 100, 100, 10, D0),))
    after, audit = run(s, None, bars(a=12, ac=13))
    assert after.equity == 2300 and not audit["fills"]
    assert audit["overnight_pnl"] == 200 and audit["intraday_pnl"] == 100


def test_explicit_fee_parameters_are_not_silently_coerced():
    with pytest.raises(ValueError):
        Fees(commission_bp=True)


def test_limit_down_and_sell_capacity_preserve_unsold_units():
    s = AccountState(D0, "fixture", 1000, (Position("A", 100, 100, 10, D0),))
    after, audit = run(s, target(s, weights=(), cash_weight=1),
                       (replace(bars()[0], open=5, close=5), bars()[1]))
    assert after.positions[0].quantity == 100
    assert audit["fills"][0]["reason"] == "LIMIT_DOWN"
    partial, audit = run(s, target(s, weights=(), cash_weight=1),
                         (replace(bars()[0], max_quantity=25), bars()[1]))
    assert partial.positions[0].quantity == 75
    assert audit["fills"][0]["quantity"] == 25
    assert audit["fills"][0]["unfilled_quantity"] == 75
    assert audit["fills"][0]["lifetime"] == "OPEN_IOC"


def test_existing_pending_order_is_rejected_instead_of_disappearing():
    from qwb.portfolio.contracts import PendingOrder

    s = AccountState(D0, "fixture", 10000, pending_orders=(PendingOrder("old", "A", "BUY", 100, D0, O1),))
    with pytest.raises(ValueError, match="pending"):
        run(s, target(s))


def test_blocked_sale_discloses_actual_constraint_breach():
    s = AccountState(D0, "fixture", 100, (Position("A", 100, 100, 10, D0),))
    intended = target(s, weights=(("A", .2),), cash_weight=.8)
    after, audit = run(s, intended, (replace(bars()[0], suspended=True), bars()[1]),
                       max_weight=.3, min_cash=.2)
    assert after.positions[0].quantity == 100
    assert {v["constraint"] for v in audit["constraint_violations"]} == {"position_cap", "minimum_cash"}
