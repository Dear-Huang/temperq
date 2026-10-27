"""Independent accounting and small exhaustive oracles for account proposals."""
from itertools import product

import numpy as np
import pytest
from pydantic import ValidationError

from qwb import governance_review as gr


def case(**overrides):
    value = dict(name="Synthetic account", observed_at="2026-10-26T15:30:00+08:00",
                 signal_available_at="2026-10-26T15:00:00+08:00", execution_date="2026-10-27",
                 signal_version="synthetic-v1", cash=10000.,
                 forecasts=[dict(code="600000.SH", value=.4), dict(code="000001.SZ", value=.4)],
                 quotes=[dict(code="600000.SH", price=10), dict(code="000001.SZ", price=10)],
                 rules=dict(max_holdings=2, max_weight=.5, turnover_cap=1.5))
    value.update(overrides)
    return gr.ReviewInput(**value)


def test_reference_plan_preserves_cash_and_all_cost_components():
    c = case(cash=1000., holdings=[dict(code="600000.SH", quantity=500, sellable_quantity=500)],
             rules=dict(max_holdings=2, max_weight=.8, min_cash=.05, turnover_cap=2))
    result = gr.review(c)
    assert result["recommended_id"]
    for candidate in result["candidates"]:
        costs = sum(row["commission"] + row["stamp_tax"] + row["slippage"] for row in candidate["rows"])
        changes = sum(row["delta"] * row["price"] for row in candidate["rows"])
        assert candidate["estimated_cost"] == pytest.approx(costs)
        assert candidate["cash_after"] == pytest.approx(c.cash - changes - costs)
        assert candidate["equity_after"] == pytest.approx(result["equity"] - costs)
        if candidate["feasible"]:
            assert candidate["cash_weight"] >= c.rules.min_cash - 1e-8
            assert all(row["projected_weight"] <= c.rules.max_weight + 1e-8 for row in candidate["rows"])


def test_blocked_gap_is_stock_only_bound_and_never_called_avoidable_return():
    c = case(cash=8000., holdings=[dict(code='600000.SH', quantity=200, sellable_quantity=0)],
        forecasts=[dict(code='600000.SH', value=.1), dict(code='000001.SZ', value=.5)],
        quotes=[dict(code='600000.SH', price=10, status='normal'),
                dict(code='000001.SZ', price=10, status='buy_blocked')],
        rules=dict(max_weight=.9, max_holdings=2, turnover_cap=1))
    result = gr.review(c)
    hold = result['candidates'][0]
    repair = hold['target_repair']
    # Locked stock is 1000 above target; forbidden buy is 5000 below target.
    assert repair['blocked_stock_gap_value'] == pytest.approx(6000.)
    assert repair['blocked_stock_gap_weight'] == pytest.approx(.6)
    assert repair['retained_value'] == 2000
    assert repair['requested_trade_value'] == pytest.approx(6000)
    assert repair['completed_toward_target_value'] == 0
    assert repair['blocked_stock_gap_weight'] + repair['other_deviation_weight'] == pytest.approx(hold['target_deviation'])
    assert repair['other_deviation_weight'] > 0  # Cash counted separately, not doubled.
    assert any(x['id'] == 'target_only' for x in result['candidates'])


def test_target_only_comparison_does_not_reallocate_forbidden_buy_budget():
    c = case(cash=10000., forecasts=[dict(code='600000.SH', value=.2), dict(code='000001.SZ', value=.6)],
        quotes=[dict(code='600000.SH', price=10, status='normal'),
                dict(code='000001.SZ', price=10, status='buy_blocked')],
        rules=dict(max_weight=.9, max_holdings=2, turnover_cap=.8, min_cash=.05))
    result = gr.review(c)
    subset = next(x for x in result['candidates'] if x['id'] == 'target_only')
    rows = {r['code']: r for r in subset['rows']}
    assert rows['600000.SH']['proposed_quantity'] <= 200
    assert rows['000001.SZ']['proposed_quantity'] == 0
    assert subset['feasible'] and not subset['violations']
    assert subset['cash_after'] >= 7900
    assert subset['target_repair']['completed_toward_target_value'] <= 2000
    assert subset['solver']['target_only']


