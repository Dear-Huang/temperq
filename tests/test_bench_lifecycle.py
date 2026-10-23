"""Stale marks are explained only by recorded suspensions or delisting."""

import numpy as np
import pandas as pd
import pytest

from qwb.bench import account, lifecycle
from qwb.bench.contracts import digest


def panel(prices):
    values = np.asarray(prices, dtype=float)
    dates = pd.bdate_range("2024-01-01", periods=len(values))
    codes = ["A", "B"]
    f = lambda x: pd.DataFrame(x, index=dates, columns=codes)
    return {"open": f(values), "close": f(values), "adj_factor": f(np.ones_like(values)),
            "up_limit": f(values * 1.1), "down_limit": f(values * 0.9)}


def events(rows):
    df = pd.DataFrame(rows, columns=lifecycle.COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    return df


# A is missing on day 2 (2024-01-03); B is missing on day 3 (2024-01-04).
PRICES = [[10, 10], [10, 10], [np.nan, 10], [10, np.nan]]


def run(explained):
    p = panel(PRICES)
    shape = p["close"].shape
    return account.replay(p, np.ones(shape, bool), np.zeros(shape), {0: [.5, .5]},
                          start=0, end=3, initial_cash=100, commission_bp=0,
                          explained=explained)


def test_masks_cover_suspension_day_and_all_days_after_delisting():
    dates = pd.bdate_range("2024-01-01", periods=4)
    sus, gone = lifecycle.masks(events([["A", "2024-01-03", "suspended"],
                                        ["B", "2024-01-04", "delisted"]]), dates, pd.Index(["A", "B"]))
    assert sus[:, 0].tolist() == [False, False, True, False]
    assert gone[:, 1].tolist() == [False, False, False, True]
    assert not sus[:, 1].any() and not gone[:, 0].any()


def test_without_records_every_stale_mark_stays_unexplained():
    path, _, held = run(None)
    m = account.metrics(path, 100)
    assert m["stale_close_name_days"] == 2
    assert m["unexplained_stale_close_name_days"] == 2


def test_recorded_suspension_explains_only_its_own_day():
    dates = pd.bdate_range("2024-01-01", periods=4)
    sus, gone = lifecycle.masks(events([["A", "2024-01-03", "suspended"]]), dates, pd.Index(["A", "B"]))
    path, _, held = run(sus | gone)
    m = account.metrics(path, 100)
    assert m["stale_close_name_days"] == 2           # raw count unchanged
    assert m["unexplained_stale_close_name_days"] == 1   # B's gap has no record


def test_classify_labels_reasons():
    held = pd.DataFrame({"date": pd.to_datetime(["2024-01-03", "2024-01-04", "2024-01-04"]),
                         "code": ["A", "B", "A"], "stale": [True, True, False]})
    out = lifecycle.classify(held, events([["A", "2024-01-03", "suspended"],
                                           ["B", "2024-01-02", "delisted"]]))
    assert out["stale_reason"].tolist() == ["suspended", "delisted", ""]
    assert lifecycle.classify(held, None)["stale_reason"].tolist() == ["unknown", "unknown", ""]


def test_declared_file_is_hash_checked(tmp_path):
    p = tmp_path / "lifecycle.parquet"
    events([["A", "2024-01-03", "suspended"]]).to_parquet(p, index=False)
    spec = {"path": str(p), "sha256": digest(p)}
    assert len(lifecycle.load_declared(spec)) == 1
    assert lifecycle.load_declared(None) is None
    events([["A", "2024-01-04", "suspended"]]).to_parquet(p, index=False)
    with pytest.raises(ValueError, match="changed"):
        lifecycle.load_declared(spec)


def test_bad_event_type_rejected(tmp_path):
    p = tmp_path / "lifecycle.parquet"
    events([["A", "2024-01-03", "renamed"]]).to_parquet(p, index=False)
    with pytest.raises(ValueError, match="events"):
        lifecycle.load(p)
