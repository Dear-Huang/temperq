from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from qwb import daily, daily_data, experiments, paper, pipeline, strategy, ui
from qwb.portfolio.reference import Fees, execute_session


D0 = "2026-10-19T15:30:00+08:00"
O1 = "2026-10-20T09:30:00+08:00"
C1 = "2026-10-20T15:00:00+08:00"
O2 = "2026-10-21T09:30:00+08:00"


def test_comparison_protocol_matches_frozen_lock():
    lock = experiments.read(Path(__file__).parents[1] / "bench" / "protocols" / "daily_strategy_v1.lock.json")
    assert strategy.protocol()[1] == lock["protocol_hash"]


def execute(cash, holdings, weights, *, code="688001.SH", price=10):
    state = strategy.account(cash, holdings, {code: price}, observed_at=D0,
                             snapshot_id="test", locked_until=O2)
    target, assets, universe_id = strategy.target_for(state, weights, execution_at=O1, recipe_hash="test")
    bar = strategy.session_bar(code, open_price=price, close_price=price, upper=price*1.2, lower=price*.8)
    return execute_session(state, target, (bar,), open_at=O1, close_at=C1, next_open_at=O2,
                           decision_assets=assets, universe_id=universe_id, protocol_id="test", fees=Fees())


@pytest.mark.parametrize("cash,expected", [(1995, 0), (2005, 200), (2015, 201), (3995, 399)])
def test_star_minimum_and_single_share_increment(cash, expected):
    after, audit = execute(cash, [], {"688001.SH": 1.0})
    assert sum(p.quantity for p in after.positions) == expected
    assert after.cash >= 0
    assert audit["total_cost"] == (5 if expected else 0)


@pytest.mark.parametrize("code", ["000001.SZ", "300001.SZ", "600000.SH"])
def test_main_and_chinext_buy_step(code):
    after, _ = execute(2015, [], {code: 1}, code=code)
    assert after.positions[0].quantity == 200


def test_t1_locked_position_and_odd_lot_sale():
    rows = [{"code": "000001.SZ", "quantity": 135, "sellable_quantity": 0}]
    state, audit = execute(20, rows, {}, code="000001.SZ")
    assert state.positions[0].quantity == 135
    assert audit["fills"][0]["reason"] == "T1_LOCKED"
    rows[0]["sellable_quantity"] = 135
    state, audit = execute(20, rows, {}, code="000001.SZ")
    assert state.positions == ()
    assert audit["fills"][0]["quantity"] == 135
    assert state.cash == pytest.approx(20 + 1350 - 5 - .675)
    rows[0].update(code="688001.SH", quantity=151, sellable_quantity=151)
    state, audit = execute(20, rows, {})
    assert audit["fills"][0]["quantity"] == 151


def test_different_actual_holdings_change_order():
    a, aa = execute(5000, [], {"688001.SH": 1})
    b, ab = execute(2990, [{"code": "688001.SH", "quantity": 201, "sellable_quantity": 201}], {"688001.SH": 1})
    assert aa["fills"][0]["quantity"] != ab["fills"][0]["quantity"]
    assert a.equity == b.equity == 4995


@pytest.mark.parametrize("raw", [b"code,quantity,sellable_quantity\n830001.BJ,100,100\n",
    b"code,quantity,sellable_quantity\n000001.SZ,100,101\n",
    b"code,quantity,sellable_quantity\n000001.SZ,1.5,1\n",
    b"code,quantity,sellable_quantity,event\n000001.SZ,100,100,split\n",
    b"code,quantity,sellable_quantity\n000001.SZ,100,100\n000001,100,100\n"])
def test_unsupported_and_invalid_accounts_rejected(raw):
    with pytest.raises(ValueError):
        strategy.read_holdings(raw)


