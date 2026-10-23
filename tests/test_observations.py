"""As-of revisions, missing names and future perturbation invariance."""

import numpy as np
import pandas as pd
import pytest

from qwb.observations import WindowBuilder

SOURCE = "sha256:" + "a" * 64


def fixture():
    dates = pd.bdate_range("2024-01-01", periods=10)
    rows = []
    for i, day in enumerate(dates):
        for code in ("A", "B"):
            price = 10 + i
            rows.append(dict(code=code, date=day, available_time=day.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15),
                             open=price, high=price + 1, low=price - 1, close=price,
                             volume=100 + i, amount=1000 + i))
    return dates, pd.DataFrame(rows)


def test_future_mutation_does_not_change_earlier_window_or_padding():
    dates, bars = fixture()
    decision = "2024-01-05T15:30:00+08:00"
    first = WindowBuilder(bars, dates, source_hash=SOURCE, lookback=6).at(decision, ("A", "B", "NEW"))
    altered = bars.copy()
    later = altered["date"] > "2024-01-05"
    altered.loc[later, ["open", "high", "low", "close", "volume", "amount"]] *= 100
    second = WindowBuilder(altered, dates, source_hash=SOURCE, lookback=6).at(decision, ("A", "B", "NEW"))
    np.testing.assert_array_equal(first.values, second.values)
    np.testing.assert_array_equal(first.valid, second.valid)
    assert first.manifest["loaded_content_hash"] != second.manifest["loaded_content_hash"]
    assert first.values.shape == (1, 3, 6, 6)
    assert first.lengths.tolist() == [[5, 5, 0]]
    assert not first.valid[:, :, 0].any()
    assert not first.valid[:, 2].any()
    assert np.isfinite(first.values).all()


def test_late_revision_cannot_rewrite_pre_publication_windows():
    dates, bars = fixture()
    revision = bars.iloc[[0]].copy()
    revision["close"] = 1000
    revision["revision"] = 1
    revision["available_time"] = pd.Timestamp("2024-01-08T16:00:00+08:00")
    bars["revision"] = 0
    revised = pd.concat([bars, revision], ignore_index=True)
    original = WindowBuilder(bars, dates, source_hash=SOURCE, lookback=8)
    updated = WindowBuilder(revised, dates, source_hash=SOURCE, lookback=8)
    before = "2024-01-08T15:30:00+08:00"
    np.testing.assert_array_equal(original.at(before, ("A",)).values, updated.at(before, ("A",)).values)
    after = "2024-01-09T15:30:00+08:00"
    assert not np.array_equal(original.at(after, ("A",)).values, updated.at(after, ("A",)).values)


def test_decision_before_publication_masks_current_bar():
    dates, bars = fixture()
    batch = WindowBuilder(bars, dates, source_hash=SOURCE).at("2024-01-05T14:30:00+08:00", ("A", "B"), eligible=("A",))
    assert not batch.valid[0, :, -1].any()
    assert batch.asset_mask.tolist() == [[True, False]]
    assert not batch.values.flags.writeable


def test_no_close_zero_price_and_negative_activity_do_not_create_infinities():
    dates, bars = fixture()
    bars.loc[bars.code == "A", "close"] = np.nan
    bars.loc[bars.code == "B", "open"] = 0
    bars.loc[bars.code == "B", "volume"] = -1
    result = WindowBuilder(bars, dates, source_hash=SOURCE).at("2024-01-05T15:30:00+08:00", ("A", "B"))
    assert not result.valid[0, 0, :, :4].any()
    assert not result.valid[0, 1, :, 0].any()
    assert not result.valid[0, 1, :, 4].any()
    assert np.isfinite(result.values).all()


def test_explicit_availability_and_unique_revision_identity_required():
    dates, bars = fixture()
    with pytest.raises(ValueError, match="require"):
        WindowBuilder(bars.drop(columns="available_time"), dates, source_hash=SOURCE)
    with pytest.raises(ValueError, match="duplicate"):
        WindowBuilder(pd.concat([bars, bars.iloc[[0]]]), dates, source_hash=SOURCE)
    bars["available_time"] = bars.available_time.dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone"):
        WindowBuilder(bars, dates, source_hash=SOURCE)
