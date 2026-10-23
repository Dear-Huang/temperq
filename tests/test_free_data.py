"""Public SDK paging, complete snapshots, failed append and UI continuity."""
from pathlib import Path
import socket

import pandas as pd
import pytest

from qwb import daily_data, experiments, free_data, ui
from qwb.jobs import Cancelled


class Response:
    def __init__(self, frame=None, error="0"):
        self.error_code, self.error_msg = error, "test failure"
        self.fields = list(frame.columns) if frame is not None else []
        self.rows = iter(frame.values.tolist() if frame is not None else [])

    def next(self):
        self.row = next(self.rows, None)
        return self.row is not None

    def get_row_data(self):
        return self.row

    def get_data(self):
        raise AssertionError("SDK get_data breaks on paged pandas >= 2 results")


class SDK:
    def __init__(self):
        self.fail, self.missing, self.logout_called = False, False, False

    def login(self):
        return Response()

    def logout(self):
        self.logout_called = True

    def query_trade_dates(self, start, end):
        days = pd.date_range(start, end)
        return Response(pd.DataFrame({"calendar_date": days.strftime("%Y-%m-%d"),
                                      "is_trading_day": [str(int(d.weekday() < 5)) for d in days]}))

    def query_history_k_data_plus(self, symbol, fields, *, start_date, end_date, frequency, adjustflag):
        assert adjustflag == "3" and frequency == "d"
        if self.fail and symbol == "sz.000001":
            return Response(error="-1")
        days = pd.bdate_range(start_date, end_date).strftime("%Y-%m-%d").tolist()
        if self.missing:
            days = days[:-1]
        return Response(pd.DataFrame([[d, symbol, "10", "11", "9", "10", "10", "10000000", "1", "0"]
                                       for d in days], columns=fields.split(",")))

    def query_adjust_factor(self, symbol, **kwargs):
        return Response(pd.DataFrame({"dividOperateDate": ["2020-01-01", "2025-01-03"],
                                      "backAdjustFactor": ["2", "3"]}))


def codes(tmp_path):
    path = tmp_path / "codes.csv"
    path.write_text("code\n600000.SH\n000001.SZ\n", encoding="utf-8")
    return path


def test_raw_prices_paging_factors_and_idempotence(tmp_path):
    sdk, feed = SDK(), tmp_path / "feed"
    timeout = socket.getdefaulttimeout()
    result = free_data.fetch(feed, codes(tmp_path), "2025-01-02", "2025-01-03", sdk=sdk)
    assert sdk.logout_called and socket.getdefaulttimeout() == timeout
    raw = pd.read_csv(Path(result["snapshot"]) / "source.csv")
    assert set(raw.open) == {10}  # prices stay raw; only factor changes
    assert raw.loc[raw.date == "2025-01-02", "adj_factor"].eq(2).all()
    assert raw.loc[raw.date == "2025-01-03", "adj_factor"].eq(3).all()
    assert raw.up_limit.eq(11).all() and raw.down_limit.eq(9).all()
    sdk.login = lambda: pytest.fail("already current: no network needed")
    assert free_data.fetch(feed, codes(tmp_path), "2025-01-02", "2025-01-03", sdk=sdk)["added_rows"] == 0


def test_failed_append_and_cancel_leave_published_snapshot_unchanged(tmp_path):
    sdk, feed = SDK(), tmp_path / "feed"
    codefile = codes(tmp_path)
    free_data.fetch(feed, codefile, "2025-01-02", "2025-01-03", sdk=sdk)
    before = (feed / "current.json").read_bytes()
    sdk.fail = True
    with pytest.raises(ValueError, match="BaoStock -1"):
        free_data.fetch(feed, codefile, "2025-01-02", "2025-01-06", sdk=sdk)
    assert (feed / "current.json").read_bytes() == before
    sdk.fail = False
    sdk.missing = True
    with pytest.raises(ValueError, match="行情缺日"):
        free_data.fetch(feed, codefile, "2025-01-02", "2025-01-06", sdk=sdk)
    assert (feed / "current.json").read_bytes() == before
    sdk.missing = False
    def cancel(done, total, message):
        raise Cancelled()
    with pytest.raises(Cancelled):
        free_data.fetch(feed, codefile, "2025-01-02", "2025-01-06", sdk=sdk, progress=cancel)
    assert (feed / "current.json").read_bytes() == before
    result = free_data.fetch(feed, codefile, "2025-01-02", "2025-01-06", sdk=sdk)
    assert result["added_rows"] == 2
    assert experiments.read(daily_data.current(feed) / "snapshot.json")["end"] == "2025-01-06"


