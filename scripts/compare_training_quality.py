"""Matched fixed-round vs purged-selection development audit, without tuning on its results."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from qwb import experiments, pipeline, runconfig, taskcard
from qwb.bench.contracts import create_protocol
from qwb.bench.runner import compare_models, evaluate_model, verify_result

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def rescore(output: Path) -> None:
    """Keep trained predictions and the frozen protocol; refresh both scorer outputs."""
    report = experiments.read(output / "QUALITY_AUDIT.json")
    scored = []
    for name, run in zip(("fixed", "selected"), report["runs"]):
        destination = output / "bench" / f"{name}-final"
        score = evaluate_model(Path(run), output / "development.yaml", destination, workdir=output)
        if score["status"] != "COMPLETED":
            raise ValueError(score["errors"])
        verify_result(destination, inputs=True)
        scored.append(destination)
    comparison = compare_models(*scored, output / "comparison-final")
    summaries = [experiments.read(path / "scorecard.json") for path in scored]
    report.update(evaluations=[str(path) for path in scored],
        signal=[{"ic": score["evaluation"]["models"]["candidate"],
                 "by_year": score["evaluation"]["by_year"],
                 "economic_status": score["economic"]["status"],
                 "economic": score["economic"].get("results", {}).get("extra_0bp_per_side", {}).get("candidate")}
                for score in summaries],
        paired_difference=comparison["difference"], decision=comparison["signal_decision"],
        economic_comparison=comparison["economic_comparison"],
        rescore_note="Same archived predictions and development.yaml; current scorer, no retraining or retuning.",
        original_audit="QUALITY_AUDIT.json")
    experiments.write(output / "QUALITY_AUDIT_FINAL.json", report)
    print(json.dumps({"decision": report["decision"],
                      "mean_ic": [score["ic"]["mean"] for score in report["signal"]]}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="new isolated directory")
    parser.add_argument("--rescore", action="store_true", help="rescore an existing audit into new *-final directories")
    parser.add_argument("--codes", type=int, default=240)
    parser.add_argument("--end-year", type=int, default=2023)
    args = parser.parse_args()
    if args.rescore:
        rescore(args.output.resolve())
        return
    if args.data_dir is None:
        parser.error("--data-dir is required for a new training audit")
    if args.codes < 40 or args.end_year < 2021:
        parser.error("use >=40 codes and end-year >=2021")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    files = sorted((args.data_dir / "bars").glob("*.parquet"))
    # Select by code hash, independent of returns and current listing status.
    eligible_files = [p for p in files if not p.stem.isdigit() or int(p.stem) <= args.end_year]
    codes = set()
    for file in eligible_files:
        frame = pd.read_parquet(file, columns=["code", "date"],
                                filters=[("date", "<=", f"{args.end_year}-12-31")])
        codes.update(c for c in frame["code"] if c.endswith((".SZ", ".SH")))
    selected = sorted(codes, key=lambda code: hashlib.sha256(code.encode()).hexdigest())[:args.codes]
    if len(selected) < 40:
        raise ValueError("insufficient securities in the development snapshot")
    sample = output / "sample"
    (sample / "bars").mkdir(parents=True)
    original = {}
    for file in eligible_files:
        frame = pd.read_parquet(file, filters=[("code", "in", selected),
            ("date", "<=", f"{args.end_year}-12-31")])
        if len(frame):
            frame.to_parquet(sample / "bars" / file.name, index=False)
            original[file.name] = experiments.digest(file)
    for name in ("st_status.parquet",):
        if (args.data_dir / name).is_file():
            shutil.copy2(args.data_dir / name, sample / name)
            original[name] = experiments.digest(args.data_dir / name)
    identity = experiments.object_digest({"source": original, "codes": selected, "end_year": args.end_year})
    experiments.write(sample / "snapshot.json", {"data_snapshot_id": "snap-dev-" + identity[-16:],
        "source": "local_real_development_subset", "codes": selected,
        "end_year": args.end_year, "sampling": "sha256(code) ascending; no return-based selection",
        "source_files": original, "scope": "development_not_independent_confirmation"})
    card = taskcard.load(ROOT / "examples" / "cn_rank_5d.yaml")
    card["name"] = "cn_rank_5d_quality_development"
    card["acceptance"] = {"go": "Development comparison only; no model promotion or confirmation."}
    runs = []
    for name, selection in (("fixed_rounds", None), ("purged_selection", {
            "validation_days": 60, "patience": 30, "min_train_days": 120})):
        model = {"name": "lightgbm", "num_boost_round": 300, "params": {"num_threads": 4}}
        if selection is not None:
            model["selection"] = selection
        config = {"name": name, "task": card, "data": {"source": "snapshot", "dir": str(sample)},
                  "model": model, "train": {"universe": "all_ex_st", "seeds": [0, 1],
                      "first_test_year": 2021, "final_test_years": list(range(2021, args.end_year + 1)),
                      "shuffle_null_draws": 2}}
        resolved, report = runconfig.resolve(config, ROOT / "examples")
        if not resolved:
            raise ValueError(str(report))
        result = pipeline.run(resolved, output)
        runs.append(Path(result["output"]))
    protocol = output / "development.yaml"
    create_protocol(runs[0], protocol, cwd=output, block_days=20, resamples=2000, min_days=60)
    scored = []
    for name, run in zip(("fixed", "selected"), runs):
        destination = output / "bench" / name
        score = evaluate_model(run, protocol, destination, workdir=output)
        if score["status"] != "COMPLETED":
            raise ValueError(score["errors"])
        verify_result(destination, inputs=True)
        scored.append(destination)
    comparison = compare_models(*scored, output / "comparison")
    summaries = [experiments.read(p / "scorecard.json") for p in scored]
    report = {"scope": "matched_real_data_development_audit_not_independent_confirmation",
              "codes": len(selected), "sample_method": "sha256(code) ascending from all historical codes",
              "end_year": args.end_year, "outer_test_years": list(range(2021, args.end_year + 1)),
              "runs": [str(p) for p in runs], "evaluations": [str(p) for p in scored],
              "signal": [{"ic": s["evaluation"]["models"]["candidate"],
                           "by_year": s["evaluation"]["by_year"],
                           "economic_status": s["economic"]["status"],
                           "economic": s["economic"].get("results", {}).get("extra_0bp_per_side", {}).get("candidate")}
                          for s in summaries],
              "paired_difference": comparison["difference"], "decision": comparison["signal_decision"],
              "economic_comparison": comparison["economic_comparison"],
              "selection": experiments.read(runs[1] / "metrics.json")["selection"],
              "limits": ["Historical subset and two seeds; not a full-universe repeated-training study.",
                         "No selection setting was chosen by maximizing this audit's outer scores.",
                         "2024-2025 bar files are excluded; this audit is not proof of untouched holdout data.",
                         "Retrospective source timestamps and survivorship completeness are not authenticated."]}
    experiments.write(output / "QUALITY_AUDIT.json", report)
    print(json.dumps({"output": str(output), "decision": report["decision"],
                      "mean_ic": [s["ic"]["mean"] for s in report["signal"]]}, indent=2))


if __name__ == "__main__":
    main()
