"""Native GRU causality and archived failures across the actual training stack."""

import copy
from dataclasses import asdict, replace
from pathlib import Path
import shutil

from qwb import experiments, runconfig, worker
from qwb.bench import targets
from qwb.jobs import JobSpec, Store
from qwb.observations.bundle import PITBundle
from qwb.observations.windows import WindowBuilder
from qwb.portfolio.lifecycle import Dividend
from qwb.portfolio.reference import Fees

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")

from qwb.portfolio.continuous import ContinuousConfig, ContinuousState
from qwb.portfolio.decision_evaluation import serialize
from qwb.portfolio.episode import run_episode
from qwb.portfolio.production import tensor_produce
from qwb.training.decision import DecisionGRU, DecisionPolicy

from test_decision_training import setup_training
from test_utility_runs import prepared  # noqa: F401 -- shared source-training fixture


@pytest.fixture(autouse=True)
def one_cpu_thread():
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    try:
        yield
    finally:
        torch.set_num_threads(previous)


def trace_account(model, bundle, protocol, config, account):
    policy = DecisionPolicy(model, bundle, **config)
    observed = []

    def action(context):
        assert not hasattr(context, "bars")
        assets = context.state.assets if isinstance(context.state, ContinuousState) else context.assets
        observation = bundle.windows.at(context.state.observed_at, assets, eligible=context.eligible_assets)
        observed.append({"values": observation.values.copy(), "valid": observation.valid.copy(),
                         "available_ns": observation.available_ns.copy(), "lengths": observation.lengths.copy(),
                         "known_actions": [asdict(event) for event in context.known_actions]})
        return policy(context)

    with torch.no_grad():
        if account == "exact":
            result = targets.replay(protocol, action)
        else:
            result = serialize(run_episode(protocol, action, config=ContinuousConfig()))
    return result, policy.audit, observed


@pytest.mark.parametrize("mutation", ["future_prices", "future_liquidity", "future_event", "late_revision", "joint"])
def test_native_gru_future_data_and_events_leave_prior_inputs_constraints_and_actions_unchanged(tmp_path, monkeypatch, mutation):
    model, bundle, train, _, config, _ = setup_training(tmp_path, monkeypatch)
    # Keep an actual input-dependent GRU and a noncash first allocation.
    with torch.no_grad():
        model.net.head.weight.fill_(.0002)
    from run_allocation_demo import synthetic_protocol

    protocol = synthetic_protocol(bundle, [session["decision_time"] for session in train["sessions"]],
                                  max_weight=.4, min_cash=.05, initial_holdings=True)
    sessions = protocol["sessions"]
    code = sessions[0]["eligible_assets"][0]
    event = Dividend("future-dividend", code, sessions[0]["open_at"], sessions[0]["close_at"],
                     str(pd.Timestamp(sessions[1]["open_at"]) - pd.Timedelta(minutes=30)),
                     str(pd.Timestamp(sessions[2]["open_at"]) - pd.Timedelta(minutes=30)),
                     .2, 0., .2, "tax_exempt")
    protocol["corporate_actions"] = [asdict(event)]
    changed_protocol = copy.deepcopy(protocol)
    changed_root = tmp_path / "changed-bundle"
    shutil.copytree(bundle.root, changed_root)
    frame = bundle.bars.copy()
    day = pd.Timestamp(sessions[0]["decision_time"]).tz_convert("Asia/Shanghai").tz_localize(None).normalize()
    future = frame["date"] > day
    if mutation in {"future_prices", "joint"}:
        frame.loc[future, ["open", "high", "low", "close"]] *= 1.01
        quotes = frame.drop_duplicates(["code", "date"], keep="last").set_index(["code", "date"])
        for session in changed_protocol["sessions"]:
            execution_day = pd.Timestamp(session["close_at"]).tz_convert("Asia/Shanghai").tz_localize(None).normalize()
            for bar in session["bars"]:
                bar.update(open=float(quotes.loc[(bar["asset_id"], execution_day), "open"]),
                           close=float(quotes.loc[(bar["asset_id"], execution_day), "close"]))
    if mutation in {"future_liquidity", "joint"}:
        frame.loc[future, ["volume", "amount"]] *= 7
    if mutation in {"future_event", "joint"}:
        changed_protocol["corporate_actions"] = [asdict(replace(event, gross_per_share=.8, ex_price_adjustment=.8))]
    if mutation in {"late_revision", "joint"}:
        late = frame[(frame["date"] == day) & (frame["code"] == code)].copy()
        late["revision"] = 1
        late["available_time"] = pd.Timestamp(sessions[0]["close_at"]) + pd.Timedelta(minutes=5)
        late[["open", "high", "low", "close", "volume", "amount"]] *= [1.01, 1.03, .99, 1.02, 3, 4]
        frame = pd.concat([frame, late], ignore_index=True)
    frame.to_parquet(changed_root / "bars.parquet", index=False)
    changed_bundle = PITBundle(changed_root)

    for account in ("exact", "continuous"):
        before, old_audit, old_observed = trace_account(model, bundle, protocol, config, account)
        after, new_audit, new_observed = trace_account(model, changed_bundle, changed_protocol, config, account)
        assert sum(old_audit[0]["production"]["produced_weights"]) > .001
        for field in ("values", "valid", "available_ns", "lengths"):
            np.testing.assert_array_equal(old_observed[0][field], new_observed[0][field])
        assert old_observed[0]["known_actions"] == new_observed[0]["known_actions"] == []
        # Includes forecasts, causal covariance/costs, locked bounds, budget,
        # raw allocation and the actual production map, not only final weights.
        assert old_audit[0] == new_audit[0]
        if account == "exact":
            for field in ("weights", "cash_weight", "receivable_weight", "state_id"):
                assert before["decisions"][0][field] == after["decisions"][0][field]
        else:
            assert before["intents"][0] == after["intents"][0]
        if mutation in {"future_prices", "future_event", "joint"}:
            assert before["states"] != after["states"], "revealed execution must be allowed to alter account feedback"
            assert old_audit[1:] != new_audit[1:]
        if mutation in {"future_prices", "future_liquidity", "late_revision", "joint"}:
            assert any(a["prediction"] != b["prediction"] for a, b in zip(old_audit[1:], new_audit[1:], strict=True)), (
                "positive control: the GRU must actually consume changed observations after availability")
        if mutation in {"future_event", "joint"}:
            assert old_observed[1]["known_actions"] != new_observed[1]["known_actions"]


