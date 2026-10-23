"""Account comparison must distinguish equal totals with different future rights."""

import copy
from dataclasses import replace

from qwb.bench import targets
from qwb.portfolio.comparison import (AGGREGATE, LIFECYCLE, BASE_LIMITS, STATE_LIMITS,
                                      lifecycle_difference, validate_acceptance)

import pytest

torch = pytest.importorskip("torch")
from qwb.portfolio.continuous import ContinuousConfig, TensorRight, tensor
from qwb.portfolio.decision_evaluation import compare
from qwb.portfolio.episode import ContinuousIntent, run_episode

from test_continuous_account import fixed_episode
from test_portfolio_lifecycle import dividend, failure, initial, policy, protocol as lifecycle_protocol


def limits(*, detailed=True):
    value = {key: 1e-8 for key in BASE_LIMITS}
    value.update(max_constraint_violation_count=0, zero_cost_relative_rule="null_use_absolute")
    if detailed:
        value.update(state_comparison_version=LIFECYCLE, **{key: 1e-8 for key in STATE_LIMITS})
        value.update(max_daily_entitlement_mismatch_count=0, max_daily_processed_event_mismatch_count=0)
    return value


def inputs(tmp_path, event=None):
    protocol = lifecycle_protocol(tmp_path, (event or dividend(),))
    for session in protocol["sessions"][1:]:
        session["bars"][0].update(open=9., close=9.)
    exact = targets.replay(protocol, policy)
    actions = {row["decision_id"]: {pair["asset_id"]: pair["target_weight"] for pair in row["weights"]}
               for row in exact["decisions"]}
    episode = fixed_episode(protocol, actions)
    return protocol, exact, actions, episode


def comparison(episode, exact, acceptance):
    policies = [{"decision_id": audit["decision_id"], "assets": state.assets,
                 "production": {"produced_weights": intent.weights.tolist()}}
                for audit, (state, _), intent in zip(episode["audits"], episode["states"], episode["intents"], strict=True)]
    return compare(episode, exact, acceptance, continuous_policy=policies, exact_actions=exact["decisions"])


def test_versioned_limits_preserve_aggregate_semantics_and_require_explicit_new_gates():
    old = limits(detailed=False)
    assert validate_acceptance(old) == old and "state_comparison_version" not in old
    old["state_comparison_version"] = AGGREGATE
    assert validate_acceptance(old) == old
    assert validate_acceptance(limits()) == limits()
    for missing in STATE_LIMITS:
        incomplete = limits()
        incomplete.pop(missing)
        with pytest.raises(ValueError, match="every versioned"):
            validate_acceptance(incomplete)
    for field, value in (("state_comparison_version", "unknown"),
                         ("max_daily_entitlement_mismatch_count", .5),
                         ("max_daily_processed_event_mismatch_count", True),
                         ("max_daily_entitlement_amount_gap_cny", float("nan")),
                         ("max_daily_entitlement_quantity_gap", -1)):
        malformed = limits()
        malformed[field] = value
        with pytest.raises(ValueError):
            validate_acceptance(malformed)
    with pytest.raises(ValueError, match="every versioned"):
        validate_acceptance({**limits(detailed=False), "max_daily_entitlement_quantity_gap": 0})


def test_actual_corporate_account_matches_rights_before_and_after_each_session(tmp_path):
    _, exact, _, episode = inputs(tmp_path)
    result = comparison(episode, exact, limits())
    assert result["comparison_eligible"] and result["state_comparison_version"] == LIFECYCLE
    assert any(state.rights for _, state in episode["states"])
    assert all(row["entitlement_mismatch_count"] == row["processed_event_mismatch_count"] == 0
               for row in result["daily"])
    legacy = comparison(episode, exact, limits(detailed=False))
    assert legacy["comparison_eligible"] and "lifecycle_states" not in legacy["daily"][0]


@pytest.mark.parametrize("mutation", ["payable_at", "event_id", "asset_id", "kind", "quantity", "processed_events", "before"])
def test_equal_receivable_total_does_not_hide_wrong_ownership_schedule_or_history(tmp_path, mutation):
    _, exact, _, episode = inputs(tmp_path)
    index = next(i for i, (_, state) in enumerate(episode["states"])
                 if state.rights and state.rights[0].recognized)
    before, state = episode["states"][index]
    right = state.rights[0]
    if mutation == "processed_events":
        changed = replace(state, processed_events=(*state.processed_events, "unexpected:record"))
    else:
        changes = {"payable_at": "2024-01-08T09:00:00+08:00", "event_id": "wrong-event",
                   "asset_id": "B", "kind": "terminal", "quantity": right.quantity + 1}
        changed = replace(state, rights=(replace(right, **{mutation: changes[mutation]}),)) if mutation != "before" else state
    if mutation == "before":
        before = replace(before, processed_events=(*before.processed_events, "unexpected:record"))
    episode["states"][index] = (before, changed)
    assert comparison(episode, exact, limits(detailed=False))["comparison_eligible"]
    detailed = comparison(episode, exact, limits())
    assert not detailed["comparison_eligible"]
    assert detailed["max_absolute_gaps"]["receivable_gap_cny"] < 1e-8
    if mutation in {"processed_events", "before"}:
        assert "processed_event_mismatch_count" in detailed["threshold_breaches"]
    elif mutation == "quantity":
        assert "entitlement_quantity_gap" in detailed["threshold_breaches"]
    else:
        assert "entitlement_mismatch_count" in detailed["threshold_breaches"]