@pytest.mark.parametrize('status,allowed_side', [('buy_blocked', 'SELL'), ('sell_blocked', 'BUY')])
def test_directional_restriction_does_not_freeze_the_opposite_side(status, allowed_side):
    c = case(cash=8000., holdings=[dict(code='600000.SH', quantity=200, sellable_quantity=200)],
        forecasts=[dict(code='600000.SH', value=.1 if allowed_side == 'SELL' else .4)],
        quotes=[dict(code='600000.SH', price=10, status=status)],
        rules=dict(max_weight=.9, max_holdings=1, turnover_cap=1, min_cash=.05, deviation_tolerance=0))
    result = gr.review(c)
    chosen = next(x for x in result['candidates'] if x['id'] == result['recommended_id'])
    assert chosen['feasible']
    assert chosen['rows'][0]['side'] == allowed_side
    assert chosen['target_repair']['blocked_stock_gap_value'] == 0


@pytest.mark.parametrize("premium", [0., 5., 50.])
def test_tracking_milp_matches_exhaustive_small_account(premium):
    c = case(cash=750., holdings=[dict(code="600000.SH", quantity=200, sellable_quantity=200)],
             quotes=[dict(code="600000.SH", price=3.), dict(code="000001.SZ", price=2.)],
             rules=dict(max_holdings=2, max_weight=.7, min_cash=.05, turnover_cap=2,
                        minimum_commission=premium, commission_bp=10, slippage_bp=2))
    feasible = []
    for q in product(range(0, 701, 100), repeat=2):
        projection = gr.project(c, q, candidate_id="oracle", label="oracle")
        if projection["feasible"]:
            objective = projection["target_deviation"] * projection["equity_after"] + projection["estimated_cost"] * 1e-5
            feasible.append(objective)
    proposed, info = gr.solve(c)
    assert info["optimal_within_tolerance"]
    actual = gr.project(c, proposed, candidate_id="actual", label="actual")
    assert actual["feasible"]
    objective = actual["target_deviation"] * actual["equity_after"] + actual["estimated_cost"] * 1e-5
    assert objective == pytest.approx(min(feasible), abs=1e-5)


def test_locked_holdings_are_retained_and_conflict_not_recommended():
    c = case(cash=1000., holdings=[dict(code="600000.SH", quantity=900, sellable_quantity=0)])
    result = gr.review(c)
    assert result["recommended_id"] is None
    assert result["solver_attempts"][0]["status"] == 2
    assert result["findings"][3]["locked"] == [{"code": "600000.SH", "quantity": 900}]
    assert result["candidates"][0]["rows"][1]["proposed_quantity"] == 900
    action = next(row for row in result["constraint_actions"] if row["rule"] == "max_weight")
    assert action["minimum_weight"] == pytest.approx(.9)
    assert action["retained_quantity"] == 900


@pytest.mark.parametrize("status", ["sell_blocked", "suspended"])
def test_known_sell_blocks_cannot_be_bypassed(status):
    c = case(cash=1000., holdings=[dict(code="600000.SH", quantity=900, sellable_quantity=900)],
             quotes=[dict(code="600000.SH", price=10, status=status), dict(code="000001.SZ", price=10)])
    assert gr.review(c)["recommended_id"] is None


def test_fee_preference_can_recommend_hold_within_explicit_tolerance():
    c = case(cash=10180., holdings=[dict(code="600000.SH", quantity=4500, sellable_quantity=4500),
                                   dict(code="000001.SZ", quantity=4400, sellable_quantity=4400)],
             forecasts=[dict(code="600000.SH", value=.45), dict(code="000001.SZ", value=.45)],
             rules=dict(max_holdings=2, max_weight=.5, min_cash=.05, turnover_cap=.25,
                        deviation_tolerance=.025, preference="fees"))
    result = gr.review(c)
    assert result["recommended_id"] == "hold"
    tracking = next(x for x in result["candidates"] if x["id"] == "tracking")
    assert tracking["estimated_cost"] > 0
    hold = result["candidates"][0]
    assert hold["target_deviation"] <= tracking["target_deviation"] + c.rules.deviation_tolerance


