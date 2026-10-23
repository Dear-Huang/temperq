"""Independent account agreement, explicit relaxations and entire-path derivatives."""

import copy
from dataclasses import asdict, replace

from qwb.bench import targets
from qwb.portfolio.contracts import AccountState, Position, TargetWeights
from qwb.portfolio.lifecycle import Split, Terminal

import numpy as np
import pytest

torch = pytest.importorskip("torch")
from qwb.portfolio.continuous import ContinuousConfig, ContinuousState, TensorLot, numbers, step, tensor
from qwb.portfolio.episode import ContinuousIntent, run_episode
from qwb.portfolio.optimization import AllocationConfig, ConvexAllocator
from qwb.portfolio.reference import ExecutionBar, Fees
from qwb.training.utility import net_utility

from test_portfolio_lifecycle import D0, C1, P2, O2, P3, dividend, protocol as lifecycle_protocol

O1, C2 = "2024-01-03T09:30:00+08:00", "2024-01-04T15:00:00+08:00"
EXACT = ContinuousConfig(integer_lots=True)


def state(cash=1000, quantity=100):
    account = AccountState(D0, "test", cash, (Position("A", quantity, quantity, 10, D0),) if quantity else ())
    return ContinuousState.from_account(account, ("A",))


def advance(book, weights, *, opened=11, closed=12, config=ContinuousConfig(fee_model="none"), fees=Fees(), **kwargs):
    return step(book, weights, (ExecutionBar("A", opened, closed, 100, 1, buy_lot=1),),
                open_at=O1, close_at=C1, next_open_at=O2, fees=fees, config=config, **kwargs)


def fixed_episode(protocol, targets_by_id, config=EXACT):
    def policy(context):
        action = targets_by_id[context.decision_id]
        if action is None:
            return ContinuousIntent(None, hold_reason="fixed hold-shares control")
        return ContinuousIntent(tensor([action.get(code, 0) for code in context.state.assets]))
    return run_episode(protocol, policy, config=config)


def test_hand_account_keeps_old_shares_overnight_and_new_shares_intraday_without_double_fees():
    after, audit = advance(state(), tensor([.5]))
    assert float(audit["equity_open"]) == 2100
    assert float(after.quantity[0]) == pytest.approx(1050 / 11)
    assert float(after.cash) == 1050
    assert float(after.equity) == pytest.approx(1050 + 1050 / 11 * 12)
    assert float(audit["overnight_pnl"]) == 100
    assert float(audit["intraday_pnl"]) == pytest.approx(1050 / 11)
    assert abs(float(audit["self_financing_residual"])) < 1e-10
    assert float(audit["total_cost"]) == 0


def test_target_old_weight_rebalances_after_gap_while_hold_control_really_keeps_shares():
    book = state()
    held, keep = advance(book, None)
    rebalanced, audit = advance(book, book.weights)
    assert float(held.quantity[0]) == 100 and keep["fills"] == []
    assert float(rebalanced.quantity[0]) < 100
    assert float(audit["fills"][0]["notional"]) == pytest.approx(50)
    assert float(rebalanced.equity) != float(held.equity)


@pytest.mark.parametrize("minimum", [False, True])
def test_fractional_funding_pays_fees_once_and_does_not_borrow(minimum):
    before = state(cash=1000, quantity=0)
    after, audit = advance(before, tensor([1.]), opened=10, closed=10,
        config=ContinuousConfig(minimum_commission=minimum), fees=Fees(10, 50, 20))
    expected_notional = min(1000 / 1.003, 950 / 1.002) if minimum else 1000 / 1.003
    assert float(after.quantity[0]) == pytest.approx(expected_notional / 10)
    assert float(after.cash) == pytest.approx(0, abs=1e-10)
    assert float(after.equity + audit["total_cost"]) == pytest.approx(1000)
    assert audit["fills"][0]["reason"] == "INSUFFICIENT_CASH"


def test_binding_cash_constraint_preserves_the_gradient_to_prior_cash():
    cash = tensor(1000.).requires_grad_()
    book = replace(state(cash=1000, quantity=0), cash=cash)
    after, _ = advance(book, tensor([1.]), opened=10, closed=11,
                       config=ContinuousConfig(minimum_commission=False), fees=Fees(10, 0, 20))
    derivative, = torch.autograd.grad(after.equity, cash)
    assert float(after.cash.detach()) == 0
    assert float(derivative) == pytest.approx(1.1 / 1.003)


