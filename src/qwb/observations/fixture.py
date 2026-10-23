"""Licensed, explicitly synthetic PIT bundle for software reproduction only."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .. import experiments
from .bundle import session_time


def create(directory, *, days=110, assets=24, seed=17):
    if type(days) is not int or days < 100 or type(assets) is not int or assets < 2:
        raise ValueError("fixture needs >=100 sessions and >=2 assets")
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range("2023-01-02", periods=days)
    codes = [f"{i:06d}.SZ" for i in range(assets)]
    ret = rng.normal(0, .015, (days, assets))
    close = 10 * np.exp(np.cumsum(ret, axis=0))
    opened = close * np.exp(rng.normal(0, .005, close.shape))
    bars = pd.DataFrame({"code": np.tile(codes, days), "date": np.repeat(cal, assets),
                         "open": opened.ravel(), "close": close.ravel(),
                         "high": np.maximum(opened, close).ravel() * 1.01,
                         "low": np.minimum(opened, close).ravel() * .99,
                         "volume": rng.uniform(1e5, 1e6, days * assets), "revision": 0})
    bars["amount"] = bars["volume"] * bars["close"]
    bars["available_time"] = [str(session_time(day, "15:05")) for day in bars["date"]]
    bars.to_parquet(root / "bars.parquet", index=False)
    universe = bars[["code", "date", "revision"]].copy()
    universe["available_time"] = [str(session_time(day, "08:00")) for day in universe["date"]]
    universe["eligible"] = True
    universe.to_parquet(root / "universe.parquet", index=False)
    experiments.write(root / "manifest.json", {"schema_version": "pit_numeric_v1",
                      "data_snapshot_id": f"snap-synthetic-pit-{days}-{assets}-{seed}",
                      "data_kind": "synthetic", "license": "CC0-1.0 generated fixture; no vendor data",
                      "universe_policy": "explicit_daily_declarations"})
    experiments.write(root / "calendar.json", {"sessions": [str(day.date()) for day in cal]})
    card = {"name": "cn_predict_intraday_v1", "market": "cn_a", "template": "daily",
            "target": {"type": "return", "horizon_days": 0},
            "timing": {"signal_available": {"day": 0, "time": "15:00"},
                       "decision": {"day": 0, "time": "15:30"},
                       "execution": {"day": 1, "time": "09:30", "price": "open"}},
            "label": {"start": {"day": 1, "time": "09:30", "price": "open"},
                      "end": {"day": 1, "time": "15:00", "price": "close"}},
            "output": "score", "metrics": ["rank_ic", "mse", "prediction_coverage"],
            "acceptance": {"stage": "development", "investment_efficacy": "not_established"}}
    config = {"spec_version": "e2e_prediction_v1", "name": "sequence-smoke", "task": card,
              "data": {"source": "pit_bundle", "dir": "."},
              "observations": {"lookback": 12, "min_history": 5},
              "model": {"name": "sequence_gru_v1", "hidden_size": 8, "epochs": 2, "batch_size": 128},
              "folds": {"first_test": str(cal[80].date()), "last_test": str(cal[days - 2].date()),
                        "frequency": "M", "validation_days": 10, "min_train_days": 40},
              "train": {"seeds": [0], "max_samples": 5000, "max_seconds": 120},
              "evaluation_asof": str(session_time(cal[-1], "16:00"))}
    (root / "run.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return root / "run.yaml"
