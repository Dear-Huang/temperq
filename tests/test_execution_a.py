import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from qwb import experiments
from test_netcost import panel

spec = importlib.util.spec_from_file_location("execution_a", Path(__file__).parents[1] / "scripts/compare_execution_a.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def test_same_predictions_and_universe_all_variants_and_no_overwrite(tmp_path, monkeypatch):
    run = tmp_path / "model"
    run.mkdir()
    config = {"cfg": {"universe": "all_ex_st", "min_age_days": 60, "min_amount_quantile": .05,
        "final_test_years": [2024], "portfolio": {"top": .1, "commission_bp": 3}}}
    card = {"label": {"start": {"day": 1, "price": "open"}, "end": {"day": 6, "price": "open"}}}
    (run / "config.resolved.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (run / "task.yaml").write_text(yaml.safe_dump(card), encoding="utf-8")
    for name in ["folds.json", "acceptance.lock"]:
        (run / name).write_text("{}", encoding="utf-8")
    P = panel(n_days=80)
    predictions = pd.DataFrame({"date": np.repeat(P["close"].index, 10),
        "code": np.tile(P["close"].columns, 80), "prediction": np.tile(np.arange(10), 80),
        "data_snapshot_id": "original"})
    predictions["decision_time"] = (predictions.date + pd.Timedelta(hours=15, minutes=30)).dt.tz_localize("Asia/Shanghai").astype(str)
    predictions.drop(columns="date").to_parquet(run / "predictions.parquet", index=False)
    before = experiments.digest(run / "predictions.parquet")
    monkeypatch.setattr(comparison.pipeline, "load_data", lambda *a: (P, "original", None, None))
    monkeypatch.setattr(comparison.daily, "universe", lambda *a: pd.DataFrame(True, index=P["close"].index, columns=P["close"].columns))
    monkeypatch.setattr(comparison.dataset, "fingerprint", lambda *a: {"content_hash": "original"})
    monkeypatch.setattr(comparison.netcost, "struct_half_spread_bp", lambda *a: P["close"] * 0 + 10)
    original = comparison.netcost.evaluate
    inputs = []
    def evaluate(*args, **kwargs):
        inputs.append((args[2]["same_prediction"], args[3]))
        return original(*args, **kwargs)
    monkeypatch.setattr(comparison.netcost, "evaluate", evaluate)
    reference = tmp_path / "reference.yaml"
    reference.write_text(yaml.safe_dump({"port_analysis_config": {"backtest": {"exchange_kwargs": {
        "deal_price": "close", "limit_threshold": .095, "open_cost": .0005,
        "close_cost": .0015, "min_cost": 5}}}}), encoding="utf-8")
    out = tmp_path / "comparison"
    result = comparison.run(run, tmp_path, out, reference)
    assert len(inputs) == 4 and all(x[0] is inputs[0][0] and x[1] is inputs[0][1] for x in inputs)
    a, b = result["rows"][2:]
    assert a["gross_ann"] == b["gross_ann"] and a["cost_ann"] != b["cost_ann"]
    assert result["identity"]["engine"] == "qwb_netcost_adapter_not_qlib_engine"
    assert experiments.digest(run / "predictions.parquet") == before
    with pytest.raises(ValueError, match="cannot be overwritten"):
        comparison.run(run, tmp_path, out, reference)