def test_minimum_fee_blocks_trade_if_cash_cannot_fund_it():
    after, audit = advance(state(cash=1, quantity=0), tensor([1.]), opened=10, closed=10,
                           config=ContinuousConfig(), fees=Fees(0, 5, 0))
    assert float(after.cash) == 1 and float(after.quantity[0]) == 0
    assert float(audit["total_cost"]) == 0


def test_causal_rates_charge_actual_drift_trade_instead_of_only_decision_weight_change():
    book = state()
    after, audit = advance(book, book.weights, config=ContinuousConfig(fee_model="causal_linear", minimum_commission=False),
                          cost_rates=(tensor([.001]), tensor([.002])))
    assert float(audit["total_cost"]) == pytest.approx(.1)
    assert float(after.cash) == pytest.approx(1049.9)
    with pytest.raises(ValueError, match="cost vectors"):
        advance(book, book.weights, config=ContinuousConfig(fee_model="causal_linear", minimum_commission=False))


@pytest.mark.parametrize("mutation", ["suspended", "limit", "capacity", "missing", "cash", "minimum"])
def test_integer_tensor_account_matches_independent_reference_under_execution_restrictions(tmp_path, mutation):
    root = targets.create_fixture(tmp_path / "fixture")
    protocol = targets.read(root / "protocol.json")
    submission = targets.read(root / "submission.json")
    first = protocol["sessions"][0]["bars"][0]
    if mutation == "suspended":
        first["suspended"] = True
    elif mutation == "limit":
        first["open"] = first["upper_limit"]
    elif mutation == "capacity":
        first["max_quantity"] = 350
    elif mutation == "missing":
        first["open"] = None
    elif mutation == "cash":
        protocol["fees"]["minimum_commission"] = 2000
    else:
        protocol["fees"]["minimum_commission"] = 0
    actions = {row["decision_id"]: dict(row["weights"]) if isinstance(row["weights"], dict) else
               {pair["asset_id"]: pair["target_weight"] for pair in row["weights"]} for row in submission["decisions"]}
    def independent_policy(context):
        weights = tuple((code, actions[context.decision_id].get(code, 0)) for code in context.assets)
        return TargetWeights(context.decision_id, context.state.observed_at, context.execution_not_before,
            "cn_alloc_daily_v1", "fixed-actions", context.state.data_snapshot_id, context.state.state_id,
            context.universe_id, context.protocol_id, weights, 1 - sum(value for _, value in weights))
    exact = targets.replay(protocol, independent_policy)
    continuous = fixed_episode(protocol, actions)
    for audit, independent, (_, book), actual in zip(continuous["audits"], exact["audits"], continuous["states"], exact["states"], strict=True):
        assert float(audit["equity_close"]) == pytest.approx(independent["equity_close"], abs=1e-8)
        assert float(audit["total_cost"]) == pytest.approx(independent["total_cost"], abs=1e-8)
        assert float(book.cash) == pytest.approx(actual["after"]["cash"], abs=1e-8)
        quantities = {p["asset_id"]: p["quantity"] for p in actual["after"]["positions"]}
        assert numbers(book.quantity) == [quantities.get(code, 0) for code in book.assets]