def test_native_gru_same_initial_account_uses_same_exact_and_continuous_action(tmp_path, monkeypatch):
    model, bundle, protocol, _, config, _ = setup_training(tmp_path, monkeypatch)
    _, exact, _ = trace_account(model, bundle, protocol, config, "exact")
    _, continuous, _ = trace_account(model, bundle, protocol, config, "continuous")
    assert exact[0] == continuous[0]


@pytest.mark.parametrize("position", range(5))
def test_production_rejects_non_cpu_arguments_without_requiring_a_gpu(position):
    args = [torch.tensor(value, dtype=torch.float64) for value in ([.2, .3], [.1, .1], [0., 0.], [1., 1.], .8)]
    args[position] = torch.empty(args[position].shape, dtype=torch.float64, device="meta")
    with pytest.raises(ValueError, match="float64 CPU"):
        tensor_produce(*args[:4], budget=args[4], max_turnover=2., tolerance=1e-6)


def test_native_policy_rejects_extra_coupled_constraints_before_solving(tmp_path, monkeypatch):
    model, bundle, _, _, config, _ = setup_training(tmp_path, monkeypatch)
    config["optimizer"]["sector_caps"] = [.2]
    with pytest.raises(TypeError, match="sector_caps"):
        DecisionPolicy(model, bundle, **config)


def test_zero_turnover_map_follows_actual_state_with_zero_raw_action_derivative():
    raw = torch.tensor([.10000001, .20000001], dtype=torch.float64, requires_grad=True)
    previous = torch.tensor([.1, .2], dtype=torch.float64, requires_grad=True)
    result, audit = tensor_produce(raw, previous, torch.zeros_like(raw), torch.ones_like(raw),
                                  budget=raw.new_tensor(.8), max_turnover=0., tolerance=1e-6)
    torch.testing.assert_close(result, previous, rtol=0, atol=0)
    raw_gradient, state_gradient = torch.autograd.grad(result.sum(), (raw, previous))
    torch.testing.assert_close(raw_gradient, torch.zeros_like(raw), rtol=0, atol=1e-10)
    torch.testing.assert_close(state_gradient, torch.ones_like(previous), rtol=0, atol=1e-10)
    assert audit["turnover_correction"]


