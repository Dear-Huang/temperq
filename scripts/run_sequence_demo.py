"""Frozen multi-seed B0/B1 software comparison on generated, non-market data."""

from __future__ import annotations

import argparse
from pathlib import Path

from qwb import experiments, metrics, pipeline, runconfig
from qwb.evidence_export import _finite
from qwb.observations.fixture import create

import numpy as np
import pandas as pd
import yaml


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new isolated directory")
    args = parser.parse_args(argv)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    config = create(root / "fixture")
    common = runconfig.load(config)
    common["observations"] = {"lookback": 60, "min_history": 20}
    common["train"]["seeds"] = [0, 1, 2]
    planned = {}
    for name, model in (("B0-ridge", {"name": "ridge_factor_v1", "alpha": 10}),
                        ("B1-gru", {"name": "sequence_gru_v1", "hidden_size": 8,
                                    "epochs": 5, "batch_size": 128, "patience": 3})):
        raw = dict(common, name=name, model=model)
        rc, report = runconfig.resolve(raw, config.parent)
        if rc is None:
            raise ValueError(str(report))
        planned[name] = rc
        (root / f"{name}.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    experiments.write(root / "plan.json", {"schema_version": "sequence_demo_plan_v1",
                      "frozen_at": experiments.now(), "evidence_status": "SYNTHETIC_ONLY",
                      "runs": planned, "selection": "none; report both regardless of outcome",
                      "comparison": "same data, observations, dates, labels, folds, seeds and per-fit time cap; different models and inputs",
                      "limits": "Synthetic data establishes no market skill; Ridge seeds are deterministic duplicates."})
    results = {name: pipeline.run(rc, root) for name, rc in planned.items()}
    predictions = {}
    for name, result in results.items():
        path = Path(result["output"])
        experiments.verify(path)
        predictions[name] = pd.read_parquet(path / "predictions.parquet").set_index(["code", "decision_time"]).sort_index()
    before, after = predictions.values()
    if not before.index.equals(after.index) or not before["label"].equals(after["label"]):
        raise ValueError("B0/B1 sample or label coverage differs")
    calendars = sorted(before.index.get_level_values("decision_time").unique())
    label = before["label"].unstack("code").reindex(calendars)
    daily_ic = {name: metrics.rank_ic(frame["prediction"].unstack("code").reindex_like(label).to_numpy(), label.to_numpy())
                for name, frame in predictions.items()}
    delta = daily_ic["B1-gru"] - daily_ic["B0-ridge"]
    result = _finite({"schema_version": "sequence_demo_result_v1", "evidence_status": "SYNTHETIC_ONLY",
                     "plan_hash": experiments.digest(root / "plan.json"), "runs": results,
                     "coverage_exact": True, "n_matched_predictions": len(before),
                     "n_ic_dates": int(np.isfinite(delta).sum()),
                     "paired_development_ic": metrics.newey_west(delta, lag=0),
                     "economic_efficacy": "NOT_EVALUATED", "model_superiority": "NOT_ESTABLISHED"})
    experiments.write(root / "comparison.json", result)
    print(f"Software comparison saved: {root / 'comparison.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