@pytest.mark.parametrize("kind", ["dividend", "split", "terminal"])
def test_events_and_delayed_rights_match_independent_account(tmp_path, kind):
    event = dividend(withholding_per_share=.2, tax_treatment="declared_withholding") if kind == "dividend" else (
        Split("split", "A", D0, P2, 2, 1, "reject") if kind == "split" else
        Terminal("terminal", "A", D0, P2, P3, 4, "declared proceeds"))
    protocol = lifecycle_protocol(tmp_path, (event,))
    if kind == "dividend":
        for session in protocol["sessions"][1:]:
            session["bars"][0].update(open=9., close=9.)
    elif kind == "split":
        for session in protocol["sessions"][1:]:
            session["bars"][0].update(open=5., close=5.)
    else:
        for session in protocol["sessions"][1:]:
            session["eligible_assets"] = ["B"]
            session["bars"][0].update(open=None, close=None)
            session["bars"].append({**session["bars"][0], "asset_id": "B", "open": 10., "close": 10.})
    from test_portfolio_lifecycle import failure

    exact = targets.replay(protocol, failure)
    episode = fixed_episode(protocol, {session["decision_id"]: None for session in protocol["sessions"]})
    np.testing.assert_allclose(numbers(episode["equity"]), [exact["initial_equity"], *[a["equity_close"] for a in exact["audits"]]], atol=1e-9)
    for audit, reference in zip(episode["audits"], exact["audits"], strict=True):
        assert float(audit["corporate_action_pnl"]) == reference["corporate_action_pnl"]
        assert float(audit["pnl_residual"].abs()) < 1e-9


def test_locked_lots_and_receivables_cannot_be_spent(tmp_path):
    book = state()
    book = replace(book, lots=(TensorLot(tensor([100.]), O2),))
    kept, audit = advance(book, tensor([0.]), config=ContinuousConfig())
    assert float(kept.quantity[0]) == 100
    assert audit["fills"][0]["reason"] == "T1_LOCKED"
    sold, _ = advance(book, tensor([0.]), config=ContinuousConfig(settlement=False))
    assert float(sold.quantity[0]) == 0
    from qwb.portfolio.lifecycle import Entitlement, LifecycleAccountState

    source = LifecycleAccountState(D0, "test", 1000., entitlements=(Entitlement("right", "A", "terminal", 100, 1000, P3, True),))
    with pytest.raises(ValueError, match="receivables"):
        advance(ContinuousState.from_account(source, ("A",)), tensor([1.]))


def test_future_execution_does_not_enter_policy_but_changes_later_state(tmp_path):
    root = targets.create_fixture(tmp_path / "fixture")
    protocol = targets.read(root / "protocol.json")
    changed = copy.deepcopy(protocol)
    changed["sessions"][0]["bars"][0]["close"] *= 1.02
    seen = []
    def policy(context):
        assert not hasattr(context, "bars")
        seen.append(numbers(context.state.weights))
        return ContinuousIntent(tensor([.5]))
    run_episode(protocol, policy)
    before = seen[:]
    seen.clear()
    run_episode(changed, policy)
    assert before[0] == seen[0] and before[1] != seen[1]


def test_path_gradient_covers_drift_fees_cash_and_variance_not_detached_daily_targets(tmp_path):
    root = targets.create_fixture(tmp_path / "fixture")
    protocol = targets.read(root / "protocol.json")
    protocol["fees"] = asdict(Fees(3, 0, 2))
    cfg = ContinuousConfig(minimum_commission=False)
    def loss_for(parameter):
        def policy(context):
            # Feedback is intentionally differentiable through prior executions.
            return ContinuousIntent(torch.sigmoid(parameter + .3 * context.state.weights))
        episode = run_episode(protocol, policy, config=cfg)
        return net_utility(episode["equity"], variance_penalty=2)[0]
    parameter = tensor([-.7]).requires_grad_()
    loss = loss_for(parameter)
    derivative, = torch.autograd.grad(loss, parameter)
    for h in (1e-3, 1e-4, 1e-5):
        finite = (loss_for(parameter.detach() + h) - loss_for(parameter.detach() - h)) / (2 * h)
        torch.testing.assert_close(derivative.squeeze(), finite, rtol=1e-3, atol=1e-5)
    assert float(derivative.abs().sum()) > 1e-4


def test_allocator_to_multiday_utility_gradient_matches_directional_difference(tmp_path):
    pytest.importorskip("cvxpylayers")
    root = targets.create_fixture(tmp_path / "fixture")
    protocol = targets.read(root / "protocol.json")
    layer = ConvexAllocator(1, AllocationConfig(risk_penalty=1, stability_penalty=.1, max_weight=.9, min_cash=.1))
    def objective(parameter):
        def policy(context):
            weights, _ = layer(parameter, tensor([[.2]]), context.state.weights, tensor([.0005]), tensor([.001]))
            return ContinuousIntent(weights)
        episode = run_episode(protocol, policy, config=ContinuousConfig(minimum_commission=False))
        return net_utility(episode["equity"], variance_penalty=1)[0]
    parameter = tensor([.02]).requires_grad_()
    gradient, = torch.autograd.grad(objective(parameter), parameter)
    for h in (1e-4, 1e-5):
        finite = (objective(parameter.detach() + h) - objective(parameter.detach() - h)) / (2 * h)
        torch.testing.assert_close(gradient.squeeze(), finite, rtol=1e-3, atol=1e-5)