def test_partial_initial_download_does_not_publish(tmp_path):
    sdk = SDK()
    sdk.fail = True
    with pytest.raises(ValueError):
        free_data.fetch(tmp_path / "feed", codes(tmp_path), "2025-01-02", "2025-01-03", sdk=sdk)
    assert not (tmp_path / "feed" / "current.json").exists()


def test_truncated_calendar_is_rejected(tmp_path):
    sdk = SDK()
    query = sdk.query_trade_dates
    sdk.query_trade_dates = lambda start, end: query(start, "2025-01-02")
    with pytest.raises(ValueError, match="交易日历不完整"):
        free_data.fetch(tmp_path / "feed", codes(tmp_path), "2025-01-02", "2025-01-03", sdk=sdk)
    assert sdk.logout_called
    assert not (tmp_path / "feed" / "current.json").exists()


def test_append_preserves_holiday_before_first_bar(tmp_path):
    feed, sdk = tmp_path / "feed", SDK()
    codefile = codes(tmp_path)
    free_data.fetch(feed, codefile, "2025-01-04", "2025-01-06", sdk=sdk)
    result = free_data.fetch(feed, codefile, "2025-01-04", "2025-01-07", sdk=sdk)
    assert result["added_rows"] == 2
    calendar = pd.read_csv(Path(result["snapshot"]) / "calendar.csv")
    assert calendar.date.min() == "2025-01-04"


def test_ui_free_download_persistent_worker_and_selection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    original = free_data.fetch
    def fake(feed, **kwargs):
        return original(feed, codes(tmp_path), "2025-01-02", "2025-01-03", sdk=SDK(),
                        log=kwargs["log"], progress=kwargs["progress"], check_cancel=kwargs["check_cancel"])
    monkeypatch.setattr(free_data, "fetch", fake)
    app = ui.create_app(tmp_path, runner=lambda fn: fn())
    client = TestClient(app)
    assert "一键下载免费数据（BaoStock）" in client.get("/").text
    assert client.post("/data/free", data={"token": "bad"}).status_code == 403
    assert "至少下载 3 年" in client.post("/data/free", data={"token": app.state.token, "start": "2026-01-01"}).text
    response = client.post("/data/free", data={"token": app.state.token}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "完成" in page and "/?ds=free_daily#step3" in page
    assert "不能代表全市场表现" in page
    job_id = response.headers["location"].split("/")[-1]
    assert app.state.store.spec(job_id)["config"]["action"] == "free_data"
    assert "free_daily" in ui.datasets(tmp_path)


def test_refresh_can_request_today_only_after_close_and_preserves_symbol_list(tmp_path):
    sdk,feed=SDK(),tmp_path/'feed'
    free_data.fetch(feed,codes(tmp_path),'2025-01-02','2025-01-03',sdk=sdk)
    assert free_data.completed_day(now='2025-01-06T17:29:00+08:00',allow_today=True)=='2025-01-05'
    assert free_data.completed_day(now='2025-01-06T17:30:00+08:00',allow_today=True)=='2025-01-06'
    with pytest.raises(ValueError):
        free_data.fetch(feed,codes(tmp_path),'2025-01-02','2025-01-06',sdk=sdk,
            now='2025-01-06T17:29:00+08:00',allow_today=True)
    sdk.missing=True
    before=(feed/'current.json').read_bytes()
    with pytest.raises(ValueError,match='行情缺日'):
        free_data.refresh_existing(feed,now='2025-01-06T17:30:00+08:00',sdk=sdk)
    assert (feed/'current.json').read_bytes()==before
    sdk.missing=False
    result=free_data.refresh_existing(feed,now='2025-01-06T18:00:00+08:00',sdk=sdk)
    assert result['added_rows']==2
    sdk.login=lambda:pytest.fail('current or closed sessions need no network')
    assert free_data.refresh_existing(feed,now='2025-01-06T19:00:00+08:00',sdk=sdk)['added_rows']==0
    assert free_data.refresh_existing(feed,now='2025-01-05T18:00:00+08:00',sdk=sdk)['state']=='UP_TO_DATE'


def test_refresh_skips_holiday_and_never_replaces_external_feed(tmp_path):
    feed=tmp_path/'feed';sdk=SDK()
    free_data.fetch(feed,codes(tmp_path),'2025-01-02','2025-01-03',sdk=sdk)
    sdk.login=lambda:pytest.fail('weekend is already covered by authoritative calendar')
    assert free_data.refresh_existing(feed,now='2025-01-04T18:00:00+08:00',sdk=sdk)['state']=='MARKET_CLOSED'
    meta_path=daily_data.current(feed)/'snapshot.json'
    meta=experiments.read(meta_path);meta['source']='user_csv';experiments.write(meta_path,meta)
    assert free_data.refresh_existing(feed,now='2025-01-06T18:00:00+08:00',sdk=sdk)['state']=='EXTERNAL_FEED'
