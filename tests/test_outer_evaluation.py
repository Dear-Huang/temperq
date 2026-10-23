"""Actual policy faults, hold accounting, recovery and recorded-fact replay."""

import copy
from dataclasses import asdict, replace
from pathlib import Path

from qwb import cli, experiments, runconfig, pipeline
from qwb.bench import targets
from qwb.jobs import Cancelled
from qwb.observations.windows import WindowBuilder
from qwb.portfolio.comparison import BASE_LIMITS
from qwb.portfolio.contracts import SettlementLot
from qwb.portfolio.lifecycle import Dividend, Split, LifecycleAccountState

import numpy as np
import pandas as pd
import pytest
import yaml

torch = pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")

from qwb.portfolio.continuous import ContinuousConfig, ContinuousState
from qwb.portfolio.decision_evaluation import evaluate
from qwb.portfolio.evaluation_failures import configuration, verify
from qwb.portfolio.optimization import AllocationError, ConvexAllocator
from qwb.training.decision import DecisionGRU, DecisionPolicy
from test_decision_training import setup_training
from test_utility_runs import prepared  # noqa: F401

EVALUATION = {"schema_version": "utility_outer_evaluation_v1", "failure_policy": "explicit_failure_hold_v1",
              "max_seconds": 120.}


@pytest.fixture(autouse=True)
def single_thread():
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    try:
        yield
    finally:
        torch.set_num_threads(previous)


@pytest.fixture
def case(tmp_path, monkeypatch):
    model, bundle, train, _, policy, _ = setup_training(tmp_path, monkeypatch)
    from run_allocation_demo import synthetic_protocol

    protocol = synthetic_protocol(bundle, [row["decision_time"] for row in train["sessions"]],
                                  max_weight=.4, min_cash=.05, initial_holdings=True)
    state = LifecycleAccountState.from_dict(protocol["initial_state"])
    position = state.positions[0]
    locked = position.quantity // 2
    position = replace(position, sellable_quantity=position.quantity - locked,
                       unsettled=(SettlementLot(locked, protocol["sessions"][1]["open_at"]),))
    protocol["initial_state"] = replace(state, positions=(position, *state.positions[1:])).to_dict()
    limits = {key: 1 for key in BASE_LIMITS}
    limits["zero_cost_relative_rule"] = "null_use_absolute"
    arguments = {"policy_config": policy, "continuous": ContinuousConfig(), "acceptance": limits,
                 "evaluation": EVALUATION, "feedback_cutoff": str(bundle.calendar[90].date()) + "T15:30:00+08:00"}
    return model, bundle, protocol, arguments


