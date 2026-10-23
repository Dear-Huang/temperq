"""Causal rolling prediction runs inside the shared archive/worker lifecycle."""

from __future__ import annotations

import importlib.metadata
import platform
import time

import numpy as np
import pandas as pd
import yaml

from .. import experiments, folds, metrics
from ..evidence_export import _finite
from ..observations.bundle import PITBundle
from .baselines import FactorRidge


def _model(rc, seed):
    cfg = {k: v for k, v in rc["model_config"].items() if k != "name"}
    if rc["model"] == "ridge_factor_v1":
        return FactorRidge(**cfg, max_seconds=rc["train"]["max_seconds"])
    try:
        from .sequence import GRUConfig, GRUPredictor
    except ImportError as exc:
        raise ValueError("sequence_gru_v1 needs the optional e2e environment; see docs/E2E_IMPLEMENTATION.md") from exc
    return GRUPredictor(GRUConfig(**cfg, seed=seed, max_seconds=rc["train"]["max_seconds"]))


def prediction_metrics(table, calendar):
    predicted = table.pivot(index="decision_time", columns="code", values="prediction").reindex(calendar)
    labels = table.pivot(index="decision_time", columns="code", values="label").reindex_like(predicted)
    pairs = np.isfinite(predicted.to_numpy()) & np.isfinite(labels.to_numpy())
    error = predicted.to_numpy() - labels.to_numpy()
    daily_ic = metrics.rank_ic(predicted.to_numpy(), labels.to_numpy())
    return _finite({"rank_ic": metrics.newey_west(daily_ic, lag=0),
                    "rank_ic_min_names": metrics.MIN_NAMES,
                    "mse": float(np.mean(error[pairs] ** 2)) if pairs.any() else None,
                    "n_predictions": len(table), "n_evaluable": int(pairs.sum()),
                    "n_missing_labels": len(table) - int(pairs.sum()),
                    "n_test_dates": len(calendar), "n_predicted_dates": int(predicted.notna().any(axis=1).sum()),
                    "n_ic_dates": int(np.isfinite(daily_ic).sum()),
                    "scope": "historical_development_signal_only"})


def compute(rc, cwd, log, out, target):
    resolved = {k: v for k, v in rc.items() if not k.startswith("_")}
    (out / "task.yaml").write_text(yaml.safe_dump(rc["card"], sort_keys=False), encoding="utf-8")
    (out / "config.resolved.yaml").write_text(yaml.safe_dump(resolved, sort_keys=False), encoding="utf-8")
    torch, old_threads = None, None
    if rc["model"] == "sequence_gru_v1":
        try:
            import torch
        except ImportError as exc:
            raise ValueError("sequence_gru_v1 needs the optional e2e environment") from exc
        old_threads = torch.get_num_threads()
        torch.set_num_threads(rc["train"]["cpu_threads"])
    try:
        return _compute(rc, cwd, log, out, target)
    finally:
        if torch is not None:
            torch.set_num_threads(old_threads)


