"""Corporate rights, receivables and raw shares across an independent account."""

import copy
from dataclasses import asdict, replace

import pytest

from qwb import experiments
from qwb.bench import targets
from qwb.portfolio.contracts import AccountState, Position, SettlementLot, TargetWeights
from qwb.portfolio.lifecycle import (Dividend, Entitlement, LifecycleAccountState, LifecycleTargetWeights,
                                     Split, Terminal, apply_phase, parse_actions, phases, validate_initial)

D0 = "2024-01-02T15:30:00+08:00"
C1 = "2024-01-03T15:00:00+08:00"
P2 = "2024-01-04T09:00:00+08:00"
O2 = "2024-01-04T09:30:00+08:00"
P3 = "2024-01-05T09:00:00+08:00"


def dividend(**changes):
    return replace(Dividend("div-A", "A", D0, C1, P2, P3, 1.0, 0.0, 1.0, "tax_exempt"), **changes)


def initial(*, cash=1000, positions=None):
    if positions is None:
        positions = (Position("A", 100, 100, 10.0, D0),)
    return LifecycleAccountState(D0, "synthetic-lifecycle", cash, positions)


def apply(state, event, phase):
    at, key, name, action = next(row for row in phases((event,)) if row[2] == phase)
    return apply_phase(state, at, key, name, action)


def protocol(tmp_path, actions=()):
    root = targets.create_fixture(tmp_path / "fixture")
    value = targets.read(root / "protocol.json")
    value.update(schema_version="target_protocol_v2", execution_model="raw_share_lifecycle_v2",
                 initial_state=initial().to_dict(), corporate_actions=[asdict(a) for a in actions])
    value["fees"] = {"commission_bp": 0.0, "minimum_commission": 0.0, "slippage_bp": 0.0}
    value["constraints"]["min_cash"] = 0
    for session in value["sessions"]:
        session["bars"][0].update(open=10.0, close=10.0, sell_lot=1)
    return value


def policy(ctx):
    claim = ctx.state.receivable_equity / ctx.state.equity
    return LifecycleTargetWeights(ctx.decision_id, ctx.state.observed_at, ctx.execution_not_before,
        "cn_alloc_daily_v1", "lifecycle-test", ctx.state.data_snapshot_id, ctx.state.state_id,
        ctx.universe_id, ctx.protocol_id, (("A", .5 * (1 - claim)),), .5 * (1 - claim), receivable_weight=claim)


def failure(ctx):
    return {"schema_version": "decision_failure_v1", "decision_id": ctx.decision_id,
            "decision_time": ctx.state.observed_at, "task": "cn_alloc_daily_v1", "model_version": "lifecycle-test",
            "data_snapshot_id": ctx.state.data_snapshot_id, "state_id": ctx.state.state_id,
            "universe_id": ctx.universe_id, "protocol_id": ctx.protocol_id, "reason": "keep holdings"}


def test_dividend_record_ex_pay_exact_equity_and_cash():
    state = initial()
    event = dividend()
    record, audit = apply(state, event, "record")
    assert record.equity == state.equity == 2000
    assert record.cash == 1000 and record.receivable_equity == 0
    assert record.entitlements[0].quantity == 100 and not record.entitlements[0].recognized
    ex, audit = apply(record, event, "ex")
    assert ex.positions[0].mark == 9 and ex.cash == 1000
    assert ex.receivable_equity == 100 and ex.equity == 2000
    paid, audit = apply(ex, event, "pay")
    assert paid.cash == 1100 and paid.entitlements == () and paid.equity == 2000
    assert audit["corporate_pnl"] == 0
    assert LifecycleAccountState.from_dict(paid.to_dict()).state_id == paid.state_id
    with pytest.raises(ValueError, match="repeated"):
        apply(paid, event, "pay")


