import importlib.util
from pathlib import Path

import pandas as pd
import pytest


spec = importlib.util.spec_from_file_location("fetch_api_daily", Path(__file__).parents[1] / "scripts" / "fetch_api_daily.py")
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)


@pytest.mark.parametrize("changes", [
    {"ts_code": ["600000.SH"]}, {"trade_date": ["20261021"]}, {"close": ["invalid"]},
])
def test_vendor_cannot_silently_ignore_security_or_date_filters(changes):
    values = {"ts_code": ["000001.SZ"], "trade_date": ["20261022"], "close": ["12.3"]}
    values.update(changes)
    with pytest.raises(ValueError):
        fetcher.normalize_response(pd.DataFrame(values), "000001.SZ", pd.DatetimeIndex(["2026-10-22"]), "daily", ["close"])


def test_duplicate_dates_fail_and_missing_sessions_are_not_forward_filled():
    response = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20261022"], "close": ["12.3"]})
    days = pd.DatetimeIndex(["2026-10-21", "2026-10-22"])
    normalized = fetcher.normalize_response(response, "000001.SZ", days, "daily", ["close"])
    assert pd.isna(normalized.loc[days[0], "close"])
    assert normalized.loc[days[1], "close"] == 12.3
    with pytest.raises(ValueError, match="duplicate"):
        fetcher.normalize_response(pd.concat([response, response]), "000001.SZ", days, "daily", ["close"])