def test_invalid_targets_or_equity_fail_instead_of_becoming_cash():
    for weights in (tensor([-.0000000001]), tensor([1.1]), tensor([float("nan")])):
        with pytest.raises(ValueError):
            advance(state(), weights)
    with pytest.raises(ValueError, match="strictly positive"):
        net_utility(tensor([1., 0.]))
    with pytest.raises(ValueError, match="explicit"):
        ContinuousIntent(None)
    loss, audit = net_utility(tensor([100., 110.]), variance_penalty=3)
    assert float(loss) == pytest.approx(-np.log(1.1))
    assert float(audit["net_return_variance"]) == 0


def test_integer_rounding_has_zero_gradient_and_detach_is_explicit():
    target = tensor([.7]).requires_grad_()
    after, _ = advance(state(), target, config=ContinuousConfig(integer_lots=True, fee_model="none"))
    gradient, = torch.autograd.grad(after.equity, target)
    assert float(gradient[0]) == 0
    assert after.quantity.grad_fn is not None
    assert after.detach().quantity.grad_fn is None


def test_same_fill_cost_decomposition_identifies_minimum_tax_and_proxy_impact():
    from qwb.portfolio.reconciliation import cost_decomposition

    fills = [{"asset_id": "A", "side": "BUY", "notional": 100., "commission": 5., "stamp_tax": 0., "slippage": .02},
             {"asset_id": "A", "side": "SELL", "notional": 100., "commission": 5., "stamp_tax": .05, "slippage": .02}]
    account = {"audits": [{"decision_id": "d", "fills": fills, "total_cost": 10.09}]}
    allocation = {"attempts": [{"decision_id": "d", "status": "target", "assets": ["A"], "intended_proxy_cost": 0.,
        "recipe": {"fees": asdict(Fees(3, 5, 2)), "cost": {"sell_tax_bp": 10}, "buy_cost": [.0015], "sell_cost": [.0025]}}]}
    totals = cost_decomposition(account, allocation)["totals"]
    assert totals["filled_proxy_cost"] == pytest.approx(.4)
    assert totals["minimum_commission_premium"] == pytest.approx(9.94)
    assert totals["sell_tax_rate_gap"] == pytest.approx(-.05)
    assert totals["negative_proxy_impact"] == pytest.approx(-.2)
    assert totals["accounting_residual"] == pytest.approx(0, abs=1e-10)
    with pytest.raises(ValueError, match="identical unique"):
        cost_decomposition(account, {"attempts": []})


def test_causal_cost_recipe_keeps_capital_gradient_without_training_on_future_liquidity(tmp_path):
    from qwb.observations.fixture import create
    from qwb.observations.bundle import PITBundle, session_time
    from qwb.portfolio.recipes import estimate_tensors

    path = create(tmp_path / "bundle", days=100, assets=3)
    bundle = PITBundle(path.parent)
    decision = session_time(bundle.calendar[65], "15:30")
    assets = tuple(sorted(bundle.universe_at(decision)[1]))
    equity = tensor(1000000.).requires_grad_()
    root, buy, sell, audit = estimate_tensors(bundle, decision, assets, equity=equity, fees=Fees())
    np.testing.assert_allclose(numbers(buy), audit["buy_cost"], rtol=1e-14)
    np.testing.assert_allclose(numbers(sell), audit["sell_cost"], rtol=1e-14)
    derivative, = torch.autograd.grad(buy.sum(), equity)
    assert float(derivative) == pytest.approx(sum(.001 / amount for amount in audit["median_amount"]))
    assert root.requires_grad is False