def test_record_rights_survive_later_sale_and_ex_day_buyer_has_no_rights():
    event = dividend()
    recorded, _ = apply(initial(), event, "record")
    sold = replace(recorded, positions=(), cash=2000)
    ex, _ = apply(sold, event, "ex")
    assert ex.receivable_equity == 100 and not ex.positions
    paid, _ = apply(ex, event, "pay")
    assert paid.cash == 2100
    empty = initial(positions=())
    empty, _ = apply(empty, event, "record")
    empty, _ = apply(empty, event, "ex")
    buyer = replace(empty, cash=100, positions=(Position("A", 100, 100, 9, P2),))
    buyer, _ = apply(buyer, event, "pay")
    assert buyer.cash == 100 and not buyer.entitlements and buyer.equity == 1000


def test_declared_withholding_is_recognized_once_and_not_recharged_at_payment():
    event = dividend(withholding_per_share=.2, tax_treatment="declared_withholding")
    record, _ = apply(initial(), event, "record")
    ex, audit = apply(record, event, "ex")
    assert ex.equity == 1980 and ex.receivable_equity == 80
    assert audit["corporate_pnl"] == -20 and audit["declared_withholding"] == 20
    paid, audit = apply(ex, event, "pay")
    assert paid.equity == 1980 and paid.cash == 1080 and audit["corporate_pnl"] == 0
    with pytest.raises(ValueError, match="tax treatment"):
        dividend(withholding_per_share=.2)


def test_split_keeps_value_and_settlement_schedule_and_rejects_fractional_lots():
    state = initial(positions=(Position("A", 100, 50, 10, D0, (SettlementLot(50, O2),)),))
    event = Split("split-A", "A", D0, P2, 2, 1, "reject")
    after, audit = apply(state, event, "split")
    position = after.positions[0]
    assert position.quantity == 200 and position.sellable_quantity == 100 and position.mark == 5
    assert position.unsettled == (SettlementLot(100, O2),)
    assert after.equity == state.equity and audit["corporate_pnl"] == 0
    with pytest.raises(ValueError, match="fractional"):
        apply(state, replace(event, numerator=1, denominator=3), "split")
    # Even when the total is integral, a fractional locked lot cannot be silently rounded.
    with pytest.raises(ValueError, match="fractional"):
        apply(initial(positions=(Position("A", 100, 49, 10, D0, (SettlementLot(51, O2),)),)),
              replace(event, numerator=1, denominator=2), "split")


@pytest.mark.parametrize("value", [0.0, 4.0])
def test_terminal_valuation_is_a_claim_not_a_fictitious_market_sale(value):
    event = Terminal("gone-A", "A", D0, P2, P3, value, "declared liquidation proceeds")
    state, audit = apply(initial(), event, "terminal")
    assert state.cash == 1000 and not state.positions
    assert state.entitlements[0].quantity == 100
    assert state.receivable_equity == 100 * value
    assert audit["corporate_pnl"] == 100 * (value - 10)
    after, audit = apply(state, event, "settle")
    assert after.cash == 1000 + 100 * value and not after.entitlements
    assert after.equity == state.equity and audit["corporate_pnl"] == 0


def test_v2_budget_preserves_receivable_and_rejects_v1_and_fabricated_claim_weights(tmp_path):
    p = protocol(tmp_path, (dividend(),))
    seen = []
    def collect(ctx):
        seen.append(ctx)
        return failure(ctx)
    targets.replay(p, collect)
    ctx = seen[-1]
    assert ctx.state.receivable_equity == 100
    wrong = policy(ctx)
    with pytest.raises(ValueError, match="receivable weight"):
        replace(wrong, receivable_weight=0, cash_weight=wrong.cash_weight + wrong.receivable_weight).validate_account(ctx.state)
    with pytest.raises(ValueError, match="v1 targets"):
        TargetWeights(ctx.decision_id, ctx.state.observed_at, ctx.execution_not_before, "cn_alloc_daily_v1",
            "fixture", ctx.state.data_snapshot_id, ctx.state.state_id, ctx.universe_id, ctx.protocol_id,
            (("A", .5),), .5).validate_account(ctx.state)
    assert wrong._budget() == pytest.approx(1)
    assert LifecycleTargetWeights.from_dict(wrong.to_dict()) == wrong