def _compute(rc, cwd, log, out, target):
    started = time.monotonic()
    bundle = PITBundle(rc["data"]["dir"], lookback=rc["observations"]["lookback"])
    experiments.write(out / "data_manifest.json", dict(bundle.identity, declaration=bundle.manifest))
    experiments.write(out / "observation_manifest.json", bundle.windows.manifest)
    fc = rc["folds"]
    last = pd.Timestamp(fc["last_test"])
    if last not in bundle.calendar or pd.Timestamp(fc["first_test"]) not in bundle.calendar:
        raise ValueError("first_test and last_test must be explicit trading sessions in the bundle")
    plan = folds.rolling_time_folds(bundle.calendar[bundle.calendar <= last],
                                   **{k: v for k, v in fc.items() if k not in {"last_test", "embargo"}})
    trials = {"schema_version": "prediction_trials_v1", "config_hash": rc["config_hash"],
              "created_at": experiments.now(), "data_content_hash": bundle.identity["content_hash"],
              "seeds": rc["train"]["seeds"], "fold_plan": [fold.to_dict() for fold in plan],
              "attempts": [{"fold": fold.fold, "seed": seed, "state": "queued"}
                           for fold in plan for seed in rc["train"]["seeds"]]}
    experiments.write(out / "trials.json", trials)
    active = None
    try:
        x, mask, samples, coverage = bundle.samples(
            min_history=rc["observations"]["min_history"], last_test=fc["last_test"],
            max_samples=rc["train"]["max_samples"], log=log)
        coverage.to_parquet(out / "coverage.parquet", index=False)
        decisions = pd.to_datetime(samples["decision_time"], utc=True)
        manifests, seed_tables = [], []
        for fold in plan:
            gap = pd.Timedelta(fc["embargo"])
            fit_cutoff = pd.Timestamp(fold.validation_start) - gap
            selection_cutoff = pd.Timestamp(fold.test_start) - gap
            train_labels = bundle.labels_at(samples, fit_cutoff)
            selection_labels = bundle.labels_at(samples, selection_cutoff)
            after_train = decisions >= pd.Timestamp(fold.validation_start)
            labels = train_labels.copy()
            labels.loc[after_train, :] = selection_labels.loc[after_train, :]
            tr, va, te, context = folds.time_fold_masks(samples["decision_time"], labels["label_end"], fold,
                label_available=labels["label_available_time"], embargo=fc["embargo"])
            y = labels["label"].to_numpy(float)
            tr = tr & np.isfinite(y)
            va = va & np.isfinite(y)
            if decisions[tr].nunique() < fc["min_train_days"] or not va.any():
                raise ValueError("insufficient usable training/validation dates after causal eligibility and maturity")
            context.update(n_train=int(tr.sum()), n_validation=int(va.sum()),
                           training_labels_asof=str(fit_cutoff), validation_labels_asof=str(selection_cutoff),
                           observation_recipe=bundle.windows.manifest["recipe_hash"],
                           data_content_hash=bundle.identity["content_hash"])
            records = []
            for seed in rc["train"]["seeds"]:
                active = next(row for row in trials["attempts"] if row["fold"] == fold.fold and row["seed"] == seed)
                active.update(state="running", started_at=experiments.now())
                experiments.write(out / "trials.json", trials)
                log(f"fold {fold.fold}, seed {seed}, train {int(tr.sum())}, validation {int(va.sum())}, test {int(te.sum())}")
                model = _model(rc, seed)
                fit = model.fit(x[tr], mask[tr], y[tr], x[va], mask[va], y[va], context=context, log=log)
                prediction = model.predict(x[te], mask[te])
                directory = out / "models" / f"fold-{fold.fold:04d}-seed-{seed}"
                model.save(directory)
                restored = type(model).load(directory)
                replay = restored.predict(x[te], mask[te])
                if not np.array_equal(prediction, replay):
                    raise ValueError("saved checkpoint prediction replay differs")
                table = samples.loc[te].copy()
                table["prediction"], table["fold"], table["seed"] = prediction, fold.fold, seed
                seed_tables.append(table)
                record = {"seed": seed, "checkpoint": directory.relative_to(out).as_posix(), "fit": fit,
                          "checkpoint_replay_exact": True}
                records.append(record)
                active.update(state="succeeded", finished_at=experiments.now(), checkpoint=record["checkpoint"])
                experiments.write(out / "trials.json", trials)
                active = None
            manifests.append(dict(context, seeds=records))
            experiments.write(out / "folds.json", {"schema_version": "prediction_folds_v1", "folds": manifests})
        all_seeds = pd.concat(seed_tables, ignore_index=True)
        table = all_seeds.groupby(["code", "decision_time", "fold"], as_index=False, sort=True)["prediction"].mean()
        evaluation = bundle.labels_at(table, rc["evaluation_asof"])
        table = pd.concat([table, evaluation], axis=1)
        version = experiments.object_digest({"fold_models": experiments.inventory(out / "models"),
                                             "config_hash": rc["config_hash"], "task": rc["card"],
                                             "data": bundle.identity["content_hash"],
                                             "code_hash": rc["_lineage"]["code_hash"]})
        for frame in (table, all_seeds):
            frame["model_version"], frame["task"] = version, rc["card"]["name"]
            frame["data_snapshot_id"] = bundle.manifest["data_snapshot_id"]
        table.to_parquet(out / "predictions.parquet", index=False)
        all_seeds.to_parquet(out / "seed_predictions.parquet", index=False)
        test_days = bundle.calendar[(bundle.calendar >= pd.Timestamp(fc["first_test"])) & (bundle.calendar <= last)]
        calendar = [(day + pd.Timedelta(hours=15, minutes=30)).tz_localize("Asia/Shanghai").isoformat(sep=" ") for day in test_days]
        stats = prediction_metrics(table, calendar)
        packages = {}
        for name in ("numpy", "pandas", "torch"):
            try:
                packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                packages[name] = None
        summary = {**rc["_lineage"], "run": rc["name"], "engine": rc["engine"],
                   "model": rc["model"], "model_version": None, "prediction_version": version,
                   "prediction_scope": "historical_walk_forward_only_no_final_refit",
                   "config_hash": rc["config_hash"], "data_snapshot_id": bundle.manifest["data_snapshot_id"],
                   "data_content_hash": bundle.identity["content_hash"], "data_kind": bundle.manifest["data_kind"],
                   "evidence_status": "SYNTHETIC_ONLY" if bundle.manifest["data_kind"] == "synthetic" else "DIAGNOSTIC_ONLY",
                   "folds": len(plan), "seeds": rc["train"]["seeds"], "prediction_metrics": stats,
                   "evaluation_asof": rc["evaluation_asof"], "elapsed_seconds": time.monotonic() - started,
                   "environment": {"python": platform.python_version(), "platform": platform.platform(), "packages": packages},
                   "economic_efficacy": "NOT_EVALUATED", "go": False}
        experiments.write(out / "metrics.json", summary)
        (out / "report.md").write_text(_report(summary), encoding="utf-8")
        bundle.assert_unchanged()
        return summary
    except BaseException as exc:
        from ..jobs import Cancelled

        if active is not None:
            active.update(state="cancelled" if isinstance(exc, (Cancelled, KeyboardInterrupt)) else "failed",
                          finished_at=experiments.now(), error=f"{type(exc).__name__}: {exc}")
        for trial in trials["attempts"]:
            if trial["state"] == "queued":
                trial["state"] = "not_run"
        trials["run_error"] = f"{type(exc).__name__}: {exc}"
        experiments.write(out / "trials.json", trials)
        raise


