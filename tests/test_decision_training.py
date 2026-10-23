"""Actual production-map, state-cut and GRU utility training graphs."""

import copy
from dataclasses import asdict, replace

from qwb import experiments
from qwb.bench import targets
from qwb.portfolio.contracts import timestamp

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")

from qwb.portfolio.continuous import ContinuousConfig, tensor
from qwb.portfolio.episode import ContinuousIntent, run_episode
from qwb.portfolio.optimization import AllocationError
from qwb.portfolio.production import tensor_produce
from qwb.portfolio.reference import Fees
from qwb.training.decision import DecisionGRU, DecisionPolicy
from qwb.training.sequence import GRUConfig, GRUPredictor
from qwb.training.utility import net_utility, net_utility_returns
from qwb.training.utility_fit import DecisionTrainingConfig, fit, validate_feedback


def produce(raw, *, previous=None, lower=None, upper=None, budget=None, turnover=2):
    return tensor_produce(raw, tensor([.1] * len(raw)) if previous is None else previous,
        torch.zeros_like(raw) if lower is None else lower, torch.ones_like(raw) if upper is None else upper,
        budget=tensor(.8) if budget is None else budget, max_turnover=turnover, tolerance=1e-6)


@pytest.mark.parametrize("raw,lower,upper,budget", [
    ([.2, .3], [0, 0], [.4, .4], .8),
    ([-1e-9, .4 + 1e-9], [0, .1], [.4, .4], .7),
    ([.40000002, .40000002], [0, 0], [1, 1], .8),
    ([0, 0], [0, 0], [0, 0], 0),
    ([.2, .3], [.2, .3], [.2, .3], .5),
])
def test_tensor_production_bounds_budget_and_audit(raw, lower, upper, budget):
    result, audit = produce(tensor(raw), lower=tensor(lower), upper=tensor(upper), budget=tensor(budget))
    assert bool((result >= tensor(lower)).all()) and bool((result <= tensor(upper)).all())
    assert float(result.sum()) <= budget
    assert audit["max_absolute_adjustment"] <= 1e-6
    assert audit["l1_adjustment"] == pytest.approx(float((result - tensor(raw)).abs().sum()))
    assert audit["policy"] == "torch_box_budget_turnover_v1"


def test_tensor_turnover_map_and_nonfeasible_anchor_fail_explicitly():
    result, audit = produce(tensor([.2000002, .2000002]), turnover=.2)
    assert float((result - .1).abs().sum()) <= .2
    assert audit["turnover_correction"]
    exact, _ = produce(tensor([.1]), turnover=0)
    assert exact.tolist() == [.1]
    with pytest.raises(AllocationError, match="anchor"):
        produce(tensor([.8]), previous=tensor([.8]), turnover=0)
    # Prior holdings can drift outside a box while a near-feasible target exists.
    result, _ = produce(tensor([.40000002, .1999999]), previous=tensor([.5, .1]),
                        upper=tensor([.4, .8]), turnover=.2)
    assert float(result[0]) <= .4


@pytest.mark.parametrize("kind", ["negative", "material", "shape", "dtype", "nan", "bounds", "budget"])
def test_production_refuses_invalid_or_material_repairs(kind):
    raw = tensor([.2, .2])
    kwargs = {}
    if kind == "negative":
        raw[0] = -.01
    elif kind == "material":
        raw[:] = .6
    elif kind == "shape":
        kwargs["previous"] = tensor([.1])
    elif kind == "dtype":
        raw = raw.float()
    elif kind == "nan":
        raw[0] = float("nan")
    elif kind == "bounds":
        kwargs["lower"], kwargs["upper"] = tensor([.3, 0]), tensor([.2, 1])
    else:
        kwargs["budget"] = tensor(-.1)
    with pytest.raises(ValueError):
        produce(raw, **kwargs)


@pytest.mark.parametrize("branch", ["interior", "box", "budget", "turnover"])
def test_actual_production_map_directional_derivative_on_each_smooth_branch(branch):
    raw = tensor([.2, .3])
    kwargs = {}
    if branch == "box":
        raw = tensor([-2e-7, .3])
    elif branch == "budget":
        raw = tensor([.4000002, .4000002])
    elif branch == "turnover":
        raw = tensor([.2000002, .2000002])
        kwargs["turnover"] = .2
    direction = tensor([.3, -.2])
    raw.requires_grad_()
    def objective(value):
        output, _ = produce(value, **kwargs)
        return output @ tensor([.7, -.4])
    gradient, = torch.autograd.grad(objective(raw), raw)
    for h in (1e-8, 3e-8):
        finite = (objective(raw.detach() + h * direction) - objective(raw.detach() - h * direction)) / (2 * h)
        torch.testing.assert_close(gradient @ direction, finite, rtol=1e-5, atol=1e-7)


