"""Exercise train -> frozen protocol -> Bench -> compare -> evidence on synthetic data."""

from __future__ import annotations

import argparse
import shutil
import uuid
from pathlib import Path

import yaml

from qwb import evidence_export, experiments, pipeline, runconfig, worker
from qwb.bench.contracts import create_protocol
from qwb.bench.runner import compare_models, evaluate_model
from qwb.jobs import JobSpec, Store

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new isolated demo workspace")
    args = parser.parse_args()
    workdir = args.output.resolve()
    workdir.mkdir(parents=True, exist_ok=False)
    data = workdir / "data" / "synthetic"
    (data / "bars").mkdir(parents=True)
    (workdir / "examples").mkdir()
    for name in ("cn_rank_5d.yaml", "csv_template.csv"):
        shutil.copy2(ROOT / "examples" / name, workdir / "examples" / name)
    bars = pipeline.synth_bars(420, 40)
    bars["state"] = "normal"
    bars["down_limit"] = bars["open"] * .8
    bars["up_limit"] = bars["open"] * 1.2
    bars.to_parquet(data / "bars" / "demo.parquet", index=False)
    experiments.write(data / "snapshot.json", {"data_snapshot_id": "snap-synthetic",
        "source": "synthetic", "start": str(bars["date"].min().date()),
        "end": str(bars["date"].max().date()), "trading_days": 420, "codes": 40,
        "has_st": False, "purpose": "workflow demonstration only; no investment evidence"})
    store = Store(workdir / "runs" / "jobs")
    runs = []
    for name, model in (("demo_lgbm", {"name": "lightgbm", "num_boost_round": 20,
                                     "params": {"num_threads": 2, "min_data_in_leaf": 100}}),
                        ("demo_ridge", {"name": "ridge"})):
        raw = {"name": name, "task": "cn_rank_5d.yaml", "data": {"source": "snapshot", "dir": str(data)},
               "model": model, "train": {"universe": "all", "min_age_days": 20, "min_amount_quantile": 0,
                   "first_test_year": 2020, "final_test_years": [2020], "seeds": [0], "shuffle_null_draws": 2}}
        config_path = workdir / "examples" / f"{name}.yaml"
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        resolved, report = runconfig.resolve(raw, config_path.parent)
        if resolved is None:
            raise ValueError(str(report))
        job_id = store.submit(JobSpec("train", {"action": "train", "resolved": resolved,
                                               "ui": {"name": name, "what": "train"}}))
        if worker.run_job(workdir, job_id, echo=True) != "succeeded":
            raise RuntimeError(store.status(job_id)["error"])
        run = Path((store.root / job_id / "artifacts" / "output.txt").read_text(encoding="utf-8"))
        runs.append(run)
    protocol = workdir / "bench" / "protocols" / "synthetic-development.yaml"
    create_protocol(runs[0], protocol, cwd=workdir, block_days=5, resamples=200, min_days=20)
    evaluations = []
    for run in runs:
        evaluation_id = uuid.uuid4().hex
        out = workdir / "runs" / "bench" / evaluation_id
        score = evaluate_model(run, protocol, out, workdir=workdir, evaluation_id=evaluation_id)
        if score["status"] != "COMPLETED":
            raise RuntimeError(score["errors"])
        evaluations.append(out)
    comparison = workdir / "runs" / "comparisons" / uuid.uuid4().hex
    compare_models(*evaluations, comparison)
    evidence = workdir / "evidence-demo.zip"
    evidence.write_bytes(evidence_export.build(runs[1], evaluations[1]))
    experiments.write(workdir / "DEMO.json", {"scope": "synthetic_workflow_only",
        "runs": [str(path) for path in runs], "evaluations": [str(path) for path in evaluations],
        "comparison": str(comparison), "evidence": str(evidence)})
    print(f"Synthetic workflow completed: {workdir}")
    print("This demonstrates the workflow, not investment performance.")


if __name__ == "__main__":
    main()
