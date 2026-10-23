"""PIT ingestion, historical revision isolation and real archived prediction runs."""

import copy
import io
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import pytest

from qwb import cli, evidence_export, experiments, pipeline, runconfig
from qwb.observations.bundle import PITBundle, session_time
from qwb.observations.fixture import create
from qwb.training.baselines import FactorRidge


def setup(tmp_path, *, frequency="M", model="ridge_factor_v1"):
    config = create(tmp_path / "data", days=100, assets=4)
    raw = runconfig.load(config)
    raw["model"] = {"name": model}
    if model == "sequence_gru_v1":
        raw["model"].update(hidden_size=4, epochs=1, batch_size=128)
    raw["folds"]["frequency"] = frequency
    rc, rep = runconfig.resolve(raw, config.parent)
    assert rc is not None, str(rep)
    return rc, config, raw


def test_versioned_config_rejects_unsupported_semantics_and_unknown_fields(tmp_path):
    rc, config, raw = setup(tmp_path)
    assert rc["data"]["dir"] == str(config.parent)
    assert rc["train"]["cpu_threads"] == 1
    mutations = [lambda r: r.update(spec_version="e2e_future_v99"),
                 lambda r: r["task"].update(output="target_weight"),
                 lambda r: r["model"].update(learningrate=.1),
                 lambda r: r["data"].update(source="snapshot"),
                 lambda r: r["observations"].update(min_history=100),
                 lambda r: r["train"].update(seeds=[True]),
                 lambda r: r["train"].update(cpu_threads=0),
                 lambda r: r.update(evaluation_asof="2020-01-01"),
                 lambda r: r["folds"].update(first_test="2023-04-24T15:30")]
    for mutate in mutations:
        candidate = copy.deepcopy(raw)
        mutate(candidate)
        resolved, report = runconfig.resolve(candidate, config.parent)
        assert resolved is None and not report.ok


def test_label_revisions_resolve_at_cutoff_and_missing_next_session_stays_missing(tmp_path):
    _, config, _ = setup(tmp_path)
    root = config.parent
    frame = pd.read_parquet(root / "bars.parquet")
    revised = frame.iloc[[4]].copy()
    revised["revision"] = 1
    revised["available_time"] = "2023-02-01T16:00:00+08:00"
    revised["close"] *= 1.2
    pd.concat([frame, revised]).to_parquet(root / "bars.parquet", index=False)
    bundle = PITBundle(root, lookback=12)
    sample = pd.DataFrame({"code": [frame.iloc[0]["code"]], "decision_time": ["2023-01-02T15:30:00+08:00"]})
    early = bundle.labels_at(sample, "2023-01-04T15:30:00+08:00")
    late = bundle.labels_at(sample, "2023-02-02T15:30:00+08:00")
    assert early["label"].iloc[0] != late["label"].iloc[0]
    assert early["label_available_time"].iloc[0] != late["label_available_time"].iloc[0]
    unknown = bundle.labels_at(sample, "2023-01-03T15:01:00+08:00")
    assert unknown["label"].isna().all()
    assert pd.Timestamp(early["label_start"].iloc[0]) == pd.Timestamp("2023-01-03T09:30:00+08:00")
    assert pd.Timestamp(early["label_end"].iloc[0]) == pd.Timestamp("2023-01-03T15:00:00+08:00")


@pytest.mark.parametrize("frequency", ["M", "W-FRI"])
def test_ridge_monthly_weekly_archives_and_export(tmp_path, frequency):
    rc, config, _ = setup(tmp_path, frequency=frequency)
    result = pipeline.run(rc, tmp_path, log=lambda *_: None)
    root = Path(result["output"])
    assert experiments.verify(root)["run_id"] == result["run_id"]
    assert result["model_version"] is None
    assert result["evidence_status"] == "SYNTHETIC_ONLY"
    assert result["prediction_metrics"]["n_predictions"] == 19 * 4
    assert result["prediction_metrics"]["n_evaluable"] == 19 * 4
    assert result["prediction_metrics"]["n_test_dates"] == 19
    assert result["prediction_metrics"]["rank_ic"]["mean"] is None  # four names, below IC evidence threshold
    trials = experiments.read(root / "trials.json")
    assert len(trials["attempts"]) == result["folds"]
    assert all(row["state"] == "succeeded" for row in trials["attempts"])
    payload = evidence_export.build(root)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        exported = json.loads(archive.read("evidence.json"))
        assert exported["synthetic"] is True
        assert exported["prediction_scope"] == "historical_walk_forward_only_no_final_refit"
        assert str(tmp_path) not in json.dumps(exported)
    bundle = PITBundle(config.parent, lookback=rc["observations"]["lookback"])
    x, valid, samples, _ = bundle.samples(min_history=5, last_test=rc["folds"]["last_test"], max_samples=5000)
    saved = pd.read_parquet(root / "predictions.parquet")
    first_fold = experiments.read(root / "folds.json")["folds"][0]
    test = pd.to_datetime(samples["decision_time"], utc=True).between(
        pd.Timestamp(first_fold["test_start"]), pd.Timestamp(first_fold["test_end"]))
    model = FactorRidge.load(root / first_fold["seeds"][0]["checkpoint"])
    expected = saved[saved["fold"] == 0].set_index(["code", "decision_time"])["prediction"]
    replay = samples.loc[test].copy()
    replay["prediction"] = model.predict(x[test], valid[test])
    np.testing.assert_array_equal(replay.set_index(["code", "decision_time"])["prediction"].sort_index(), expected.sort_index())