def test_equal_total_claim_amounts_cannot_cancel_across_event_ids():
    from qwb.portfolio.continuous import ContinuousState

    state = ContinuousState.from_account(initial(positions=()), ("A",))
    payable = "2024-01-05T09:00:00+08:00"
    state = replace(state, rights=tuple(TensorRight(event, "A", "dividend", tensor(100.), tensor(amount), payable, True)
                                       for event, amount in (("first", 40.), ("second", 60.))))
    exact = {"entitlements": [{"event_id": event, "asset_id": "A", "kind": "dividend", "quantity": 100,
                               "amount": 50., "payable_at": payable, "recognized": True} for event in ("first", "second")]}
    assert float(state.receivable) == 100
    result = lifecycle_difference(state, exact)
    assert result["entitlement_amount_gap_cny"] == 10 and result["entitlement_mismatch_count"] == 0
    exact["entitlements"][0]["payable_at"] = "2024-01-05T01:00:00+00:00"
    assert lifecycle_difference(state, exact) == result


def test_positive_quantity_zero_amount_claim_is_not_removed_or_hidden(tmp_path):
    event = dividend(withholding_per_share=1., tax_treatment="declared_withholding")
    _, exact, _, episode = inputs(tmp_path, event)
    assert comparison(episode, exact, limits())["comparison_eligible"]
    index = next(i for i, (_, state) in enumerate(episode["states"]) if state.rights)
    before, state = episode["states"][index]
    assert float(state.rights[0].quantity) > 0 and float(state.rights[0].amount) == 0
    changed = replace(state, rights=())
    episode["states"][index] = (before, changed)
    assert comparison(episode, exact, limits(detailed=False))["comparison_eligible"]
    assert not comparison(episode, exact, limits())["comparison_eligible"]
    episode["states"][index] = (before, replace(state, rights=(replace(state.rights[0], recognized=True),)))
    assert "entitlement_mismatch_count" in comparison(episode, exact, limits())["threshold_breaches"]


@pytest.mark.parametrize("kind", ["dividend", "terminal"])
def test_unowned_asset_does_not_generate_zero_quantity_rights(tmp_path, kind):
    from qwb.portfolio.lifecycle import Terminal
    from test_portfolio_lifecycle import D0, P2, P3

    event = dividend() if kind == "dividend" else Terminal("terminal", "A", D0, P2, P3, 0., "declared proceeds")
    protocol = lifecycle_protocol(tmp_path, (event,))
    protocol["initial_state"] = initial(positions=()).to_dict()
    for session in protocol["sessions"]:
        if kind == "terminal" and session["open_at"] > P2:
            session["eligible_assets"] = ["B"]
            session["bars"][0].update(open=None, close=None)
            session["bars"].append({**session["bars"][0], "asset_id": "B", "open": 10., "close": 10.})
    exact = targets.replay(protocol, failure)
    episode = run_episode(protocol, lambda context: ContinuousIntent(None, hold_reason="hold shares"))
    for (before, after), states in zip(episode["states"], exact["states"], strict=True):
        assert not before.rights and not after.rights
        assert lifecycle_difference(before, states["before"])["processed_event_mismatch_count"] == 0
        assert lifecycle_difference(after, states["after"])["entitlement_mismatch_count"] == 0
        assert float(after.equity) == states["after"]["cash"]


def test_zero_exact_fee_denominator_keeps_absolute_cost_gate_active(tmp_path):
    protocol, exact, actions, _ = inputs(tmp_path)
    paid_protocol = copy.deepcopy(protocol)
    paid_protocol["fees"]["minimum_commission"] = 2.
    episode = fixed_episode(paid_protocol, actions, config=ContinuousConfig(integer_lots=False))
    acceptance = limits()
    for key in BASE_LIMITS | STATE_LIMITS:
        acceptance[key] = 1000000 if key.endswith("_count") else 1e9
    acceptance["max_total_cost_gap_cny"] = 0
    acceptance["max_total_cost_relative_gap"] = 0
    assert exact["total_cost"] == 0
    result = comparison(episode, exact, acceptance)
    assert result["continuous_total_cost"] > 0 and result["total_cost_relative_gap"] is None
    assert all(row["cost_relative_gap"] is None for row in result["daily"])
    assert result["threshold_breaches"] == ["total_cost_gap_cny"]
    acceptance["max_total_cost_gap_cny"] = result["continuous_total_cost"]
    assert comparison(episode, exact, acceptance)["comparison_eligible"]
