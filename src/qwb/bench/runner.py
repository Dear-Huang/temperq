"""Load existing artifacts, score without retraining, and compare matching runs."""

from __future__ import annotations

import importlib.metadata
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .. import daily, dataset, experiments
from . import VERSION, diagnostics, economics, signal
from .assessment import assess
from .contracts import (align_predictions, digest, object_digest, read_json, read_yaml,
                        validate_protocol, verify_inputs, write_json)
from .evidence import inspect_evidence
from .report import comparison_report, model_report
from .statistics import block_summary, conclusion


def implementation_hash() -> str:
    root = Path(__file__).resolve().parent
    paths = [*sorted(root.glob("*.py")),
             *(root.parent / name for name in ("daily.py", "dataset.py", "metrics.py", "folds.py", "taskcard.py", "netcost.py")),
             root.parent / "rules" / "__init__.py", root.parent / "rules" / "cn_a.yaml"]
    return object_digest({p.relative_to(root.parent).as_posix(): digest(p) for p in paths})


def environment() -> dict:
    return {"python": sys.version.split()[0], "platform": platform.platform(),
            "packages": {name: importlib.metadata.version(name) for name in
                         ("numpy", "pandas", "pyarrow", "pyyaml", "ashare-tcost")}}


def new_output(output) -> Path:
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    return out