def test_budget_and_actual_previous_state_keep_their_gradients():
    budget = tensor(.8).requires_grad_()
    result, _ = produce(tensor([.4000002, .4000002]), budget=budget)
    derivative, = torch.autograd.grad(result.sum(), budget)
    assert float(derivative) == pytest.approx(1)
    previous = tensor([.1, .1]).requires_grad_()
    result, _ = produce(tensor([.2000002, .2000002]), previous=previous, turnover=.2)
    derivative, = torch.autograd.grad(result.sum(), previous)
    torch.testing.assert_close(derivative, tensor([1., 1.]))


def test_episode_truncation_preserves_values_and_cuts_both_sides_of_return_ratio(tmp_path):
    root = targets.create_fixture(tmp_path / "fixture")
    protocol = targets.read(root / "protocol.json")
    for i, session in enumerate(protocol["sessions"]):
        session["bars"][0].update(open=10 + i, close=11 + i, buy_lot=1, sell_lot=1)
    first, later = tensor([.3]).requires_grad_(), tensor([.4]).requires_grad_()
    def policy(context):
        return ContinuousIntent(first if context.decision_id == "d0" else later)
    config = ContinuousConfig(fee_model="none")
    full = run_episode(protocol, policy, config=config)
    cut = run_episode(protocol, policy, config=config, detach_every=1)
    torch.testing.assert_close(full["equity"], cut["equity"])
    torch.testing.assert_close(full["net_returns"], cut["net_returns"])
    assert cut["detach_before_steps"] == [1, 2]
    before, after = cut["states"][1]
    assert not before.equity.requires_grad
    assert after.equity.requires_grad
    old_grad, new_grad = torch.autograd.grad(cut["net_returns"][1], (first, later), allow_unused=True, retain_graph=True)
    assert old_grad is None or float(old_grad.abs().sum()) == 0
    assert float(new_grad.abs().sum()) > 0
    # A naive concatenated equity ratio would introduce a spurious denominator path.
    wrong = cut["equity"][2] / cut["equity"][1] - 1
    wrong_grad, = torch.autograd.grad(wrong, first)
    assert float(wrong_grad.abs().sum()) > 0
    torch.testing.assert_close(net_utility(full["equity"])[0], net_utility_returns(full["net_returns"])[0])
    for invalid in (0, -1, True, 1.2):
        with pytest.raises(ValueError, match="detach_every"):
            run_episode(protocol, policy, detach_every=invalid)


def setup_training(tmp_path, monkeypatch, *, controlled=True):
    from test_prediction_allocation import fixture, config
    from qwb.observations.bundle import session_time

    _, bundle, train, _ = fixture(tmp_path, monkeypatch)
    from run_allocation_demo import synthetic_protocol
    validation = synthetic_protocol(bundle, [str(session_time(day, "15:30")) for day in bundle.calendar[84:87]],
                                    max_weight=.4, min_cash=.05, initial_holdings=True)
    source = GRUPredictor(GRUConfig(hidden_size=4, seed=0))
    source.fit_record = {"context": {"selection_cutoff": str(session_time(bundle.calendar[70], "15:30"))},
                         "label_transform": {"mean": 0., "scale": 1.}, "history": [], "scope": "controlled_unit_fixture"}
    if controlled:
        # An interior allocation for derivative diagnostics, not a market-trained model.
        with torch.no_grad():
            source.net.head.weight.zero_()
            source.net.head.bias.fill_(.002)
    checkpoint = tmp_path / "prediction"
    source.save(checkpoint)
    model = DecisionGRU.from_prediction(checkpoint)
    cfg = config()
    cfg["optimizer"].update(stability_penalty=.02)
    policy = {"risk": cfg["risk"], "cost": cfg["cost"], "optimizer": cfg["optimizer"],
              "fees": Fees(**train["fees"]), "min_history": 20}
    context = {"fit_cutoff": validation["sessions"][0]["decision_time"],
               "selection_cutoff": str(session_time(bundle.calendar[88], "15:30"))}
    return model, bundle, train, validation, policy, context