def fixture(tmp_path, monkeypatch):
    dates = pd.bdate_range("2026-07-01", "2026-10-30")
    codes = ["000001.SZ", "600000.SH", "688001.SH"]
    bars = pipeline.synth_bars(len(dates), len(codes))
    bars["date"] = np.repeat(dates, len(codes))
    bars["code"] = np.tile(codes, len(dates))
    bars["down_limit"] = bars["close"] * .8
    bars["up_limit"] = bars["close"] * 1.2
    panel = daily.panel_from_bars(bars)
    model = tmp_path / "runs" / "model" / "final_model"
    model.mkdir(parents=True)
    names = daily.feature_names(daily.features(panel))
    cfg = deepcopy(daily.DEFAULTS)
    cfg.update(universe="all", min_age_days=1, min_amount_quantile=0, portfolio={"n_hold": 1})
    card = yaml.safe_load((pipeline.Path(__file__).parents[1] / "examples" / "cn_rank_5d.yaml").read_text("utf-8"))
    meta = {"model": "ridge", "files": ["ridge.json"], "features": names, "train_end": "2026-09-30",
            "data_end": "2026-10-01", "snapshot_id": "fixture", "card": card,
            "cfg": cfg, "data": {"source": "snapshot", "dir": str(tmp_path / "data")}}
    experiments.write(model / "meta.json", meta)
    experiments.write(model / "ridge.json", {"coef": [0.0] * (len(names)+1)})
    (tmp_path / "data").mkdir()
    pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "is_open": 1}).to_csv(tmp_path / "data" / "calendar.csv", index=False)
    monkeypatch.setattr(pipeline, "load_data", lambda *_: (panel, "fixture", None, None))
    monkeypatch.setattr(strategy.picks, "_loaded", lambda *_: (panel, None, None, daily.features(panel)))
    return model.parent, panel


def test_ledger_restart_idempotence_and_rebalance(tmp_path, monkeypatch):
    run, panel = fixture(tmp_path, monkeypatch)
    paper.initialize(tmp_path, "daily", run, 100000, [], replay_start="2026-10-19")
    result = paper.tick(tmp_path, "daily", now="2026-10-27T16:00:00+08:00")
    assert result["new_sessions"] == 7
    history = paper.inspect(tmp_path, "daily")["sessions"]
    assert history[0]["next_weights"] and history[5]["next_weights"]
    assert all(r["next_weights"] is None for r in history[1:5])
    assert history[1]["audit"]["fills"][0]["quantity"] > 0
    assert all(r["state"]["cash"] >= 0 for r in history)
    again = paper.tick(tmp_path, "daily", now="2026-10-27T16:00:00+08:00")
    assert again["new_sessions"] == 0
    assert paper.inspect(tmp_path, "daily")["sessions"] == history


def test_corporate_action_aborts_without_partial_state(tmp_path, monkeypatch):
    run, panel = fixture(tmp_path, monkeypatch)
    paper.initialize(tmp_path, "blocked", run, 100000, [], replay_start="2026-10-19")
    paper.tick(tmp_path, "blocked", now="2026-10-19T16:00:00+08:00")
    before = paper.inspect(tmp_path, "blocked")["sessions"]
    panel["adj_factor"].loc["2026-10-20", "000001.SZ"] = 2
    with pytest.raises(ValueError, match="公司行动"):
        paper.tick(tmp_path, "blocked", now="2026-10-20T16:00:00+08:00")
    assert paper.inspect(tmp_path, "blocked")["sessions"] == before


def test_config_change_rejected_and_future_not_backfilled(tmp_path, monkeypatch):
    run, _ = fixture(tmp_path, monkeypatch)
    paper.initialize(tmp_path, "forward", run, 100000, [])
    paper.tick(tmp_path, "forward", now="2026-10-22T16:00:00+08:00")
    assert not paper.inspect(tmp_path, "forward")["sessions"]
    path = paper.location(tmp_path, "forward") / "strategy.json"
    config = experiments.read(path)
    config["cash"] = 200000
    experiments.write(path, config)
    with pytest.raises(ValueError, match="配置被修改"):
        paper.tick(tmp_path, "forward", now="2026-10-26T16:00:00+08:00")