def evaluate_model(source, protocol_path, output, *, data_dir=None, log=print,
                   workdir=None, evaluation_id=None) -> dict:
    protocol = read_yaml(protocol_path)
    validate_protocol(protocol)
    source = Path(source).resolve()
    requested_source = source
    run_dir = source if source.is_dir() else None
    prediction = source / "predictions.parquet" if run_dir else source
    out = new_output(output)
    write_json(out / "protocol.json", protocol)
    s = {"schema_version": 2, "scorer_version": VERSION,
         "evaluation_id": evaluation_id or uuid.uuid4().hex,
         "implementation_hash": implementation_hash(), "protocol_hash": object_digest(protocol),
         "created_at": datetime.now(timezone.utc).isoformat(), "status": "INVALID",
         "task": protocol["task"]["name"], "evaluation_years": protocol["evaluation_years"],
         "data_snapshot_id": protocol["data"]["snapshot_id"], "statistics": protocol["statistics"],
         "source": str(source), "output": str(out), "errors": [], "artifacts": {},
         "economic": {"status": "NOT_EVALUATED", "reason": "account_inputs_not_evaluated"},
         "platform": {"status": "NOT_EVALUATED", "reason": "full_workflow_runner_not_implemented"}}
    try:
        source = experiments.frozen_source(source)
        run_dir = source if source.is_dir() else None
        prediction = source / "predictions.parquet" if run_dir else source
        s["source"] = str(source)
        s["requested_source"] = str(requested_source)
        s["lineage"] = None
        if run_dir and (run_dir / "run.json").is_file():
            record = experiments.verify(run_dir)
            write_json(out / "training-run.json", record)
            s["artifacts"]["training-run.json"] = digest(out / "training-run.json")
            s["lineage"] = {k: record.get(k) for k in
                            ("run_id", "experiment_id", "model_version", "prediction_version", "config_hash", "code_hash", "origin", "data_content_hash")}
            s["lineage"]["artifact_hash"] = record["artifact_hash"]
        s["protocol_id"] = s["protocol_hash"]
        s["prediction_hash"] = digest(prediction)
        log("verifying snapshot and frozen reference SHA-256 inventories")
        root, anchor = verify_inputs(protocol, data_dir)
        log("loading snapshot and rebuilding labels/universe (no training)")
        loaded = dataset.load(root)
        panel = loaded["P"]
        dates, codes = panel["close"].index, panel["close"].columns
        st = None
        u = protocol["universe"]["universe"]
        if u in {"st", "all_ex_st", *daily.INDEX_CODES} and loaded["st"] is not None:
            st = daily.st_wide(loaded["st"], dates, codes)
        members = None
        if u in daily.INDEX_CODES:
            if loaded["members"] is None:
                raise ValueError("index universe requires historical constituent snapshots")
            members = daily.members_wide(loaded["members"], dates, codes, daily.INDEX_CODES[u])
            if st is not None:
                members &= ~st
        universe = daily.universe(panel, protocol["universe"], st, members).to_numpy()
        labels = daily.labels(panel, protocol["task"]).to_numpy()
        context = diagnostics.build_context(panel, protocol)
        adjusted = panel["close"] * panel["adj_factor"]
        changes = adjusted / adjusted.shift(1) - 1
        references = {"reversal_5d": -(adjusted / adjusted.shift(5) - 1).to_numpy(),
                      "momentum_20d": (adjusted / adjusted.shift(20) - 1).to_numpy(),
                      "low_vol_20d": -changes.rolling(20, min_periods=15).std().to_numpy()}
        del adjusted, changes, st
        predictions, s["submission"] = align_predictions(prediction, dates, codes, protocol)
        if (s.get("lineage") or {}).get("prediction_version"):
            if s["submission"]["model_version"] != s["lineage"]["prediction_version"]:
                raise ValueError("prediction table version differs from training lineage")
        references["lgbm_anchor_v1"], _ = align_predictions(
            anchor / "predictions.parquet", dates, codes, protocol)
        s["evidence"] = inspect_evidence(run_dir, dates, protocol)
        s["anchor_evidence"] = inspect_evidence(anchor, dates, protocol)
        log("computing daily Rank IC and paired date-block confidence intervals")
        evaluation, path = signal.evaluate(dates, labels, universe, predictions, references,
                                           protocol, s["evidence"], s["anchor_evidence"])
        s["evaluation"] = evaluation
        log("diagnosing period/liquidity weaknesses and practical evidence gaps")
        detail, diagnostic_path = diagnostics.evaluate(
            dates, labels, universe, predictions, references, protocol, path, context)
        s["diagnostics"] = detail
        log("replaying independent cash/units accounts and predeclared execution-cost stresses")
        try:
            economic, accounts, fills, holdings = economics.evaluate(
                panel, root, universe, predictions, references, protocol)
            economics.verify_ledger(accounts, fills, holdings, protocol)
            s["economic"] = economic
            for name, frame in (("account_paths.parquet", accounts), ("fills.parquet", fills),
                                ("holdings.parquet", holdings)):
                frame.to_parquet(out / name, index=False)
                s["artifacts"][name] = digest(out / name)
            del accounts, fills, holdings
        except (ValueError, KeyError, ImportError) as exc:
            s["economic"] = {"status": "NOT_EVALUATED", "reason": str(exc),
                             "formal_score_eligible": False}
        del panel
        write_json(out / "economics.json", s["economic"])
        s["artifacts"]["economics.json"] = digest(out / "economics.json")
        s["readiness"] = assess(evaluation, detail, s["evidence"], protocol, s["economic"])
        s["status"] = "COMPLETED"
        path.to_parquet(out / "daily_metrics.parquet", index=False)
        s["artifacts"]["daily_metrics.parquet"] = digest(out / "daily_metrics.parquet")
        diagnostic_path.to_parquet(out / "diagnostic_paths.parquet", index=False)
        write_json(out / "diagnostics.json", detail)
        write_json(out / "readiness.json", s["readiness"])
        for name in ("diagnostic_paths.parquet", "diagnostics.json", "readiness.json"):
            s["artifacts"][name] = digest(out / name)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        s["status"] = "INVALID"
        s.pop("evaluation", None)
        s.pop("diagnostics", None)
        s.pop("readiness", None)
        s["errors"].append(f"{type(exc).__name__}: {exc}")
    write_json(out / "environment.json", environment())
    s["artifacts"]["environment.json"] = digest(out / "environment.json")
    s["artifacts"]["protocol.json"] = digest(out / "protocol.json")
    (out / "report.md").write_text(model_report(s), encoding="utf-8")
    s["artifacts"]["report.md"] = digest(out / "report.md")
    write_json(out / "scorecard.json", s)
    write_json(out / "checksums.json", {"schema_version": 1,
               "files": {**s["artifacts"], "scorecard.json": digest(out / "scorecard.json")},
               "scope": "local_artifact_integrity_not_third_party_authentication"})
    if workdir is not None:
        experiments.register_evaluation(Path(workdir), out, s)
    return s