def test_tight_deviation_bound_prevents_cash_only_fake_cost_improvement():
    c = case(rules=dict(max_holdings=2, max_weight=.5, min_cash=.02, turnover_cap=1,
                       deviation_tolerance=0))
    result = gr.review(c)
    chosen = next(x for x in result["candidates"] if x["id"] == result["recommended_id"])
    assert chosen["holdings_count"] == 2
    assert chosen["cash_weight"] < .4


@pytest.mark.parametrize("minimum", [5., 100., 500.])
def test_empty_positions_cannot_pay_phantom_sell_commissions(minimum):
    c = case(cash=5000., holdings=[], rules=dict(max_holdings=2, max_weight=.9,
              min_cash=0, turnover_cap=1, minimum_commission=minimum))
    proposed, info = gr.solve(c)
    assert proposed is not None, info
    projected = gr.project(c, proposed, candidate_id="check", label="check")
    assert projected["estimated_cost"] == pytest.approx(minimum * projected["order_count"])
    assert all(row["side"] in {"BUY", "HOLD"} for row in projected["rows"])


def test_one_sided_turnover_not_confused_with_gross_trade_budget():
    c = case(cash=1000., holdings=[dict(code="600000.SH", quantity=500, sellable_quantity=500)],
             rules=dict(max_holdings=2, max_weight=.9, turnover_cap=.1))
    for candidate in gr.review(c)["candidates"]:
        if candidate["feasible"]:
            gross = sum(abs(r["delta"]) * r["price"] for r in candidate["rows"])
            assert candidate["turnover"] == pytest.approx(gross / 6000)
            assert gross <= 600 + 1e-6


def test_budget_conflict_has_quantitative_lower_bound_without_relaxing_rules():
    c = case(cash=1000., holdings=[dict(code="600000.SH", quantity=900, sellable_quantity=900)],
             rules=dict(max_holdings=2, max_weight=.5, turnover_cap=.1))
    result = gr.review(c)
    assert result["recommended_id"] is None
    action = next(row for row in result["constraint_actions"] if row["rule"] == "turnover_cap")
    assert action["minimum_turnover_lower_bound"] == pytest.approx(.4)
    assert c.rules.turnover_cap == .1


def test_odd_lot_can_only_be_sold_in_full():
    c = case(cash=1000., holdings=[dict(code="600000.SH", quantity=150, sellable_quantity=150)],
             forecasts=[dict(code="000001.SZ", value=.7)],
             rules=dict(max_holdings=1, max_weight=.9, min_cash=0, turnover_cap=2))
    result = gr.review(c)
    chosen = next(x for x in result["candidates"] if x["id"] == result["recommended_id"])
    old = next(row for row in chosen["rows"] if row["code"] == "600000.SH")
    assert old["delta"] == -150


def test_signal_score_scale_does_not_change_rank_targets():
    c = case(signal_kind="score", forecasts=[dict(code="600000.SH", value=-2), dict(code="000001.SZ", value=5)])
    alternative = c.model_copy(deep=True)
    for row in alternative.forecasts:
        row.value = row.value * 1e6 + 100
    assert np.array_equal(gr.context(c)[6], gr.context(alternative)[6])


@pytest.mark.parametrize("overrides", [
    dict(signal_available_at="2026-10-27T00:00:00+08:00"),
    dict(observed_at="2026-10-26"),
    dict(execution_date="2026-10-25"),
    dict(cash=float("nan")),
    dict(forecasts=[dict(code="600000.SH", value=.6), dict(code="000001.SZ", value=.6)]),
    dict(quotes=[dict(code="600000.SH", price=10)]),
    dict(pending_orders=[{"code": "600000.SH"}]),
])
def test_invalid_inputs_rejected_without_computing(overrides):
    with pytest.raises((ValueError, ValidationError)):
        case(**overrides)