def fault_policy(patch, *, kind, dates, accounts=("exact",)):
    current = [None]
    original_action = DecisionPolicy._action

    def observe(self, context, detail):
        current[0] = context
        return original_action(self, context, detail)

    patch.setattr(DecisionPolicy, "_action", observe)

    def selected():
        context = current[0]
        account = "continuous" if isinstance(context.state, ContinuousState) else "exact"
        return str(context.state.observed_at) in dates and account in accounts

    if kind == "history":
        original = WindowBuilder.at

        def missing(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            return replace(result, lengths=np.zeros_like(result.lengths)) if selected() else result

        patch.setattr(WindowBuilder, "at", missing)
    elif kind == "forecast":
        original = DecisionGRU.forecast

        def nonfinite(self, *args):
            result = original(self, *args)
            return result * float("nan") if selected() else result

        patch.setattr(DecisionGRU, "forecast", nonfinite)
    elif kind == "solver":
        from cvxpylayers.torch import CvxpyLayer
        original = CvxpyLayer.forward

        def broken(self, *args, **kwargs):
            if selected():
                raise RuntimeError("controlled transient backend fault")
            return original(self, *args, **kwargs)

        patch.setattr(CvxpyLayer, "forward", broken)
    elif kind == "production":
        original = ConvexAllocator.__call__

        def invalid(self, *args, **kwargs):
            raw, audit = original(self, *args, **kwargs)
            if selected():
                raw = raw.clone()
                raw[0] = self.config.max_weight + .01
            return raw, audit

        patch.setattr(ConvexAllocator, "__call__", invalid)
    else:
        from qwb.training import decision
        from qwb.portfolio.recipes import RiskRecipe
        original = decision.estimate_tensors

        def missing_recipe(*args, **kwargs):
            if selected():
                kwargs["risk"] = RiskRecipe(100, 100, .2)
            return original(*args, **kwargs)

        patch.setattr(decision, "estimate_tensors", missing_recipe)


@pytest.mark.parametrize("kind", ["history", "forecast", "solver", "production", "recipe"])
@pytest.mark.parametrize("indices", [(0,), (1,), (0, 1)])
def test_native_exact_faults_hold_recover_and_replay_without_retrigger(case, tmp_path, monkeypatch, kind, indices):
    model, bundle, protocol, arguments = case
    output = tmp_path / "diagnostic"
    dates = {protocol["sessions"][index]["decision_time"] for index in indices}
    with monkeypatch.context() as patch:
        fault_policy(patch, kind=kind, dates=dates)
        result, audit = evaluate(model, bundle, protocol, output, **arguments)
    assert result["evaluation_status"] == "DIAGNOSTIC_WITH_FAILURES"
    assert result["coverage"] == {"declared": 3, "exact_targets": 3 - len(indices), "exact_failures": len(indices),
                                  "continuous_completed": 3, "continuous_failed": 0, "continuous_not_run": 0}
    assert not result["closed_loop_comparison_eligible"] and len(audit) == 3 - len(indices)
    exact = experiments.read(output / "exact/replay.json")
    assert len(exact["audits"]) == len(protocol["sessions"])
    for index in indices:
        assert exact["audits"][index]["fills"] == []
        assert exact["audits"][index]["target_weights"] is None
        before, after = exact["states"][index]["before"], exact["states"][index]["after"]
        assert before["cash"] == after["cash"]
        assert {p["asset_id"]: p["quantity"] for p in before["positions"]} == {
            p["asset_id"]: p["quantity"] for p in after["positions"]}
        assert experiments.read(output / "differences.json")["closed_loop"]["daily"][index]["action_weight_gap"] is None
    assert exact["decisions"][-1]["schema_version"] == "target_weights_v2"
    assert verify(model, bundle, protocol, output, **arguments)[0] == result


def test_failure_days_still_process_split_settlement_and_dividend_payment(case, tmp_path, monkeypatch):
    model, bundle, protocol, arguments = case
    first, second, third = protocol["sessions"]
    code = protocol["initial_state"]["positions"][0]["asset_id"]
    available = first["decision_time"]
    ex = str(pd.Timestamp(second["open_at"]) - pd.Timedelta(minutes=30))
    pay = str(pd.Timestamp(third["open_at"]) - pd.Timedelta(minutes=30))
    protocol["corporate_actions"] = [asdict(Dividend("failure-dividend", code, available, first["close_at"],
        ex, pay, .2, 0., .1, "tax_exempt")), asdict(Split("failure-split", code, available, ex, 2, 1, "reject"))]
    output = tmp_path / "corporate-failure"
    with monkeypatch.context() as patch:
        fault_policy(patch, kind="history", dates={second["decision_time"], third["decision_time"]})
        result, _ = evaluate(model, bundle, protocol, output, **arguments)
    exact = experiments.read(output / "exact/replay.json")
    before, after = exact["states"][1]["before"], exact["states"][1]["after"]
    quantity = lambda state: next(p["quantity"] for p in state["positions"] if p["asset_id"] == code)
    assert quantity(after) == 2 * quantity(before)
    assert LifecycleAccountState.from_dict(after).receivable_equity > 0
    paid = exact["states"][2]
    assert paid["after"]["cash"] > paid["before"]["cash"]
    assert LifecycleAccountState.from_dict(paid["after"]).receivable_equity == 0
    assert all(row["fills"] == [] for row in exact["audits"][1:])
    assert verify(model, bundle, protocol, output, **arguments)[0] == result


def test_continuous_failure_keeps_prefix_and_declared_not_run_days(case, tmp_path, monkeypatch):
    model, bundle, protocol, arguments = case
    output = tmp_path / "continuous-failure"
    with monkeypatch.context() as patch:
        fault_policy(patch, kind="history", dates={protocol["sessions"][1]["decision_time"]}, accounts=("continuous",))
        result, _ = evaluate(model, bundle, protocol, output, **arguments)
    assert result["continuous_net_return"] is None
    assert result["coverage"]["continuous_completed"] == 1
    assert result["coverage"]["continuous_failed"] == result["coverage"]["continuous_not_run"] == 1
    assert not (output / "continuous.json").exists()
    assert len(experiments.read(output / "continuous-progress.json")["steps"]) == 1
    rows = experiments.read(output / "differences.json")["closed_loop"]["daily"]
    assert [row["continuous_status"] for row in rows] == ["completed", "failed", "not_run"]
    assert all(row["action_weight_gap"] is None for row in rows)
    assert verify(model, bundle, protocol, output, **arguments)[0] == result


@pytest.mark.parametrize("error", [Cancelled, TimeoutError, MemoryError, torch.OutOfMemoryError, TypeError, ValueError])
def test_backend_cancellation_budget_and_programming_errors_are_not_continued(case, tmp_path, monkeypatch, error):
    from cvxpylayers.torch import CvxpyLayer

    model, bundle, protocol, arguments = case
    def broken(*args, **kwargs):
        raise error("controlled fatal error")
    monkeypatch.setattr(CvxpyLayer, "forward", broken)
    expected = AllocationError if error in (TypeError, ValueError) else error
    with pytest.raises(expected, match="controlled fatal error"):
        evaluate(model, bundle, protocol, tmp_path / "fatal", **arguments)
    assert not (tmp_path / "fatal/result.json").exists()


def test_outer_time_budget_and_invalid_data_identity_abort(case, tmp_path):
    model, bundle, protocol, arguments = case
    with pytest.raises(TimeoutError, match="time budget"):
        evaluate(model, bundle, protocol, tmp_path / "budget", **dict(arguments, evaluation=dict(EVALUATION, max_seconds=.001)))
    changed = copy.deepcopy(protocol)
    changed["initial_state"]["data_snapshot_id"] = "wrong-snapshot"
    with pytest.raises(ValueError, match="snapshot differs"):
        evaluate(model, bundle, changed, tmp_path / "identity", **arguments)


def test_successful_versioned_evaluation_matches_legacy_accounts(case, tmp_path):
    model, bundle, protocol, arguments = case
    legacy_arguments = {key: value for key, value in arguments.items() if key not in {"evaluation", "feedback_cutoff"}}
    legacy, old_audit = evaluate(model, bundle, protocol, tmp_path / "legacy", **legacy_arguments)
    current, new_audit = evaluate(model, bundle, protocol, tmp_path / "versioned", **arguments)
    assert all(current[key] == value for key, value in legacy.items())
    assert old_audit == new_audit and current["evaluation_status"] == "COMPLETED"
    for name in ("continuous.json", "differences.json", "submission.json", "fixed-continuous-action-exact.json"):
        assert experiments.read(tmp_path / "legacy" / name) == experiments.read(tmp_path / "versioned" / name)


def test_legitimate_zero_fills_remain_targets(case, tmp_path):
    model, bundle, protocol, arguments = case
    for session in protocol["sessions"]:
        for bar in session["bars"]:
            bar["suspended"] = True
    result, _ = evaluate(model, bundle, protocol, tmp_path / "no-fills", **arguments)
    assert result["evaluation_status"] == "COMPLETED" and result["coverage"]["exact_failures"] == 0
    exact = experiments.read(tmp_path / "no-fills/exact/replay.json")
    assert all(row["decision_status"] == "target" and row["total_cost"] == 0
               and all(fill["quantity"] == 0 for fill in row["fills"]) for row in exact["audits"])


def test_accounting_and_malformed_forecasts_are_fatal(case, tmp_path, monkeypatch):
    model, bundle, protocol, arguments = case
    with monkeypatch.context() as patch:
        def broken_account(*args, **kwargs):
            raise ValueError("controlled accounting failure")
        patch.setattr(targets, "execute_lifecycle_session", broken_account)
        with pytest.raises(ValueError, match="controlled accounting failure"):
            evaluate(model, bundle, protocol, tmp_path / "accounting", **arguments)
    original = model.forecast
    monkeypatch.setattr(model, "forecast", lambda *args: original(*args).float())
    with pytest.raises(ValueError, match="aligned float64 CPU"):
        evaluate(model, bundle, protocol, tmp_path / "forecast-shape", **arguments)


@pytest.mark.parametrize("value", [None, {}, dict(EVALUATION, failure_policy="zero_cash"),
    dict(EVALUATION, schema_version="unknown"), dict(EVALUATION, max_seconds=True),
    dict(EVALUATION, max_seconds=float("nan")), dict(EVALUATION, ignore_errors=True)])
def test_outer_configuration_rejects_implicit_or_unsupported_semantics(value):
    with pytest.raises(ValueError):
        configuration(value)


@pytest.mark.parametrize("mutation", ["delete", "context", "submission", "summary"])
def test_failure_artifact_arithmetic_mutations_are_rejected(case, tmp_path, monkeypatch, mutation):
    model, bundle, protocol, arguments = case
    output = tmp_path / "tamper"
    with monkeypatch.context() as patch:
        fault_policy(patch, kind="history", dates={protocol["sessions"][0]["decision_time"]})
        evaluate(model, bundle, protocol, output, **arguments)
    if mutation in {"delete", "context"}:
        value = experiments.read(output / "decision-attempts.json")
        if mutation == "delete":
            value["decisions"].pop(0)
        else:
            value["decisions"][0]["context_hash"] = "wrong-context"
        experiments.write(output / "decision-attempts.json", value)
    elif mutation == "submission":
        value = experiments.read(output / "submission.json")
        value["decisions"][0] = value["decisions"][1]
        experiments.write(output / "submission.json", value)
    else:
        value = experiments.read(output / "result.json")
        value["evaluation_status"] = "COMPLETED"
        experiments.write(output / "result.json", value)
    with pytest.raises(ValueError, match="outer .* (coverage|context|arithmetic).*differs|outer evaluation arithmetic mismatch"):
        verify(model, bundle, protocol, output, **arguments)


@pytest.mark.parametrize("mutation", ["weights", "label_mean", "label_scale"])
def test_all_failure_archive_still_binds_actual_model(case, tmp_path, monkeypatch, mutation):
    model, bundle, protocol, arguments = case
    output = tmp_path / "model-binding"
    with monkeypatch.context() as patch:
        fault_policy(patch, kind="history", dates={row["decision_time"] for row in protocol["sessions"]},
                     accounts=("exact", "continuous"))
        result, _ = evaluate(model, bundle, protocol, output, **arguments)
    assert result["coverage"]["exact_targets"] == 0
    assert verify(model, bundle, protocol, output, **arguments)[0] == result
    if mutation == "weights":
        with torch.no_grad():
            next(model.net.parameters()).add_(.01)
    else:
        setattr(model, mutation, getattr(model, mutation) + .01)
    with pytest.raises(ValueError, match="plan or model identity differs"):
        verify(model, bundle, protocol, output, **arguments)


def test_shared_worker_all_outer_failures_keeps_empty_forecasts_and_replays(prepared, tmp_path, monkeypatch, capsys):
    from qwb.training.utility_runner import verify as verify_run

    root, original = prepared
    raw = copy.deepcopy(original)
    raw["outer_evaluation"] = EVALUATION
    raw["validation_audit"] = {"schema_version": "utility_validation_audit_v1"}
    config = tmp_path / "outer.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    dates = {session["decision_time"] for session in experiments.read(Path(raw["folds"][0]["test"]))["sessions"]}
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        fault_policy(patch, kind="history", dates=dates, accounts=("exact", "continuous"))
        assert cli.main(["run", str(config), "--check"]) == 0
        assert cli.main(["run", str(config)]) == 0
    assert "DIAGNOSTIC_WITH_FAILURES" in capsys.readouterr().out
    output = Path(experiments.list_runs(tmp_path)[0]["path"])
    summary = experiments.read(output / "metrics.json")
    assert summary["outer_exact_failures"] == 4 and summary["outer_continuous_failures"] == 2
    assert summary["validation_audit_complete"] and not summary["comparison_eligible"]
    assert pd.read_parquet(output / "predictions.parquet").empty
    assert verify_run(output, inputs=True)["status"] == "PASS"
    trials = experiments.read(output / "trials.json")
    assert trials["attempts"][0]["training_state"] == "succeeded"
    assert trials["attempts"][0]["evaluation_status"] == "DIAGNOSTIC_WITH_FAILURES"
    from run_utility_execution_audit import compute as execution_audit
    from run_utility_production_ablation import compute as production_ablation

    for diagnostic in (execution_audit, production_ablation):
        destination = tmp_path / diagnostic.__module__
        with pytest.raises(ValueError, match="requires a complete outer evaluation"):
            diagnostic(output, destination)
        assert not destination.exists()

    account = next((output / "accounts").glob("*/B2-utility"))
    original_record = (output / "run.json").read_bytes()
    for mutation in ("coverage", "context", "submission", "summary", "trial"):
        filename = {"coverage": account / "decision-attempts.json", "context": account / "decision-attempts.json",
                    "submission": account / "submission.json", "summary": account / "result.json",
                    "trial": output / "trials.json"}[mutation]
        original_bytes = filename.read_bytes()
        value = experiments.read(filename)
        if mutation == "coverage":
            value["decisions"].pop(0)
        elif mutation == "context":
            value["decisions"][0]["context_hash"] = "wrong-context"
        elif mutation == "submission":
            value["decisions"][0] = value["decisions"][1]
        elif mutation == "summary":
            value["evaluation_status"] = "COMPLETED"
        else:
            value["attempts"][0]["evaluation_status"] = "COMPLETED"
        experiments.write(filename, value)
        record = experiments.read(output / "run.json")
        record["artifacts"] = experiments.inventory(output)
        record["artifact_hash"] = experiments.object_digest(record["artifacts"])
        experiments.write(output / "run.json", record)
        assert experiments.verify(output)["state"] == "succeeded"
        with pytest.raises(ValueError, match="outer .*differs|outer evaluation arithmetic mismatch"):
            verify_run(output)
        filename.write_bytes(original_bytes)
        (output / "run.json").write_bytes(original_record)