def test_actual_fee_switch_and_zero_trade_discontinuity_keep_piecewise_gradients():
    from test_continuous_account import advance, state

    book = state(cash=100000, quantity=0)
    fees = Fees(3, 5, 0)

    def cost(weight):
        _, audit = advance(book, weight.reshape(1), opened=10, closed=10,
                           config=ContinuousConfig(), fees=fees)
        return audit["total_cost"]

    assert float(cost(torch.tensor(0., dtype=torch.float64))) == 0
    assert float(cost(torch.tensor(1e-12, dtype=torch.float64))) == 5
    for weight, expected in ((.16, 0.), (.18, 30.)):
        value = torch.tensor(weight, dtype=torch.float64, requires_grad=True)
        derivative, = torch.autograd.grad(cost(value), value)
        assert float(derivative) == pytest.approx(expected)
        for step in (1e-7, 3e-8, 1e-8):
            finite = (cost(value.detach() + step) - cost(value.detach() - step)) / (2 * step)
            torch.testing.assert_close(derivative, finite, rtol=1e-6, atol=1e-6)
    switch = torch.tensor(5 / .0003 / 100000, dtype=torch.float64)
    assert float(cost(switch)) == pytest.approx(5)
    assert float(cost(switch - 1e-8)) == 5
    assert float(cost(switch + 1e-8)) > 5


