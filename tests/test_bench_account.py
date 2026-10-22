"""Hand-calculated accounting cases, including locked quantities and audit failures."""

import numpy as np
import pandas as pd
import pytest

from qwb.bench import account, economics


def panel(prices, factors=None):
    values = np.asarray(prices, dtype=float)
    dates = pd.bdate_range("2023-08-25", periods=len(values))
    codes = [f"stock{i}" for i in range(values.shape[1])]
    def frame(x):
        return pd.DataFrame(x, index=dates, columns=codes)
    return {"open": frame(values), "close": frame(values),
            "adj_factor": frame(np.ones_like(values) if factors is None else factors),
            "up_limit": frame(values * 1.1), "down_limit": frame(values * 0.9)}


def replay(p, targets, cash=100, commission=0, spread=None, normal=None, extra=0):
    shape = p["close"].shape
    return account.replay(p, np.ones(shape, dtype=bool) if normal is None else normal,
        np.zeros(shape) if spread is None else spread, targets,
        start=0, end=shape[0] - 1, initial_cash=cash, commission_bp=commission,
        extra_slippage_bp=extra)


def ledger_frames(result, cash=100):
    frames = []
    for frame in result:
        frame = frame.copy()
        frame["scenario"], frame["strategy"] = "base", "equal_weight"
        frames.append(frame)
    return frames, {"account": dict(account.DEFAULT_ACCOUNT, initial_cash=cash)}


def test_gold_cash_fees_stamp_and_timing():
    p = panel([[999], [10], [12]])
    path, fills, held = replay(p, {0: [1], 1: [0]}, cash=102, commission=100,
                               spread=np.full((3, 1), 100))
    # 102 buys exactly 10 units at 10 plus 2 in fees. Sell at 12 after tax cut.
    assert path["equity"].tolist() == pytest.approx([102, 100, 117.54])
    assert fills["notional"].tolist() == pytest.approx([100, 120])
    assert fills["stamp_tax"].tolist() == pytest.approx([0, .06])
    assert fills["total_cost"].sum() == pytest.approx(4.46)
    assert (fills["decision_date"] < fills["date"]).all()
    assert fills.iloc[0]["raw_open"] == 10  # never execute on decision bar's 999
    assert held.iloc[0]["units"] == pytest.approx(10)
    frames, cfg = ledger_frames((path, fills, held), cash=102)
    economics.verify_ledger(*frames, cfg)
    m = account.metrics(path, 102)
    assert m["total_return"] == pytest.approx(117.54 / 102 - 1)
    assert m["max_drawdown"] == pytest.approx(100 / 102 - 1)


def test_locked_position_quantity_cannot_be_reweighted_and_buys_need_cash():
    p = panel([[10, 10], [10, 10], [9, 10], [12, 10]])
    p["down_limit"].iloc[2, 0] = 9
    path, fills, held = replay(p, {0: [1, 0], 1: [0, 1], 2: [.5, .5]})
    locked = held[held["date"] == p["close"].index[2]]
    assert locked["units"].tolist() == [10] and locked["code"].tolist() == ["stock0"]
    orders = fills[fills["date"] == p["close"].index[2]]
    assert set(orders["reason"]) == {"LIMIT_DOWN", "CASH_SCALED"}
    assert orders["notional"].sum() == 0 and path.iloc[2]["equity"] == 90
    end = held[held["date"] == p["close"].index[3]]
    assert end["units"].tolist() == pytest.approx([5, 5.997])
    frames, cfg = ledger_frames((path, fills, held))
    economics.verify_ledger(*frames, cfg)


@pytest.mark.parametrize("reason", ["LIMIT_UP", "NON_NORMAL_OR_UNKNOWN_STATE", "MISSING_LIMITS", "MISSING_SPREAD", "MISSING_OPEN_OR_FACTOR"])
def test_failed_buy_keeps_cash_and_exposes_reason(reason):
    p = panel([[10], [10], [10]])
    normal = np.ones((3, 1), dtype=bool)
    spread = np.zeros((3, 1))
    if reason == "LIMIT_UP":
        p["up_limit"].iloc[1, 0] = 10
    elif reason == "NON_NORMAL_OR_UNKNOWN_STATE":
        normal[1, 0] = False
    elif reason == "MISSING_LIMITS":
        p["down_limit"].iloc[1, 0] = np.nan
    elif reason == "MISSING_SPREAD":
        spread[1, 0] = np.nan
    else:
        p["open"].iloc[1, 0] = np.nan
    path, fills, held = replay(p, {0: [1]}, normal=normal, spread=spread)
    assert (path["equity"] == 100).all() and (path["cash"] == 100).all()
    assert held.empty and fills.iloc[0]["reason"] == reason


