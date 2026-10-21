import pandas as pd

from qwb.formats import validate_frame

TZ = "Asia/Shanghai"


def bar(**over):
    row = dict(source="tushare.daily", record_id="000001.SZ|2024-01-02", revision=0,
               available_time="2024-01-02 15:30", code="000001.SZ", date="2024-01-02",
               open=10.0, high=10.5, low=9.8, close=10.2, vol=1000, state="normal")
    row.update(over)
    return row


def bar2():
    return bar(record_id="000001.SZ|2024-01-03", date="2024-01-03",
               available_time="2024-01-03 15:30")


def test_clean_bar_passes():
    rep = validate_frame(pd.DataFrame([bar(), bar2()]), "bar", TZ)
    assert rep.ok and not rep.issues, str(rep)


def test_naive_time_without_timezone_is_error():
    rep = validate_frame(pd.DataFrame([bar()]), "bar")
    assert "NAIVE_TIME" in rep.codes("ERROR")


def test_explicit_offset_needs_no_market_tz():
    rep = validate_frame(pd.DataFrame([bar(available_time="2024-01-02T15:30+08:00")]), "bar")
    assert rep.ok, str(rep)


def test_missing_common_field():
    df = pd.DataFrame([bar()]).drop(columns="record_id")
    assert "MISSING_COLUMN" in validate_frame(df, "bar", TZ).codes()


def test_halt_encoded_as_zero_is_caught():
    df = pd.DataFrame([bar(open=0, high=0, low=0, close=0, vol=0, state="suspended")])
    assert "HALT_ENCODED_AS_ZERO" in validate_frame(df, "bar", TZ).codes("ERROR")


def test_suspended_with_empty_price_is_fine():
    df = pd.DataFrame([bar(open=None, high=None, low=None, close=None, vol=0,
                           state="suspended")])
    assert validate_frame(df, "bar", TZ).ok


def test_zero_price_on_normal_row_is_error():
    df = pd.DataFrame([bar(close=0, low=0)])
    assert "PRICE_NONPOSITIVE" in validate_frame(df, "bar", TZ).codes("ERROR")


def test_zero_volume_without_state_flag_warns():
    rep = validate_frame(pd.DataFrame([bar(vol=0)]), "bar", TZ)
    assert rep.ok and "ZERO_VOLUME_NORMAL" in rep.codes("WARN")


def test_available_before_date_in_local_time():
    # 2024-01-02 01:00 Shanghai is 2024-01-01 17:00 UTC; local date must be used
    ok = validate_frame(pd.DataFrame([bar(available_time="2024-01-02 01:00")]), "bar", TZ)
    assert "AVAILABLE_BEFORE_DATE" not in ok.codes()
    bad = validate_frame(pd.DataFrame([bar(available_time="2024-01-01 20:00")]), "bar", TZ)
    assert "AVAILABLE_BEFORE_DATE" in bad.codes()


def test_duplicate_record_revision():
    assert "DUPLICATE_KEY" in validate_frame(pd.DataFrame([bar(), bar()]), "bar", TZ).codes()


def test_ohlc_inconsistent():
    assert "OHLC_INCONSISTENT" in validate_frame(pd.DataFrame([bar(close=11.0)]), "bar",
                                                 TZ).codes()


def test_revision_order():
    df = pd.DataFrame([bar(revision=0, available_time="2024-01-05 09:00"),
                       bar(revision=1, available_time="2024-01-04 09:00")])
    assert "REVISION_ORDER" in validate_frame(df, "bar", TZ).codes()


def test_bad_revision():
    assert "BAD_REVISION" in validate_frame(pd.DataFrame([bar(revision=-1)]), "bar",
                                            TZ).codes()


def obs(**over):
    row = dict(source="tushare.income", record_id="000001.SZ|2023Q4|income", revision=0,
               available_time="2024-03-15 18:00", code="000001.SZ", period_end="2023-12-31",
               revenue=1.0e10)
    row.update(over)
    return row


def test_observation_with_restatement_passes():
    df = pd.DataFrame([obs(), obs(revision=1, available_time="2024-08-20 18:00",
                                  revenue=0.9e10)])
    assert validate_frame(df, "observation", TZ).ok


def test_observation_known_before_period_end():
    df = pd.DataFrame([obs(available_time="2023-12-31 18:00")])
    assert "AVAILABLE_BEFORE_PERIOD_END" in validate_frame(df, "observation", TZ).codes()


def test_text_available_before_publish():
    df = pd.DataFrame([dict(source="news", record_id="n1", revision=0, codes="000001.SZ",
                            publish_time="2024-01-02 20:00", available_time="2024-01-02 19:00",
                            title="t", body="b")])
    assert "AVAILABLE_BEFORE_PUBLISH" in validate_frame(df, "text", TZ).codes()


def st(rid, start, end, rev=0, avail="2020-01-01 09:00", field="st"):
    return dict(source="tushare.stock_st", record_id=rid, revision=rev, available_time=avail,
                code="A", field=field, start=start, end=end, value=True)


def test_status_overlap_judged_on_latest_revision():
    # revision 1 shortens the first interval, so the two no longer overlap
    rows = [st("r1", "2020-01-01", "2021-01-01"),
            st("r1", "2020-01-01", "2020-05-01", rev=1, avail="2021-03-01 09:00"),
            st("r2", "2020-06-01", None, avail="2020-06-01 09:00")]
    assert validate_frame(pd.DataFrame(rows), "status", TZ).ok

    rows[1] = st("r1", "2020-01-01", "2020-12-01", rev=1, avail="2021-03-01 09:00")
    assert "INTERVAL_OVERLAP" in validate_frame(pd.DataFrame(rows), "status", TZ).codes()


def test_status_half_open_adjacent_is_not_overlap():
    rows = [st("r1", "2020-01-01", "2020-06-01"),
            st("r2", "2020-06-01", None, avail="2020-06-01 09:00"),
            st("r3", "2020-03-01", None, field="index:csi300")]
    assert validate_frame(pd.DataFrame(rows), "status", TZ).ok