def test_result_digest_and_repeated_run_are_deterministic():
    c = case()
    assert gr.review(c) == gr.review(c)


def test_tolerance_scenarios_match_exhaustive_fee_oracle_and_preserve_rules():
    c = case(cash=750., holdings=[dict(code='600000.SH', quantity=200, sellable_quantity=200)],
             quotes=[dict(code='600000.SH', price=3.), dict(code='000001.SZ', price=2.)],
             rules=dict(max_holdings=2, max_weight=.7, min_cash=.05, turnover_cap=2,
                        minimum_commission=5, commission_bp=10, deviation_tolerance=.2))
    original = c.model_dump()
    result = gr.review(c)
    comparison = result['tolerance_comparison']
    assert [r['additional_tolerance'] for r in comparison['rows']] == [0, .1, .2]
    assert c.model_dump() == original
    baseline = next(r for r in result['candidates'] if r['id'] == 'tracking')
    oracle = [gr.project(c, quantities, candidate_id='oracle', label='oracle')
              for quantities in product(range(0, 701, 100), repeat=2)]
    for row in comparison['rows']:
        assert row['status'] == 'verified'
        candidate = next(r for r in result['candidates'] if r['id'] == row['candidate_id'])
        assert candidate['violations'] == []
        assert candidate['target_deviation'] <= row['deviation_bound'] + 1e-7
        feasible = [r for r in oracle if r['feasible'] and r['target_deviation'] <= row['deviation_bound'] + 1e-7]
        assert row['estimated_cost'] == pytest.approx(min(r['estimated_cost'] for r in feasible))
        assert row['cost_saved_vs_tracking'] == pytest.approx(baseline['estimated_cost'] - row['estimated_cost'])


def test_zero_tolerance_deduplicates_grid_and_conflicts_remain_unavailable():
    result = gr.review(case(rules=dict(deviation_tolerance=0, max_weight=.5, max_holdings=2)))
    assert len(result['tolerance_comparison']['rows']) == 1
    assert result['tolerance_comparison']['rows'][0]['candidate_id'] == 'fees'
    blocked = gr.review(case(cash=1000., holdings=[dict(code='600000.SH', quantity=900, sellable_quantity=0)]))
    assert blocked['tolerance_comparison']['status'] == 'unavailable'
    assert blocked['tolerance_comparison']['rows'] == []


def test_nested_fee_budgets_keep_optimal_cost_consistent_on_blocked_account():
    from qwb.governance_review_ui import sample_input
    result = gr.review(gr.ReviewInput.model_validate(sample_input()))
    rows = result['tolerance_comparison']['rows']
    assert all(r['solver']['optimal_within_tolerance'] for r in rows)
    assert all(r['solver']['objective_unit'] == 'CNY' for r in rows)
    # A previously feasible, cheaper plan also fits every wider deviation band.
    for narrow, wide in zip(rows, rows[1:]):
        assert narrow['target_deviation'] <= wide['deviation_bound']
        assert wide['estimated_cost'] <= narrow['estimated_cost'] + 1e-5


def test_tolerance_timeout_without_incumbent_is_not_reported_as_infeasible(monkeypatch):
    original_solve = gr.solve
    def interrupted(case, **kwargs):
        if kwargs.get('objective') == 'fees' and kwargs['deviation_bound'] < .03:
            return None, dict(status=1, objective='fees', optimal_within_tolerance=False,
                              gap=None, message='synthetic time limit', deviation_bound=kwargs['deviation_bound'])
        return original_solve(case, **kwargs)
    monkeypatch.setattr(gr, 'solve', interrupted)
    c = case(rules=dict(max_holdings=2, max_weight=.5, deviation_tolerance=.2, turnover_cap=1.5))
    result = gr.review(c)
    first = result['tolerance_comparison']['rows'][0]
    assert first['status'] == 'unresolved'
    assert first['candidate_id'] is None
    assert 'estimated_cost' not in first
