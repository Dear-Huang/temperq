"""Export a small, explicit public summary without paths, data or model weights."""

from __future__ import annotations

import io
import hashlib
import json
import math
import zipfile
from pathlib import Path

import yaml

from . import experiments


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def build(run: Path, evaluation: Path | None = None) -> bytes:
    run = Path(run)
    record = experiments.verify(run)
    metrics = experiments.read(run / "metrics.json")
    task = yaml.safe_load((run / "task.yaml").read_text(encoding="utf-8"))
    key = metrics.get("model_key", "lgbm_ensemble")
    summary = {"schema_version": 1, "scope": "author_submitted_local_research",
               **{key: record.get(key) for key in ("run_id", "experiment_id", "model_version", "prediction_version", "origin",
                   "config_hash", "code_hash", "artifact_hash", "created_at", "data_snapshot_id", "data_content_hash")},
               "task": task["name"], "model": record["model"],
               "final_test_years": metrics.get("final_test_years"),
               "signal": metrics.get("ic", {}).get(key),
               "signal_screen": metrics.get("signal_screen"),
               "leak_check_passed": (metrics.get("leak_check") or {}).get("ok"),
               "test_views": (metrics.get("ledger") or {}).get("views"),
               "bench": None,
               "synthetic": metrics.get("data_kind") == "synthetic" or record.get("data_snapshot_id") == "snap-synthetic",
               "prediction_scope": metrics.get("prediction_scope", "walk_forward_fold_models; final_refit_model_not_independently_evaluated"),
               "evidence_status": metrics.get("evidence_status"),
               "prediction_development": metrics.get("prediction_metrics"),
               "limits": ["Local records are author-submitted, not platform authentication.",
                          "Historical development results are not independent forward confirmation.",
                          "Data, weights, local paths and execution code are not included."]}
    if evaluation is not None:
        from .bench.runner import verify_result
        verify_result(evaluation)
        score = experiments.read(Path(evaluation) / "scorecard.json")
        if score["status"] != "COMPLETED" or (score.get("lineage") or {}).get("run_id") != record["run_id"]:
            raise ValueError("evaluation does not belong to this completed training run")
        if (score["lineage"].get("artifact_hash") != record["artifact_hash"]
                or score["prediction_hash"] != record["artifacts"]["predictions.parquet"]):
            raise ValueError("evaluation and training artifacts differ")
        summary["bench"] = {
            "evaluation_id": score["evaluation_id"], "protocol_id": score["protocol_hash"],
            "scorer_version": score["scorer_version"], "implementation_hash": score["implementation_hash"],
            "prediction_hash": score["prediction_hash"], "scorecard_hash": experiments.digest(Path(evaluation) / "scorecard.json"),
            "signal": score["evaluation"]["models"]["candidate"],
            "signal_conclusion": score["evaluation"]["signal"], "coverage": score["evaluation"]["coverage"],
            "economic_status": score["economic"]["status"],
            "economic_candidate": (score["economic"].get("results", {}).get("extra_0bp_per_side") or {}).get("candidate"),
            "live_capital_eligible": False}
    fm = {"qwb_version": "0.0.1", "tags": ["qwb", "quant", task["market"]],
          "task": task["name"], "market": task["market"], "template": task["template"],
          "base_model": record["model"], "run_id": record["run_id"],
          "model_version": record["model_version"] or "unknown",
          "prediction_version": record.get("prediction_version") or "unknown",
          "data_snapshot_id": record.get("data_snapshot_id") or "unknown",
          "evidence_source": "author_submitted_local_research",
          "evaluation_id": (summary["bench"] or {}).get("evaluation_id", "not_evaluated"),
          "protocol_id": (summary["bench"] or {}).get("protocol_id", "not_evaluated"),
          "custom_code": False, "includes_data": False}
    body = ("# Quant Workbench research evidence\n\n"
            f"Task: `{task['name']}`. Model: `{record['model']}`. Run: `{record['run_id']}`.\n\n"
            "This is an author-submitted summary of local research, not platform certification.\n"
            "See `evidence.json` for fixed version identifiers and evaluation scope.\n\n"
            "No data, weights, local paths or executable code are included. This evidence-only\n"
            "bundle cannot load or reproduce the model. Data and model licenses require separate review.\n"
            "Historical development results are not independent forward confirmation or live-trading approval.\n")
    files = {"README.md": "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + body,
             "evidence.json": json.dumps(_finite(summary), ensure_ascii=False, indent=2, allow_nan=False) + "\n"}
    checksums = {name: "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
                 for name, content in files.items()}
    files["checksums.json"] = json.dumps({"files": checksums, "scope": "file_integrity_only"}, indent=2) + "\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    return stream.getvalue()