def test_total_return_units_and_corporate_action_are_not_raw_shares():
    p = panel([[10], [10], [5], [5]], [[1], [1], [2], [2]])
    path, fills, held = replay(p, {0: [1], 2: [0]})
    assert path["equity"].tolist() == pytest.approx([100, 100, 100, 99.95])
    assert held["units"].tolist() == [10, 10]
    assert fills.iloc[-1]["notional"] == 100 and fills.iloc[-1]["raw_open"] == 5


def test_missing_held_mark_is_flagged_and_no_future_price_backfill():
    p = panel([[10], [10], [10], [20]])
    p["close"].iloc[2, 0] = np.nan
    path, fills, held = replay(p, {0: [1]})
    assert path.iloc[2]["equity"] == 100 and path.iloc[2]["stale_close_names"] == 1
    assert held.iloc[1]["adjusted_mark"] == 10 and held.iloc[1]["stale"]
    p["close"].iloc[3, 0] = 999
    later, _, _ = replay(p, {0: [1]})
    pd.testing.assert_frame_equal(path.iloc[:3], later.iloc[:3])
    frames, cfg = ledger_frames((path, fills, held))
    cfg["statistics"] = dict(block_days=1, resamples=100, seed=0, min_days=2)
    summary = economics.summarize(frames[0], cfg)["base"]["equal_weight"]
    assert summary["qualification"] == "DIAGNOSTIC_ONLY"


def test_no_trade_sharpe_null_and_terminal_holding_not_liquidated():
    p = panel([[10], [10], [11]])
    path, fills, held = replay(p, {})
    assert fills.empty and held.empty
    m = account.metrics(path, 100)
    assert m["sharpe"] is None and m["sharpe_reason"]
    path, fills, held = replay(p, {0: [1]})
    assert len(fills) == 1 and fills.iloc[0]["side"] == "BUY"
    assert path.iloc[-1]["equity"] == 110 and path.iloc[-1]["cash"] == 0


def test_gold_independent_metric_definitions():
    path, _, _ = replay(panel([[10], [10], [11], [9.9]]), {0: [1]})
    # Daily account returns are [0, +10%, -10%], not 0% cumulative.
    m = account.metrics(path, 100)
    assert m["total_return"] == pytest.approx(-.01)
    assert m["cagr"] == pytest.approx(.99 ** 84 - 1)
    assert m["max_drawdown"] == pytest.approx(-.1)
    assert m["annual_volatility"] == pytest.approx(.1 * np.sqrt(252))
    assert m["sharpe"] == pytest.approx(0, abs=1e-12)


@pytest.mark.parametrize("weights", [[-1], [1.1], [np.nan], [1, 0]])
def test_invalid_target_rejected(weights):
    with pytest.raises(ValueError, match="target"):
        replay(panel([[10], [10], [10]]), {0: weights})


@pytest.mark.parametrize("tamper", ["cash", "units", "cost", "return"])
def test_auditor_rejects_inconsistent_cash_quantities_cost_and_return(tamper):
    frames, cfg = ledger_frames(replay(panel([[10], [10], [12]]), {0: [1]}))
    if tamper == "cash":
        frames[0].loc[1, "cash"] += 1
    elif tamper == "units":
        frames[2].loc[0, "units"] += 1
    elif tamper == "cost":
        frames[1].loc[0, "total_cost"] += 1
    else:
        frames[0].loc[1, "net_return"] += .01
    with pytest.raises(ValueError, match="reconcile"):
        economics.verify_ledger(*frames, cfg)


def test_targets_use_decision_universe_and_require_complete_predictions():
    u = np.ones((3, 20), dtype=bool)
    score = np.tile(np.arange(20), (3, 1)).astype(float)
    target = economics.targets_for(score, u, [0], .1)[0]
    assert np.flatnonzero(target).tolist() == [18, 19]
    score[0, 0] = np.nan
    with pytest.raises(ValueError, match="incomplete"):
        economics.targets_for(score, u, [0], .1)