def test_full_dividend_replay_reconciles_and_freezes_for_cli_audit(tmp_path):
    p = protocol(tmp_path, (dividend(),))
    for session in p["sessions"][1:]:
        session["bars"][0].update(open=9.0, close=9.0)
    result = targets.replay(p, failure)
    assert result["schema_version"] == "target_replay_v2"
    assert [r["equity_close"] for r in result["audits"]] == [2000, 2000, 2000]
    assert [s["after"]["cash"] for s in result["states"]] == [1000, 1000, 1100]
    assert [r["receivable_equity"] for r in result["audits"]] == [0, 100, 0]
    assert all(row["pnl_residual"] == 0 for row in result["audits"])
    root = tmp_path / "inputs"
    root.mkdir()
    experiments.write(root / "protocol.json", p)
    experiments.write(root / "submission.json", {"schema_version": "target_submission_v2",
        "protocol_id": result["protocol_id"], "decisions": result["decisions"]})
    output = tmp_path / "evaluated"
    assert targets.evaluate(root / "protocol.json", root / "submission.json", output)["status"] == "COMPLETED"
    assert targets.verify(output, inputs=True)["status"] == "PASS"


def test_claim_cannot_finance_a_purchase_and_known_events_do_not_leak(tmp_path):
    event = dividend()
    p = protocol(tmp_path, (event,))
    p["initial_state"] = initial(cash=0).to_dict()
    for session in p["sessions"][1:]:
        session["bars"][0].update(open=9.0, close=9.0)
    result = targets.replay(p, policy)
    # New payable cash on the last day is available only after the payment phase.
    assert result["states"][1]["after"]["cash"] >= 0
    split = Split("split-A", "A", "2024-01-03T08:59:00+08:00", "2024-01-03T09:00:00+08:00", 2, 1, "reject")
    p = copy.deepcopy(p)
    p["corporate_actions"] = [asdict(split)]
    contexts = []
    def collect(ctx):
        contexts.append(ctx)
        return failure(ctx)
    result = targets.replay(p, collect)
    assert contexts[0].known_actions == ()
    assert contexts[1].known_actions == (split,)
    assert result["states"][0]["after"]["positions"][0]["quantity"] == 200


def test_unpaid_claim_is_not_spendable_even_when_target_requests_a_new_asset(tmp_path):
    p = protocol(tmp_path, (dividend(),))
    p["initial_state"] = initial(cash=0).to_dict()
    for i, session in enumerate(p["sessions"]):
        session["eligible_assets"].append("B")
        session["bars"][0].update(open=10.0 if i == 0 else 9.0, close=10.0 if i == 0 else 9.0, suspended=True)
        from qwb.portfolio.reference import ExecutionBar
        row = asdict(ExecutionBar("B", 1.0, 1.0, 2.0, .5, buy_lot=1))
        row.pop("buy_allowed")
        session["bars"].append(row)
    def purchase(ctx):
        if ctx.decision_id == "d0":
            return failure(ctx)
        claim = ctx.state.receivable_equity / ctx.state.equity
        return LifecycleTargetWeights(ctx.decision_id, ctx.state.observed_at, ctx.execution_not_before,
            "cn_alloc_daily_v1", "lifecycle-test", ctx.state.data_snapshot_id, ctx.state.state_id,
            ctx.universe_id, ctx.protocol_id, (("B", 1 - claim),), 0, receivable_weight=claim)
    result = targets.replay(p, purchase)
    before_payment = result["audits"][1]
    fill = next(row for row in before_payment["fills"] if row["asset_id"] == "B")
    assert fill["quantity"] == 0 and fill["reason"] == "INSUFFICIENT_CASH"
    assert before_payment["actual_cash_weight"] == 0 and before_payment["receivable_equity"] == 100
    after_payment = next(row for row in result["audits"][2]["fills"] if row["asset_id"] == "B")
    assert after_payment["quantity"] == 100
    assert result["states"][2]["after"]["cash"] == 0
    assert result["final_equity"] == 1000