def test_future_bars_and_late_revisions_do_not_change_first_fold_model(tmp_path):
    rc, config, _ = setup(tmp_path)
    before = pipeline.run(rc, tmp_path, log=lambda *_: None)
    first = experiments.read(Path(before["output"]) / "folds.json")["folds"][0]
    frame = pd.read_parquet(config.parent / "bars.parquet")
    date = pd.Timestamp(first["test_start"]).tz_localize(None).normalize()
    future = frame["date"] > date
    frame.loc[future, ["open", "high", "low", "close"]] *= 2
    revisions = frame[frame["date"] < date].iloc[:8].copy()
    revisions["revision"], revisions["available_time"] = 1, "2024-01-01T16:00:00+08:00"
    revisions[["open", "high", "low", "close"]] *= 3
    pd.concat([frame, revisions]).to_parquet(config.parent / "bars.parquet", index=False)
    after = pipeline.run(rc, tmp_path, log=lambda *_: None)
    for result in (before, after):
        experiments.verify(Path(result["output"]))
    old_model = experiments.read(Path(before["output"]) / first["seeds"][0]["checkpoint"] / "model.json")
    new_model = experiments.read(Path(after["output"]) / first["seeds"][0]["checkpoint"] / "model.json")
    for key in ("mean", "scale", "coef", "intercept"):
        assert old_model[key] == new_model[key]
    # The first test decision sees the same original data; later test observations may change.
    old = pd.read_parquet(Path(before["output"]) / "predictions.parquet")
    new = pd.read_parquet(Path(after["output"]) / "predictions.parquet")
    first_old = old[old["decision_time"] == first["test_start"]]["prediction"]
    first_new = new[new["decision_time"] == first["test_start"]]["prediction"]
    assert len(first_old) == len(first_new) == 4
    np.testing.assert_array_equal(first_old, first_new)


def test_budget_and_content_mutation_fail_preserving_latest_and_trials(tmp_path):
    rc, config, _ = setup(tmp_path)
    success = pipeline.run(rc, tmp_path, log=lambda *_: None)
    bad = copy.deepcopy(rc)
    bad["train"]["max_samples"] = 1
    with pytest.raises(ValueError, match="budget"):
        pipeline.run(bad, tmp_path, log=lambda *_: None)
    failed = experiments.list_runs(tmp_path)[0]
    assert failed["state"] == "failed"
    assert experiments.read(Path(failed["path"]) / "trials.json")["attempts"][0]["state"] == "not_run"
    assert experiments.verify(tmp_path / "runs" / rc["name"])["run_id"] == success["run_id"]
    changed = False
    def change(message):
        nonlocal changed
        if "Ridge validation" in message and not changed:
            changed = True
            path = config.parent / "manifest.json"
            value = experiments.read(path)
            value["license"] += " changed"
            experiments.write(path, value)
    with pytest.raises(ValueError, match="content changed"):
        pipeline.run(rc, tmp_path, log=change)
    assert experiments.verify(tmp_path / "runs" / rc["name"])["run_id"] == success["run_id"]


def test_atomic_ledger_write_retries_transient_windows_file_lock(tmp_path, monkeypatch):
    replace = Path.replace
    attempts = 0
    def transient(path, target):
        nonlocal attempts
        attempts += 1
        if attempts <= 5:
            raise PermissionError("simulated sync client lock")
        return replace(path, target)
    monkeypatch.setattr(Path, "replace", transient)
    monkeypatch.setattr(experiments.time, "sleep", lambda *_: None)
    experiments.write(tmp_path / "trials.json", {"complete": True})
    assert attempts == 6
    assert experiments.read(tmp_path / "trials.json") == {"complete": True}


def test_bundle_rejects_fake_preclose_availability_and_universe_unknown_at_decision(tmp_path):
    _, config, _ = setup(tmp_path)
    root = config.parent
    bars = pd.read_parquet(root / "bars.parquet")
    original = bars.copy()
    bars.loc[0, "available_time"] = "2023-01-02T10:00:00+08:00"
    bars.to_parquet(root / "bars.parquet", index=False)
    with pytest.raises(ValueError, match="session close"):
        PITBundle(root)
    original.to_parquet(root / "bars.parquet", index=False)
    universe = pd.read_parquet(root / "universe.parquet")
    universe.loc[universe["date"] == universe["date"].min(), "available_time"] = "2024-01-01T16:00:00+08:00"
    universe.to_parquet(root / "universe.parquet", index=False)
    with pytest.raises(ValueError, match="universe declaration"):
        PITBundle(root).universe_at("2023-01-02T15:30:00+08:00")


def test_gru_worker_cli_runs_and_archives_actual_predictions(tmp_path, monkeypatch, capsys):
    pytest.importorskip("torch")
    _, config, raw = setup(tmp_path, model="sequence_gru_v1")
    import yaml

    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(config), "--check"]) == 0
    assert cli.main(["run", str(config)]) == 0
    assert "SYNTHETIC_ONLY" in capsys.readouterr().out
    record = experiments.list_runs(tmp_path)[0]
    assert experiments.verify(Path(record["path"]))["state"] == "succeeded"
