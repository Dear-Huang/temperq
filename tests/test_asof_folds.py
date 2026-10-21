import pandas as pd
import pytest

from qwb import asof, folds, taskcard
from qwb.formats import normalize
from pathlib import Path

TZ = "Asia/Shanghai"
EX = Path(__file__).resolve().parents[1] / "examples"


def industry_frame():
    # Stock A classified "bank" from 2019; in 2023 the vendor corrects it to
    # "insurance" retroactively from 2019.
    rows = [dict(source="sw", record_id="A|ind|2019", revision=0,
                 available_time="2019-01-02 09:00", code="A", field="industry:sw1",
                 start="2019-01-01", end=None, value="bank"),
            dict(source="sw", record_id="A|ind|2019", revision=1,
                 available_time="2023-05-10 09:00", code="A", field="industry:sw1",
                 start="2019-01-01", end=None, value="insurance")]
    return normalize(pd.DataFrame(rows), "status", TZ)


def tau(s):
    return pd.Timestamp(s, tz=TZ)


def test_correction_does_not_leak_into_history():
    df = industry_frame()
    then = asof.status_on(df, tau("2021-06-30 15:30"), "2021-06-30", "industry:sw1")
    assert then["value"].tolist() == ["bank"]
    now = asof.status_on(df, tau("2024-01-02 15:30"), "2021-06-30", "industry:sw1")
    assert now["value"].tolist() == ["insurance"]


def test_first_release_policy():
    df = industry_frame()
    v = asof.status_on(df, tau("2024-01-02 15:30"), "2021-06-30", "industry:sw1",
                       policy="first_release")
    assert v["value"].tolist() == ["bank"]


def test_naive_tau_rejected():
    with pytest.raises(ValueError):
        asof.select(industry_frame(), pd.Timestamp("2021-01-01"))


def test_label_maturity_drops_recent_samples():
    card = taskcard.load(EX / "st_task.yaml")          # label ends at t+6 09:30
    cal = pd.bdate_range("2024-01-01", "2024-03-29")
    dates = pd.Series(cal)
    lend = folds.label_end_times(dates, cal, card, TZ)
    assert lend.isna().sum() == 6                      # last 6 days beyond calendar

    cutoff = tau("2024-03-01 15:30")
    mask, man = folds.build_fold(0, dates, lend, ("2024-01-01", "2024-03-01"), cutoff,
                                 "snap-1", "lgbm-0.1")
    # sample on 2024-02-22: label ends 2024-03-01 09:30 <= cutoff -> kept
    # sample on 2024-02-23: label ends 2024-03-04 09:30 > cutoff  -> dropped
    kept = dates[mask]
    assert kept.max() == pd.Timestamp("2024-02-22")
    assert man.n_dropped_immature == 6
    assert pd.Timestamp(man.max_label_end) <= cutoff
    assert man.to_dict()["data_snapshot_id"] == "snap-1"


def test_non_trading_day_rejected():
    card = taskcard.load(EX / "st_task.yaml")
    cal = pd.bdate_range("2024-01-01", "2024-01-31")
    with pytest.raises(ValueError):
        folds.label_end_times(pd.Series([pd.Timestamp("2024-01-06")]), cal, card, TZ)
