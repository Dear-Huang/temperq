from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qwb import daily, metrics, taskcard

EX = Path(__file__).resolve().parents[1] / "examples"
TZ = "Asia/Shanghai"


def synth(n_days=320, n_codes=60, seed=0, signal=0.0):
    """Synthetic bars; `signal` ties the open[t+1]->open[t+2] label to r1 on day t.

    open ~ close, so that label is roughly ret[t+2]; the dependence is planted
    at lag 2 (a lag-1 AR term would leave only signal**2 at that horizon).
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-01", periods=n_days)
    codes = [f"{i:06d}.SZ" for i in range(n_codes)]
    ret = rng.normal(0, 0.02, (n_days, n_codes))
    for t in range(2, n_days):
        ret[t] += signal * ret[t - 2]
    close = 10 * np.exp(np.cumsum(ret, axis=0))
    opn = close * np.exp(rng.normal(0, 0.002, close.shape))
    rows = []
    for i, d in enumerate(dates):
        for j, c in enumerate(codes):
            o, cl = opn[i, j], close[i, j]
            rows.append(dict(code=c, date=d, open=o, high=max(o, cl) * 1.01,
                             low=min(o, cl) * 0.99, close=cl,
                             amount=1e6 * (1 + j), adj_factor=1.0, up_limit=cl * 1.1))
    return pd.DataFrame(rows)


def test_features_do_not_cross_stocks():
    bars = synth(n_days=80, n_codes=3)
    P = daily.panel_from_bars(bars)
    raw = daily.raw_features(P)
    c0 = P["close"].columns[0]
    s = bars[bars.code == c0].set_index("date")["close"]
    np.testing.assert_allclose(raw["r5"][c0].values, (s / s.shift(5) - 1).values)
    assert raw["r5"].iloc[:5].isna().all().all()     # no value borrowed from another stock


def test_labels_follow_task_card_offsets():
    bars = synth(n_days=40, n_codes=2)
    P = daily.panel_from_bars(bars)
    card = taskcard.load(EX / "st_task.yaml")          # open[t+1] -> open[t+6]
    y = daily.labels(P, card)
    o = P["open"]
    np.testing.assert_allclose(y.iloc[3].values, (o.iloc[9] / o.iloc[4] - 1).values)
    assert y.iloc[-6:].isna().all().all()


def test_rank_ic_and_newey_west():
    rng = np.random.default_rng(1)
    y = rng.normal(size=(200, 50))
    assert np.allclose(metrics.rank_ic(y, y), 1.0)
    ic = metrics.rank_ic(rng.normal(size=y.shape), y)
    nw = metrics.newey_west(ic, lag=5)
    assert abs(nw["t"]) < 4 and nw["mde"] > 0


def test_walk_forward_finds_planted_signal_and_respects_maturity():
    pytest.importorskip("lightgbm")
    card = taskcard.load(EX / "st_task.yaml")
    card["label"]["start"] = {"day": 1, "time": "09:30", "price": "open"}
    card["label"]["end"] = {"day": 2, "time": "09:30", "price": "open"}
    P = daily.panel_from_bars(synth(n_days=520, n_codes=60, signal=-0.3))
    cfg = dict(daily.DEFAULTS, universe="all", min_age_days=20, first_test_year=2020,
               seeds=[0], num_boost_round=50,
               lgbm=dict(daily.DEFAULTS["lgbm"], min_data_in_leaf=50, num_threads=2))
    res = daily.run(P, card, cfg, "snap-test", TZ, log=lambda *_: None)
    ic = metrics.rank_ic(res["pred"][0], res["y"])
    assert np.nanmean(ic) > 0.1                       # lag-2 reversal planted at -0.3
    m = res["manifests"][0]
    # last train date 2019-12-31; its label ends t+2 > cutoff, so it is dropped
    assert pd.Timestamp(m["train_end"]) < pd.Timestamp("2019-12-31")
    assert pd.Timestamp(m["max_label_end"]) <= pd.Timestamp(m["fit_cutoff"])
    tab = daily.prediction_table(res, card, "snap-test", TZ)
    assert {"code", "decision_time", "prediction", "data_snapshot_id"} <= set(tab.columns)


def test_deviation_list():
    cfg = dict(daily.DEFAULTS, num_boost_round=100,
               lgbm=dict(daily.DEFAULTS["lgbm"], num_leaves=31))
    d = daily.deviations(cfg)
    assert any("num_boost_round" in x for x in d) and any("lgbm.num_leaves" in x for x in d)
