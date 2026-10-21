import pandas as pd
import pytest

from qwb import rules
from qwb.connectors import cn_a
from qwb.connectors.cache import Cache
from qwb.connectors.tushare import ApiError, TushareClient
from qwb.formats import validate_frame


class Fake:
    """Transport that answers from a dict keyed by (api, frozen params)."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, url, payload, timeout):
        p = dict(payload["params"])
        self.calls.append((payload["api_name"], p))
        off = p.pop("offset", 0)
        lim = p.pop("limit", None)
        key = (payload["api_name"], tuple(sorted(p.items())))
        ans = self.table[key]
        if isinstance(ans, dict):
            return ans
        fields, items = ans
        items = items[off: off + lim] if lim else items
        return {"code": 0, "msg": "", "data": {"fields": fields, "items": items}}


def client(table):
    return TushareClient("t" * 30, transport=Fake(table), min_interval=0)


def test_nonzero_code_raises_and_empty_is_ok():
    c = client({("x", ()): {"code": 40001, "msg": "no permission", "data": None},
                ("y", ()): (["a"], [])})
    with pytest.raises(ApiError, match="40001"):
        c.call("x")
    assert c.call("y").empty and list(c.call("y").columns) == ["a"]


def test_throttle_is_retried_then_raised(monkeypatch):
    monkeypatch.setattr("qwb.connectors.tushare.time.sleep", lambda s: None)
    answers = [{"code": 429, "msg": "slow down", "data": None},
               {"code": 0, "msg": "", "data": {"fields": ["a"], "items": [[1]]}}]
    c = TushareClient("t" * 30, transport=lambda u, p, t: answers.pop(0), min_interval=0)
    assert len(c.call("x")) == 1                      # recovered after one 429

    always = TushareClient("t" * 30, retries=2, min_interval=0,
                           transport=lambda u, p, t: {"code": 429, "msg": "x", "data": None})
    with pytest.raises(ApiError, match="429"):
        always.call("x")


def test_malformed_body_raises():
    c = client({("x", ()): {"code": 0, "msg": ""}})
    with pytest.raises(ApiError, match="malformed"):
        c.call("x")


def test_token_not_in_repr():
    c = TushareClient("secret-token-value-123456")
    assert "secret" not in repr(c)


def test_paging_and_overlap_detection():
    rows = [[f"c{i}"] for i in range(5)]
    c = client({("p", ()): (["k"], rows)})
    assert len(c.paged("p", 2, ["k"])) == 5

    class Repeating(Fake):  # vendor ignores offset
        def __call__(self, url, payload, timeout):
            return {"code": 0, "msg": "", "data": {"fields": ["k"], "items": rows[:2]}}
    c2 = TushareClient("t" * 30, transport=Repeating({}), min_interval=0)
    with pytest.raises(ApiError, match="overlaps"):
        c2.paged("p", 2, ["k"])


def test_cache_tolerates_concurrent_writer(tmp_path, monkeypatch):
    """Another process finishing the same key first must not fail this one."""
    from pathlib import Path

    c = Cache(tmp_path)
    real_replace = Path.replace

    def racing_replace(self, target):
        if str(self).endswith(".tmp") and target.suffix == ".parquet":
            pd.DataFrame({"a": [9]}).to_parquet(target, index=False)  # the other writer
            raise PermissionError("WinError 5")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", racing_replace)
    df = c.get("api", {"d": "1"}, lambda: pd.DataFrame({"a": [1]}))
    assert df["a"].tolist() == [9]                      # the winner's file is used
    assert not list(tmp_path.rglob("*.tmp"))


def test_cache_reuses_and_snapshot_is_stable(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return pd.DataFrame({"a": [1, 2]})

    c1 = Cache(tmp_path)
    c1.get("api", {"d": "1"}, fetch)
    c2 = Cache(tmp_path)
    c2.get("api", {"d": "1"}, fetch)
    assert len(calls) == 1 and c1.snapshot_id() == c2.snapshot_id()


DAYS = pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04",
                                        "2024-01-05"]))


def test_st_status_intervals_are_half_open_and_open_ended():
    daily = pd.DataFrame({"code": ["A", "A", "A", "B", "B"],
                          "name": ["ST甲", "ST甲", "*ST甲", "ST乙", "ST乙"],
                          "date": DAYS[[0, 1, 2, 0, 3]]})
    s = cn_a.st_status(daily, DAYS)
    got = {(r.code, r.value, r.start, r.end) for r in s.itertuples()}
    assert got == {("A", "ST", "2024-01-02", "2024-01-04"),
                   ("A", "*ST", "2024-01-04", "2024-01-05"),
                   ("B", "ST", "2024-01-02", "2024-01-03"),
                   ("B", "ST", "2024-01-05", None)}
    assert validate_frame(s, "status").ok


def test_namechange_derivation_and_cross_check():
    ev = pd.DataFrame({"code": ["A", "A"], "name": ["甲", "ST甲"],
                       "start_date": pd.to_datetime(["2020-01-01", "2024-01-03"])})
    nc = cn_a.st_daily_from_namechange(ev, DAYS)
    assert sorted(nc["date"].dt.strftime("%m%d")) == ["0103", "0104", "0105"]
    primary = pd.DataFrame({"code": ["A", "A"], "date": DAYS[[2, 3]]})
    cc = cn_a.cross_check(primary, nc)
    assert cc["both"] == 2 and cc["only_check"] == 1 and cc["only_primary"] == 0


def test_limit_rules_by_board_status_and_date():
    assert rules.limit_pct("600000.SH", False, "2024-01-02") == 0.10
    assert rules.limit_pct("600000.SH", True, "2024-01-02") == 0.05
    assert rules.limit_pct("300001.SZ", True, "2020-08-21") == 0.05
    assert rules.limit_pct("300001.SZ", True, "2020-08-24") == 0.20
    assert rules.limit_pct("688001.SH", False, "2024-01-02") == 0.20
    assert rules.limit_pct("830001.BJ", False, "2024-01-02") == 0.30
    assert rules.limit_pct("302132.SZ", False, "2024-01-02") == 0.20
    # BSE truncates toward the pre-close; main board rounds half up
    assert rules.limit_prices("830001.BJ", False, "2024-01-02", 10.07) == (13.09, 7.05)
    assert rules.limit_prices("600000.SH", False, "2024-01-02", 10.05) == (11.06, 9.05)


def test_daily_bars_units_states_and_format():
    d = "20240102"
    bars_f = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "change",
              "pct_chg", "vol", "amount"]
    table = {
        ("daily", (("trade_date", d),)): (bars_f, [
            ["A.SZ", d, 10, 10.5, 9.9, 10.2, 10, 0.2, 2, 12.0, 12.3]]),
        ("adj_factor", (("trade_date", d),)): (["ts_code", "trade_date", "adj_factor"],
                                              [["A.SZ", d, 1.5]]),
        ("stk_limit", (("trade_date", d),)): (["trade_date", "ts_code", "up_limit", "down_limit"],
                                             [[d, "A.SZ", 11.0, 9.0]]),
        ("suspend_d", (("trade_date", d),)): (["ts_code", "trade_date", "suspend_timing",
                                               "suspend_type"], [["B.SZ", d, None, "S"]]),
    }
    cache = Cache(pytest.importorskip("tempfile").mkdtemp())
    uni = pd.DataFrame({"code": ["A.SZ", "B.SZ", "C.SZ"], "date": [pd.Timestamp(d)] * 3})
    out = cn_a.daily_bars(client(table), cache, pd.DatetimeIndex([pd.Timestamp(d)]), uni)
    a = out.set_index("code")
    assert a.loc["A.SZ", "vol"] == 1200 and a.loc["A.SZ", "amount"] == 12300
    assert a.loc["B.SZ", "state"] == "suspended" and pd.isna(a.loc["B.SZ", "close"])
    assert a.loc["C.SZ", "state"] == "missing"
    rep = validate_frame(out, "bar")
    assert rep.ok, str(rep)
