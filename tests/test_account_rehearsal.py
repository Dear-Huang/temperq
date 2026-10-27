"""Account-specific labels must retain costs, restrictions and decision identity."""

from dataclasses import replace

import pytest

from qwb.account_rehearsal import Action, RehearsalPlan, replay_actions
from qwb.portfolio.contracts import AccountState, Position, SettlementLot, TargetWeights, TASK
from qwb.portfolio.reference import ExecutionBar, Fees

D0 = "2024-01-02T15:30:00+08:00"
O1 = "2024-01-03T09:30:00+08:00"
C1 = "2024-01-03T15:30:00+08:00"
O2 = "2024-01-04T09:30:00+08:00"


def plan(equity=10000, locked=False):
    qty = int(equity / 20)
    state = AccountState(D0, "fixture", equity / 2,
                         (Position("A", qty, 0 if locked else qty, 10, D0,
                                   (SettlementLot(qty, O2),) if locked else ()),))
    target = TargetWeights("switch", D0, O1, TASK, "signal-v1", "fixture", state.state_id,
                           "A-B", "open-ioc", (("B", .5),), .5)
    return RehearsalPlan(state, (Action("hold", None), Action("switch", target)),
                         "hold", ("A", "B"), "A-B", "open-ioc", O1, C1, O2)


def market():
    return (ExecutionBar("A", 10, 10, 11, 9), ExecutionBar("B", 10, 10.02, 11, 9))


def test_same_signal_has_opposite_net_action_value_at_different_account_sizes():
    small = replay_actions(plan(), market())["rows"][1]
    large = replay_actions(plan(1000000), market())["rows"][1]
    # 500 A sold: 5 commission + 2.5 tax; 500 B bought: 5 commission.
    # Initial cash covers the buy fees: 500 * .02 gain minus 12.5 = -2.5.
    assert small["total_cost"] == 12.5
    assert small["simulated_delta_net_pnl"] == pytest.approx(-2.5)
    # Same signal, target and returns; minimum fees and integer lots differ.
    assert large["total_cost"] == pytest.approx(550)
    assert large["simulated_delta_net_pnl"] == pytest.approx(450)
    assert small["training_label_bp"] < 0 < large["training_label_bp"]


def test_existing_holdings_change_value_of_identical_target():
    original = plan()
    state = replace(original.state, positions=(Position("B", 500, 500, 10, D0),))
    action = replace(original.actions[1], target=replace(original.actions[1].target, state_id=state.state_id))
    already_held = replace(original, state=state, actions=(original.actions[0], action))
    report = replay_actions(already_held, market())
    assert report["rows"][1]["simulated_delta_net_pnl"] == 0
    assert report["rows"][1]["audit"]["fills"] == []
    assert replay_actions(original, market())["rows"][1]["simulated_delta_net_pnl"] < 0


def test_t1_locked_sale_is_retained_and_reported():
    result = replay_actions(plan(locked=True), market())["rows"][1]
    positions = {p["asset_id"]: p for p in result["state_after"]["positions"]}
    assert positions["A"]["quantity"] == 500
    assert result["audit"]["fills"][0]["reason"] == "T1_LOCKED"
    assert result["audit"]["fills"][0]["quantity"] == 0
    assert result["stock_weight_close"] > .5  # Blocked sells may alter exposure.


def test_future_execution_restriction_does_not_mutate_decision_plan():
    frozen = plan()
    before, key = frozen.to_dict(), frozen.plan_id
    report = replay_actions(frozen, (market()[0], replace(market()[1], suspended=True)))
    assert frozen.to_dict() == before and frozen.plan_id == key
    assert report["plan_id"] == key
    assert report["rows"][1]["audit"]["fills"][1]["reason"] == "SUSPENDED"
    assert "best_action" not in report


@pytest.mark.parametrize("change", [{"open": None}, {"close": None}, {"upper_limit": None}])
def test_missing_candidate_market_data_is_not_a_trainable_label(change):
    result = replay_actions(plan(), (market()[0], replace(market()[1], **change)))["rows"][1]
    assert not result["paired_label_eligible"]
    assert result["training_label_bp"] is None
    assert result["missing_assets"] == ["B"]


def test_missing_baseline_mark_invalidates_all_paired_labels():
    result = replay_actions(plan(), (replace(market()[0], close=None), market()[1]))
    assert all(row["training_label_bp"] is None for row in result["rows"])
    assert result["rows"][1]["baseline_issues"] == ["incomplete_market_data"]


def test_constraint_breach_cannot_be_sold_as_eligible_improvement():
    frozen = replace(plan(locked=True), min_cash=.5)
    result = replay_actions(frozen, market())["rows"][1]
    assert "actual_constraint_violation" in result["issues"]
    assert result["training_label_bp"] is None


def test_declared_baseline_can_be_a_mandatory_adjustment_instead_of_hold():
    frozen = replace(plan(), baseline_id="switch")
    rows = replay_actions(frozen, market())["rows"]
    assert rows[1]["simulated_delta_net_pnl"] == 0
    assert rows[0]["simulated_delta_net_pnl"] == pytest.approx(2.5)


def test_plan_rejects_stale_targets_missing_hold_and_mutable_action_list():
    frozen = plan()
    with pytest.raises(ValueError, match="mismatched"):
        replace(frozen, state=replace(frozen.state, cash=6000))
    with pytest.raises(ValueError, match="explicit hold"):
        replace(frozen, actions=(frozen.actions[1],), baseline_id="switch")
    with pytest.raises(ValueError, match="immutable"):
        replace(frozen, actions=list(frozen.actions))
    with pytest.raises(ValueError, match="declared action"):
        replace(frozen, baseline_id="undeclared")


def test_missing_holdings_are_rejected_before_replay():
    with pytest.raises(ValueError, match="retain existing"):
        replace(plan(), assets=("B",))


def test_candidate_order_does_not_chain_states_and_plan_covers_fees():
    frozen = plan()
    normal = replay_actions(frozen, market())
    reverse = replay_actions(replace(frozen, actions=tuple(reversed(frozen.actions))), market())
    assert {r["action_id"]: r["net_pnl"] for r in normal["rows"]} == {
        r["action_id"]: r["net_pnl"] for r in reverse["rows"]}
    assert all(r["audit"]["state_before"] == frozen.state.state_id for r in normal["rows"])
    assert replace(frozen, fees=Fees(minimum_commission=6)).plan_id != frozen.plan_id
