"""Authorization, availability and independent closed-loop timing replay."""

from dataclasses import replace

import pandas as pd
import pytest

from qwb import governance as g, strategy_workspace
from qwb.account_rehearsal import Action, RehearsalPlan
from qwb.governance_timing import SignalSnapshot, TimingMemory, TimingPolicy, assess, replay
from qwb.portfolio.contracts import AccountState, Position, SettlementLot, TargetWeights, TASK

D0 = "2024-01-02T15:30:00+08:00"
O1 = "2024-01-03T09:30:00+08:00"
C1 = "2024-01-03T15:00:00+08:00"
O2 = "2024-01-04T09:30:00+08:00"
A, B = "000001.SZ", "600000.SH"


def fixture():
    state = AccountState(D0, "fixture", 5000, (Position(A, 500, 500, 10, D0),))
    target = TargetWeights("switch", D0, O1, TASK, "v1", "fixture", state.state_id,
                           "AB", "IOC", ((B, .5),), .5)
    plan = RehearsalPlan(state, (Action("hold", None), Action("adjust", target)), "hold", (A, B),
                         "AB", "IOC", O1, C1, O2)
    signal = SignalSnapshot(D0, D0, "2024-01-02T15:00:00+08:00", "user-v1",
                            ((A, 0.), (B, .02)), "expected_return", 5,
                            "user-calibration-v1", "2023-12-29T15:30:00+08:00")
    return plan, signal, {A: 10, B: 10}


def gate(*, index=1, policy=None, memory=TimingMemory(), signal=None):
    plan, original, prices = fixture()
    return assess(plan, "adjust", signal or original, prices,
                  policy or TimingPolicy(allow_between=True, extra_turnover_cap=2),
                  session_index=index, memory=memory)


def test_default_waits_and_periodic_dates_remain_anchored_after_extra():
    report, memory = gate(policy=TimingPolicy())
    assert report["reason"] == "WAIT_PERIOD" and memory == TimingMemory()
    report, memory = gate()
    assert report["reason"] == "EXTRA_AUTHORIZED"
    assert memory.last_extra_session == 1 and memory.extra_count == 1
    scheduled, same = gate(index=5, memory=memory)
    assert scheduled["reason"] == "SCHEDULED" and same == memory


def test_return_gain_uses_actual_quantities_and_explicit_fee_quote():
    report, _ = gate()
    assert report["quote"]["cost"] == 12.5
    assert report["quote"]["cost_bp"] == 12.5
    assert report["quote"]["estimated_gain_bp"] == 100
    assert report["quote"]["required_gain_bp"] == 23.75


def test_ranking_is_never_interpreted_as_basis_points():
    _, snapshot, _ = fixture()
    rank = replace(snapshot, score_kind="ranking", values=((A, 1.), (B, 99999.)))
    report, _ = gate(signal=rank)
    assert report["reason"] == "UNCALIBRATED_SIGNAL" and report["quote"] is None
    assert gate(index=0, signal=rank)[0]["authorized"]


def test_locked_sale_cannot_silently_turn_switch_into_an_extra_buy():
    original, signal, prices = fixture()
    held = replace(original.state.positions[0], sellable_quantity=0,
                   unsettled=(SettlementLot(500, O2),))
    state = replace(original.state, positions=(held,))
    target = replace(original.actions[1].target, state_id=state.state_id)
    frozen = replace(original, state=state, actions=(original.actions[0], Action('adjust', target)))
    result, memory = assess(frozen, 'adjust', signal, prices,
                            TimingPolicy(allow_between=True, extra_turnover_cap=2), session_index=1)
    assert result['reason'] == 'QUOTE_INFEASIBLE'
    assert any(fill['reason'] == 'T1_LOCKED' for fill in result['quote']['fills'])
    assert memory.extra_count == 0


def test_quote_that_already_breaches_cash_floor_is_not_authorized():
    plan, signal, prices = fixture()
    result, _ = assess(replace(plan, min_cash=.5), 'adjust', signal, prices,
                        TimingPolicy(allow_between=True, extra_turnover_cap=2), session_index=1)
    assert result['reason'] == 'QUOTE_INFEASIBLE'


@pytest.mark.parametrize("changes,reason", [
    ({"extra_turnover_cap": .5}, "TURNOVER_BUDGET"),
    ({"extra_direction": "reduce_only"}, "DIRECTION_NOT_AUTHORIZED"),
    ({"extra_direction": "add_only"}, "DIRECTION_NOT_AUTHORIZED"),
    ({"minimum_surplus_bp": 100}, "INSUFFICIENT_ESTIMATED_SURPLUS"),
    ({"evaluation_horizon": 10}, "HORIZON_MISMATCH"),
])
def test_each_authorization_limit_is_enforced(changes, reason):
    policy = replace(TimingPolicy(allow_between=True, extra_turnover_cap=2), **changes)
    report, memory = gate(policy=policy)
    assert report["reason"] == reason and not report["authorized"]
    assert memory == TimingMemory()


def test_weekly_quota_and_cooldown_are_distinct_and_attempts_consume_quota():
    report, memory = gate()
    assert report["authorized"]
    assert gate(index=2, memory=memory)[0]["reason"] == "EXTRA_LIMIT"
    policy = TimingPolicy(allow_between=True, extra_turnover_cap=2, max_extra_per_week=3)
    assert gate(index=2, memory=memory, policy=policy)[0]["reason"] == "COOLDOWN"
    # A new ISO week resets quota, but does not erase the previous session index.
    new_week_memory = replace(memory, extra_week="2023-52")
    assert gate(index=2, memory=new_week_memory)[0]["reason"] == "COOLDOWN"


