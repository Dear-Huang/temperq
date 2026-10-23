"""Utility training inside shared CLI/worker, archives and independent accounts."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import shutil
import tempfile
import time

import pandas as pd
import torch
import yaml

from .. import experiments
from ..bench import targets
from ..observations.bundle import PITBundle
from ..portfolio.continuous import ContinuousConfig
from ..portfolio.comparison import AGGREGATE
from ..portfolio.contracts import timestamp
from ..portfolio.decision_evaluation import evaluate, serialize
from ..portfolio.prediction import environment
from ..portfolio.reference import Fees
from .decision import DecisionGRU
from .utility_fit import DecisionTrainingConfig, fit, validate_feedback


def policy_config(rc, protocol):
    portfolio = rc["portfolio"]
    production = portfolio["production"]
    return {"risk": portfolio["risk"], "cost": portfolio["cost"], "optimizer": portfolio["optimizer"],
        "fees": Fees(**protocol["fees"]), "min_history": rc["observations"]["min_history"],
        "production_policy": production["policy"], "production_buffer": production["buffer"],
        "dust_threshold": production["dust_threshold"], "fee_model": rc["continuous"]["fee_model"]}


def load_protocols(fold):
    result = {}
    for phase, entry in fold["protocols"].items():
        path = Path(entry["path"])
        if experiments.digest(path) != entry["file_hash"]:
            raise ValueError("utility protocol changed after config resolution")
        result[phase] = targets.read(path)
        if targets.validate_protocol(result[phase]) != entry["protocol_id"]:
            raise ValueError("utility protocol identity changed")
    return result


def _forecast_tables(rows, bundle, cutoff):
    seeds = pd.DataFrame(rows, columns=["code", "decision_time", "prediction", "fold", "seed"])
    if not rows:
        seeds = seeds.astype({"prediction": "float64", "fold": "int64", "seed": "int64"})
    averaged = seeds.groupby(["code", "decision_time", "fold"], as_index=False, sort=True)["prediction"].mean()
    labels = (bundle.labels_at(averaged, cutoff) if rows else pd.DataFrame({
        key: pd.Series(dtype="float64" if key == "label" else "object")
        for key in ("label", "label_available_time", "label_start", "label_end")}))
    return seeds, pd.concat([averaged, labels], axis=1)


def _outer_summary(evaluations):
    results = [result for row in evaluations for result in row["results"].values()]
    return {"outer_evaluation_status": "COMPLETED" if all(result["evaluation_status"] == "COMPLETED"
                                                           for result in results) else "DIAGNOSTIC_WITH_FAILURES",
            "outer_exact_failures": sum(result["coverage"]["exact_failures"] for result in results),
            "outer_continuous_failures": sum(result["coverage"]["continuous_failed"] for result in results)}


def compute(rc, cwd, log, out, target):
    started = time.monotonic()
    (out / "task.yaml").write_text(yaml.safe_dump(rc["card"], sort_keys=False), encoding="utf-8")
    (out / "config.resolved.yaml").write_text(yaml.safe_dump({k: v for k, v in rc.items() if not k.startswith("_")},
        sort_keys=False), encoding="utf-8")
    trials = {"schema_version": "utility_trials_v1", "config_hash": rc["config_hash"],
        "attempts": [{"fold": fold["id"], "seed": seed, "state": "queued"}
                     for fold in rc["folds"] for seed in rc["train"]["seeds"]]}
    experiments.write(out / "trials.json", trials)
    active = None
    old_threads = torch.get_num_threads()
    try:
        torch.set_num_threads(rc["train"]["cpu_threads"])
        bundle = PITBundle(rc["data"]["dir"], lookback=rc["observations"]["lookback"])
        source = Path(rc["initialization"]["run"])
        source_record = experiments.verify(source)
        experiments.write(out / "data_manifest.json", dict(bundle.identity, declaration=bundle.manifest))
        experiments.write(out / "observation_manifest.json", bundle.windows.manifest)
        experiments.write(out / "initialization.json", {"source": str(source), "run": source_record})
        frozen_protocols = {}
        for fold in rc["folds"]:
            protocols = load_protocols(fold)
            frozen_protocols[fold["id"]] = protocols
            for phase, protocol in protocols.items():
                experiments.write(out / f"protocol-fold-{fold['id']:04d}-{phase}.json", protocol)
                cutoff = fold["fit_cutoff"] if phase == "train" else fold["selection_cutoff"] if phase == "validation" else rc["evaluation_asof"]
                validate_feedback(bundle, protocol, cutoff)
            if len({experiments.object_digest(p["fees"]) for p in protocols.values()}) != 1:
                raise ValueError("utility train/validation/test account fees must match")
            for seed in rc["train"]["seeds"]:
                model = DecisionGRU.from_run(source, fold=rc["initialization"]["fold"], seed=seed, bundle=bundle)
                if timestamp(model.source["fit"]["context"]["selection_cutoff"]) > timestamp(protocols["train"]["sessions"][0]["decision_time"]):
                    raise ValueError("utility source initialization is later than its first training decision")
        fold_records, tables, evaluations = [], [], []
        continuous = ContinuousConfig(**rc["continuous"])
        for fold in rc["folds"]:
            protocols = frozen_protocols[fold["id"]]
            records = []
            for seed in rc["train"]["seeds"]:
                active = next(row for row in trials["attempts"] if row["fold"] == fold["id"] and row["seed"] == seed)
                active.update(state="running", started_at=experiments.now(), history=[])
                experiments.write(out / "trials.json", trials)
                log(f"utility fold {fold['id']} seed {seed}")
                model = DecisionGRU.from_run(source, fold=rc["initialization"]["fold"], seed=seed, bundle=bundle)
                control = DecisionGRU.from_run(source, fold=rc["initialization"]["fold"], seed=seed, bundle=bundle)
                trial_name = f"fold-{fold['id']:04d}-seed-{seed}"
                control_path = out / "initialization" / trial_name
                control_path.parent.mkdir(exist_ok=True)
                shutil.copytree(model.source["path"], control_path)
                policy = policy_config(rc, protocols["train"])
                training = DecisionTrainingConfig(**{k: v for k, v in rc["train"].items() if k != "seeds"},
                    seed=seed, variance_penalty=rc["utility"]["variance_penalty"], freeze_encoder=rc["model_config"]["freeze_encoder"])
                def training_log(entry):
                    if isinstance(entry, dict):
                        active["history"].append(entry)
                        experiments.write(out / "trials.json", trials)
                    log(entry)
                audit = None
                if "validation_audit" in rc:
                    from .validation_audit import ValidationAudit

                    audit = ValidationAudit(out / "validation-candidates" / trial_name, model, bundle, protocols["validation"],
                        config=training, context={key: fold[key] for key in ("fit_cutoff", "selection_cutoff")},
                        policy=policy, continuous=continuous, acceptance=rc["acceptance"])
                try:
                    fit_record = fit(model, bundle, protocols["train"], protocols["validation"], policy_config=policy,
                        context={key: fold[key] for key in ("fit_cutoff", "selection_cutoff")}, config=training,
                        continuous=continuous, log=training_log, candidate_audit=audit)
                finally:
                    if audit is not None:
                        audit_result = audit.finish(model.fit_record)
                        active["validation_audit_status"] = audit_result["status"]
                        experiments.write(out / "trials.json", trials)
                checkpoint = out / "models" / trial_name
                model.save(checkpoint)
                restored = DecisionGRU.load(checkpoint)
                if any(not torch.equal(a, b) for a, b in zip(model.net.parameters(), restored.net.parameters(), strict=True)):
                    raise ValueError("utility checkpoint reload differs")
                version = experiments.object_digest(experiments.inventory(checkpoint))
                results, audits = {}, {}
                for label, fitted in (("B1-float64-control", control), ("B2-utility", restored)):
                    trial_policy = dict(policy, model_version=f"{version}-{label}")
                    results[label], audits[label] = evaluate(fitted, bundle, protocols["test"], out / "accounts" / trial_name / label,
                        policy_config=trial_policy, continuous=continuous, acceptance=rc["acceptance"], log=log,
                        evaluation=rc.get("outer_evaluation"), feedback_cutoff=rc["evaluation_asof"])
                for decision in audits["B2-utility"]:
                    for code, prediction in zip(decision["assets"], decision["prediction"], strict=True):
                        if code in decision["eligible_assets"]:
                            tables.append({"code": code, "decision_time": decision["decision_time"], "prediction": prediction,
                                           "fold": fold["id"], "seed": seed})
                record = {"seed": seed, "checkpoint": checkpoint.relative_to(out).as_posix(),
                    "control_checkpoint": control_path.relative_to(out).as_posix(), "fit": fit_record,
                    "evaluation": results, "evaluation_model_version": version, "checkpoint_replay_exact": True}
                if audit is not None:
                    record["validation_audit"] = {"path": audit.root.relative_to(out).as_posix(),
                                                   "status": audit_result["status"]}
                records.append(record)
                evaluations.append({"fold": fold["id"], "seed": seed, "results": results})
                active.update(state="succeeded", finished_at=experiments.now(), checkpoint=record["checkpoint"],
                              comparison_eligible=all(result["closed_loop_comparison_eligible"] for result in results.values()))
                if "outer_evaluation" in rc:
                    active.update(training_state="succeeded", evaluation_status=_outer_summary([evaluations[-1]])["outer_evaluation_status"])
                experiments.write(out / "trials.json", trials)
                active = None
            fold_records.append({"fold": fold["id"], "fit_cutoff": fold["fit_cutoff"],
                                 "selection_cutoff": fold["selection_cutoff"], "seeds": records})
            experiments.write(out / "folds.json", {"schema_version": "utility_folds_v1", "folds": fold_records})
        if not tables and "outer_evaluation" not in rc:
            raise ValueError("utility run produced no eligible auxiliary forecasts")
        all_seeds, table = _forecast_tables(tables, bundle, rc["evaluation_asof"])
        version = experiments.object_digest({"models": experiments.inventory(out / "models"),
            "config": rc["config_hash"], "data": bundle.identity["content_hash"], "code": rc["_lineage"]["code_hash"]})
        for frame in (table, all_seeds):
            frame["model_version"], frame["task"] = version, rc["card"]["name"]
            frame["data_snapshot_id"] = bundle.manifest["data_snapshot_id"]
            frame["prediction_kind"] = "auxiliary_next_open_to_close_return_not_target_weight"
        table.to_parquet(out / "predictions.parquet", index=False)
        all_seeds.to_parquet(out / "seed_predictions.parquet", index=False)
        prediction_version = experiments.digest(out / "predictions.parquet")
        bundle.assert_unchanged()
        for fold in rc["folds"]:
            load_protocols(fold)
        if experiments.verify(source) != source_record or experiments.code_identity() != rc["_lineage"]["code_hash"]:
            raise ValueError("utility source or implementation changed during run")
        summary = {**rc["_lineage"], "run": rc["name"], "engine": rc["engine"], "model": rc["model"],
            "model_version": version, "prediction_version": prediction_version, "config_hash": rc["config_hash"],
            "data_snapshot_id": bundle.manifest["data_snapshot_id"], "data_content_hash": bundle.identity["content_hash"],
            "data_kind": bundle.manifest["data_kind"], "evidence_status": "SYNTHETIC_ONLY"
                if bundle.manifest["data_kind"] == "synthetic" else "DIAGNOSTIC_ONLY",
            "folds": len(rc["folds"]), "seeds": rc["train"]["seeds"], "evaluations": evaluations,
            "comparison_eligible": all(row["comparison_eligible"] for row in trials["attempts"]),
            "state_comparison_version": rc["acceptance"].get("state_comparison_version", AGGREGATE),
            "prediction_scope": "auxiliary_historical_only_no_final_refit", "environment": environment(),
            "economic_efficacy": "NOT_ESTABLISHED", "model_superiority": "NOT_ESTABLISHED", "go": False,
            "elapsed_seconds": time.monotonic() - started}
        if "validation_audit" in rc:
            summary["validation_audit_complete"] = all(row["validation_audit_status"] == "COMPLETED" for row in trials["attempts"])
        if "outer_evaluation" in rc:
            summary.update(_outer_summary(evaluations))
        experiments.write(out / "metrics.json", summary)
        lines = [f"# Utility research run: {rc['name']}", "", f"Evidence: {summary['evidence_status']}. "
            "Economic efficacy and model superiority: NOT_ESTABLISHED.", "",
            f"Proxy comparison eligible: {summary['comparison_eligible']}. "
            "Threshold breaches are diagnostic, not a successful economic comparison.", "",
            "| Fold | Seed | Model | Exact net return | Exact cost CNY | Proxy eligible |",
            "| --- | --- | --- | ---: | ---: | --- |"]
        for trial in evaluations:
            for name, result in trial["results"].items():
                lines.append(f"| {trial['fold']} | {trial['seed']} | {name} | {result['exact_net_return']:.8g} | "
                             f"{result['exact_total_cost']:.8g} | {result['closed_loop_comparison_eligible']} |")
        lines += ["", "Each episode has its own declared initial account. Seeds/folds are not concatenated into a fictitious trading path.",
            "", "Target submissions, exact accounts, continuous accounts, frozen-action diagnostics and daily differences are in accounts/.",
            "Auxiliary return forecasts are separate from target weights. No final forward refit; whole-fold rerun only."]
        if "validation_audit" in rc:
            lines += ["", f"Inner-validation candidate audit complete: {summary['validation_audit_complete']}. "
                      "All reached epochs and diagnostic ranking reversals are in validation-candidates/. "
                      "Selection remains continuous-validation strict improvement; exact diagnostics do not select epochs."]
        if "outer_evaluation" in rc:
            lines += ["", f"Outer evaluation: {summary['outer_evaluation_status']}; "
                f"exact decision failures: {summary['outer_exact_failures']}; "
                f"continuous failures: {summary['outer_continuous_failures']}. "
                "A completed job does not mean all decisions succeeded. "
                "All declared dates remain in the exact account; missing continuous differences are null. "
                "Recorded transient failures can be replayed, but their historical occurrence is not attested."]
        (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return summary
    except BaseException as exc:
        from ..jobs import Cancelled

        if active is not None:
            active.update(state="cancelled" if isinstance(exc, (Cancelled, KeyboardInterrupt)) else "failed",
                          finished_at=experiments.now(), error=f"{type(exc).__name__}: {exc}")
            if hasattr(exc, "qwb_training_failure"):
                active["failure_context"] = exc.qwb_training_failure
            elif hasattr(exc, "qwb_decision_failure"):
                active["failure_context"] = {"schema_version": "utility_evaluation_failure_v1",
                    "phase": "outer_evaluation", "decision": exc.qwb_decision_failure}
        for attempt in trials["attempts"]:
            if attempt["state"] == "queued":
                attempt["state"] = "not_run"
        trials["run_error"] = f"{type(exc).__name__}: {exc}"
        experiments.write(out / "trials.json", trials)
        raise
    finally:
        torch.set_num_threads(old_threads)


def verify(directory, *, data_dir=None, inputs=False):
    root = Path(directory).resolve()
    record = experiments.verify(root)
    rc = yaml.safe_load((root / "config.resolved.yaml").read_text(encoding="utf-8"))
    if rc["engine"] != "e2e_utility_v1" or record["code_hash"] != experiments.code_identity():
        raise ValueError("utility arithmetic verification requires its recorded implementation")
    config_hash = experiments.object_digest({key: value for key, value in rc.items()
        if key not in {"name", "output", "deviations", "config_hash"}})
    if config_hash != record["config_hash"]:
        raise ValueError("utility resolved configuration identity differs")
    bundle = PITBundle(data_dir or rc["data"]["dir"], lookback=rc["observations"]["lookback"])
    if bundle.identity["content_hash"] != record["data_content_hash"]:
        raise ValueError("utility verification observation content differs")
    expected_summary = experiments.read(root / "metrics.json")
    folds = experiments.read(root / "folds.json")["folds"]
    if [fold["fold"] for fold in folds] != [fold["id"] for fold in rc["folds"]]:
        raise ValueError("utility fold coverage differs from configuration")
    if inputs:
        for fold in rc["folds"]:
            load_protocols(fold)
        original = experiments.read(root / "initialization.json")
        if experiments.verify(Path(original["source"])) != original["run"]:
            raise ValueError("utility initialization source changed")
    old_threads = torch.get_num_threads()
    try:
        torch.set_num_threads(rc["train"]["cpu_threads"])
        forecast_rows, evaluation_rows = [], []
        for fold in folds:
            protocol = experiments.read(root / f"protocol-fold-{fold['fold']:04d}-test.json")
            resolved_fold = next(row for row in rc["folds"] if row["id"] == fold["fold"])
            if [row["seed"] for row in fold["seeds"]] != rc["train"]["seeds"]:
                raise ValueError("utility seed coverage differs from configuration")
            for phase in ("train", "validation", "test"):
                frozen = experiments.read(root / f"protocol-fold-{fold['fold']:04d}-{phase}.json")
                if targets.validate_protocol(frozen) != resolved_fold["protocols"][phase]["protocol_id"]:
                    raise ValueError("utility frozen protocol identity differs")
                cutoff = resolved_fold["fit_cutoff"] if phase == "train" else resolved_fold["selection_cutoff"] if phase == "validation" else rc["evaluation_asof"]
                validate_feedback(bundle, frozen, cutoff)
            for trial in fold["seeds"]:
                trial_name = f"fold-{fold['fold']:04d}-seed-{trial['seed']}"
                selected = DecisionGRU.load(root / trial["checkpoint"])
                if selected.fit_record != trial["fit"]:
                    raise ValueError("utility selected checkpoint fit/history differs")
                control = DecisionGRU.from_prediction(root / trial["control_checkpoint"])
                policy = policy_config(rc, protocol)
                if "validation_audit" in rc:
                    from .validation_audit import verify_audit

                    training = DecisionTrainingConfig(**{k: v for k, v in rc["train"].items() if k != "seeds"},
                        seed=trial["seed"], variance_penalty=rc["utility"]["variance_penalty"], freeze_encoder=rc["model_config"]["freeze_encoder"])
                    expected_path = f"validation-candidates/fold-{fold['fold']:04d}-seed-{trial['seed']}"
                    if trial.get("validation_audit") != {"path": expected_path, "status": "COMPLETED"}:
                        raise ValueError("validation candidate audit missing or incomplete")
                    verify_audit(root / expected_path, selected, bundle,
                        experiments.read(root / f"protocol-fold-{fold['fold']:04d}-validation.json"), config=training,
                        context={key: resolved_fold[key] for key in ("fit_cutoff", "selection_cutoff")},
                        policy=policy, continuous=ContinuousConfig(**rc["continuous"]), acceptance=rc["acceptance"], fit_record=trial["fit"])
                with tempfile.TemporaryDirectory(prefix="qwb-utility-verify-") as temporary:
                    for name, model in (("B1-float64-control", control), ("B2-utility", selected)):
                        recorded = root / "accounts" / trial_name / name
                        arguments = {"policy_config": dict(policy, model_version=f"{trial['evaluation_model_version']}-{name}"),
                                     "continuous": ContinuousConfig(**rc["continuous"]), "acceptance": rc["acceptance"]}
                        if "outer_evaluation" in rc:
                            from ..portfolio.evaluation_failures import verify as verify_outer

                            result, audit = verify_outer(model, bundle, protocol, recorded,
                                evaluation=rc["outer_evaluation"], feedback_cutoff=rc["evaluation_asof"], **arguments)
                        else:
                            result, audit = evaluate(model, bundle, protocol, Path(temporary) / name, **arguments)
                            for filename in ("policy.json", "submission.json", "continuous.json", "continuous-policy.json",
                                             "fixed-continuous-action-exact.json", "differences.json", "result.json"):
                                if experiments.read(recorded / filename) != experiments.read(Path(temporary) / name / filename):
                                    raise ValueError(f"utility policy/account arithmetic mismatch: {filename}")
                            targets.verify(recorded / "exact", inputs=False)
                        if result != trial["evaluation"][name]:
                            raise ValueError("utility fold evaluation summary differs")
                        if name == "B2-utility":
                            for decision in audit:
                                for code, prediction in zip(decision["assets"], decision["prediction"], strict=True):
                                    if code in decision["eligible_assets"]:
                                        forecast_rows.append({"code": code, "decision_time": decision["decision_time"],
                                            "prediction": prediction, "fold": fold["fold"], "seed": trial["seed"]})
                evaluation_rows.append({"fold": fold["fold"], "seed": trial["seed"], "results": trial["evaluation"]})
        if evaluation_rows != expected_summary["evaluations"]:
            raise ValueError("utility metric evaluation summary differs")
        version = experiments.object_digest({"models": experiments.inventory(root / "models"),
            "config": rc["config_hash"], "data": bundle.identity["content_hash"], "code": record["code_hash"]})
        if version != record["model_version"]:
            raise ValueError("utility model collection identity differs")
        seeds, averaged = _forecast_tables(forecast_rows, bundle, rc["evaluation_asof"])
        for frame in (seeds, averaged):
            frame["model_version"], frame["task"] = version, rc["card"]["name"]
            frame["data_snapshot_id"] = bundle.manifest["data_snapshot_id"]
            frame["prediction_kind"] = "auxiliary_next_open_to_close_return_not_target_weight"
        if (not seeds.equals(pd.read_parquet(root / "seed_predictions.parquet"))
                or not averaged.equals(pd.read_parquet(root / "predictions.parquet"))
                or experiments.digest(root / "predictions.parquet") != record["prediction_version"]):
            raise ValueError("utility auxiliary forecast arithmetic differs")
        if expected_summary["comparison_eligible"] != all(all(result["closed_loop_comparison_eligible"]
                for result in trial["results"].values()) for trial in expected_summary["evaluations"]):
            raise ValueError("utility comparison eligibility summary differs")
        if expected_summary["state_comparison_version"] != rc["acceptance"].get("state_comparison_version", AGGREGATE):
            raise ValueError("utility state comparison version differs")
        if "validation_audit" in rc and expected_summary.get("validation_audit_complete") is not True:
            raise ValueError("utility validation candidate audit is incomplete")
        if "outer_evaluation" in rc:
            if any(expected_summary.get(key) != value for key, value in _outer_summary(evaluation_rows).items()):
                raise ValueError("outer evaluation metric summary differs")
            attempts = experiments.read(root / "trials.json")["attempts"]
            if [(row["fold"], row["seed"]) for row in attempts] != [(row["fold"], row["seed"]) for row in evaluation_rows]:
                raise ValueError("outer evaluation trial coverage differs")
            for attempt, row in zip(attempts, evaluation_rows, strict=True):
                if (attempt["state"] != "succeeded" or attempt.get("training_state") != "succeeded"
                        or attempt.get("evaluation_status") != _outer_summary([row])["outer_evaluation_status"]
                        or attempt["comparison_eligible"] != all(result["closed_loop_comparison_eligible"]
                                                               for result in row["results"].values())):
                    raise ValueError("outer evaluation trial state differs")
        bundle.assert_unchanged()
        return {"status": "PASS", "scope": "utility_policy_and_account_arithmetic_not_training_reproduction",
                "inputs_checked": inputs, "evidence_status": expected_summary["evidence_status"],
                "comparison_eligible": expected_summary["comparison_eligible"],
                **({"validation_audit_complete": True} if "validation_audit" in rc else {})}
    finally:
        torch.set_num_threads(old_threads)
