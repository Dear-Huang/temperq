from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze_signal_horizons import analyze, clean, require_date_bound


def inputs():
    dates = pd.bdate_range("2021-01-01", periods=100)
    codes = [f"S{i}" for i in range(25)]
    rate = np.linspace(-.005, .005, 25)
    opened = pd.DataFrame(10 * np.exp(np.arange(100)[:, None] * rate), index=dates, columns=codes)
    panel = {"open": opened, "adj_factor": opened * 0 + 1}
    scores = pd.DataFrame(np.tile(rate, (100, 1)), index=dates, columns=codes)
    return panel, scores


def test_maturity_and_future_price_changes_cannot_affect_cutoff_report():
    panel, scores = inputs()
    cutoff = panel["open"].index[70]
    first, paths = analyze(panel, scores, cutoff=cutoff)
    panel["open"].loc[panel["open"].index > cutoff] *= 999
    second, repeated = analyze(panel, scores, cutoff=cutoff)
    pd.testing.assert_frame_equal(paths, repeated)
    assert clean(first) == clean(second)
    row = next(r for r in first["series"] if r["kind"] == "cumulative" and r["to_holding_day"] == 20 and r["coverage"] == "common")
    assert row["statistics"]["n"] == 50
    assert row["statistics"]["mean"] == pytest.approx(1)
    assert row["label_start_trading_offset"] == 1
    assert row["label_end_trading_offset"] == 21


def test_incremental_signal_disappears_after_initial_jump():
    panel, scores = inputs()
    dates, codes = panel["open"].index, panel["open"].columns
    rate = np.linspace(0, .2, len(codes))
    panel["open"].iloc[:, :] = 10
    panel["open"].iloc[2:, :] = 10 * (1 + rate)
    scores.iloc[1:, :] = np.nan
    report, paths = analyze(panel, scores, cutoff=dates[-1])
    assert paths.loc[0, "cumulative_0_20_available"] == pytest.approx(1)
    assert np.isnan(paths.loc[0, "incremental_5_10_available"])


def test_adjustment_prevents_split_from_becoming_a_false_negative_signal():
    panel, scores = inputs()
    before, first = analyze(panel, scores, cutoff=panel["open"].index[-1])
    panel["open"].iloc[30:, :10] /= 2
    panel["adj_factor"].iloc[30:, :10] *= 2
    after, second = analyze(panel, scores, cutoff=panel["open"].index[-1])
    pd.testing.assert_frame_equal(first, second)


def test_unknown_calendar_date_is_rejected():
    panel, scores = inputs()
    scores = scores.rename(index={scores.index[0]: pd.Timestamp("2020-12-31")})
    with pytest.raises(ValueError, match="absent"):
        analyze(panel, scores, cutoff=panel["open"].index[-1])


def test_holdout_date_in_misnamed_partition_is_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / "2023.parquet"
    pd.DataFrame({"date": pd.to_datetime(["2023-12-29", "2026-01-05"]), "open": [10, 11]}).to_parquet(path)
    import pyarrow.parquet as pq
    monkeypatch.setattr(pq, "read_table", lambda *a, **k: pytest.fail("outcome rows must not be opened"))
    with pytest.raises(ValueError, match="rows not opened"):
        require_date_bound(path, "date", "2023-12-31")