def test_signal_provenance_and_missing_forecasts_are_not_filled_with_zero():
    _, snapshot, _ = fixture()
    for changes in ({"available_at": "2024-01-02T16:00:00+08:00"},
                    {"calibration_end_at": D0}, {"calibration_id": None},
                    {"available_at": "2024-01-02T15:30:00"}):
        with pytest.raises(ValueError):
            replace(snapshot, **changes)
    report, _ = gate(signal=replace(snapshot, values=((B, .02),)))
    assert report["reason"] == "INCOMPLETE_RETURN_FORECAST"
    plan, _, prices = fixture()
    assert assess(plan, "adjust", None, prices, TimingPolicy(allow_between=True),
                  session_index=1)[0]["reason"] == "WAIT_SIGNAL"


@pytest.fixture
def market():
    dates = pd.bdate_range("2024-01-02", periods=17)
    close = pd.DataFrame(10., index=dates, columns=[A, B])
    panel = {"open": close.copy(), "close": close, "up_limit": close*1.1,
             "down_limit": close*.9, "adj_factor": close*0+1}
    snapshots = {}
    for index, day in enumerate(dates):
        at = str(day.tz_localize("Asia/Shanghai")+pd.Timedelta(hours=15, minutes=30))
        values = ((A, .02), (B, 0.)) if index < 2 else ((A, 0.), (B, .02))
        snapshots[day] = SignalSnapshot(at, at, at, "fixture-v1", values, "expected_return", 5,
                                        "fixture-calibration", "2023-12-29T15:30:00+08:00")
    cfg = g.GovernanceConfig(n_hold=1, hold_until_rank=1, max_weight=1., exposure_cap=.5)
    return panel, snapshots, cfg


def run(market, timing=TimingPolicy()):
    panel, snapshots, cfg = market
    return replay(panel, snapshots, cfg, timing, start=panel["close"].index[0],
                  end=panel["close"].index[12], cash=100000)


def test_periodic_mode_matches_existing_fixed_engine_economics(market):
    panel, snapshots, cfg = market
    current = run(market)
    signals = pd.DataFrame({day: dict(s.values) for day, s in snapshots.items()}).T
    legacy = g.replay(panel, signals, cfg, mode="fixed", start=signals.index[0], end=signals.index[12],
                      initial_cash=100000, snapshot_id="fixture", samples=pd.DataFrame())
    assert [row["equity"] for row in current["path"]] == [row["equity"] for row in legacy["path"]]
    assert [row["weights"] for row in current["decisions"]] == [row["weights"] for row in legacy["decisions"]]


def test_hybrid_executes_next_session_and_does_not_restart_schedule(market):
    timing = TimingPolicy(allow_between=True, extra_turnover_cap=2)
    current = run(market, timing)
    dates = market[0]["close"].index
    extra = next(row for row in current["decisions"] if row["timing_reason"] == "EXTRA_AUTHORIZED")
    assert extra["date"] == str(dates[2].date())
    assert extra["execution_date"] == str(dates[3].date())
    scheduled = [row["date"] for row in current["decisions"] if row["timing_reason"] == "SCHEDULED"]
    assert scheduled == [str(dates[i].date()) for i in (0, 5, 10)]
    # The two policies evolve their own real holdings and cost histories.
    assert current["positions"][3]["state"] != run(market)["positions"][3]["state"]


def test_future_prices_cannot_change_prior_intent_but_can_block_real_execution(market):
    panel, snapshots, cfg = market
    timing = TimingPolicy(allow_between=True, extra_turnover_cap=2)
    original = run(market, timing)
    modified = {key: value.copy() for key, value in panel.items()}
    day = panel["close"].index[3]
    modified["open"].loc[day, B] = 11
    changed = run((modified, snapshots, cfg), timing)
    assert changed["checks"][:3] == original["checks"][:3]
    assert changed["decisions"][:2] == original["decisions"][:2]
    assert any(f["reason"] == "LIMIT_UP" for f in changed["fills"] if f["date"] == str(day.date()))


def test_missing_signal_waits_between_cycles_but_blocks_scheduled_day(market):
    panel, snapshots, cfg = market
    missing = dict(snapshots)
    del missing[panel["close"].index[2]]
    assert run((panel, missing, cfg))["checks"][2]["reason"] == "WAIT_SIGNAL"
    del missing[panel["close"].index[5]]
    with pytest.raises(ValueError, match="scheduled session"):
        run((panel, missing, cfg))


def test_new_replay_does_not_change_frozen_workspace_identity(market):
    before = strategy_workspace.implementation()
    run(market, TimingPolicy(allow_between=True, extra_turnover_cap=2))
    assert strategy_workspace.implementation() == before


@pytest.mark.parametrize("kwargs", [{"allow_between": 1}, {"period_sessions": True},
    {"cost_multiplier": .5}, {"extra_turnover_cap": -1}, {"extra_direction": "whatever"}])
def test_invalid_timing_policy_rejected(kwargs):
    with pytest.raises(ValueError):
        TimingPolicy(**kwargs)
