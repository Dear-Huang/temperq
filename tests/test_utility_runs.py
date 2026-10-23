"""Versioned utility configuration, actual worker training and frozen verification."""

import copy
from dataclasses import asdict
from pathlib import Path

from qwb import cli, experiments, pipeline, runconfig
from qwb.observations.bundle import PITBundle, session_time
from qwb.observations.fixture import create
from qwb.portfolio.recipes import CostRecipe, RiskRecipe
from qwb.training.utility_config import LIMITS
from qwb.portfolio.comparison import LIFECYCLE, STATE_LIMITS

import pytest
import yaml

pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = tmp_path_factory.mktemp("utility-source")
    path = create(root / "bundle", days=100, assets=3)
    source_config = runconfig.load(path)
    source_config["model"]["epochs"] = 1
    source_config["folds"]["last_test"] = source_config["folds"]["first_test"]
    resolved, report = runconfig.resolve(source_config, path.parent)
    assert resolved is not None, str(report)
    source = pipeline.run(resolved, root, log=lambda *_: None)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from run_allocation_demo import synthetic_protocol

    bundle = PITBundle(path.parent, lookback=12)
    protocols = {phase: synthetic_protocol(bundle, [str(session_time(day, "15:30")) for day in bundle.calendar[start:start + 2]],
        max_weight=.4, min_cash=.05, initial_holdings=True) for phase, start in (("train", 80), ("validation", 82), ("test", 84))}
    for phase, protocol in protocols.items():
        experiments.write(root / f"{phase}.json", protocol)
    task = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples/cn_alloc_daily_v1.yaml").read_text(encoding="utf-8"))
    limits = {key: 1 for key in LIMITS}
    limits.update(max_daily_equity_gap_cny=10, max_total_cost_gap_cny=20,
                  max_total_cost_relative_gap=.1, max_constraint_violation_count=0, zero_cost_relative_rule="null_use_absolute")
    limits.update(state_comparison_version=LIFECYCLE, **{key: 1e-8 for key in STATE_LIMITS})
    limits.update(max_daily_entitlement_mismatch_count=0, max_daily_processed_event_mismatch_count=0)
    raw = {"spec_version": "e2e_utility_v1", "name": "utility-worker-smoke", "task": task,
        "data": {"source": "pit_bundle", "dir": str(path.parent)}, "observations": {"lookback": 12, "min_history": 5},
        "initialization": {"run": source["output"], "fold": 0}, "model": {"name": "decision_gru_v1"},
        "folds": [{"id": 0, **{phase: str(root / f"{phase}.json") for phase in protocols},
                   "fit_cutoff": protocols["validation"]["sessions"][0]["decision_time"],
                   "selection_cutoff": protocols["test"]["sessions"][0]["decision_time"]}],
        "portfolio": {"risk": asdict(RiskRecipe(20, 10, .2)),
            "cost": asdict(CostRecipe(10, 10, "2020-01-01T00:00:00+08:00")),
            "optimizer": {"max_weight": .4, "min_cash": .05, "stability_penalty": .02},
            "production": {"policy": "torch_box_budget_turnover_dust_v1", "buffer": 1e-12, "dust_threshold": 1e-10}},
        "continuous": {"integer_lots": False, "settlement": True, "market_restrictions": True,
                       "minimum_commission": True, "fee_model": "declared_execution"},
        "utility": {"variance_penalty": 0.}, "train": {"seeds": [0], "epochs": 1, "max_episode_steps": 8, "detach_every": 1},
        "acceptance": limits, "evaluation_asof": str(session_time(bundle.calendar[88], "15:30")), "output": "runs/utility-latest"}
    return root, raw


@pytest.mark.parametrize("kind", ["unknown", "task", "overlap", "duplicate", "seed", "integer", "dust", "limits", "sector", "state_version", "state_limits"])
def test_utility_config_rejects_unsupported_and_ambiguous_semantics(prepared, kind):
    root, original = prepared
    raw = copy.deepcopy(original)
    if kind == "unknown":
        raw["train"]["learning_rte"] = .1
    elif kind == "task":
        raw["task"]["output"] = "score"
    elif kind == "overlap":
        raw["folds"][0]["fit_cutoff"] = raw["folds"][0]["selection_cutoff"]
    elif kind == "duplicate":
        raw["folds"].append(copy.deepcopy(raw["folds"][0]))
    elif kind == "seed":
        raw["train"]["seeds"] = [0, 0]
    elif kind == "integer":
        raw["continuous"]["integer_lots"] = True
    elif kind == "dust":
        raw["portfolio"]["production"]["dust_threshold"] = 1e-3
    elif kind == "limits":
        raw["acceptance"].pop("max_daily_cash_gap_cny")
    elif kind == "state_version":
        raw["acceptance"]["state_comparison_version"] = "unknown"
    elif kind == "state_limits":
        raw["acceptance"].pop("max_daily_entitlement_quantity_gap")
    else:
        raw["portfolio"]["optimizer"]["sector_caps"] = [.2]
    resolved, report = runconfig.resolve(raw, root)
    assert resolved is None and not report.ok