def _report(s):
    p = s["prediction_metrics"]
    return (f"# Causal prediction run: {s['run']}\n\n"
            f"Evidence: **{s['evidence_status']}**. Economic efficacy: **NOT_EVALUATED**.\n\n"
            f"Model `{s['model']}`; {s['folds']} time folds; declared seeds {s['seeds']}. "
            "Labels are next-session open-to-close decimal returns. "
            "Train/validation labels use their own historical cutoff and visible revisions.\n\n"
            f"Predictions: {p['n_predictions']}; evaluable labels: {p['n_evaluable']}; "
            f"missing labels: {p['n_missing_labels']}; MSE: {p['mse']}. "
            f"Rank IC mean: {p['rank_ic']['mean']}; valid IC dates: {p['n_ic_dates']}/{p['n_test_dates']}. "
            f"IC requires at least {p['rank_ic_min_names']} names per day.\n\n"
            "Historical validation selects GRU epochs; Ridge alpha is fixed. Validation is not refitted. "
            "See folds.json, trials.json and coverage.parquet for selection, failures and excluded observations. "
            "Seed predictions are equally averaged; deterministic Ridge seeds are duplicate controls, not independent trials.\n\n"
            "Each fold checkpoint was reloaded and reproduced its predictions exactly. "
            "No separately refitted forward model exists. The legacy 5d/20d Bench is incompatible. "
            "Signal statistics do not establish transaction-cost-adjusted returns or model superiority.\n")
