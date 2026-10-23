"""Causal estimation, state-based allocation, failures and frozen recomputation."""

import copy
from dataclasses import asdict, replace
from pathlib import Path

from qwb import experiments, pipeline, runconfig
from qwb.bench import targets
from qwb.observations.bundle import PITBundle, session_time
from qwb.observations.fixture import create
from qwb.portfolio import prediction
from qwb.portfolio.contracts import Position, SettlementLot
from qwb.portfolio.lifecycle import Entitlement, LifecycleAccountState
from qwb.portfolio.recipes import CostRecipe, RecipeError, RiskRecipe, estimate
from qwb.portfolio.reference import Fees
from qwb.portfolio.production import project
from qwb.portfolio.optimization import AllocationError

import numpy as np
import pandas as pd
import pytest
import yaml

import importlib.util

needs_torch = pytest.mark.skipif(
    any(importlib.util.find_spec(m) is None for m in ("torch", "cvxpy", "cvxpylayers")),
    reason="optional e2e extra (torch, cvxpy, cvxpylayers) not installed")


def config():
    return {"schema_version": "prediction_allocation_v1", "risk": asdict(RiskRecipe(20, 10, .2)),
            "cost": asdict(CostRecipe(10, 10, "2020-01-01T00:00:00+08:00")),
            "optimizer": {"max_weight": .4, "min_cash": .05}, "output_policy": "strict_raw",
            "diagnostics": {"max_cost_gap_equity": .001, "max_turnover_gap": .1}}


def fixture(tmp_path, monkeypatch):
    run_path = create(tmp_path / "fixture", days=100, assets=3)
    bundle = PITBundle(run_path.parent)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from run_allocation_demo import synthetic_protocol

    decisions = [str(session_time(day, "15:30")) for day in bundle.calendar[80:83]]
    protocol = synthetic_protocol(bundle, decisions, max_weight=.4, min_cash=.05)
    rows = [{"code": code, "decision_time": decision, "prediction": .001 + i * .0001,
             "model_version": "frozen-prediction", "task": "cn_predict_intraday_v1",
             "data_snapshot_id": bundle.manifest["data_snapshot_id"]}
            for decision in decisions for i, code in enumerate(protocol["sessions"][0]["eligible_assets"])]
    return run_path, bundle, protocol, pd.DataFrame(rows)