def verify_result(root, *, inputs=False, integrity_only=False) -> dict:
    if integrity_only and inputs:
        raise ValueError("--integrity-only cannot be combined with --inputs; use the original scorer for input/arithmetic verification")
    root = Path(root).resolve()
    checksums = read_json(root / "checksums.json")
    required = {"scorecard.json", "protocol.json", "report.md", "environment.json"}
    files = checksums["files"]
    if not isinstance(files, dict) or not required <= set(files):
        raise ValueError("incomplete scored artifact manifest")
    for name, expected in files.items():
        if Path(name).name != name or digest(root / name) != expected:
            raise ValueError(f"scored artifact changed: {name}")
    s = read_json(root / "scorecard.json")
    protocol = read_json(root / "protocol.json")
    if object_digest(protocol) != s["protocol_hash"]:
        raise ValueError("scored protocol hash mismatch")
    if files != {**s["artifacts"], "scorecard.json": digest(root / "scorecard.json")}:
        raise ValueError("scorecard artifact manifest mismatch")
    if s.get("lineage"):
        if "training-run.json" not in files:
            raise ValueError("missing frozen training lineage")
        record = read_json(root / "training-run.json")
        if any(record.get(key) != value for key, value in s["lineage"].items()):
            raise ValueError("evaluation and frozen training lineage differ")
        if record.get("artifacts", {}).get("predictions.parquet") != s.get("prediction_hash"):
            raise ValueError("evaluation prediction does not belong to training version")
    if integrity_only:
        return {"status": "PASS", "result_status": s["status"], "checked_files": len(files),
                "inputs_checked": False, "daily_mean_recomputed": False,
                "account_ledger_recomputed": False,
                "scope": "local_artifact_integrity_only_not_arithmetic_or_certification"}
    validate_protocol(protocol)
    if s["status"] == "COMPLETED":
        needed = {"daily_metrics.parquet", "diagnostic_paths.parquet", "diagnostics.json", "readiness.json", "economics.json"}
        if not needed <= set(files):
            raise ValueError("missing scored diagnostic/evidence artifacts")
        path = pd.read_parquet(root / "daily_metrics.parquet")
        means = path[["ic__" + name for name in s["evaluation"]["models"]]].mean()
        for name, model in s["evaluation"]["models"].items():
            actual = means["ic__" + name]
            expected = model["mean"]
            if not ((expected is None and pd.isna(actual)) or
                    (expected is not None and np.isclose(actual, expected, rtol=0, atol=1e-12))):
                raise ValueError(f"daily IC does not reproduce the score: {name}")
            if expected is not None and not np.isclose(model["signal_score"], expected * 100,
                                                      rtol=0, atol=1e-12):
                raise ValueError("signal score/unit conversion mismatch")
        if (read_json(root / "diagnostics.json") != s["diagnostics"]
                or read_json(root / "readiness.json") != s["readiness"]
                or read_json(root / "economics.json") != s["economic"]):
            raise ValueError("report evidence differs from scorecard")
        if s["economic"]["status"] == "COMPLETED":
            if not {"account_paths.parquet", "fills.parquet", "holdings.parquet"} <= set(files):
                raise ValueError("missing account ledger artifacts")
            accounts, fills, holdings = (pd.read_parquet(root / name) for name in
                ("account_paths.parquet", "fills.parquet", "holdings.parquet"))
            economics.verify_ledger(accounts, fills, holdings, protocol)
            stored = s["economic"]["results"]
            again = economics.summarize(accounts, protocol)
            # Results written before a field existed are checked on the fields they stored.
            again = {sc: {n: {k: v for k, v in m.items() if k in stored.get(sc, {}).get(n, {})}
                          for n, m in ms.items()} for sc, ms in again.items()}
            if again != stored:
                raise ValueError("account paths do not reproduce economic metrics")
    if inputs:
        verify_inputs(protocol)
        source = Path(s["source"])
        prediction = source / "predictions.parquet" if source.is_dir() else source
        if digest(prediction) != s["prediction_hash"]:
            raise ValueError("candidate predictions changed after evaluation")
        if s.get("lineage"):
            record = experiments.verify(source)
            if record != read_json(root / "training-run.json"):
                raise ValueError("training run changed after evaluation")
        for section in ("evidence", "anchor_evidence"):
            base = source if section == "evidence" else Path(protocol["anchor"]["run_dir"])
            for name, expected in s.get(section, {}).get("files", {}).items():
                if Path(name).name != name or digest(base / name) != expected:
                    raise ValueError(f"training evidence changed: {section}/{name}")
    return {"status": "PASS", "result_status": s["status"], "checked_files": len(files),
            "inputs_checked": inputs, "daily_mean_recomputed": s["status"] == "COMPLETED",
            "account_ledger_recomputed": s["economic"]["status"] == "COMPLETED",
            "scope": "local_integrity_and_arithmetic_not_training_or_live_certification"}


def load_scored_run(root: Path) -> tuple[dict, pd.DataFrame]:
    verify_result(root)
    s = read_json(root / "scorecard.json")
    if s["status"] != "COMPLETED" or not s["evaluation"]["comparable"]:
        raise ValueError(f"run lacks complete comparable signal evidence: {root}")
    for name, expected in s["artifacts"].items():
        if Path(name).name != name or digest(root / name) != expected:
            raise ValueError(f"scored artifact changed: {name}")
    protocol = read_json(root / "protocol.json")
    validate_protocol(protocol)
    if object_digest(protocol) != s["protocol_hash"]:
        raise ValueError("scored protocol hash mismatch")
    if s["implementation_hash"] != implementation_hash():
        raise ValueError("scorer implementation changed; rescore both candidates")
    return s, pd.read_parquet(root / "daily_metrics.parquet")