def test_shared_gru_policy_produces_strict_exact_targets_and_causal_actions(tmp_path, monkeypatch):
    model, bundle, train, validation, cfg, _ = setup_training(tmp_path, monkeypatch)
    policy = DecisionPolicy(model, bundle, **cfg)
    result = targets.replay(train, policy)
    assert result["explicit_failures"] == 0
    assert len(policy.audit) == len(train["sessions"])
    first = policy.audit[0]
    assert first["production"]["policy"] == "torch_box_budget_turnover_v1"
    changed = copy.deepcopy(train)
    changed["sessions"][-1]["bars"][0]["close"] *= 1.01
    other = DecisionPolicy(model, bundle, **cfg)
    targets.replay(changed, other)
    assert first == other.audit[0]
    assert result == targets.replay_submission(train, {"schema_version": "target_submission_v2",
        "protocol_id": result["protocol_id"], "decisions": result["decisions"]})


def test_gru_solver_mapping_multiday_utility_directional_gradient(tmp_path, monkeypatch):
    model, bundle, train, _, cfg, _ = setup_training(tmp_path, monkeypatch)
    parameter = model.net.head.bias
    initial = parameter.detach().clone()
    def objective():
        episode = run_episode(train, DecisionPolicy(model, bundle, **cfg),
                              config=ContinuousConfig(minimum_commission=False))
        return net_utility_returns(episode["net_returns"])[0]
    loss = objective()
    derivative, = torch.autograd.grad(loss, parameter)
    assert float(derivative.abs().sum()) > 1e-7
    for h in (1e-5, 3e-6):
        with torch.no_grad():
            parameter.copy_(initial + h)
        plus = objective().detach()
        with torch.no_grad():
            parameter.copy_(initial - h)
        minus = objective().detach()
        torch.testing.assert_close(derivative.squeeze(), (plus - minus) / (2 * h), rtol=1e-3, atol=1e-5)
    with torch.no_grad():
        parameter.copy_(initial)


