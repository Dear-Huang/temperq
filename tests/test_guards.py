import numpy as np
import pandas as pd

from qwb import daily, leakcheck, ledger


def panel(n_days=120, n_codes=30, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    codes = [f"{i:06d}.SZ" for i in range(n_codes)]
    close = pd.DataFrame(10 * np.exp(np.cumsum(rng.normal(0, 0.02, (n_days, n_codes)), 0)),
                         index=dates, columns=codes)
    P = {"open": close * 1.001, "high": close * 1.01, "low": close * 0.99, "close": close,
         "amount": close * 0 + 1e7 * (1 + np.arange(n_codes)), "adj_factor": close * 0 + 1,
         "up_limit": close * 1.1}
    return P


CFG = dict(daily.DEFAULTS, min_age_days=20, final_test_years=[2024])


def test_clean_pipeline_passes_prefix_invariance():
    r = leakcheck.prefix_invariance(panel(), CFG, days=[60, 90, 119])
    assert r["ok"], r["violations"]


def test_centred_window_is_caught(monkeypatch):
    real = daily.raw_features

    def leaky(P):
        f = real(P)
        f["vol20"] = f["r1"].rolling(21, center=True, min_periods=5).std()  # peeks 10 days ahead
        return f
    monkeypatch.setattr(daily, "raw_features", leaky)
    r = leakcheck.prefix_invariance(panel(), CFG, days=[60, 90])
    assert not r["ok"] and {v["item"] for v in r["violations"]} == {"vol20"}


def test_backfill_is_caught(monkeypatch):
    real = daily.raw_features

    def leaky(P):
        f = real(P)
        f["r60"] = f["r60"].bfill()                  # fills warm-up rows from the future
        return f
    monkeypatch.setattr(daily, "raw_features", leaky)
    r = leakcheck.prefix_invariance(panel(), CFG, days=[30])
    assert not r["ok"]


def test_ledger_counts_views_configs_and_best(tmp_path):
    p = tmp_path / "ledger.jsonl"
    h = ledger.history(p, "t", "snap", [2024], "c1", 0.05)
    assert h["views"] == 1 and not h["is_best"]
    ledger.append(p, {"task": "t", "snapshot": "snap", "config_hash": "c1",
                      "final_test_years": [2024], "final_ic": 0.05})
    ledger.append(p, {"task": "t", "snapshot": "snap", "config_hash": "c2",
                      "final_test_years": [2024], "final_ic": 0.03})
    ledger.append(p, {"task": "other", "snapshot": "snap", "config_hash": "c9",
                      "final_test_years": [2024], "final_ic": 0.9})
    with open(p, "a", encoding="utf-8") as f:
        f.write("{torn line\n")
    h = ledger.history(p, "t", "snap", [2024], "c3", 0.08)
    assert h["views"] == 3 and h["configs"] == 3 and h["is_best"]
    h = ledger.history(p, "t", "snap", [2025], "c4", 0.01)
    assert h["years_changed"] and h["views"] == 1