def compare_models(old, new, output) -> dict:
    old_root, new_root = Path(old).resolve(), Path(new).resolve()
    a, da = load_scored_run(old_root)
    b, db = load_scored_run(new_root)
    if a["protocol_hash"] != b["protocol_hash"] or a["scorer_version"] != b["scorer_version"]:
        raise ValueError("different protocols/scorers cannot be ranked together")
    columns = ["date", "eligible_names", *(f"ic__{name}" for name in
               ("reversal_5d", "momentum_20d", "low_vol_20d", "lgbm_anchor_v1"))]
    if not da[columns].equals(db[columns]):
        raise ValueError("evaluation dates, eligible samples or reference paths differ")
    difference = block_summary((db["ic__candidate"] - da["ic__candidate"]).to_numpy(),
                               **a["statistics"])[0]
    protocol = read_json(old_root / "protocol.json")
    minimum_gain = protocol["assessment"]["min_anchor_gain_ic"]
    ci = difference["ci95"]
    decision = ("INCONCLUSIVE" if ci is None else "IMPROVED" if ci[0] > minimum_gain
                else "DEGRADED" if ci[1] < 0 else "NO_CHANGE" if ci == [0, 0]
                else "BELOW_MINIMUM_GAIN" if ci[0] >= 0 and ci[1] <= minimum_gain
                else "INCONCLUSIVE")
    s = {"schema_version": 1, "track": "signal", "qualification": "development_historical",
         "protocol_hash": a["protocol_hash"], "scorer_version": VERSION,
         "implementation_hash": implementation_hash(),
         "old": {"path": str(old_root), "scorecard_hash": digest(old_root / "scorecard.json")},
         "new": {"path": str(new_root), "scorecard_hash": digest(new_root / "scorecard.json")},
         "lineage": {"old": a.get("lineage"), "new": b.get("lineage")},
         "difference": difference, "signal_score_gain": 100 * difference["mean"],
         "conclusion": conclusion(difference, minimum_gain),
         "signal_decision": {"status": decision, "scope": "paired_rank_ic_only",
                             "minimum_gain_ic": minimum_gain},
         "minimum_signal_score_gain": 100 * minimum_gain, "statistics": a["statistics"]}
    s["economic_comparison"] = {"status": "NOT_EVALUATED", "reason": "both_account_paths_required"}
    if a["economic"]["status"] == b["economic"]["status"] == "COMPLETED":
        aa = pd.read_parquet(old_root / "account_paths.parquet")
        bb = pd.read_parquet(new_root / "account_paths.parquet")
        results = {}
        for scenario in a["economic"]["results"]:
            base_cols = ["date", "equity", "cash", "net_return", "total_cost"]
            def select(frame, name):
                return frame.loc[(frame["scenario"] == scenario) & (frame["strategy"] == name)].reset_index(drop=True)
            if not select(aa, "equal_weight")[base_cols].equals(select(bb, "equal_weight")[base_cols]):
                raise ValueError("economic reference paths differ")
            ca, cb = select(aa, "candidate"), select(bb, "candidate")
            if not ca["date"].equals(cb["date"]):
                raise ValueError("economic candidate dates differ")
            delta = block_summary((cb["net_return"] - ca["net_return"]).to_numpy()[1:], **a["statistics"])[0]
            qualified = all(x["economic"]["results"][scenario]["candidate"]["qualification"] == "CONDITIONAL_RESEARCH" for x in (a, b))
            results[scenario] = {"annual_net_gain_bp": delta["mean"] * 252 * 10000,
                "ci95_annual_net_gain_bp": None if delta["ci95"] is None else [v * 252 * 10000 for v in delta["ci95"]],
                "conclusion": conclusion(delta) if qualified else {"status": "NOT_EVALUATED", "reason": "diagnostic_valuation_or_data_gaps"},
                "qualification": "CONDITIONAL_RESEARCH" if qualified else "DIAGNOSTIC_ONLY"}
            operations = ("commission", "stamp_tax", "slippage", "mean_one_way_turnover",
                          "requested_orders", "rejected_orders", "scaled_orders", "data_gap_orders",
                          "stale_close_name_days", "unexplained_data_gap_orders",
                          "unexplained_stale_close_name_days")
            previous = a["economic"]["results"][scenario]["candidate"]
            current = b["economic"]["results"][scenario]["candidate"]
            results[scenario]["operational_deltas"] = {
                key: {"old": previous[key], "new": current[key], "delta": current[key] - previous[key]}
                for key in operations if key in previous and key in current}
        s["economic_comparison"] = {"status": "COMPLETED", "results": results,
            "scope": "paired_research_accounts_not_full_platform_workflows", "formal_score_eligible": False}
    out = new_output(output)
    write_json(out / "comparison.json", s)
    (out / "report.md").write_text(comparison_report(s), encoding="utf-8")
    return s
