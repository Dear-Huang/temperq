import numpy as np
import pandas as pd
import pytest

from qwb import daily, pipeline, runconfig, scenario


def test_capital_tiers_follow_user_bands():
    labels = [t["label"] for t in scenario.TIERS.values()]
    assert labels == ["0–30 万", "30–100 万", "100–600 万", "600 万–1000 万",
                      "1000 万–1 亿", "1 亿–5 亿", "5 亿及以上"]
    d = scenario.derive("1e_5e")                     # 5 亿 over 100 names, 5% of ADV
    assert d["n_hold"] == 100 and d["min_adv_yuan"] == pytest.approx(5e8 / 100 / 0.05)
    # more money never loosens the liquidity floor
    floors = [scenario.derive(k)["min_adv_yuan"] for k in scenario.TIERS]
    assert floors == sorted(floors)


def test_task_card_per_horizon_is_valid():
    from qwb import taskcard
    for h in scenario.HORIZONS:
        card = scenario.task_card(h)
        assert taskcard.validate(card).ok
        assert card["label"]["end"]["day"] - card["label"]["start"]["day"] == h


def _panel(n_days=40, codes=("000001.SZ", "000002.SZ", "600000.SH")):
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    one = pd.DataFrame(10.0, index=dates, columns=list(codes))
    P = {k: one.copy() for k in ("open", "high", "low", "close")}
    P["amount"] = one * 0 + np.array([1e7, 1e8, 1e9])      # 1000 万 / 1 亿 / 10 亿 per day
    P["adj_factor"], P["up_limit"] = one * 0 + 1, one * 1.1
    return P


def test_members_use_latest_snapshot_on_or_before_the_day():
    P = _panel()
    d = P["close"].index
    m = pd.DataFrame({"code": ["000001.SZ", "000002.SZ"], "index": "000300.SH",
                      "date": [str(d[0].date()), str(d[10].date())]})
    w = daily.members_wide(m, d, P["close"].columns, "000300.SH")
    assert w.loc[d[5], "000001.SZ"] and not w.loc[d[5], "000002.SZ"]   # rebalance not visible early
    assert w.loc[d[12], "000002.SZ"] and not w.loc[d[12], "000001.SZ"]


def test_capital_floor_filters_universe():
    P = _panel()
    cfg = dict(daily.DEFAULTS, min_age_days=1, min_amount_quantile=0.0, min_adv_yuan=5e8)
    U = daily.universe(P, cfg)
    assert U.iloc[-1].tolist() == [False, False, True]


def test_ridge_runs_and_is_reported(tmp_path):
    cfg = {"name": "r", "task": scenario.task_card(5), "data": {"source": "synthetic"},
           "model": {"name": "ridge"},
           "train": {"universe": "all", "min_age_days": 20, "first_test_year": 2020,
                     "final_test_years": [2021], "seeds": [0], "shuffle_null_draws": 2,
                     "portfolio": {"n_hold": 20}},
           "output": "out"}
    rc, rep = runconfig.resolve(cfg, tmp_path)
    assert rc is not None, str(rep)
    s = pipeline.run(rc, tmp_path, log=lambda *_: None)
    assert s["model_key"] == "ridge" and "ridge" in s["ic"]
    assert not any(k.startswith("lgbm") for k in s["ic"])
    assert s["ic"]["ridge"]["final_test"]["mean"] > 0          # planted reversal is linear
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "Ridge" in report and "排名前 20 只" in report


def test_unknown_universe_and_list_validation(tmp_path):
    base = {"name": "x", "task": scenario.task_card(5), "data": {"source": "synthetic"}}
    _, rep = runconfig.resolve(dict(base, train={"universe": "list"}), tmp_path)
    assert "BAD_UNIVERSE" in rep.codes("ERROR")
    _, rep = runconfig.resolve(dict(base, train={"universe": "hs300"}), tmp_path)
    assert "BAD_UNIVERSE" in rep.codes("ERROR")
    rc, _ = runconfig.resolve(dict(base, train={"universe": "csi300"}), tmp_path)
    assert rc is not None