def test_integer_split_rejects_fractional_locked_lot_while_fractional_relaxation_is_explicit():
    future = "2024-01-05T09:30:00+08:00"
    book = replace(state(), lots=(TensorLot(tensor([51.]), future),))
    event = Split("split", "A", D0, P2, 1, 2, "reject")
    args = dict(open_at=O2, close_at=C2, next_open_at=future, actions=(event,))
    bars = (ExecutionBar("A", 20, 20, 100, 1),)
    with pytest.raises(ValueError, match="fractional split"):
        step(book, None, bars, config=EXACT, **args)
    fractional, _ = step(book, None, bars, config=ContinuousConfig(), **args)
    assert float(fractional.quantity[0]) == 50
    assert float(fractional.lots[0].quantity[0]) == 25.5
    assert float(fractional.equity) == 2000


def test_dividend_rights_survive_sale_and_payment_gradient_flows_through_recorded_shares(tmp_path):
    protocol = lifecycle_protocol(tmp_path, (dividend(),))
    for session in protocol["sessions"][1:]:
        session["bars"][0].update(open=9., close=9.)
    def policy(context):
        if context.decision_id == protocol["sessions"][0]["decision_id"]:
            return ContinuousIntent(context.state.weights)
        return ContinuousIntent(tensor([0.]))
    episode = run_episode(protocol, policy, config=EXACT)
    second = episode["states"][1][1]
    assert float(second.quantity[0]) == 0 and float(second.receivable) == 100
    assert float(episode["final_state"].receivable) == 0
    assert float(episode["final_state"].cash) > float(second.cash)
    parameter = tensor([.25]).requires_grad_()
    def differentiable_policy(context):
        return ContinuousIntent(parameter if context.decision_id == protocol["sessions"][0]["decision_id"] else tensor([0.]))
    relaxed = run_episode(protocol, differentiable_policy, config=ContinuousConfig(fee_model="none"))
    derivative, = torch.autograd.grad(relaxed["final_state"].cash, parameter)
    # Dividend plus ex-price loss exactly offsets without withholding or trading fees.
    assert float(derivative.abs().sum()) < 1e-8


def test_continuous_audit_recomputes_recipes_rejects_rehashed_forgery_and_keeps_attempts(tmp_path, monkeypatch):
    from qwb import experiments, pipeline, runconfig
    from qwb.portfolio import prediction
    from test_prediction_allocation import fixture, config
    import yaml

    path, bundle, protocol, _ = fixture(tmp_path, monkeypatch)
    raw = runconfig.load(path)
    raw["model"] = {"name": "ridge_factor_v1", "alpha": 10}
    rc, report = runconfig.resolve(raw, path.parent)
    assert rc is not None, str(report)
    trained = pipeline.run(rc, tmp_path)
    experiments.write(tmp_path / "protocol.json", protocol)
    cfg = config()
    cfg["output_policy"] = "highs_l1_projection_v1"
    (tmp_path / "allocation.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    allocation = tmp_path / "allocation"
    assert prediction.evaluate(trained["output"], tmp_path / "protocol.json", bundle.root,
                               tmp_path / "allocation.yaml", allocation)["status"] == "COMPLETED"
    from run_continuous_audit import compute, SCENARIOS

    output = tmp_path / "audit"
    result = compute(allocation, output)
    assert result["status"] == "COMPLETED" and result["source_causal_recipes"] == "RECOMPUTED"
    assert set(result["scenarios"]) == set(SCENARIOS)
    assert all(row["state"] == "COMPLETED" for row in targets.read(output / "attempts.json").values())
    assert max(result["scenarios"]["exact_constraints"]["independent_max_absolute_differences"].values()) < 1e-8
    changed = targets.read(allocation / "allocation_audit.json")
    changed["attempts"][0]["recipe"]["buy_cost"][0] += .1
    experiments.write(allocation / "allocation_audit.json", changed)
    inventory = experiments.inventory(allocation)
    inventory.pop("checksums.json")
    experiments.write(allocation / "checksums.json", inventory)
    with pytest.raises(ValueError, match="causal recipe"):
        compute(allocation, tmp_path / "forged-audit")
    assert targets.read(tmp_path / "forged-audit/result.json")["status"] == "INVALID_OR_INTERRUPTED"
