"""Explicit target intent, actual raw-share paths and independent replay checks."""

import copy
from dataclasses import asdict
from pathlib import Path

import pytest

from qwb import cli, experiments
from qwb.bench import targets
from qwb.portfolio.contracts import TargetWeights


def fixture(tmp_path):
    root = targets.create_fixture(tmp_path / "fixture")
    return root, targets.read(root / "protocol.json"), targets.read(root / "submission.json")


def test_synthetic_targets_exact_cash_t1_drift_and_cli_replay(tmp_path, capsys):
    root, protocol, submission = fixture(tmp_path)
    result = targets.replay_submission(protocol, submission)
    from qwb.portfolio.contracts import AccountState

    assert result["decisions"][0]["state_id"] == AccountState.from_dict(protocol["initial_state"]).state_id
    first = result["audits"][0]
    assert first["equity_close"] == 10895
    assert result["states"][0]["after"]["cash"] == 995
    assert result["states"][0]["after"]["positions"][0]["quantity"] == 900
    assert result["states"][0]["after"]["positions"][0]["sellable_quantity"] == 0
    assert first["self_financing_residual"] == 0
    assert result["audits"][1]["fills"][0]["quantity"] == 0  # no nine-share rebalance under sell_lot=100
    assert result["audits"][1]["fills"][0]["reason"] == "LOT_ROUNDED"
    assert result["unfilled_quantity"] > 0
    output = tmp_path / "result"
    assert cli.main(["bench", "targets", str(root / "submission.json"), "--protocol", str(root / "protocol.json"),
                     "--output", str(output)]) == 0
    assert cli.main(["bench", "target-verify", str(output), "--inputs"]) == 0
    assert "SYNTHETIC_ONLY" in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        targets.evaluate(root / "protocol.json", root / "submission.json", output)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "state", "decision", "score", "nonfinite"])
def test_submission_rejects_missing_duplicate_stale_and_mixed_outputs(tmp_path, mutation):
    _, protocol, submission = fixture(tmp_path)
    row = submission["decisions"][0]
    if mutation == "missing":
        submission["decisions"].pop()
    elif mutation == "duplicate":
        submission["decisions"][1] = copy.deepcopy(row)
    elif mutation == "state":
        row["state_id"] = "unrelated-state"
    elif mutation == "decision":
        row["decision_id"] = "unknown"
    elif mutation == "score":
        row["prediction"] = .9
    else:
        row["weights"][0]["target_weight"] = float("nan")
    with pytest.raises(ValueError):
        targets.replay_submission(protocol, submission)


def test_explicit_failure_keeps_actual_held_assets_and_revalues_them(tmp_path):
    _, protocol, _ = fixture(tmp_path)
    def policy(ctx):
        if ctx.decision_id == "d1":
            return {"schema_version": "decision_failure_v1", "decision_id": ctx.decision_id,
                    "decision_time": ctx.state.observed_at, "state_id": ctx.state.state_id,
                    "task": protocol["task"], "model_version": "fixture", "reason": "declared solver failure",
                    "data_snapshot_id": ctx.state.data_snapshot_id, "universe_id": ctx.universe_id,
                    "protocol_id": ctx.protocol_id}
        return TargetWeights(ctx.decision_id, ctx.state.observed_at, ctx.execution_not_before,
                             protocol["task"], "fixture", ctx.state.data_snapshot_id, ctx.state.state_id,
                             ctx.universe_id, ctx.protocol_id, (("A", .9),), .1)
    result = targets.replay(protocol, policy)
    assert result["explicit_failures"] == 1
    before, after = result["states"][1]["before"], result["states"][1]["after"]
    assert before["positions"][0]["quantity"] == after["positions"][0]["quantity"] == 900
    assert after["positions"][0]["mark"] == 10.5
    assert not result["audits"][1]["fills"]


def test_execution_revelation_changes_fills_without_changing_first_policy_action(tmp_path):
    _, protocol, _ = fixture(tmp_path)
    def policy(ctx):
        return TargetWeights(ctx.decision_id, ctx.state.observed_at, ctx.execution_not_before,
                             protocol["task"], "fixture", ctx.state.data_snapshot_id, ctx.state.state_id,
                             ctx.universe_id, ctx.protocol_id, (("A", .9),), .1)
    before = targets.replay(protocol, policy)
    protocol["sessions"][0]["bars"][0]["suspended"] = True
    after = targets.replay(protocol, policy)
    assert before["decisions"][0]["weights"] == after["decisions"][0]["weights"]
    assert before["decisions"][0]["state_id"] == after["decisions"][0]["state_id"]
    assert before["states"][0]["after"]["positions"]
    assert not after["states"][0]["after"]["positions"]


def test_out_of_pool_holdings_remain_and_cannot_receive_new_buys(tmp_path):
    _, protocol, _ = fixture(tmp_path)
    from qwb.portfolio.reference import ExecutionBar

    protocol["sessions"][1]["eligible_assets"] = ["B"]
    b = asdict(ExecutionBar("B", 10, 10, 20, 1, sell_lot=100))
    b.pop("buy_allowed")
    protocol["sessions"][1]["bars"].append(b)
    protocol["sessions"][1]["bars"][0].update(open=5, close=5)
    def policy(ctx):
        return TargetWeights(ctx.decision_id, ctx.state.observed_at, ctx.execution_not_before,
                             protocol["task"], "fixture", ctx.state.data_snapshot_id, ctx.state.state_id,
                             ctx.universe_id, ctx.protocol_id, (("A", .9),), .1)
    result = targets.replay(protocol, policy)
    trade = result["audits"][1]["fills"][0]
    assert trade["reason"] == "NOT_IN_DECISION_UNIVERSE" and trade["quantity"] == 0
    assert result["states"][1]["after"]["positions"][0]["quantity"] == 900


def test_artifact_arithmetic_and_original_input_changes_are_detected(tmp_path):
    root, _, _ = fixture(tmp_path)
    output = tmp_path / "result"
    assert targets.evaluate(root / "protocol.json", root / "submission.json", output)["status"] == "COMPLETED"
    assert targets.verify(output, inputs=True)["status"] == "PASS"
    changed = targets.read(root / "submission.json")
    changed["decisions"][0]["cash_weight"] = .2
    experiments.write(root / "submission.json", changed)
    assert targets.verify(output)["status"] == "PASS"
    with pytest.raises(ValueError, match="original input"):
        targets.verify(output, inputs=True)
    score = targets.read(output / "scorecard.json")
    score["summary"]["net_return"] = 100
    experiments.write(output / "scorecard.json", score)
    hashes = experiments.inventory(output)
    hashes.pop("checksums.json")
    experiments.write(output / "checksums.json", hashes)
    with pytest.raises(ValueError, match="arithmetic"):
        targets.verify(output)


def test_nonempty_corporate_actions_and_missing_holdings_fail_explicitly(tmp_path):
    _, protocol, _ = fixture(tmp_path)
    protocol["corporate_actions"] = [{"event": "cash_dividend"}]
    with pytest.raises(ValueError, match="corporate actions"):
        targets.validate_protocol(protocol)
    protocol["corporate_actions"] = []
    protocol["sessions"][1]["eligible_assets"] = ["B"]
    protocol["sessions"][1]["bars"] = []
    with pytest.raises(ValueError, match="missing eligible"):
        targets.validate_protocol(protocol)
