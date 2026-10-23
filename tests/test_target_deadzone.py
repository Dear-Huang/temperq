"""Declared numerical target suppression, not hidden fee or account repair."""

from qwb.portfolio.contracts import AccountState, Position

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")

from qwb.portfolio.continuous import ContinuousConfig, ContinuousState, step, tensor
from qwb.portfolio.optimization import AllocationError
from qwb.portfolio.production import tensor_produce_dust
from qwb.portfolio.reference import ExecutionBar, Fees
from qwb.training.decision import DecisionPolicy
from qwb.training.utility_fit import DecisionTrainingConfig, fit
from test_decision_training import setup_training


def produce(raw, *, previous=None, lower=None, turnover=2):
    return tensor_produce_dust(raw, torch.zeros_like(raw) if previous is None else previous,
        torch.zeros_like(raw) if lower is None else lower, torch.ones_like(raw),
        budget=tensor(.9), max_turnover=turnover, tolerance=1e-6, dust_threshold=1e-10)


def test_declared_threshold_suppresses_numerical_targets_but_retains_larger_and_locked_weights():
    raw = tensor([0, 5e-11, 1e-10, 1.01e-10, 2e-11, .2])
    lower = tensor([0, 0, 0, 0, 1e-11, 0])
    result, audit = produce(raw, lower=lower)
    assert result.tolist() == [0, 0, 0, 1.01e-10, 2e-11, .2]
    assert audit["suppressed_indices"] == [1, 2]
    assert audit["suppressed_weight"] == pytest.approx(1.5e-10)
    assert audit["base_mapping"]["policy"] == "torch_box_budget_turnover_v1"
    assert audit["max_absolute_adjustment"] <= 1e-6


def test_deadzone_has_actual_zero_derivative_and_outside_branch_matches_difference():
    raw = tensor([2e-11, .2]).requires_grad_()
    result, _ = produce(raw)
    gradient, = torch.autograd.grad(result.sum(), raw)
    torch.testing.assert_close(gradient, tensor([0, 1]))
    direction = tensor([1e-12, .3])
    for h in (1e-5, 1e-6):
        plus = produce(raw.detach() + h * direction)[0].sum()
        minus = produce(raw.detach() - h * direction)[0].sum()
        torch.testing.assert_close(gradient @ direction, (plus - minus) / (2 * h), rtol=1e-5, atol=1e-7)
    # The jump at the threshold is intentional; no unique derivative claimed.
    assert float(produce(tensor([1e-10]))[0][0]) == 0
    assert float(produce(tensor([1.0001e-10]))[0][0]) > 0


def test_suppression_cannot_breach_turnover_or_repair_material_errors():
    with pytest.raises(AllocationError, match="turnover"):
        produce(tensor([5e-11, .2]), previous=tensor([5e-11, .1]), turnover=.1)
    with pytest.raises(AllocationError, match="numerical limits"):
        produce(tensor([-.01]))
    for threshold in (0, -1, 1e-5, float("nan"), True):
        with pytest.raises(ValueError):
            tensor_produce_dust(tensor([1e-11]), tensor([0]), tensor([0]), tensor([1]),
                budget=tensor(.9), max_turnover=2, tolerance=1e-6, dust_threshold=threshold)


def test_zero_target_does_not_remove_existing_position_without_an_actual_fee_paying_sale():
    decision = "2024-01-02T15:30:00+08:00"
    account = AccountState(decision, "test", 1000, (Position("A", 1, 1, 10, decision),))
    book = ContinuousState.from_account(account, ("A",))
    weight, _ = produce(tensor([5e-11]), previous=book.weights)
    assert float(book.quantity[0]) == 1
    after, audit = step(book, weight, (ExecutionBar("A", 11, 11, 20, 1, buy_lot=1),),
        open_at="2024-01-03T09:30:00+08:00", close_at="2024-01-03T15:00:00+08:00",
        next_open_at="2024-01-04T09:30:00+08:00", fees=Fees(), config=ContinuousConfig())
    assert float(after.quantity[0]) == 0
    fill = next(row for row in audit["fills"] if row["side"] == "SELL")
    assert float(fill["quantity"]) == 1 and float(fill["commission"]) == 5
    blocked, audit = step(book, weight, (ExecutionBar("A", 11, 11, 20, 1, suspended=True),),
        open_at="2024-01-03T09:30:00+08:00", close_at="2024-01-03T15:00:00+08:00",
        next_open_at="2024-01-04T09:30:00+08:00", config=ContinuousConfig())
    assert float(blocked.quantity[0]) == 1 and float(audit["total_cost"]) == 0


def test_actual_all_cash_gru_fit_records_zero_gradients_and_no_phantom_commissions(tmp_path, monkeypatch):
    model, bundle, train, validation, policy, context = setup_training(tmp_path, monkeypatch)
    with torch.no_grad():
        model.net.head.weight.zero_()
        model.net.head.bias.fill_(-1)
    policy.update(production_policy="torch_box_budget_turnover_dust_v1", dust_threshold=1e-10)
    record = fit(model, bundle, train, validation, policy_config=policy, context=context,
                 config=DecisionTrainingConfig(epochs=1))
    assert record["history"][0]["train"]["total_cost"] == 0
    assert record["history"][0]["gradient_norm"] == 0
    assert record["parameter_change_norm"] == 0
    assert record["production"]["dust_threshold"] == 1e-10
    invalid = {**policy, "production_policy": "torch_box_budget_turnover_v1"}
    with pytest.raises(ValueError, match="explicit dust"):
        DecisionPolicy(model, bundle, **invalid)
