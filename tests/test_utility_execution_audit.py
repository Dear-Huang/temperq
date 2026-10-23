"""Execution attribution uses actual fills, preserves failures and checks identities."""

import copy
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")

from qwb import experiments
from qwb.bench import targets
from qwb.portfolio.continuous import ContinuousConfig
from qwb.portfolio.episode import run_episode
from qwb.portfolio.contracts import AccountState, Position, SettlementLot, TargetWeights

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_utility_execution_audit import (BRIDGE_ORDER, bridge, diagnose, fill_differences, first_divergence,
    fixed_policy, paired_effects, scenarios, summary, compare_recomputed)
from test_utility_comparison import limits


def fixture(tmp_path, pressure=None):
    root = targets.create_fixture(tmp_path / "fixture")
    protocol = targets.read(root / "protocol.json")
    if pressure == "settlement":
        state = AccountState.from_dict(protocol["initial_state"])
        locked = SettlementLot(300, protocol["sessions"][1]["open_at"])
        state = AccountState(state.observed_at, state.data_snapshot_id, 7000,
                             (Position("A", 300, 0, 10, state.observed_at, (locked,)),))
        protocol["initial_state"] = state.to_dict()
    elif pressure == "suspension":
        protocol["sessions"][0]["bars"][0]["suspended"] = True
    elif pressure == "funding":
        protocol["fees"]["minimum_commission"] = 2500
    policies = []
    for session in protocol["sessions"]:
        assets = session["eligible_assets"]
        policies.append({"decision_id": session["decision_id"], "assets": assets,
            "production": {"produced_weights": [0.01 if pressure == "settlement" else .9 if pressure == "funding" else .312345] * len(assets)},
            "production_inputs": {"previous_weights": [0.] * len(assets)},
            "recipe": {"actual_equity": 100000., "buy_cost": [.001] * len(assets),
                       "sell_cost": [.002] * len(assets), "fees": protocol["fees"], "cost": {"sell_tax_bp": 5.}}})
    by_id = {row["decision_id"]: row for row in policies}

    def exact_policy(context):
        row = by_id[context.decision_id]
        weights = dict(zip(row["assets"], row["production"]["produced_weights"]))
        values = tuple((code, weights.get(code, 0)) for code in context.assets)
        return TargetWeights(context.decision_id, context.state.observed_at, context.execution_not_before,
            "cn_alloc_daily_v1", "frozen-test", context.state.data_snapshot_id, context.state.state_id,
            context.universe_id, context.protocol_id, values, 1 - sum(w for _, w in values))

    exact = targets.replay(protocol, exact_policy)
    episode = run_episode(protocol, fixed_policy(policies))
    return protocol, policies, exact, episode


def test_full_mechanism_audit_reconciles_fills_costs_and_2x2_interaction(tmp_path):
    protocol, policies, exact, episode = fixture(tmp_path)
    result = diagnose(protocol, episode, policies, exact, exact, limits(), tmp_path / "audit")
    assert result["status"] == "COMPLETED"
    assert set(result["scenarios"]) == set(scenarios(ContinuousConfig())) | set(BRIDGE_ORDER[-2:])
    assert all(abs(value) < 1e-8 for value in result["bridge"]["residuals"].values())
    exact_mode = result["scenarios"]["integer_minimum"]
    assert exact_mode["comparison_eligible"]
    assert exact_mode["final_equity"] == pytest.approx(exact["final_equity"], abs=1e-8)
    assert result["paired_effects"]["status"] == "COMPLETED"
    fractional = experiments.read(tmp_path / "audit/fractional_minimum.json")
    assert fractional["first_divergence"] is not None
    assert fractional["fills"]
    for row in fractional["same_fill_costs"]["daily"]:
        assert abs(row["bridge_residual"]) < 1e-8
        assert row["actual_to_intended_gap"] == pytest.approx(row["intent_to_fill_gap"] + row["actual_to_filled_gap"])
        bp = row["bp_of_initial_equity"]
        assert bp["actual_to_intended_gap"] == pytest.approx(bp["intent_to_fill_gap"] + bp["actual_to_filled_gap"])
    assert fractional["same_fill_costs"]["schema_version"] == "same_fill_cost_decomposition_v2"
    for fill in fractional["same_fill_costs"]["fills"]:
        assert fill["bp_of_initial_equity"]["actual_cost"] == pytest.approx(
            fill["cny"]["actual_cost"] / fractional["same_fill_costs"]["initial_equity_cny"] * 10000)
    held = experiments.read(tmp_path / "audit/hold_shares_control.json")
    assert all(not row["fills"] for row in held["episode"]["audits"])


