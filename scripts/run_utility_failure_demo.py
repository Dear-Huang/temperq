"""Self-contained synthetic source training and outer missing-observation replay."""

from __future__ import annotations

import argparse
from pathlib import Path

from qwb import experiments, pipeline, runconfig, worker
from qwb.jobs import JobSpec, Store
from qwb.observations.bundle import PITBundle
from qwb.observations.fixture import create
from qwb.training.utility_runner import verify

import pandas as pd

from run_utility_pipeline_demo import create_config


def compute(output):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    identity = {"implementation_hash": experiments.code_identity(), "script_hash": experiments.digest(Path(__file__)),
                "pipeline_demo_script_hash": experiments.digest(Path(__file__).with_name("run_utility_pipeline_demo.py"))}
    intent = {"schema_version": "utility_failure_demo_v1", "created_at": experiments.now(), **identity,
              "source": {"days": 110, "assets": 24, "seed": 17, "epochs": 1, "lookback": 60, "min_history": 20},
              "missing_observation": {"asset_index": 0, "calendar_index": 87, "field": "high", "value": "NaN"},
              "utility": {"epochs": 2, "seeds": [0], "validation_audit": True, "outer_evaluation": True},
              "evidence_status": "SYNTHETIC_ONLY", "economic_efficacy": "NOT_ESTABLISHED",
              "scope": "predeclared missing input before real prediction pretraining; no runtime fault injection or hand-set head"}
    experiments.write(root / "intent.json", intent)
    try:
        source_path = create(root / "bundle", days=110, assets=24, seed=17)
        manifest = experiments.read(source_path.parent / "manifest.json")
        manifest["data_snapshot_id"] += "-outer-missing-high-v1"
        experiments.write(source_path.parent / "manifest.json", manifest)
        bars = pd.read_parquet(source_path.parent / "bars.parquet")
        date = pd.DatetimeIndex(sorted(bars["date"].unique()))[87]
        code = sorted(bars["code"].unique())[0]
        mask = (bars["date"] == date) & (bars["code"] == code)
        if int(mask.sum()) != 1:
            raise ValueError("failure fixture missing row is ambiguous")
        bars.loc[mask, "high"] = float("nan")
        bars.to_parquet(source_path.parent / "bars.parquet", index=False)
        raw = runconfig.load(source_path)
        raw["observations"] = {"lookback": 60, "min_history": 20}
        raw["model"]["epochs"] = 1
        raw["folds"]["last_test"] = raw["folds"]["first_test"]
        resolved, report = runconfig.resolve(raw, source_path.parent)
        if resolved is None:
            raise ValueError(str(report))
        bundle = PITBundle(source_path.parent)
        experiments.write(root / "source-plan.json", {"data_identity": bundle.identity,
            "source_config": resolved, "missing_row": {"asset_id": code, "date": str(date.date()), "field": "high"}})
        source = pipeline.run(resolved, root / "source", log=print)
        experiments.verify(Path(source["output"]))
        config, utility = create_config(source["output"], bundle.root, root / "utility", epochs=2,
                                       validation_audit=True, outer_evaluation=True)
        store = Store(config.parent / "runs/jobs")
        job = store.submit(JobSpec("train", {"action": "train", "resolved": utility}))
        if worker.run_job(config.parent, job, echo=True) != "succeeded":
            raise ValueError(str(store.status(job)))
        run = Path((store.root / job / "artifacts/output.txt").read_text(encoding="utf-8"))
        verified = verify(run, inputs=True)
        experiments.write(root / "verification.json", verified)
        metrics = experiments.read(run / "metrics.json")
        if (metrics["outer_evaluation_status"] != "DIAGNOSTIC_WITH_FAILURES"
                or metrics["outer_exact_failures"] != 2 or metrics["outer_continuous_failures"] != 2
                or metrics["comparison_eligible"] or not metrics["validation_audit_complete"]):
            raise ValueError("predeclared outer failure/recovery coverage differs")
        for trial in metrics["evaluations"]:
            for result in trial["results"].values():
                if result["coverage"] != {"declared": 3, "exact_targets": 2, "exact_failures": 1,
                    "continuous_completed": 1, "continuous_failed": 1, "continuous_not_run": 1}:
                    raise ValueError("failure demo deleted dates or failed to recover")
        if (experiments.code_identity() != identity["implementation_hash"]
                or experiments.digest(Path(__file__)) != identity["script_hash"]
                or experiments.digest(Path(__file__).with_name("run_utility_pipeline_demo.py")) != identity["pipeline_demo_script_hash"]):
            raise ValueError("failure demo implementation changed")
        result = {"status": "COMPLETED", "run": str(run), "verification": verified,
                  "outer_evaluation_status": metrics["outer_evaluation_status"],
                  "outer_exact_failures": metrics["outer_exact_failures"],
                  "outer_continuous_failures": metrics["outer_continuous_failures"],
                  "validation_audit_complete": metrics["validation_audit_complete"],
                  "comparison_eligible": metrics["comparison_eligible"], **identity,
                  "evidence_status": "SYNTHETIC_ONLY", "economic_efficacy": "NOT_ESTABLISHED"}
        experiments.write(root / "result.json", result)
        return result
    except BaseException as exc:
        experiments.write(root / "result.json", {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        experiments.write(root / "checksums.json", experiments.inventory(root))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compute(args.output)
    print(f"{result['status']}; {result['outer_evaluation_status']}; comparison eligible {result['comparison_eligible']}")


if __name__ == "__main__":
    main()
