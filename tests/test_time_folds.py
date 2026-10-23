"""Weekly/monthly windows with actual label availability and unchanged legacy API."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from qwb.folds import TimeFold, rolling_time_folds, time_fold_masks


@pytest.mark.parametrize("frequency", ["M", "W-FRI"])
@pytest.mark.parametrize("train_days", [None, 50])
def test_rolling_windows_use_only_past_training_and_validation(frequency, train_days):
    cal = pd.bdate_range("2022-01-01", periods=200)
    folds = rolling_time_folds(cal, first_test=cal[100], frequency=frequency,
                              validation_days=10, min_train_days=40, train_days=train_days)
    decisions = (cal + pd.Timedelta(hours=15, minutes=30)).tz_localize("Asia/Shanghai")
    ends = pd.Series(decisions + pd.Timedelta(days=4))
    for fold in folds:
        train, valid, test, evidence = time_fold_masks(decisions, ends, fold)
        assert not ((train & valid) | (valid & test) | (train & test)).any()
        assert decisions[train].max() < decisions[valid].min() < decisions[test].min()
        assert ends[train].max() <= pd.Timestamp(evidence["fit_cutoff"])
        assert ends[valid].max() <= pd.Timestamp(evidence["selection_cutoff"])
        assert evidence["n_purged_train"] > 0
        assert evidence["n_purged_validation"] > 0
        if train_days:
            assert evidence["n_train"] <= train_days


def test_late_label_availability_not_just_end_controls_fit():
    cal = pd.bdate_range("2022-01-01", periods=160)
    fold = rolling_time_folds(cal, first_test=cal[100], validation_days=10)[0]
    d = (cal + pd.Timedelta(hours=15, minutes=30)).tz_localize("Asia/Shanghai")
    ends = pd.Series(d + pd.Timedelta(days=1))
    available = ends.copy()
    available.iloc[20] = pd.Timestamp(fold.test_start) + pd.Timedelta(days=1)
    train, valid, test, record = time_fold_masks(d, ends, fold, label_available=available, embargo="12h")
    assert not train[20]
    changed = ends.copy()
    changed.iloc[np.flatnonzero(test)] += pd.Timedelta(days=100)
    a, b, c, _ = time_fold_masks(d, changed, fold, label_available=None, embargo="12h")
    base_a, base_b, base_c, _ = time_fold_masks(d, ends, fold, embargo="12h")
    np.testing.assert_array_equal(a, base_a)
    np.testing.assert_array_equal(b, base_b)
    np.testing.assert_array_equal(c, base_c)


def test_invalid_calendar_naive_times_and_overlapping_windows_rejected():
    cal = pd.bdate_range("2022-01-01", periods=160)
    with pytest.raises(ValueError, match="sorted"):
        rolling_time_folds(cal[::-1], first_test=cal[100])
    fold = rolling_time_folds(cal, first_test=cal[100])[0]
    with pytest.raises(ValueError, match="nonoverlapping"):
        replace(fold, train_end=fold.validation_start)
    with pytest.raises(ValueError, match="timezone"):
        time_fold_masks(cal, cal, fold)