@pytest.mark.parametrize("fault,expected", [
    ("history", "lacks current or sufficient history"),
    ("solver", "SCS allocation failed: RuntimeError: controlled backend solver failure"),
    ("production", "tensor production exceeds numerical limits"),
    ("gradient", "missing or nonfinite utility gradients"),
])
@pytest.mark.parametrize("after_completed", [0, 1])
def test_native_training_faults_fail_worker_and_preserve_all_trial_states(prepared, tmp_path, monkeypatch, fault, expected, after_completed):
    root, original = prepared
    raw = copy.deepcopy(original)
    raw["train"]["epochs"] = 2
    raw["validation_audit"] = {"schema_version": "utility_validation_audit_v1"}
    raw["outer_evaluation"] = {"schema_version": "utility_outer_evaluation_v1",
                               "failure_policy": "explicit_failure_hold_v1", "max_seconds": 120.}
    from run_allocation_demo import synthetic_protocol
    from qwb.observations.bundle import session_time

    bundle = PITBundle(raw["data"]["dir"], lookback=12)
    later = synthetic_protocol(bundle, [str(session_time(day, "15:30")) for day in bundle.calendar[86:88]],
                               max_weight=.4, min_cash=.05, initial_holdings=True)
    later_path = tmp_path / "later-test.json"
    experiments.write(later_path, later)
    raw["folds"].append(dict(copy.deepcopy(raw["folds"][0]), id=1, test=str(later_path)))
    resolved, report = runconfig.resolve(raw, root)
    assert resolved is not None, str(report)
    reached = []
    calls = [0]
    train_steps = len(experiments.read(Path(raw["folds"][0]["train"]))["sessions"])
    if fault == "history":
        original_at = WindowBuilder.at

        def missing_history(self, *args, **kwargs):
            result = original_at(self, *args, **kwargs)
            if torch.is_grad_enabled():
                calls[0] += 1
                if calls[0] > after_completed * train_steps:
                    reached.append("train_observation")
                    return replace(result, lengths=np.zeros_like(result.lengths))
            return result

        monkeypatch.setattr(WindowBuilder, "at", missing_history)
    elif fault == "solver":
        from cvxpylayers.torch import CvxpyLayer
        original_forward = CvxpyLayer.forward

        def broken_solver(self, *args, **kwargs):
            if torch.is_grad_enabled():
                calls[0] += 1
                if calls[0] > after_completed * train_steps:
                    reached.append("train_solver")
                    raise RuntimeError("controlled backend solver failure")
            return original_forward(self, *args, **kwargs)

        monkeypatch.setattr(CvxpyLayer, "forward", broken_solver)
    elif fault == "production":
        from qwb.portfolio.optimization import ConvexAllocator
        original_solve = ConvexAllocator.__call__

        def invalid_raw_weights(self, *args, **kwargs):
            weights, audit = original_solve(self, *args, **kwargs)
            if torch.is_grad_enabled():
                calls[0] += 1
                if calls[0] > after_completed * train_steps:
                    reached.append("train_production")
                    weights = weights.clone()
                    weights[weights.argmax()] = self.config.max_weight + .01
            return weights, audit

        monkeypatch.setattr(ConvexAllocator, "__call__", invalid_raw_weights)
    else:
        original_from_run = DecisionGRU.from_run.__func__

        def poisoned_gradient(gradient):
            calls[0] += 1
            if calls[0] > after_completed:
                reached.append("train_backward")
                return torch.full_like(gradient, float("nan"))
            return gradient

        def model_with_gradient_fault(cls, *args, **kwargs):
            model = original_from_run(cls, *args, **kwargs)
            model.net.head.bias.register_hook(poisoned_gradient)
            return model

        monkeypatch.setattr(DecisionGRU, "from_run", classmethod(model_with_gradient_fault))
    store = Store(tmp_path / "runs/jobs")
    job = store.submit(JobSpec("train", {"action": "train", "resolved": resolved}))
    assert worker.run_job(tmp_path, job) == "failed"
    assert store.status(job)["state"] == "failed" and reached
    runs = experiments.list_runs(tmp_path)
    assert len(runs) == 1 and runs[0]["state"] == "failed"
    output = Path(runs[0]["path"])
    attempts = experiments.read(output / "trials.json")["attempts"]
    assert [row["state"] for row in attempts] == ["failed", "not_run"]
    assert expected in attempts[0]["error"] and len(attempts[0]["history"]) == after_completed
    failure = attempts[0]["failure_context"]
    assert failure["schema_version"] == "utility_training_failure_v1"
    assert failure["epoch"] == after_completed + 1
    assert failure["completed_epochs"] == after_completed
    assert failure["phase"] == ("gradient_validation" if fault == "gradient" else "training_episode")
    assert failure["reason"] in attempts[0]["error"]
    if fault == "gradient":
        assert failure["exception_type"] == "ValueError" and "decision" not in failure
    else:
        decision = failure["decision"]
        protocol = experiments.read(Path(raw["folds"][0]["train"]))
        session = protocol["sessions"][0]
        assert decision["schema_version"] == "decision_policy_error_v1"
        assert decision["decision_id"] == session["decision_id"]
        assert pd.Timestamp(decision["decision_time"]) == pd.Timestamp(session["decision_time"])
        assert decision["assets"] == sorted(set(session["eligible_assets"]) |
                                            {p["asset_id"] for p in protocol["initial_state"]["positions"]})
        assert decision["data_snapshot_id"] == bundle.manifest["data_snapshot_id"]
        assert decision["stage"] == {"history": "observations", "solver": "allocation", "production": "production"}[fault]
        assert decision["exception_type"] == ("ValueError" if fault == "history" else "AllocationRejection")
        assert decision["reason"] == failure["reason"]
        if fault == "history":
            assert decision["history_lengths"] == [0] * len(decision["assets"])
        else:
            assert decision["recipe_id"] and decision["prediction_repr"]
            constraints = decision["constraints"]
            assert len(constraints["previous_weights"]) == len(decision["assets"])
            assert constraints["budget"] <= 1 - constraints["min_cash"]
            if fault == "production":
                assert max(decision["raw_weights"]) > constraints["max_weight"]
                assert max(decision["allocation"]["raw_weights"]) <= constraints["max_weight"] + constraints["tolerance"]
    audit = experiments.read(output / "validation-candidates/fold-0000-seed-0/summary.json")
    assert audit["status"] == "INTERRUPTED_TRAINING" and audit["selected_epoch"] is None
    assert [row["state"] for row in audit["attempts"]] == ["succeeded"] * after_completed + ["not_run"] * (2 - after_completed)
    assert not list((output / "models").rglob("weights.pt"))
    assert not (output / "final_model").exists() and not (output / "metrics.json").exists()
    assert not (store.root / job / "artifacts/output.txt").exists()