@pytest.mark.parametrize("same_time", [False, True])
def test_terminal_path_keeps_unpaid_value_and_forbids_reentry(tmp_path, same_time):
    event = Terminal("gone-A", "A", D0, P2, P2 if same_time else P3, 4.0, "fixed cash settlement")
    p = protocol(tmp_path, (event,))
    from qwb.portfolio.reference import ExecutionBar
    for i, session in enumerate(p["sessions"]):
        row = asdict(ExecutionBar("B", 10.0, 10.0, 20.0, 1.0))
        row.pop("buy_allowed")
        session["bars"].append(row)
        session["eligible_assets"].append("B")
        if i:
            session["bars"][0].update(open=None, close=None)
        if i == 2:
            session["eligible_assets"] = ["B"]
    result = targets.replay(p, failure)
    assert [a["equity_close"] for a in result["audits"]] == [2000, 1400, 1400]
    assert result["states"][1]["after"]["cash"] == (1400 if same_time else 1000)
    assert result["states"][1]["after"]["positions"] == ()
    assert result["states"][2]["after"]["cash"] == 1400
    assert result["audits"][1]["corporate_action_pnl"] == -600
    assert not result["audits"][1]["fills"]
    assert all(a["pnl_residual"] == 0 for a in result["audits"])
    p["sessions"][2]["eligible_assets"].append("A")
    with pytest.raises(ValueError, match="terminal assets"):
        targets.validate_protocol(p)


def test_future_action_perturbation_keeps_prior_state_target_and_known_events(tmp_path):
    event = Split("split-A", "A", "2024-01-04T08:00:00+08:00", P2, 2, 1, "reject")
    p = protocol(tmp_path, (event,))
    seen = []
    def collect(ctx):
        seen.append(ctx)
        return policy(ctx)
    first = targets.replay(p, collect)
    first_contexts = seen[:]
    seen.clear()
    changed = copy.deepcopy(p)
    changed["corporate_actions"][0]["numerator"] = 3
    second = targets.replay(changed, collect)
    for i in (0, 1):
        assert first_contexts[i].known_actions == seen[i].known_actions == ()
        assert first_contexts[i].state == seen[i].state
        for key in ("weights", "cash_weight", "receivable_weight", "state_id"):
            assert first["decisions"][i][key] == second["decisions"][i][key]
    assert first["states"][1]["after"] != second["states"][1]["after"]


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "late", "intraday", "v1", "wrong_state"])
def test_protocol_rejects_unsupported_and_ambiguous_action_semantics(tmp_path, mutation):
    p = protocol(tmp_path, (dividend(),))
    event = p["corporate_actions"][0]
    if mutation == "unknown":
        event["kind"] = "rights_issue"
    elif mutation == "duplicate":
        p["corporate_actions"].append(copy.deepcopy(event))
    elif mutation == "late":
        event["available_at"] = P2
    elif mutation == "intraday":
        event["ex_at"] = "2024-01-04T11:00:00+08:00"
    elif mutation == "v1":
        p.update(schema_version="target_protocol_v1", execution_model="raw_share_open_ioc_v1")
    else:
        p["initial_state"] = AccountState(D0, "synthetic", 1000).to_dict()
    with pytest.raises(ValueError):
        targets.validate_protocol(p)


def test_initial_history_claim_integrity_and_future_phases_rejected():
    state, _ = apply(initial(), dividend(), "record")
    validate_initial(state, (dividend(),))
    with pytest.raises(ValueError, match="history"):
        validate_initial(replace(state, processed_events=()), (dividend(),))
    changed = replace(state.entitlements[0], amount=200)
    with pytest.raises(ValueError, match="amount"):
        validate_initial(replace(state, entitlements=(changed,)), (dividend(),))
    with pytest.raises(ValueError, match="history"):
        validate_initial(replace(state, processed_events=(*state.processed_events, "div-A:ex")), (dividend(),))