def test_actual_utility_fit_freezes_encoder_selects_and_reloads_consistent_checkpoint(tmp_path, monkeypatch):
    model, bundle, train, validation, cfg, context = setup_training(tmp_path, monkeypatch)
    original = copy.deepcopy(model.net.state_dict())
    observations = []
    from qwb.training import utility_fit
    original_utility = utility_fit.net_utility_returns
    validation_calls = []
    epoch_weights = []
    def controlled_validation(*args, **kwargs):
        loss, audit = original_utility(*args, **kwargs)
        if not torch.is_grad_enabled():
            assert not model.net.training
            validation_calls.append(True)
            # Force an earlier best epoch to detect mixing best/last state.
            loss = loss + len(validation_calls)
        return loss, audit
    monkeypatch.setattr(utility_fit, "net_utility_returns", controlled_validation)
    def log(entry):
        observations.append(entry)
        if isinstance(entry, dict):
            epoch_weights.append(copy.deepcopy(model.net.state_dict()))
    record = fit(model, bundle, train, validation, policy_config=cfg, context=context,
        config=DecisionTrainingConfig(epochs=2, freeze_encoder=True, learning_rate=.0001, detach_every=2),
        continuous=ContinuousConfig(minimum_commission=False), log=log)
    assert record["parameter_change_norm"] > 0
    assert any(row["gradient_norm"] > 0 for row in record["history"])
    assert all(not name.startswith("encoder.") for name in record["trainable_parameters"])
    for name, parameter in model.net.encoder.state_dict().items():
        assert torch.equal(parameter, original["encoder." + name])
    assert record["best_epoch"] == min(record["history"], key=lambda r: r["validation_loss"])["epoch"]
    assert record["best_epoch"] == 1 and len(validation_calls) == 3
    assert all(torch.equal(value, epoch_weights[0][name]) for name, value in model.net.state_dict().items())
    assert any(not torch.equal(value, epoch_weights[-1][name]) for name, value in model.net.state_dict().items())
    assert model.training_state["epoch"] == record["best_epoch"]
    assert all(float(value["step"]) == record["best_epoch"] for value in model.training_state["optimizer"]["state"].values())
    path = tmp_path / "decision"
    model.save(path)
    restored = DecisionGRU.load(path)
    assert restored.fit_record["history"] == record["history"]
    assert not any(p.requires_grad for p in restored.net.encoder.parameters())
    for p, q in zip(model.net.parameters(), restored.net.parameters(), strict=True):
        assert torch.equal(p, q)
    assert targets.replay(validation, DecisionPolicy(model, bundle, **cfg)) == targets.replay(
        validation, DecisionPolicy(restored, bundle, **cfg))
    assert any(isinstance(entry, dict) for entry in observations)
    with pytest.raises(FileExistsError):
        model.save(path)
    (path / "training_state.pt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        DecisionGRU.load(path)


@pytest.mark.parametrize("kind", ["source_future", "overlap", "late_price", "step_budget", "fees", "cancel"])
def test_training_rejects_leakage_budgets_and_preserves_failed_selection(tmp_path, monkeypatch, kind):
    model, bundle, train, validation, cfg, context = setup_training(tmp_path, monkeypatch)
    config = DecisionTrainingConfig(epochs=1)
    log = lambda *_: None
    if kind == "source_future":
        model.source["fit"]["context"]["selection_cutoff"] = context["selection_cutoff"]
    elif kind == "overlap":
        context["fit_cutoff"] = context["selection_cutoff"]
    elif kind == "late_price":
        train["sessions"][0]["bars"][0]["close"] *= 2
    elif kind == "step_budget":
        config = replace(config, max_episode_steps=1)
    elif kind == "fees":
        cfg["fees"] = Fees(100)
    else:
        def log(*_):
            raise ValueError("cancelled by worker")
    with pytest.raises(ValueError):
        fit(model, bundle, train, validation, policy_config=cfg, context=context, config=config, log=log)
    assert model.fit_record is None and model.training_state is None


def test_feedback_uses_available_revision_rather_than_merely_session_close(tmp_path, monkeypatch):
    _, bundle, train, _, _, context = setup_training(tmp_path, monkeypatch)
    final_close = timestamp(train["sessions"][-1]["close_at"])
    with pytest.raises(ValueError, match="visible source"):
        validate_feedback(bundle, train, final_close)
    assert timestamp(validate_feedback(bundle, train, context["fit_cutoff"])) > final_close


def test_source_archive_verification_rejects_missing_seed_and_changed_weights(tmp_path, monkeypatch):
    from qwb import pipeline, runconfig
    from qwb.observations.bundle import PITBundle
    from qwb.observations.fixture import create

    path = create(tmp_path / "bundle", days=100, assets=3)
    raw = runconfig.load(path)
    raw["model"]["epochs"] = 1
    raw["folds"]["last_test"] = raw["folds"]["first_test"]
    resolved, report = runconfig.resolve(raw, path.parent)
    assert resolved is not None, str(report)
    result = pipeline.run(resolved, tmp_path)
    bundle = PITBundle(path.parent, lookback=12)
    source = DecisionGRU.from_run(result["output"], fold=0, seed=0, bundle=bundle)
    assert source.source["run_id"] == result["run_id"]
    with pytest.raises(ValueError, match="seed"):
        DecisionGRU.from_run(result["output"], fold=0, seed=1, bundle=bundle)
    with pytest.raises(ValueError, match="observation recipe"):
        DecisionGRU.from_run(result["output"], fold=0, seed=0, bundle=PITBundle(path.parent, lookback=60))
    from pathlib import Path
    checkpoint = Path(source.source["path"])
    (checkpoint / "weights.pt").write_bytes(b"forged weights")
    with pytest.raises(ValueError, match="artifact changed"):
        DecisionGRU.from_run(result["output"], fold=0, seed=0, bundle=bundle)


def test_finite_zero_gradient_is_a_recorded_negative_result(tmp_path, monkeypatch):
    from qwb.training import utility_fit

    model, bundle, train, validation, cfg, context = setup_training(tmp_path, monkeypatch)
    class FlatControl:
        def __init__(self, model, *_args, **_kwargs):
            self.model = model
        def __call__(self, context):
            weight = self.model.net.head.bias.sum() * 0 + .2
            return ContinuousIntent(weight.expand(len(context.state.assets)))
    monkeypatch.setattr(utility_fit, "DecisionPolicy", FlatControl)
    record = fit(model, bundle, train, validation, policy_config=cfg, context=context,
                 config=DecisionTrainingConfig(epochs=1), continuous=ContinuousConfig(minimum_commission=False))
    assert record["history"][0]["gradient_norm"] == 0
    assert record["history"][0]["zero_gradient_fraction"] == 1
    assert record["parameter_change_norm"] == 0
    assert record["memory"]["peak_rss_bytes"] > 0
