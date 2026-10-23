from pathlib import Path

import pandas as pd
import pytest

from qwb import daily_operation, experiments, full_market, paper, pipeline
from qwb.connectors import cn_a
from test_daily_strategy import fixture


class DirectCache:
    def get(self, api, params, fetch):
        return fetch()


def responses():
    codes = ["000001.SZ", "600000.SH"]
    base = {"ts_code": codes, "trade_date": ["20261022"] * 2}
    return {
        "daily": pd.DataFrame(dict(base, open=[10., 10.], high=[11., 11.], low=[9., 9.],
                                   close=[10., 10.], pre_close=[10., 10.], vol=[100., 100.], amount=[100., 100.])),
        "adj_factor": pd.DataFrame(dict(base, adj_factor=[1., 1.])),
        "stk_limit": pd.DataFrame(dict(base, up_limit=[11., 11.], down_limit=[9., 9.])),
        "stock_st": pd.DataFrame({"ts_code": [], "trade_date": [], "name": []}),
        "suspend_d": pd.DataFrame(),
    }


def fetch(tables, minimum=2):
    class Client:
        def call(self, api, **kwargs):
            return tables[api].copy()

        def paged(self, api, *args, **kwargs):
            return self.call(api)
    return full_market.daily_tables(Client(), DirectCache(), "2026-10-22", min_stocks=minimum)


def test_full_market_normalizes_units_and_accepts_multiple_suspend_events():
    tables = responses()
    tables["suspend_d"] = pd.DataFrame({"ts_code": ["000002.SZ"] * 2,
        "trade_date": ["20261022"] * 2, "suspend_type": ["S", "R"]})
    bars, status = fetch(tables)
    assert bars.loc[bars.code.eq("000001.SZ"), "vol"].item() == 10000
    assert bars.loc[bars.code.eq("000001.SZ"), "amount"].item() == 100000
    assert bars.loc[bars.code.eq("000002.SZ"), "state"].item() == "suspended"
    assert status.empty


@pytest.mark.parametrize("kind", ["short", "wrong_date", "duplicate", "missing_factor"])
def test_full_market_rejects_incomplete_or_wrong_data(kind):
    tables = responses()
    if kind == "short":
        tables["daily"] = tables["daily"].iloc[:1]
    elif kind == "wrong_date":
        tables["daily"]["trade_date"] = "20261021"
    elif kind == "duplicate":
        tables["daily"] = pd.concat([tables["daily"], tables["daily"].iloc[:1]])
    else:
        tables["adj_factor"] = tables["adj_factor"].iloc[:1]
    with pytest.raises(ValueError):
        fetch(tables)


def bound_account(tmp_path, monkeypatch):
    run, _ = fixture(tmp_path, monkeypatch)
    paper.initialize(tmp_path, "live", run, 100000, [])
    feed = tmp_path / "feed"
    snapshot = feed / "snapshots" / "current"
    snapshot.mkdir(parents=True)
    experiments.write(snapshot / "snapshot.json", {"data_snapshot_id": "new", "end": "2026-10-23"})
    experiments.write(feed / "current.json", {"snapshot": "snapshots/current"})
    pd.DataFrame({"date": ["2026-10-22", "2026-10-23", "2026-10-26"], "is_open": [1, 1, 1]}) \
        .to_csv(snapshot / "calendar.csv", index=False)
    daily_operation.activate_unstarted(tmp_path, "live", feed)
    return feed, snapshot


def test_bind_preserves_original_and_refuses_started_account(tmp_path, monkeypatch):
    feed, _ = bound_account(tmp_path, monkeypatch)
    root = paper.location(tmp_path, "live")
    config = experiments.read(root / "strategy.json")
    assert Path(config["data"]["dir"]) == feed.resolve()
    old = experiments.read(next((root / "config_history").glob("*.json")))
    assert old["data"]["dir"] != config["data"]["dir"]
    with paper.connection(root) as con:
        con.execute("INSERT INTO sessions VALUES (?,?)", ("2026-10-23", "{}"))
    with pytest.raises(ValueError, match="already has records"):
        daily_operation.activate_unstarted(tmp_path, "live", feed)


@pytest.mark.parametrize("failure", ["error", "stale"])
def test_failed_or_stale_update_never_triggers_ledger(tmp_path, monkeypatch, failure):
    feed, snapshot = bound_account(tmp_path, monkeypatch)
    def update(*args):
        if failure == "error":
            raise RuntimeError("private endpoint details must not escape")
        experiments.write(snapshot / "snapshot.json", {"data_snapshot_id": "old", "end": "2026-10-22"})
        return {"end": "2026-10-22"}
    monkeypatch.setattr(full_market, "update", update)
    monkeypatch.setattr(paper, "tick", lambda *a, **kw: pytest.fail("stale data triggered ledger"))
    result = daily_operation.run(tmp_path, "live", feed, now="2026-10-23T17:30:00+08:00")
    assert result["state"] == "BLOCKED" and not result["ledger_triggered"]
    assert "private endpoint" not in str(result)
    assert len(list((paper.location(tmp_path, "live") / "operation_history").glob("*.json"))) == 1