def test_legacy_utility_config_keeps_explicitly_limited_state_scope(prepared):
    root, original = prepared
    raw = copy.deepcopy(original)
    for key in STATE_LIMITS | {"state_comparison_version"}:
        raw["acceptance"].pop(key)
    resolved, report = runconfig.resolve(raw, root)
    assert resolved is not None, str(report)
    assert resolved["acceptance"] == raw["acceptance"]
    assert "validation_audit" not in resolved


@pytest.mark.parametrize("value", [None, {}, {"schema_version": "unknown"},
    {"schema_version": "utility_validation_audit_v1", "selection": "exact_loss"}])
def test_validation_candidate_config_rejects_unsupported_selection_and_versions(prepared, value):
    root, original = prepared
    raw = copy.deepcopy(original)
    raw["validation_audit"] = value
    resolved, report = runconfig.resolve(raw, root)
    assert resolved is None and not report.ok


def test_actual_worker_utility_training_and_independent_recomputation(prepared, tmp_path, monkeypatch, capsys):
    root, raw = prepared
    config = tmp_path / "utility.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(config), "--check"]) == 0
    assert cli.main(["run", str(config)]) == 0
    assert "proxy comparison eligible" in capsys.readouterr().out
    row = experiments.list_runs(tmp_path)[0]
    output = Path(row["path"])
    assert experiments.verify(output)["state"] == "succeeded"
    assert cli.main(["e2e", "utility-verify", str(output), "--inputs"]) == 0
    metrics = experiments.read(output / "metrics.json")
    assert metrics["engine"] == "e2e_utility_v1" and not metrics["go"]
    assert metrics["comparison_eligible"] is False
    import pandas as pd
    predictions = pd.read_parquet(output / "predictions.parquet")
    assert predictions["prediction_kind"].str.startswith("auxiliary_").all()
    trials = experiments.read(output / "trials.json")
    assert trials["attempts"][0]["state"] == "succeeded" and len(trials["attempts"][0]["history"]) == 1
    assert metrics["state_comparison_version"] == LIFECYCLE
    from run_utility_production_ablation import compute as ablation
    diagnostic = tmp_path / "production-ablation"
    compared = ablation(output, diagnostic)
    assert compared["status"] == "COMPLETED" and compared["trained"] is False
    assert compared["frozen_solve_count"] == 8
    for model in compared["evaluations"].values():
        assert set(model) == {"torch_box_budget_turnover_v1", "torch_box_budget_turnover_dust_v1"}
        assert len({row["checkpoint_identity"] for row in model.values()}) == 1
    for counts in experiments.read(diagnostic / "frozen-map-comparison.json")["counts"].values():
        assert counts["accepted"] + counts["rejected"] == 8
    from qwb.training.utility_runner import verify
    for filename in ("predictions.parquet", "seed_predictions.parquet"):
        originals = {name: (output / name).read_bytes() for name in (filename, "metrics.json", "run.json")}
        try:
            frame = pd.read_parquet(output / filename)
            frame.loc[0, "prediction"] += .01
            frame.to_parquet(output / filename, index=False)
            record = experiments.read(output / "run.json")
            if filename == "predictions.parquet":
                metrics = experiments.read(output / "metrics.json")
                metrics["prediction_version"] = record["prediction_version"] = experiments.digest(output / filename)
                experiments.write(output / "metrics.json", metrics)
            record["artifacts"] = experiments.inventory(output)
            record["artifact_hash"] = experiments.object_digest(record["artifacts"])
            experiments.write(output / "run.json", record)
            assert experiments.verify(output)["state"] == "succeeded"
            with pytest.raises(ValueError, match="auxiliary forecast arithmetic differs"):
                verify(output)
        finally:
            for name, content in originals.items():
                (output / name).write_bytes(content)
    differences = output / "accounts/fold-0000-seed-0/B2-utility/differences.json"
    changed = experiments.read(differences)
    changed["closed_loop"]["daily"][0]["cash_gap_cny"] += 100
    experiments.write(differences, changed)
    record = experiments.read(output / "run.json")
    record["artifacts"][differences.relative_to(output).as_posix()] = experiments.digest(differences)
    record["artifact_hash"] = experiments.object_digest(record["artifacts"])
    experiments.write(output / "run.json", record)
    assert experiments.verify(output)["state"] == "succeeded"
    from qwb.training.utility_runner import verify
    with pytest.raises(ValueError, match="arithmetic mismatch"):
        verify(output)