def test_failed_counterfactual_is_retained_and_bridge_is_incomplete(tmp_path, monkeypatch):
    import run_utility_execution_audit as audit
    protocol, policies, exact, episode = fixture(tmp_path)
    original = audit.run_episode

    def fail_integer(*args, **kwargs):
        if kwargs["config"].integer_lots and not kwargs["config"].minimum_commission:
            raise ValueError("intentional counterfactual failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "run_episode", fail_integer)
    result = diagnose(protocol, episode, policies, exact, exact, limits(), tmp_path / "audit")
    assert result["status"] == "DIAGNOSTIC_WITH_FAILURES"
    assert result["bridge"]["status"] == result["paired_effects"]["status"] == "INCOMPLETE"
    assert "integer_proportional" in result["bridge"]["missing_scenarios"]
    attempts = experiments.read(tmp_path / "audit/attempts.json")
    assert attempts["integer_proportional"]["state"] == "failed"
    assert "intentional" in experiments.read(tmp_path / "audit/integer_proportional.json")["error"]
    assert all(row["state"] in {"succeeded", "failed"} for row in attempts.values())


def test_bridge_detects_coverage_mismatch_and_reports_known_interaction():
    results = {name: summary([{"decision_id": "d", "equity_before": 100, "equity_close": 100 + i,
                              "total_cost": cost, "overnight_pnl": 0, "intraday_pnl": i + cost}])
               for i, (name, cost) in enumerate(zip(BRIDGE_ORDER, (20, 2, 1, 5, 5, 7)))}
    effect = paired_effects(results)["effects"]["total_cost"]
    assert effect == {"minimum_fee_effect_fractional": 18, "minimum_fee_effect_integer": 4,
                      "integer_x_minimum_interaction": -14}
    assert bridge(results)["endpoint_deltas"]["cost_delta_cny"] == -13
    results[BRIDGE_ORDER[2]]["daily"][0]["decision_id"] = "wrong"
    with pytest.raises(ValueError, match="coverage"):
        bridge(results)
    with pytest.raises(ValueError, match="duplicate"):
        summary([{"decision_id": "d"}, {"decision_id": "d"}])
    with pytest.raises(ValueError, match="PnL"):
        summary([{"decision_id": "d", "equity_before": 100, "equity_close": 101,
                  "total_cost": 5, "overnight_pnl": 0, "intraday_pnl": 0}])


def test_fill_alignment_retains_missing_orders_side_and_metadata_without_cancellation():
    fills = [{"asset_id": "A", "side": side, "quantity": 1, "notional": 10, "reason": "FILLED"}
             for side in ("BUY", "SELL")]
    approximate = [{"decision_id": "d", "fills": fills}]
    exact = [{"decision_id": "d", "fills": []}]
    rows = fill_differences(approximate, exact)
    assert len(rows) == 2 and all(row["record_mismatch"] for row in rows)
    zero = {key: 0 for key in ("equity_gap_cny", "cash_gap_cny", "receivable_gap_cny", "cost_gap_cny",
                              "quantity_gap", "locked_quantity_gap", "unfilled_quantity_gap")}
    first = first_divergence({"daily": [{"decision_id": "d", **zero}]}, rows, money=1e-7, quantity=1e-8)
    assert first["decision_id"] == "d" and len(first["fills"]) == 2
    with pytest.raises(ValueError, match="duplicate"):
        fill_differences([{"decision_id": "d", "fills": [fills[0], fills[0]]}], exact)
    with pytest.raises(ValueError, match="aligned"):
        fill_differences(approximate, [{"decision_id": "wrong", "fills": []}])


def test_unsupported_execution_modes_and_uncovered_frozen_actions_are_rejected(tmp_path):
    for key, value in (("integer_lots", True), ("minimum_commission", False), ("market_restrictions", False)):
        config = {**asdict(ContinuousConfig()), key: value}
        with pytest.raises(ValueError, match="requires"):
            scenarios(ContinuousConfig(**config))
    protocol, policies, exact, episode = fixture(tmp_path)
    with pytest.raises(ValueError, match="coverage"):
        diagnose(protocol, episode, policies[:-1], exact, exact, limits(), tmp_path / "audit")
    changed = copy.deepcopy(policies)
    changed[0]["production"]["produced_weights"][0] = 0.1
    result = diagnose(protocol, episode, changed, exact, exact, limits(), tmp_path / "changed")
    assert "fractional_minimum" in result["failures"]
    assert result["bridge"]["status"] == "INCOMPLETE"


@pytest.mark.parametrize("pressure,scenario", [("settlement", "integer_without_settlement"),
    ("suspension", "integer_without_market_restrictions"), ("funding", "integer_proportional")])
def test_pressure_scenarios_change_actual_fills_and_keep_independent_integer_agreement(tmp_path, pressure, scenario):
    protocol, policies, exact, episode = fixture(tmp_path, pressure)
    result = diagnose(protocol, episode, policies, exact, exact, limits(), tmp_path / "audit")
    assert result["status"] == "COMPLETED"
    baseline = experiments.read(tmp_path / "audit/integer_minimum.json")
    counterfactual = experiments.read(tmp_path / f"audit/{scenario}.json")
    actual = baseline["episode"]["audits"][0]["fills"]
    changed = counterfactual["episode"]["audits"][0]["fills"]
    assert actual != changed
    assert baseline["summary"]["final_equity"] == pytest.approx(exact["final_equity"], abs=1e-8)
    assert all(abs(row["pnl_residual"]) < 1e-7 for row in counterfactual["summary"]["daily"])


def test_rehashed_diagnostic_tampering_is_rejected_by_arithmetic_comparison(tmp_path):
    original, fresh = tmp_path / "original", tmp_path / "fresh"
    for directory in (original, fresh):
        directory.mkdir()
        experiments.write(directory / "plan.json", {"created_at": str(directory), "fixed": True})
        experiments.write(directory / "result.json", {"cost_delta": 5})
        experiments.write(directory / "checksums.json", experiments.inventory(directory))
    compare_recomputed(original, fresh)
    experiments.write(original / "result.json", {"cost_delta": 0})
    inventory = experiments.inventory(original)
    inventory.pop("checksums.json")
    experiments.write(original / "checksums.json", inventory)
    with pytest.raises(ValueError, match="arithmetic differs"):
        compare_recomputed(original, fresh)
