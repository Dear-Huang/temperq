"""Signal score, coverage and references on a candidate-independent sample."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import metrics
from .contracts import REFERENCES
from .statistics import block_summary, conclusion


def evaluation_mask(dates, labels, universe, references, protocol):
    selected = np.isin(dates.year, protocol["evaluation_years"])
    eligible = universe[selected] & np.isfinite(labels[selected])
    for name in REFERENCES[:-1]:
        eligible &= np.isfinite(references[name][selected])
    active = eligible.sum(axis=1) >= protocol["coverage"]["min_names"]
    eligible &= active[:, None]
    return selected, eligible, active


def evaluate(dates, labels, universe, predictions, references: dict, protocol: dict,
             evidence: dict, anchor_evidence: dict) -> tuple[dict, pd.DataFrame]:
    if tuple(references) != REFERENCES:
        raise ValueError("missing or reordered fixed references")
    selected, eligible, active = evaluation_mask(dates, labels, universe, references, protocol)
    if any(not (dates.year == year).any() for year in protocol["evaluation_years"]):
        raise ValueError("not all evaluation years are present in the snapshot")
    days = dates[selected]
    y = labels[selected]
    if not active.any():
        raise ValueError("no mature evaluation dates with at least 20 eligible securities")
    expected = eligible.sum(axis=1)
    have = (eligible & np.isfinite(predictions[selected])).sum(axis=1)
    coverage = np.divide(have, expected, out=np.full(len(days), np.nan), where=expected > 0)
    complete = bool(np.array_equal(have, expected))
    total = float(have.sum() / expected.sum())
    y = np.where(eligible, y, np.nan)
    paths = {"candidate": metrics.rank_ic(np.where(eligible, predictions[selected], np.nan), y)}
    reference_complete = True
    for name, values in references.items():
        reference_complete &= bool(np.isfinite(values[selected][eligible]).all())
        paths[name] = metrics.rank_ic(np.where(eligible, values[selected], np.nan), y)
    daily = pd.DataFrame({"date": days, "eligible_names": expected, "predicted_names": have,
                          "coverage": coverage, "date_status": np.where(active, "scored", "excluded")})
    for name, values in paths.items():
        daily[f"ic__{name}"] = values
    series = [*paths.values(), *(paths["candidate"] - paths[name] for name in REFERENCES)]
    summaries = block_summary(np.column_stack(series), **protocol["statistics"],
                              family_size=protocol["assessment"]["comparison_family_size"])
    models = {}
    for i, name in enumerate(paths):
        s = summaries[i]
        models[name] = {**s, "signal_score": None if s["mean"] is None else 100 * s["mean"],
                        "score_ci95": None if s["ci95"] is None else [100 * v for v in s["ci95"]]}
    differences = {}
    for i, name in enumerate(REFERENCES):
        s = summaries[len(paths) + i]
        family_summary = {**s, "ci95": s["simultaneous_ci95"]}
        differences[name] = {**s, "conclusion": conclusion(family_summary),
                             "marginal_conclusion": conclusion(s)}
    all_dates_valid = all(np.isfinite(v[active]).all() for v in paths.values())
    issues = []
    if not complete:
        issues.append("candidate_prediction_coverage_incomplete")
    if not reference_complete:
        issues.append("reference_prediction_coverage_incomplete")
    if not all_dates_valid:
        issues.append("undefined_daily_ic_constant_or_insufficient_predictions")
    if evidence["status"] != "PASS":
        issues.append("candidate_training_evidence_missing_or_invalid")
    if anchor_evidence["status"] != "PASS":
        issues.append("reference_training_evidence_missing_or_invalid")
    comparable = not issues
    raw_signal = conclusion(models["candidate"], protocol["signal_threshold"])
    signal = raw_signal if comparable else {
        "status": "INVALID" if evidence["status"] == "INVALID" else "NOT_EVALUATED",
        "reason": ";".join(issues), "diagnostic_conclusion": raw_signal}
    rank = None
    if comparable:
        value = round(models["candidate"]["mean"], 8)
        rank = {"position": 1 + sum(round(models[name]["mean"], 8) > value for name in REFERENCES),
                "out_of": 5, "ties": sum(round(models[name]["mean"], 8) == value for name in REFERENCES),
                "scope": "development_fixed_references", "basis": "mean_daily_rank_ic",
                "precision": 8, "point_estimate_only": True}
    by_year = {}
    for year in protocol["evaluation_years"]:
        rows = days.year == year
        values = paths["candidate"][rows]
        finite = values[np.isfinite(values)]
        by_year[str(year)] = {"n": len(finite), "mean": float(finite.mean()) if len(finite) else None}
    candidate = paths["candidate"]
    return {
        "scorable": models["candidate"]["mean"] is not None, "comparable": comparable,
        "formal_score_eligible": False, "qualification": "development_diagnostic",
        "issues": issues, "models": models, "paired_differences": differences,
        "reference_rank": rank, "signal": signal, "by_year": by_year,
        "coverage": {"total": total, "min_daily": float(np.nanmin(coverage)),
                     "eligible_pairs": int(expected.sum()), "predicted_pairs": int(have.sum()),
                     "calendar_dates": len(days), "eligible_dates": int(active.sum()),
                     "valid_ic_dates": int(np.isfinite(candidate).sum()),
                     "excluded_dates": int((~active).sum()),
                     "missing_ic_dates": int((active & ~np.isfinite(candidate)).sum())},
    }, daily