def test_fresh_update_triggers_once_and_keeps_attempt_history(tmp_path, monkeypatch):
    feed, _ = bound_account(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(full_market, "update", lambda *a: {"end": "2026-10-23"})
    def tick(*args, **kwargs):
        calls.append(kwargs["now"])
        return {"state": "RUNNING", "message": "ok", "new_sessions": int(len(calls) == 1)}
    monkeypatch.setattr(paper, "tick", tick)
    for _ in range(2):
        result = daily_operation.run(tmp_path, "live", feed, now="2026-10-23T17:30:00+08:00")
    assert len(calls) == 2 and result["new_sessions"] == 0 and result["ledger_triggered"]
    assert len(list((paper.location(tmp_path, "live") / "operation_history").glob("*.json"))) == 2


def test_missed_dates_distinguish_failed_absent_and_late_records(tmp_path, monkeypatch):
    feed, snapshot = bound_account(tmp_path, monkeypatch)
    account = paper.inspect(tmp_path, "live")
    account["config"]["start_date"] = "2026-10-22"
    account["sessions"] = [{"day": "2026-10-22", "scope": "forward_paper"},
                           {"day": "2026-10-23", "scope": "late_reconstruction"}]
    monkeypatch.setattr(paper, "inspect", lambda *a: account)
    daily_operation.record_attempt(tmp_path, "live", {"attempted_at": "2026-10-23T17:30:00+08:00",
        "state": "BLOCKED", "message": "接口更新失败"})
    status = daily_operation.coverage(tmp_path, "live", now="2026-10-26T18:30:00+08:00")
    assert [r["day"] for r in status["missed"]] == ["2026-10-23", "2026-10-26"]
    assert status["missed"][0]["record_type"] == "晚到补记"
    assert status["missed"][0]["reason"] == "接口更新失败"
    assert "具体原因未知" in status["missed"][1]["reason"]
    assert not daily_operation.coverage(tmp_path, "live", now="2026-10-23T17:30:00+08:00")["missed"]


def test_append_keeps_old_snapshot_and_st_intervals(tmp_path, monkeypatch):
    base = tmp_path / "original"
    (base / "bars").mkdir(parents=True)
    bars = pipeline.synth_bars(2, 2)
    bars["code"] = ["000001.SZ", "600000.SH"] * 2
    bars["date"] = pd.to_datetime(["2026-10-20"] * 2 + ["2026-10-21"] * 2)
    bars.to_parquet(base / "bars" / "2026.parquet", index=False)
    st = cn_a.st_status(pd.DataFrame({"code": ["600000.SH"] * 2, "name": ["ST test"] * 2,
        "date": pd.to_datetime(["2026-10-20", "2026-10-21"])}), pd.DatetimeIndex(["2026-10-20", "2026-10-21"]))
    st.to_parquet(base / "st_status.parquet", index=False)
    experiments.write(base / "snapshot.json", {"start": "2026-10-20", "end": "2026-10-21",
        "trading_days": 2, "data_snapshot_id": "original"})
    old_hash = experiments.digest(base / "bars" / "2026.parquet")
    monkeypatch.setattr(pipeline, "find_local_config", lambda *a: {"data_dir": str(base),
        "cache_dir": str(tmp_path / "cache"), "tushare": {"token_file": "unused", "url": "unused"}})
    monkeypatch.setattr(full_market, "load_token", lambda *a: "test")
    class CalendarClient:
        def __init__(self, *a, **kw):
            pass
        def call(self, *a, **kw):
            dates = pd.date_range("2026-10-20", "2026-11-05")
            return pd.DataFrame({"cal_date": dates.strftime("%Y%m%d"), "is_open": (dates.weekday < 5).astype(int)})
    monkeypatch.setattr(full_market, "TushareClient", CalendarClient)
    fresh, status = fetch(responses())
    monkeypatch.setattr(full_market, "daily_tables", lambda *a: (fresh, status))
    feed = tmp_path / "feed"
    first = full_market.update(tmp_path, feed, "2026-10-22", log=lambda *a, **kw: None)
    assert first["end"] == "2026-10-22" and first["new_sessions"] == 1
    assert experiments.digest(base / "bars" / "2026.parquet") == old_hash
    appended = pd.read_parquet(Path(first["snapshot"]) / "bars" / "2026.parquet")
    assert len(appended) == 6
    updated_st = pd.read_parquet(Path(first["snapshot"]) / "st_status.parquet")
    assert str(updated_st.iloc[0].end)[:10] == "2026-10-22"
    second = full_market.update(tmp_path, feed, "2026-10-22", log=lambda *a, **kw: None)
    assert first["snapshot"] == second["snapshot"] and second["new_sessions"] == 0