def test_cancellation_and_changed_protocol_are_persisted_as_failed_attempts(prepared, tmp_path):
    root, raw = prepared
    resolved, report = runconfig.resolve(raw, root)
    assert resolved is not None, str(report)
    def cancel(message):
        if str(message).startswith("utility decision"):
            from qwb.jobs import Cancelled
            raise Cancelled("utility cancelled")
    from qwb.jobs import Cancelled
    with pytest.raises(Cancelled):
        pipeline.run(resolved, tmp_path, log=cancel)
    record = experiments.list_runs(tmp_path)[0]
    assert record["state"] == "cancelled"
    trial = experiments.read(Path(record["path"]) / "trials.json")["attempts"][0]
    assert trial["state"] == "cancelled"
    changed = copy.deepcopy(resolved)
    changed["folds"][0]["protocols"]["train"]["file_hash"] = "sha256:wrong"
    with pytest.raises(ValueError, match="protocol changed"):
        pipeline.run(changed, tmp_path, log=lambda *_: None)


def test_shared_candidate_audit_is_independent_of_outer_execution_and_rejects_rehashed_result(prepared, tmp_path):
    from qwb.training.decision import DecisionGRU
    from qwb.training.utility_runner import verify
    import torch

    root, original = prepared
    raw = copy.deepcopy(original)
    raw["train"]["epochs"] = 2
    raw["validation_audit"] = {"schema_version": "utility_validation_audit_v1"}
    outputs = []
    for variant in ("original", "outer_suspended"):
        config = copy.deepcopy(raw)
        if variant == "outer_suspended":
            protocol = experiments.read(Path(config["folds"][0]["test"]))
            for session in protocol["sessions"]:
                for bar in session["bars"]:
                    bar["suspended"] = True
            path = tmp_path / "suspended-test.json"
            experiments.write(path, protocol)
            config["folds"][0]["test"] = str(path)
        resolved, report = runconfig.resolve(config, root)
        assert resolved is not None, str(report)
        result = pipeline.run(resolved, tmp_path / variant, log=lambda *_: None)
        output = Path(result["output"])
        outputs.append(output)
        assert experiments.read(output / "metrics.json")["validation_audit_complete"] is True
        assert experiments.read(output / "trials.json")["attempts"][0]["validation_audit_status"] == "COMPLETED"
    first, second = outputs
    checkpoint = "models/fold-0000-seed-0"
    models = [DecisionGRU.load(output / checkpoint) for output in outputs]
    assert models[0].fit_record["best_epoch"] == models[1].fit_record["best_epoch"]
    assert models[0].fit_record["history"] == models[1].fit_record["history"]
    assert all(torch.equal(a, b) for a, b in zip(models[0].net.parameters(), models[1].net.parameters(), strict=True))
    for epoch in (1, 2):
        relative = f"validation-candidates/fold-0000-seed-0/epoch-{epoch:04d}"
        candidates = [DecisionGRU.load(output / relative / "checkpoint") for output in outputs]
        assert all(torch.equal(a, b) for a, b in zip(candidates[0].net.parameters(), candidates[1].net.parameters(), strict=True))
        assert experiments.read(first / relative / "result.json")["metrics"] == experiments.read(second / relative / "result.json")["metrics"]
    assert verify(first, inputs=True)["validation_audit_complete"] is True
    target = first / "validation-candidates/fold-0000-seed-0/epoch-0001/result.json"
    forged = experiments.read(target)
    forged["metrics"]["exact"]["loss"] += .001
    experiments.write(target, forged)
    record = experiments.read(first / "run.json")
    record["artifacts"] = experiments.inventory(first)
    record["artifact_hash"] = experiments.object_digest(record["artifacts"])
    experiments.write(first / "run.json", record)
    assert experiments.verify(first)["state"] == "succeeded"
    with pytest.raises(ValueError, match="candidate result differs"):
        verify(first)