def test_risk_covariance_root_units_and_cost_use_actual_capital_and_past_liquidity(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    assets = tuple(protocol["sessions"][0]["eligible_assets"])
    decision = frame["decision_time"].iloc[0]
    cfg = config()
    root, buy, sell, audit = estimate(bundle, decision, assets, equity=1000000, fees=Fees(),
        risk=RiskRecipe(**cfg["risk"]), cost=CostRecipe(**cfg["cost"]))
    np.testing.assert_allclose(root.T @ root, audit["covariance"], atol=1e-14)
    raw = bundle.bars[bundle.bars["date"].isin(bundle.calendar[61:81])]
    r = raw.pivot(index="date", columns="code", values="close") / raw.pivot(index="date", columns="code", values="open") - 1
    covariance = np.cov(r.to_numpy(), rowvar=False)
    expected = .8 * covariance + .2 * np.diag(np.diag(covariance))
    np.testing.assert_allclose(audit["covariance"], expected, atol=1e-12)
    np.testing.assert_allclose(sell - buy, .001)
    np.testing.assert_allclose(buy, (.0003 + .001 * 1000000 / np.array(audit["median_amount"])))
    _, doubled, _, _ = estimate(bundle, decision, assets, equity=2000000, fees=Fees(),
        risk=RiskRecipe(**cfg["risk"]), cost=CostRecipe(**cfg["cost"]))
    np.testing.assert_allclose(doubled - .0003, 2 * (buy - .0003))
    assert audit["n_common_dates"] == 20 and audit["return_units"].startswith("decimal_")


def test_future_prices_liquidity_and_late_revisions_do_not_change_prior_estimates(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    day = bundle.calendar[80]
    stamp = session_time(day, "15:30")
    assets = tuple(protocol["sessions"][0]["eligible_assets"])
    before = estimate(bundle, stamp, assets, equity=1000000, fees=Fees())
    changed = bundle.bars.copy()
    changed.loc[changed["date"] > day, ["open", "close", "amount"]] *= 5
    late = changed[(changed["date"] == day) & (changed["code"] == assets[0])].copy()
    late["revision"] = 1
    late["available_time"] = session_time(bundle.calendar[85], "16:00").tz_convert("UTC")
    late[["open", "close", "amount"]] *= [.5, 2, 10]
    bundle.bars = pd.concat([changed, late], ignore_index=True)
    after = estimate(bundle, stamp, assets, equity=1000000, fees=Fees())
    for old, new in zip(before[:3], after[:3]):
        np.testing.assert_array_equal(old, new)
    assert before[3] == after[3]
    matured = estimate(bundle, session_time(bundle.calendar[86], "15:30"), assets, equity=1000000, fees=Fees())
    assert matured[3]["source_rows_hash"] != before[3]["source_rows_hash"]


def test_missing_risk_asset_fails_without_shrinking_universe_or_inventing_liquidity(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    assets = tuple(protocol["sessions"][0]["eligible_assets"])
    bundle.bars.loc[bundle.bars["code"] == assets[-1], "close"] = np.nan
    with pytest.raises(RecipeError, match="common causal risk"):
        estimate(bundle, frame["decision_time"].iloc[0], assets, equity=1000000, fees=Fees())
    bundle.bars.loc[bundle.bars["code"] == assets[-1], "close"] = 10
    bundle.bars.loc[bundle.bars["code"] == assets[-1], "amount"] = 0
    with pytest.raises(RecipeError, match="liquidity"):
        estimate(bundle, frame["decision_time"].iloc[0], assets, equity=1000000, fees=Fees())
    with pytest.raises(RecipeError, match="not yet"):
        estimate(bundle, frame["decision_time"].iloc[0], assets, equity=1000000, fees=Fees(),
                 cost=replace(CostRecipe(), available_at="2030-01-01T00:00:00+08:00"))


@needs_torch
def test_prediction_policy_uses_actual_state_and_preserves_full_account_path(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    account, audit = prediction.replay(protocol, bundle, frame, config())
    assert account["explicit_failures"] == 0
    assert len(audit["attempts"]) == 3
    assert account["states"][0]["after"]["positions"]
    before = account["states"][1]["before"]
    state = LifecycleAccountState.from_dict(before)
    np.testing.assert_allclose(audit["attempts"][1]["previous_weights"], state.weights(audit["attempts"][1]["assets"]))
    assert audit["attempts"][1]["previous_weights"] != [w["target_weight"] for w in account["decisions"][0]["weights"]]
    for row in audit["diagnostics"]:
        assert row["actual_cost"] - row["intended_proxy_cost"] == pytest.approx(row["intent_to_fill_cost_gap"] + row["cost_model_gap"])


@needs_torch
def test_future_execution_cannot_change_prior_allocation_but_changes_actual_fills(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    before, old_audit = prediction.replay(protocol, bundle, frame, config())
    changed = copy.deepcopy(protocol)
    for row in changed["sessions"][0]["bars"]:
        row["suspended"] = True
    after, new_audit = prediction.replay(changed, bundle, frame, config())
    assert before["decisions"][0]["weights"] == after["decisions"][0]["weights"]
    assert old_audit["attempts"][0]["recipe"] == new_audit["attempts"][0]["recipe"]
    assert before["states"][0]["after"]["positions"] and not after["states"][0]["after"]["positions"]


@needs_torch
def test_missing_prediction_is_explicit_failure_and_keeps_held_shares(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    decision = frame["decision_time"].unique()[1]
    frame = frame.drop(frame[frame["decision_time"] == decision].index[0])
    account, audit = prediction.replay(protocol, bundle, frame, config())
    assert account["explicit_failures"] == 1
    assert audit["attempts"][1]["status"] == "explicit_failure"
    assert "missing eligible predictions" in audit["attempts"][1]["error"]
    assert [p["quantity"] for p in account["states"][1]["before"]["positions"]] == [p["quantity"] for p in account["states"][1]["after"]["positions"]]


@needs_torch
def test_locked_account_and_receivable_define_bounds_and_budget(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    s = protocol["sessions"][0]
    code = s["eligible_assets"][0]
    p = Position(code, 100, 0, 10, s["decision_time"], (SettlementLot(100, protocol["sessions"][1]["next_open_at"]),))
    claim = Entitlement("past-dividend", code, "dividend", 100, 100, protocol["sessions"][2]["open_at"], True)
    state = LifecycleAccountState(s["decision_time"], bundle.manifest["data_snapshot_id"], 1000000, (p,), entitlements=(claim,))
    policy = prediction.PredictionAllocator(bundle, frame, config(), model_version="bound-test", fees=Fees())
    context = targets.DecisionContext(s["decision_id"], state, tuple(s["eligible_assets"]), tuple(s["eligible_assets"]),
        "universe", "protocol", s["open_at"], .4, .05)
    target = policy(context)
    assert policy.attempts[0]["lower"][0] == pytest.approx(1000 / state.equity)
    assert target.receivable_weight == pytest.approx(100 / state.equity)
    assert sum(w for _, w in target.weights) + target.cash_weight + target.receivable_weight == pytest.approx(1)


@pytest.mark.parametrize("mutation", ["duplicate", "nan", "task", "snapshot"])
def test_prediction_contract_rejects_ambiguous_inputs(tmp_path, monkeypatch, mutation):
    _, bundle, _, frame = fixture(tmp_path, monkeypatch)
    if mutation == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif mutation == "nan":
        frame.loc[0, "prediction"] = np.nan
    else:
        frame.loc[0, "task" if mutation == "task" else "data_snapshot_id"] = "wrong"
    with pytest.raises(ValueError):
        prediction.PredictionAllocator(bundle, frame, config(), model_version="test", fees=Fees())


@needs_torch
def test_verified_archive_cli_and_recomputation_detect_forged_policy_evidence(tmp_path, monkeypatch):
    path, bundle, protocol, _ = fixture(tmp_path, monkeypatch)
    raw = runconfig.load(path)
    raw["model"] = {"name": "ridge_factor_v1", "alpha": 10}
    raw["name"] = "allocation-source"
    rc, report = runconfig.resolve(raw, path.parent)
    assert rc is not None, str(report)
    trained = pipeline.run(rc, tmp_path)
    root = tmp_path / "inputs"
    root.mkdir()
    experiments.write(root / "protocol.json", protocol)
    (root / "allocation.yaml").write_text(yaml.safe_dump(config()), encoding="utf-8")
    from qwb import cli

    out = tmp_path / "evaluation"
    assert cli.main(["bench", "allocate", trained["output"], "--protocol", str(root / "protocol.json"),
        "--data-dir", str(bundle.root), "--config", str(root / "allocation.yaml"), "--output", str(out)]) == 0
    assert cli.main(["bench", "allocation-verify", str(out), "--inputs"]) == 0
    assert len(targets.read(out / "attempts.json")) == 3
    for filename, mutate in (
        ("result.json", lambda row: row["account_summary"].update(net_return=100)),
        ("result.json", lambda row: row.update(source_run_id="forged")),
        ("attempts.json", lambda row: next(iter(row.values())).update(status="forged")),
        ("allocation_audit.json", lambda row: row["attempts"][0].update(previous_weights=[1, 0, 0])),
    ):
        original = (out / filename).read_bytes()
        changed = targets.read(out / filename)
        mutate(changed)
        experiments.write(out / filename, changed)
        hashes = experiments.inventory(out)
        hashes.pop("checksums.json")
        experiments.write(out / "checksums.json", hashes)
        with pytest.raises(ValueError, match="recomputation mismatch"):
            prediction.verify(out)
        (out / filename).write_bytes(original)
    hashes = experiments.inventory(out)
    hashes.pop("checksums.json")
    experiments.write(out / "checksums.json", hashes)
    assert prediction.verify(out)["status"] == "PASS"
    (root / "allocation.yaml").write_text("changed after freeze", encoding="utf-8")
    assert prediction.verify(out)["status"] == "PASS"
    with pytest.raises(ValueError, match="original input"):
        prediction.verify(out, inputs=True)


def test_disclosed_projection_preserves_constraints_and_rejects_material_repair():
    raw = np.array([-1e-10, .40000000001, .2])
    previous = np.array([0, .3, .1])
    output, audit = project(raw, previous, np.zeros(3), np.full(3, .4), budget=.8, max_turnover=.3, tolerance=1e-6)
    assert (output >= 0).all() and (output <= .4).all() and output.sum() <= .8
    assert np.abs(output - previous).sum() <= .3 + 1e-9
    assert audit["postprocessed"] and audit["raw_weights"] == raw.tolist()
    assert audit["max_absolute_adjustment"] <= 1e-6
    with pytest.raises(AllocationError, match="numerical limits"):
        project(np.array([-.01, .5]), np.zeros(2), np.zeros(2), np.ones(2), budget=1, max_turnover=2, tolerance=1e-6)
    with pytest.raises(AllocationError, match="failed"):
        project(np.array([.4, .4]), np.zeros(2), np.full(2, .4), np.ones(2), budget=.5, max_turnover=2, tolerance=1e-6)


@needs_torch
def test_declared_projection_keeps_negative_boundary_raw_values_visible(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    frame.loc[frame["code"] == frame["code"].iloc[0], "prediction"] = -.01
    cfg = config()
    cfg["output_policy"] = "highs_l1_projection_v1"
    account, audit = prediction.replay(protocol, bundle, frame, cfg)
    assert account["explicit_failures"] == 0
    assert all(row["production"]["postprocessed"] for row in audit["attempts"])
    assert all(row["production"]["max_absolute_adjustment"] <= cfg.get("optimizer", {}).get("tolerance", 1e-6) for row in audit["attempts"])
    assert all(row["allocation"]["postprocessed"] is False for row in audit["attempts"])


@needs_torch
def test_out_of_pool_holdings_have_zero_forecast_and_no_increased_intent(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    s = protocol["sessions"][0]
    assets = tuple(s["eligible_assets"])
    position = Position(assets[0], 1000, 1000, 10, s["decision_time"])
    state = LifecycleAccountState(s["decision_time"], bundle.manifest["data_snapshot_id"], 100000, (position,))
    cfg = config()
    cfg["output_policy"] = "highs_l1_projection_v1"
    policy = prediction.PredictionAllocator(bundle, frame, cfg, model_version="out-of-pool", fees=Fees())
    context = targets.DecisionContext(s["decision_id"], state, assets[1:], assets, "universe", "protocol", s["open_at"], .4, .05)
    target = policy(context)
    assert policy.attempts[0]["prediction"][0] == 0
    assert policy.attempts[0]["held_outside_prediction_pool"] == [assets[0]]
    assert dict(target.weights)[assets[0]] <= position.quantity * position.mark / state.equity + 1e-9


@needs_torch
def test_implementation_and_source_file_changes_during_replay_fail(tmp_path, monkeypatch):
    _, bundle, protocol, frame = fixture(tmp_path, monkeypatch)
    changed = False
    def mutate(row):
        nonlocal changed
        if row["status"] == "target":
            changed = True
    with monkeypatch.context() as patch:
        patch.setattr(experiments, "code_identity", lambda: "new" if changed else "old")
        with pytest.raises(ValueError, match="implementation changed"):
            prediction.replay(protocol, bundle, frame, config(), on_attempt=mutate)
    def change_input(row):
        if row["status"] == "target":
            (bundle.root / "manifest.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="content changed"):
        prediction.replay(protocol, bundle, frame, config(), on_attempt=change_input)


@needs_torch
def test_cancelled_allocation_keeps_plan_and_marks_unrun_decisions(tmp_path, monkeypatch):
    path, bundle, protocol, _ = fixture(tmp_path, monkeypatch)
    raw = runconfig.load(path)
    raw["model"] = {"name": "ridge_factor_v1", "alpha": 10}
    rc, report = runconfig.resolve(raw, path.parent)
    assert rc is not None, str(report)
    trained = pipeline.run(rc, tmp_path)
    experiments.write(tmp_path / "protocol.json", protocol)
    (tmp_path / "allocation.yaml").write_text(yaml.safe_dump(config()), encoding="utf-8")
    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt("controlled cancellation")
    monkeypatch.setattr(prediction, "replay", cancelled)
    out = tmp_path / "cancelled"
    with pytest.raises(KeyboardInterrupt):
        prediction.evaluate(trained["output"], tmp_path / "protocol.json", bundle.root, tmp_path / "allocation.yaml", out)
    assert targets.read(out / "result.json")["status"] == "INTERRUPTED_OR_FAILED"
    assert all(row["status"] == "not_run" for row in targets.read(out / "attempts.json").values())
    assert (out / "plan.json").is_file() and (out / "checksums.json").is_file()