def test_simultaneous_ex_payment_and_terminal_settlement_are_not_double_income(tmp_path):
    p = protocol(tmp_path, (dividend(pay_at=P2),))
    for session in p["sessions"][1:]:
        session["bars"][0].update(open=9.0, close=9.0)
    result = targets.replay(p, failure)
    assert result["states"][1]["after"]["cash"] == 1100
    assert result["final_equity"] == 2000
    assert result["states"][1]["after"]["entitlements"] == ()
    assert len(result["audits"][1]["corporate_actions"]) == 2


def test_lifecycle_cli_fixture_combines_split_dividend_and_frozen_replay(tmp_path):
    from qwb import cli

    root = tmp_path / "cli-fixture"
    assert cli.main(["bench", "target-fixture", "--lifecycle", "--output", str(root)]) == 0
    p = targets.read(root / "protocol.json")
    s = targets.read(root / "submission.json")
    assert s["schema_version"] == "target_submission_v2"
    result = targets.replay_submission(p, s)
    second = result["audits"][1]
    assert [a["phase"] for a in second["corporate_actions"]] == ["split", "ex"]
    assert second["receivable_equity"] == 900
    assert second["corporate_action_pnl"] == 0
    assert result["states"][1]["after"]["positions"][0]["quantity"] == 1900
    assert result["states"][1]["after"]["positions"][0]["sellable_quantity"] == 1800
    assert result["states"][1]["after"]["cash"] == 490
    assert result["final_equity"] == pytest.approx(11444.74)
    output = tmp_path / "cli-result"
    assert cli.main(["bench", "targets", str(root / "submission.json"), "--protocol", str(root / "protocol.json"),
                     "--output", str(output)]) == 0
    assert cli.main(["bench", "target-verify", str(output), "--inputs"]) == 0
    s["schema_version"] = "target_submission_v1"
    with pytest.raises(ValueError, match="schema mismatch"):
        targets.replay_submission(p, s)


def test_dividend_and_split_units_are_checked_before_replay(tmp_path):
    split = Split("split-A", "A", D0, P2, 2, 1, "reject")
    p = protocol(tmp_path, (dividend(), split))
    with pytest.raises(ValueError, match="share units"):
        targets.validate_protocol(p)
    p["corporate_actions"][0]["ex_price_adjustment"] = .5
    assert targets.validate_protocol(p)


def test_split_and_dividend_with_missing_market_prices_preserve_explicit_marks(tmp_path):
    split = Split("split-A", "A", D0, P2, 2, 1, "reject")
    p = protocol(tmp_path, (dividend(ex_price_adjustment=.5), split))
    p["sessions"][1]["bars"][0].update(open=None, close=None, suspended=True)
    p["sessions"][2]["bars"][0].update(open=4.5, close=4.5)
    result = targets.replay(p, failure)
    after = result["states"][1]["after"]
    assert after["positions"][0]["quantity"] == 200
    assert after["positions"][0]["mark"] == 4.5
    assert after["positions"][0]["mark_time"] == "2024-01-04 01:00:00+00:00"
    assert result["audits"][1]["receivable_equity"] == 100
    assert any(n["reason"] == "STALE_CLOSE" for n in result["audits"][1]["notices"])
    assert [a["equity_close"] for a in result["audits"]] == [2000, 2000, 2000]


def test_receivable_target_fields_cannot_enter_score_bench(tmp_path):
    import pandas as pd
    from qwb.bench.contracts import align_predictions

    path = tmp_path / "not-a-score.parquet"
    pd.DataFrame({"code": ["A"], "decision_time": [D0], "prediction": [.9],
                  "receivable_weight": [.1]}).to_parquet(path)
    with pytest.raises(ValueError, match="separate portfolio"):
        align_predictions(path, pd.DatetimeIndex(["2024-01-02"]), pd.Index(["A"]), {})
