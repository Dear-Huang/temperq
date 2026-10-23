"""Limited checks of locally declared yearly walk-forward training manifests."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .. import folds
from .contracts import digest, object_digest, read_json, read_yaml


def inspect_evidence(run_dir: Path | None, dates: pd.DatetimeIndex, protocol: dict) -> dict:
    result = {"sample_status": "unknown", "source": "local_declared", "files": {},
              "status": "NOT_EVALUATED", "issues": [],
              "limits": ["local declarations do not authenticate training or external test access",
                         "model loading, feature provenance and inference reproduction are not checked"]}
    if run_dir is None or not (run_dir / "folds.json").is_file():
        result["issues"].append("training_fold_evidence_missing")
        return result
    for name in ("folds.json", "task.yaml", "config.resolved.yaml", "metrics.json", "data_manifest.json"):
        if (run_dir / name).is_file():
            result["files"][name] = digest(run_dir / name)
    if (run_dir / "data_manifest.json").is_file():
        manifest = read_json(run_dir / "data_manifest.json")
        expected = {"files": protocol["data"]["files"],
                    "content_hash": object_digest(protocol["data"]["files"])}
        if manifest != expected:
            result["issues"].append("training_data_content_mismatch")
    else:
        result["limits"].append("training_data_content_not_recorded_legacy_declaration_only")
    for name in ("task.yaml", "config.resolved.yaml"):
        if not (run_dir / name).is_file():
            result["issues"].append(f"missing_training_artifact:{name}")
    if (run_dir / "task.yaml").is_file():
        card = read_yaml(run_dir / "task.yaml")
        for key in ("name", "market", "template", "target", "timing", "label", "output"):
            if card.get(key) != protocol["task"].get(key):
                result["issues"].append("training_task_mismatch")
                break
    manifests = read_json(run_dir / "folds.json")
    if not isinstance(manifests, list) or not manifests:
        result["issues"].append("empty_or_invalid_fold_manifest")
    else:
        required = {"fit_cutoff", "train_start", "train_end", "max_label_end", "data_snapshot_id", "n_train"}
        by_year = {}
        for manifest in manifests:
            if not isinstance(manifest, dict) or not required <= set(manifest):
                result["issues"].append("incomplete_fold_manifest")
                continue
            try:
                cutoff = pd.Timestamp(manifest["fit_cutoff"])
                maximum = pd.Timestamp(manifest["max_label_end"])
                lo, hi = pd.Timestamp(manifest["train_start"]), pd.Timestamp(manifest["train_end"])
                if (cutoff.tzinfo is None or maximum.tzinfo is None or pd.isna(cutoff)
                        or pd.isna(maximum) or lo not in dates or hi not in dates or lo > hi
                        or manifest["n_train"] <= 0):
                    raise ValueError("invalid fold times or sample count")
                expected = folds.label_end_times(pd.Series([hi]), dates, protocol["task"],
                                                protocol["timezone"]).iloc[0]
                if pd.isna(expected) or maximum != expected or maximum > cutoff:
                    result["issues"].append("training_label_not_mature_or_manifest_inconsistent")
                if manifest["data_snapshot_id"] != protocol["data"]["snapshot_id"]:
                    result["issues"].append("training_snapshot_mismatch")
                year = cutoff.tz_convert(protocol["timezone"]).year + 1
                if year in by_year:
                    result["issues"].append("duplicate_yearly_fold")
                by_year[year] = cutoff
            except (ValueError, TypeError, KeyError):
                result["issues"].append("invalid_fold_times")
        for year in protocol["evaluation_years"]:
            scoring_dates = dates[dates.year == year]
            cutoff = by_year.get(year)
            if cutoff is None or not len(scoring_dates):
                result["issues"].append(f"missing_yearly_fold:{year}")
            elif cutoff >= (scoring_dates[0] + pd.Timedelta(hours=15, minutes=30)).tz_localize(
                    protocol["timezone"]):
                result["issues"].append("fit_not_before_test_decision")
    if (run_dir / "metrics.json").is_file():
        # Historical metrics may contain NaN; never reuse their performance numbers.
        import json
        old = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        if not isinstance(old, dict):
            raise ValueError("metrics.json evidence must be an object")
        if old.get("acceptance_changed_after_lock"):
            result["limits"].append("legacy_acceptance_changed_after_lock")
        if (old.get("leak_check") or {}).get("ok") is False:
            result["issues"].append("declared_feature_prefix_check_failed")
        ledger = old.get("ledger") or {}
        result["declared_test_views"] = ledger.get("views")
        if ledger.get("views", 0) > 1:
            result["limits"].append("test_period_previously_reused")
    result["issues"] = sorted(set(result["issues"]))
    result["status"] = "INVALID" if result["issues"] else "PASS"
    if not result["issues"]:
        result["sample_status"] = "historical_oos_declared"
    return result