def test_account_ui_and_download(tmp_path, monkeypatch):
    run, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(experiments, "frozen_source", lambda source: source)
    app = ui.create_app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/models/model/account")
        assert page.status_code == 200 and "可用现金" in page.text
        assert client.post("/models/model/account", data={"token": "bad"}).status_code == 403
        response = client.post("/models/model/account", data={"token": app.state.token, "cash": "100000",
                              "execution_day": "2026-11-02", "supported_only": "yes"}, follow_redirects=False)
        assert response.status_code == 303, response.text
        report = client.get(response.headers["location"])
        assert "建议增减" in report.text and "尚未成交" in report.text
        assert client.get(response.headers["location"] + "/orders.csv").content.startswith(b"\xef\xbb\xbf")
        frozen = client.get(response.headers["location"] + "/protocol.json").json()
        assert experiments.object_digest(frozen["recipe"]) == frozen["protocol_hash"]


def test_preview_follows_feed_and_selected_features(tmp_path, monkeypatch):
    run, panel = fixture(tmp_path, monkeypatch)
    feed = tmp_path / "feed"
    old, new = feed / "snapshots" / "old", feed / "snapshots" / "new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    experiments.write(old / "snapshot.json", {"data_snapshot_id": "old"})
    experiments.write(new / "snapshot.json", {"data_snapshot_id": "new"})
    experiments.write(feed / "current.json", {"snapshot": "snapshots/new"})
    path = run / "final_model" / "meta.json"
    meta = experiments.read(path)
    meta["data"]["dir"] = str(old)
    meta["features"] = meta["cfg"]["features"] = ["r5", "r20"]
    experiments.write(path, meta)
    experiments.write(path.parent / "ridge.json", {"coef": [0, 0, 0]})
    def loaded(model, cwd):
        assert pipeline.Path(model["data"]["dir"]) == new.resolve()
        return panel, None, None, daily.features(panel)
    monkeypatch.setattr(strategy.picks, "_loaded", loaded)
    advice = strategy.preview(run, tmp_path, 100000, [], "2026-11-02")
    assert advice["input_snapshot_id"] == "new" and advice["rows"]


def test_snapshot_append_keeps_previous_and_rejects_revisions(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=8)
    bars = pipeline.synth_bars(len(dates), 2)
    bars["date"] = np.repeat(dates, 2)
    bars["code"] = np.tile(["000001.SZ", "600000.SH"], len(dates))
    bars["down_limit"] = bars["close"] * .9
    bars["is_st"] = 0
    calendar = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "is_open": 1}).to_csv(index=False).encode()
    feed = tmp_path / "feed"
    first = daily_data.update(feed, bars.iloc[:8].to_csv(index=False).encode(), calendar)
    previous_hash = experiments.digest(pipeline.Path(first["snapshot"]) / "bars" / "2024.parquet")
    second = daily_data.update(feed, bars.iloc[8:].to_csv(index=False).encode(), calendar)
    assert first["snapshot"] != second["snapshot"]
    assert experiments.digest(pipeline.Path(first["snapshot"]) / "bars" / "2024.parquet") == previous_hash
    assert experiments.read(daily_data.current(feed) / "snapshot.json")["rows"] == 16
    repeat = daily_data.update(feed, bars.iloc[8:].to_csv(index=False).encode(), calendar)
    assert repeat["added_rows"] == 0
    changed = bars.iloc[8:].copy()
    changed.iloc[0, changed.columns.get_loc("close")] += .1
    with pytest.raises(ValueError, match="历史数据修订"):
        daily_data.update(feed, changed.to_csv(index=False).encode(), calendar)


def test_calendar_gap_and_model_change_stop_ledger(tmp_path, monkeypatch):
    run, _ = fixture(tmp_path, monkeypatch)
    paper.initialize(tmp_path, "missing", run, 100000, [], replay_start="2026-10-19")
    paper.tick(tmp_path, "missing", now="2026-10-19T16:00:00+08:00")
    con_root = paper.location(tmp_path, "missing")
    (con_root / "final_model" / "ridge.json").write_text('{"coef":[1]}', encoding="utf-8")
    with pytest.raises(ValueError, match="冻结模型"):
        paper.tick(tmp_path, "missing", now="2026-10-20T16:00:00+08:00")
    assert len(paper.inspect(tmp_path, "missing")["sessions"]) == 1
