from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from qwb import cli, daily, dataset, folds, pipeline, taskcard
from qwb.bench.contracts import (create_protocol, digest, read_json,
                                validate_protocol, verify_inputs, write_json)
from qwb.bench.runner import compare_models, evaluate_model, verify_result
from qwb.bench.statistics import block_summary, conclusion


EX = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def market(tmp_path):
    root = tmp_path / "snapshot"
    (root / "bars").mkdir(parents=True)
    bars = pipeline.synth_bars(n_days=420, n_codes=40)
    bars.to_parquet(root / "bars" / "fixture.parquet", index=False)
    (root / "snapshot.json").write_text(json.dumps({"data_snapshot_id": "snap-fixture"}))
    panel = daily.panel_from_bars(bars)
    card = taskcard.load(EX / "cn_rank_5d.yaml")
    cfg = dict(daily.DEFAULTS, universe="all", min_age_days=20, min_amount_quantile=0,
               final_test_years=[2020])
    dates, codes = panel["close"].index, panel["close"].columns
    labels = daily.labels(panel, card)
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / "task.yaml").write_text(yaml.safe_dump(card), encoding="utf-8")
    raw = {"data": {"source": "snapshot", "dir": str(root)}, "cfg": cfg, "model": "lightgbm"}
    (anchor / "config.resolved.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    maturity = folds.label_end_times(pd.Series(dates), dates, card, "Asia/Shanghai")
    _, manifest = folds.build_fold(0, pd.Series(dates), maturity,
                                  (dates[0], pd.Timestamp("2019-12-31")),
                                  "2019-12-31 15:30+08:00", "snap-fixture", "fixture")
    (anchor / "folds.json").write_text(json.dumps([manifest.to_dict()]))
    table = daily.prediction_table({"dates": dates, "codes": codes, "pred": {0: labels.values}},
                                   card, "snap-fixture", "Asia/Shanghai")
    table.to_parquet(anchor / "predictions.parquet", index=False)
    protocol_path = tmp_path / "protocol.yaml"
    protocol = create_protocol(anchor, protocol_path, cwd=tmp_path, block_days=2,
                               resamples=200, min_days=8)
    return {"root": root, "anchor": anchor, "path": protocol_path, "protocol": protocol,
            "dates": dates, "codes": codes, "table": table, "panel": panel, "card": card}


def test_bootstrap_known_constant_zero_negative_and_missing():
    values = np.column_stack([np.ones(100), np.zeros(100), -np.ones(100)])
    settings = dict(block_days=5, resamples=200, seed=7, min_days=10)
    s = block_summary(values, **settings)
    assert s[0]["ci95"] == [1, 1] and conclusion(s[0])["status"] == "PASS"
    assert s[1]["ci95"] == [0, 0] and conclusion(s[1])["status"] == "FAIL"
    assert s[2]["ci95"] == [-1, -1] and conclusion(s[2])["status"] == "FAIL"
    assert block_summary(values[:3], **settings)[0]["ci95"] is None
    missing = np.full(100, np.nan)
    s = block_summary(missing, **settings)[0]
    assert s["mean"] is None and conclusion(s)["status"] == "INCONCLUSIVE"


def test_bootstrap_preserves_pairing_and_is_deterministic():
    rng = np.random.default_rng(3)
    x = rng.normal(size=200)
    data = np.column_stack([x, x, x - x])
    args = dict(block_days=10, resamples=200, seed=11, min_days=20)
    first = block_summary(data, **args)
    assert first == block_summary(data, **args)
    assert first[0] == first[1]
    assert first[2]["ci95"] == [0, 0]


def test_cli_model_end_to_end_and_same_model_comparison(market, tmp_path):
    output = tmp_path / "result"
    assert cli.main(["bench", "model", str(market["anchor"]), "--protocol", str(market["path"]),
                     "--output", str(output)]) == 0
    s = read_json(output / "scorecard.json")
    ev = s["evaluation"]
    assert ev["comparable"] and ev["coverage"]["total"] == 1
    assert ev["models"]["candidate"]["mean"] == pytest.approx(1)
    assert ev["models"]["candidate"]["signal_score"] == pytest.approx(100)
    assert ev["reference_rank"]["position"] == 1 and ev["reference_rank"]["out_of"] == 5
    assert ev["signal"]["status"] == "PASS"
    assert not ev["formal_score_eligible"] and s["economic"]["status"] == "NOT_EVALUATED"
    assert s["readiness"]["level"] == "SIGNAL_RESEARCH_CANDIDATE"
    assert not s["readiness"]["live_capital_eligible"]
    assert not s["readiness"]["paper_trading_eligible"]
    assert s["readiness"]["incremental_claim"] == "FAIL"  # strict superiority, same anchor
    assert s["diagnostics"]["cohort_economics"]["account_score_eligible"] is False
    assert cli.main(["bench", "verify", str(output), "--inputs"]) == 0
    daily_path = pd.read_parquet(output / "daily_metrics.parquet")
    assert len(daily_path) == sum(market["dates"].year == 2020)
    assert (daily_path["date_status"] == "excluded").sum() == 6
    assert "训练真实性" in (output / "report.md").read_text(encoding="utf-8")
    comparison = compare_models(output, output, tmp_path / "comparison")
    assert comparison["difference"]["mean"] == 0
    assert comparison["difference"]["ci95"] == [0, 0]
    assert comparison["signal_score_gain"] == 0
    assert cli.main(["bench", "model", str(market["anchor"]), "--protocol", str(market["path"]),
                     "--output", str(output)]) == 1


def test_legacy_integrity_mode_does_not_accept_old_accounts_or_hide_changes(market, tmp_path):
    from qwb.bench.contracts import object_digest

    output = tmp_path / "legacy"
    result = evaluate_model(market["anchor"], market["path"], output, log=lambda *_: None)
    protocol = read_json(output / "protocol.json")
    del protocol["account"]["history_eligibility"]
    write_json(output / "protocol.json", protocol)
    result["protocol_hash"] = object_digest(protocol)
    result["artifacts"]["protocol.json"] = digest(output / "protocol.json")
    write_json(output / "scorecard.json", result)
    checksums = read_json(output / "checksums.json")
    checksums["files"] = {**result["artifacts"], "scorecard.json": digest(output / "scorecard.json")}
    write_json(output / "checksums.json", checksums)

    checked = verify_result(output, integrity_only=True)
    assert checked["status"] == "PASS"
    assert not any(checked[k] for k in
                   ("inputs_checked", "daily_mean_recomputed", "account_ledger_recomputed"))
    assert checked["scope"] == "local_artifact_integrity_only_not_arithmetic_or_certification"
    assert cli.main(["bench", "verify", str(output), "--integrity-only"]) == 0
    with pytest.raises(ValueError, match="unsupported account"):
        verify_result(output)
    with pytest.raises(ValueError, match="unsupported account"):
        compare_models(output, output, tmp_path / "comparison")
    with pytest.raises(ValueError, match="cannot be combined"):
        verify_result(output, inputs=True, integrity_only=True)
    (output / "report.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="scored artifact changed"):
        verify_result(output, integrity_only=True)


@pytest.mark.parametrize("universe", ["all_ex_st", "list", "csi300"])
def test_composite_factors_and_professional_universes_reach_independent_bench(market, tmp_path, universe):
    root = market["root"]
    codes = market["codes"]
    pd.DataFrame({"field": ["st"], "code": [codes[0]], "start": ["2020-02-01"],
                  "end": ["2020-03-01"]}).to_parquet(root / "st_status.parquet", index=False)
    pd.DataFrame({"index": "000300.SH", "code": codes[:30], "date": "2019-01-01"}).to_parquet(
        root / "index_members.parquet", index=False)
    combined = tmp_path / "combined"
    dataset.create(root, combined)
    source = dataset.add_factor(combined, pd.DataFrame({"code": codes[0],
        "date": market["dates"][:40].strftime("%Y-%m-%d"), "flow": np.arange(40.)}),
        "next_day", "own factor", "fixture")
    raw = read_json(root / "snapshot.json")
    composite_id = read_json(combined / "snapshot.json")["data_snapshot_id"]
    config = yaml.safe_load((market["anchor"] / "config.resolved.yaml").read_text(encoding="utf-8"))
    config["data"]["dir"] = str(combined)
    config["cfg"]["universe"] = universe
    if universe == "list":
        config["cfg"]["universe_list"] = list(codes[:30])
    (market["anchor"] / "config.resolved.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    manifests = read_json(market["anchor"] / "folds.json")
    for manifest in manifests:
        manifest["data_snapshot_id"] = composite_id
    write_json(market["anchor"] / "folds.json", manifests)
    market["table"].assign(data_snapshot_id=composite_id).to_parquet(
        market["anchor"] / "predictions.parquet", index=False)
    protocol_path = tmp_path / "composite.yaml"
    protocol = create_protocol(market["anchor"], protocol_path, cwd=tmp_path,
                               block_days=2, resamples=100, min_days=8)
    assert protocol["schema_version"] == 5
    assert {"dataset.json", "base/index_members.parquet", f"sources/{source['id']}.parquet"} <= set(protocol["data"]["files"])
    result = evaluate_model(market["anchor"], protocol_path, tmp_path / "scored", log=lambda *_: None)
    assert result["status"] == "COMPLETED", result["errors"]
    assert result["evaluation"]["comparable"], result["evaluation"]["issues"]
    assert result["evaluation"]["models"]["candidate"]["mean"] == pytest.approx(1)
    factor_path = combined / "sources" / f"{source['id']}.parquet"
    values = pd.read_parquet(factor_path).assign(flow=-5.)
    values.to_parquet(factor_path, index=False)
    with pytest.raises(ValueError, match="snapshot content changed"):
        verify_inputs(protocol)
    assert read_json(root / "snapshot.json") == raw


def test_independent_accounts_end_to_end_and_recomputed_evidence(market, tmp_path):
    bars_path = market["root"] / "bars" / "fixture.parquet"
    bars = pd.read_parquet(bars_path)
    bars["state"] = "normal"
    bars["down_limit"] = bars["open"] * .8
    bars["up_limit"] = bars["open"] * 1.2
    bars.to_parquet(bars_path, index=False)
    # Complete tail predictions: economic replay never conditions on future labels.
    labels = daily.labels(market["panel"], market["card"]).fillna(0)
    table = daily.prediction_table({"dates": market["dates"], "codes": market["codes"],
        "pred": {0: labels.values}}, market["card"], "snap-fixture", "Asia/Shanghai")
    table.to_parquet(market["anchor"] / "predictions.parquet", index=False)
    protocol_path = tmp_path / "account_protocol.yaml"
    create_protocol(market["anchor"], protocol_path, cwd=tmp_path, block_days=2,
                    resamples=200, min_days=8)
    output = tmp_path / "accounts"
    result = evaluate_model(market["anchor"], protocol_path, output, log=lambda *_: None)
    assert result["status"] == "COMPLETED"
    assert result["economic"]["status"] == "COMPLETED", result["economic"]
    summary = result["economic"]["results"]["extra_0bp_per_side"]
    assert summary["candidate"] == summary["lgbm_anchor_v1"]
    assert summary["equal_weight"]["net_excess_ann_bp"] == 0
    assert summary["cash"]["sharpe"] is None
    assert not result["economic"]["formal_score_eligible"]
    assert not result["readiness"]["live_capital_eligible"]
    assert verify_result(output, inputs=True)["account_ledger_recomputed"]
    comparison = compare_models(output, output, tmp_path / "account_compare")
    delta = comparison["economic_comparison"]["results"]["extra_0bp_per_side"]
    assert delta["annual_net_gain_bp"] == 0 and delta["ci95_annual_net_gain_bp"] == [0, 0]
    assert delta["conclusion"]["status"] == "FAIL"  # no strict improvement over itself
    accounts = pd.read_parquet(output / "account_paths.parquet")
    assert accounts["date"].max() == market["dates"][-1]  # no label-maturity truncation
    # Update local hashes too: arithmetic audit still rejects a rewritten fill.
    fills_path = output / "fills.parquet"
    fills = pd.read_parquet(fills_path)
    fills.loc[0, "notional"] += 100
    fills.to_parquet(fills_path, index=False)
    result["artifacts"]["fills.parquet"] = digest(fills_path)
    write_json(output / "scorecard.json", result)
    checksums = read_json(output / "checksums.json")
    checksums["files"].update({"fills.parquet": digest(fills_path),
                                "scorecard.json": digest(output / "scorecard.json")})
    write_json(output / "checksums.json", checksums)
    with pytest.raises(ValueError, match="reconcile"):
        verify_result(output)


def test_negative_signal_and_direction_of_comparison(market, tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in ("folds.json", "task.yaml", "config.resolved.yaml"):
        (candidate / name).write_bytes((market["anchor"] / name).read_bytes())
    table = market["table"].copy()
    table["prediction"] *= -1
    table.to_parquet(candidate / "predictions.parquet", index=False)
    old, new = tmp_path / "old", tmp_path / "new"
    evaluate_model(market["anchor"], market["path"], old, log=lambda *_: None)
    result = evaluate_model(candidate, market["path"], new, log=lambda *_: None)
    assert result["evaluation"]["signal"]["status"] == "FAIL"
    assert result["readiness"]["level"] == "RESEARCH_ONLY"
    assert "SIGNAL_NOT_CONFIRMED" in {v["id"] for v in result["readiness"]["findings"]}
    delta = compare_models(old, new, tmp_path / "delta")
    assert delta["difference"]["mean"] == pytest.approx(-2)
    assert delta["signal_score_gain"] == pytest.approx(-200)


def test_missing_predictions_do_not_change_reference_sample(market, tmp_path):
    table = market["table"]
    scoring_row = table.index[table["decision_time"].str.startswith("2020")][0]
    path = tmp_path / "missing.parquet"
    table.drop(scoring_row).to_parquet(path, index=False)
    good = evaluate_model(market["anchor"], market["path"], tmp_path / "good", log=lambda *_: None)
    bad = evaluate_model(path, market["path"], tmp_path / "bad", log=lambda *_: None)
    assert bad["status"] == "COMPLETED"
    ev = bad["evaluation"]
    assert ev["scorable"] and not ev["comparable"] and ev["reference_rank"] is None
    assert ev["coverage"]["eligible_pairs"] == good["evaluation"]["coverage"]["eligible_pairs"]
    assert ev["coverage"]["predicted_pairs"] == ev["coverage"]["eligible_pairs"] - 1
    for name in ("reversal_5d", "momentum_20d", "low_vol_20d", "lgbm_anchor_v1"):
        assert ev["models"][name] == good["evaluation"]["models"][name]
    with pytest.raises(ValueError, match="comparable"):
        compare_models(tmp_path / "good", tmp_path / "bad", tmp_path / "no")


@pytest.mark.parametrize("corruption", ["duplicate", "naive", "clock", "code", "task", "snapshot", "inf"])
def test_bad_prediction_contract_is_invalid(market, tmp_path, corruption):
    table = market["table"].copy()
    if corruption == "duplicate":
        table = pd.concat([table, table.iloc[:1]])
    elif corruption == "naive":
        table["decision_time"] = table["decision_time"].str.replace("+08:00", "", regex=False)
    elif corruption == "clock":
        table.loc[0, "decision_time"] = table.loc[0, "decision_time"].replace("15:30", "15:00")
    elif corruption == "inf":
        table.loc[0, "prediction"] = np.inf
    else:
        field = {"code": "code", "task": "task", "snapshot": "data_snapshot_id"}[corruption]
        table.loc[0, field] = "wrong"
    path = tmp_path / "corrupt.parquet"
    table.to_parquet(path, index=False)
    s = evaluate_model(path, market["path"], tmp_path / "bad", log=lambda *_: None)
    assert s["status"] == "INVALID" and "evaluation" not in s and s["errors"]
    assert "未产生分数或排名" in (tmp_path / "bad" / "report.md").read_text(encoding="utf-8")


def test_prediction_only_is_diagnostic_and_constant_predictions_have_no_rank(market, tmp_path):
    s = evaluate_model(market["anchor"] / "predictions.parquet", market["path"],
                       tmp_path / "raw", log=lambda *_: None)
    assert s["evaluation"]["scorable"] and s["evidence"]["sample_status"] == "unknown"
    assert s["evaluation"]["reference_rank"] is None
    constant = market["table"].copy()
    constant["prediction"] = 1
    path = tmp_path / "constant.parquet"
    constant.to_parquet(path, index=False)
    s = evaluate_model(path, market["path"], tmp_path / "constant", log=lambda *_: None)
    assert not s["evaluation"]["scorable"] and s["evaluation"]["reference_rank"] is None
    assert s["evaluation"]["models"]["candidate"]["mean"] is None


def test_immature_training_label_invalidates_signal(market, tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in ("predictions.parquet", "task.yaml"):
        (candidate / name).write_bytes((market["anchor"] / name).read_bytes())
    manifests = read_json(market["anchor"] / "folds.json")
    manifests[0]["max_label_end"] = "2020-01-02 09:30+08:00"
    (candidate / "folds.json").write_text(json.dumps(manifests))
    s = evaluate_model(candidate, market["path"], tmp_path / "bad", log=lambda *_: None)
    assert s["evidence"]["status"] == "INVALID"
    assert s["evaluation"]["scorable"] and s["evaluation"]["signal"]["status"] == "INVALID"
    assert s["evaluation"]["reference_rank"] is None


def test_data_and_anchor_hashes_are_enforced(market):
    p = market["protocol"]
    verify_inputs(p)
    anchor = market["anchor"] / "folds.json"
    original = anchor.read_bytes()
    anchor.write_bytes(original + b" ")
    with pytest.raises(ValueError, match="reference changed"):
        verify_inputs(p)
    anchor.write_bytes(original)
    snapshot = market["root"] / "snapshot.json"
    snapshot.write_text(json.dumps({"data_snapshot_id": "snap-fixture", "revision": 1}))
    with pytest.raises(ValueError, match="snapshot content changed"):
        verify_inputs(p)


def test_changed_protocol_and_changed_result_are_not_comparable(market, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    evaluate_model(market["anchor"], market["path"], a, log=lambda *_: None)
    p = copy.deepcopy(market["protocol"])
    p["statistics"]["seed"] = 99
    second = tmp_path / "other.yaml"
    second.write_text(yaml.safe_dump(p), encoding="utf-8")
    evaluate_model(market["anchor"], second, b, log=lambda *_: None)
    with pytest.raises(ValueError, match="different protocols"):
        compare_models(a, b, tmp_path / "comparison")
    (a / "daily_metrics.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact changed"):
        compare_models(a, a, tmp_path / "comparison")


def test_protocol_rejects_unknown_fields_and_claimed_formal_stage(market):
    p = copy.deepcopy(market["protocol"])
    p["stage"] = "formal"
    with pytest.raises(ValueError, match="development"):
        validate_protocol(p)
    p = copy.deepcopy(market["protocol"])
    p["statstics"] = {}
    with pytest.raises(ValueError, match="schema"):
        validate_protocol(p)
    p = copy.deepcopy(market["protocol"])
    p["evaluation_years"] = [2018, 2020]
    with pytest.raises(ValueError, match="consecutive"):
        validate_protocol(p)


def test_optional_legacy_evidence_is_pinned(market, tmp_path):
    metrics = market["anchor"] / "metrics.json"
    metrics.write_text(json.dumps({"leak_check": {"ok": True}}))
    p = create_protocol(market["anchor"], tmp_path / "with_metrics.yaml", cwd=tmp_path,
                        block_days=2, resamples=200, min_days=8)
    assert p["anchor"]["files"]["metrics.json"] == digest(metrics)
    metrics.write_text(json.dumps({"leak_check": {"ok": False}}))
    with pytest.raises(ValueError, match="reference changed: metrics.json"):
        verify_inputs(p)


def test_twenty_day_task_uses_twenty_one_day_label_end(market, tmp_path):
    p = copy.deepcopy(market["protocol"])
    p["task"]["name"] = "cn_rank_20d"
    p["task"]["target"]["horizon_days"] = 20
    p["task"]["label"]["end"]["day"] = 21
    validate_protocol(p)
    y = daily.labels(market["panel"], p["task"])
    np.testing.assert_allclose(y.iloc[25],
                               market["panel"]["open"].iloc[46] / market["panel"]["open"].iloc[26] - 1)
    assert y.iloc[-21:].isna().all().all()
    bad = copy.deepcopy(p)
    bad["task"]["label"]["end"]["day"] = 20
    with pytest.raises(ValueError, match="next-open labels"):
        validate_protocol(bad)
    anchor = tmp_path / "anchor20"
    anchor.mkdir()
    (anchor / "task.yaml").write_text(yaml.safe_dump(p["task"]), encoding="utf-8")
    (anchor / "config.resolved.yaml").write_bytes(
        (market["anchor"] / "config.resolved.yaml").read_bytes())
    dates = market["dates"]
    maturity = folds.label_end_times(pd.Series(dates), dates, p["task"], "Asia/Shanghai")
    _, manifest = folds.build_fold(0, pd.Series(dates), maturity,
                                  (dates[0], pd.Timestamp("2019-12-31")),
                                  "2019-12-31 15:30+08:00", "snap-fixture", "fixture20")
    (anchor / "folds.json").write_text(json.dumps([manifest.to_dict()]))
    daily.prediction_table({"dates": dates, "codes": market["codes"], "pred": {0: y.values}},
                           p["task"], "snap-fixture", "Asia/Shanghai").to_parquet(
                               anchor / "predictions.parquet", index=False)
    path = tmp_path / "protocol20.yaml"
    create_protocol(anchor, path, cwd=tmp_path, block_days=2, resamples=200, min_days=8)
    s = evaluate_model(anchor, path, tmp_path / "result20", log=lambda *_: None)
    assert s["status"] == "COMPLETED" and s["evaluation"]["comparable"]
    assert s["evaluation"]["coverage"]["excluded_dates"] == 21
    assert s["evaluation"]["models"]["candidate"]["mean"] == pytest.approx(1)


def test_csv_accepts_equivalent_explicit_utc_time(market, tmp_path):
    table = market["table"].copy()
    table["decision_time"] = pd.to_datetime(table["decision_time"], utc=True).astype(str)
    path = tmp_path / "utc.csv"
    table.to_csv(path, index=False)
    s = evaluate_model(path, market["path"], tmp_path / "result", log=lambda *_: None)
    assert s["status"] == "COMPLETED"
    assert s["evaluation"]["models"]["candidate"]["mean"] == pytest.approx(1)


def test_family_intervals_widen_without_breaking_date_pairing():
    rng = np.random.default_rng(10)
    a = rng.normal(size=180)
    s = block_summary(np.column_stack([a, a, a - a]), block_days=10,
                      resamples=1000, seed=0, min_days=30, family_size=4)
    assert s[0] == s[1]
    assert s[0]["simultaneous_ci95"][0] <= s[0]["ci95"][0]
    assert s[0]["simultaneous_ci95"][1] >= s[0]["ci95"][1]
    assert s[2]["simultaneous_ci95"] == [0, 0]


def test_minimum_score_gain_is_predeclared_and_not_live_authorization(market, tmp_path):
    path = tmp_path / "target.yaml"
    p = create_protocol(market["anchor"], path, cwd=tmp_path, block_days=2,
                        resamples=200, min_days=8, min_signal_score=101,
                        min_anchor_gain_score=1)
    assert p["signal_threshold"] == 1.01 and p["assessment"]["min_anchor_gain_ic"] == 0.01
    s = evaluate_model(market["anchor"], path, tmp_path / "result", log=lambda *_: None)
    assert s["evaluation"]["signal"]["status"] == "FAIL"
    findings = {v["id"]: v for v in s["readiness"]["findings"]}
    assert findings["SIGNAL_NOT_CONFIRMED"]["shortfall"] == pytest.approx(1)
    assert findings["ANCHOR_INCREMENT_NOT_CONFIRMED"]["shortfall"] == pytest.approx(1)
    assert not s["readiness"]["live_capital_eligible"]


def test_scorecard_tampering_is_detected(market, tmp_path):
    out = tmp_path / "result"
    s = evaluate_model(market["anchor"], market["path"], out, log=lambda *_: None)
    s["evaluation"]["models"]["candidate"]["signal_score"] += 1
    write_json(out / "scorecard.json", s)
    with pytest.raises(ValueError, match="artifact changed: scorecard.json"):
        verify_result(out)
    # Even if a local user recomputes the hash, unit/mean inconsistencies are independently caught.
    manifest = read_json(out / "checksums.json")
    manifest["files"]["scorecard.json"] = digest(out / "scorecard.json")
    write_json(out / "checksums.json", manifest)
    with pytest.raises(ValueError, match="unit conversion mismatch"):
        verify_result(out)


def test_verify_detects_changed_original_predictions(market, tmp_path):
    out = tmp_path / "result"
    evaluate_model(market["anchor"], market["path"], out, log=lambda *_: None)
    path = market["anchor"] / "predictions.parquet"
    table = pd.read_parquet(path)
    table["prediction"] = table["prediction"].astype("float64")
    table.loc[0, "prediction"] += 1
    table.to_parquet(path, index=False)
    assert verify_result(out)["status"] == "PASS"
    with pytest.raises(ValueError, match="reference changed"):
        verify_result(out, inputs=True)


def test_liquidity_context_is_prefix_invariant(market):
    from qwb.bench.diagnostics import build_context

    panel, protocol = market["panel"], market["protocol"]
    full = build_context(panel, protocol)
    cut = 200
    prefix = build_context({k: v.iloc[:cut] for k, v in panel.items()}, protocol)
    np.testing.assert_allclose(full["liquidity_pct"][:cut], prefix["liquidity_pct"], equal_nan=True)
    np.testing.assert_allclose(full["market_trailing_mean"][:cut],
                               prefix["market_trailing_mean"], equal_nan=True)


def test_cohort_cost_scenarios_have_known_bp_units_and_are_not_account_returns(market):
    from qwb.bench import diagnostics, signal
    from qwb.bench.contracts import REFERENCES

    dates, codes, p = market["dates"], market["codes"], market["protocol"]
    n, m = len(dates), len(codes)
    predictions = np.tile(np.arange(m), (n, 1)).astype(float)
    labels = np.tile(np.linspace(-0.01, 0.01, m), (n, 1))
    refs = {name: predictions.copy() for name in REFERENCES}
    universe = np.ones((n, m), dtype=bool)
    evidence = {"status": "PASS"}
    _, path = signal.evaluate(dates, labels, universe, predictions, refs, p, evidence, evidence)
    context = {"liquidity_pct": np.tile((np.arange(m) + 1) / m, (n, 1)),
               "market_trailing_mean": np.ones(n)}
    d, rows = diagnostics.evaluate(dates, labels, universe, predictions, refs, p, path, context)
    c = d["cohort_economics"]
    expected = np.linspace(-0.01, 0.01, m)[-4:].mean() * 10000
    assert c["mean_top_minus_universe_gross_bp"] == pytest.approx(expected)
    assert c["cost_scenarios"][0]["mean_residual_bp"] == pytest.approx(expected - 10)
    assert c["cost_scenarios"][1]["mean_residual_bp"] == pytest.approx(expected - 30)
    assert c["cost_scenarios"][2]["mean_residual_bp"] == pytest.approx(expected - 60)
    assert not c["account_score_eligible"] and c["status"] == "DIAGNOSTIC_ONLY"
    assert len(rows) == sum(dates.year == 2020)  # preserve daily overlapping cohorts, no compounding


def test_positive_ic_does_not_imply_profitable_top_cohort(market):
    from qwb.bench import diagnostics, signal
    from qwb.bench.contracts import REFERENCES

    dates, p = market["dates"], market["protocol"]
    m = 100
    prediction = np.tile(np.arange(m), (len(dates), 1)).astype(float)
    y = np.linspace(0.001, 0.02, m)
    y[-10:] = -1  # most stocks are ranked correctly; the actual proposed buys fail completely
    labels = np.tile(y, (len(dates), 1))
    refs = {name: prediction.copy() for name in REFERENCES}
    universe = np.ones_like(prediction, dtype=bool)
    ev, path = signal.evaluate(dates, labels, universe, prediction, refs, p,
                               {"status": "PASS"}, {"status": "PASS"})
    assert ev["signal"]["status"] == "PASS"
    context = {"liquidity_pct": np.full_like(prediction, 0.5),
               "market_trailing_mean": np.ones(len(dates))}
    d, _ = diagnostics.evaluate(dates, labels, universe, prediction, refs, p, path, context)
    assert d["cohort_economics"]["mean_top_minus_universe_gross_bp"] < 0
    assert all(v["diagnostic_conclusion"]["status"] == "FAIL"
               for v in d["cohort_economics"]["cost_scenarios"])


def test_missing_label_warning_and_no_trade_permission(market):
    from qwb.bench import diagnostics, signal
    from qwb.bench.assessment import assess
    from qwb.bench.contracts import REFERENCES

    dates, p = market["dates"], market["protocol"]
    shape = (len(dates), 60)
    labels = np.tile(np.linspace(-0.01, 0.01, 60), (len(dates), 1))
    prediction = labels.copy()
    labels[dates.year == 2020, 0] = np.nan
    refs = {name: prediction.copy() for name in REFERENCES}
    universe = np.ones(shape, dtype=bool)
    evidence = {"status": "PASS", "sample_status": "historical_oos_declared"}
    ev, path = signal.evaluate(dates, labels, universe, prediction, refs, p, evidence, evidence)
    context = {"liquidity_pct": np.full(shape, 0.5), "market_trailing_mean": np.ones(len(dates))}
    d, _ = diagnostics.evaluate(dates, labels, universe, prediction, refs, p, path, context)
    r = assess(ev, d, evidence, p)
    assert d["data_gaps"]["missing_mature_label_pairs"] > 0
    assert "MATURE_LABEL_GAPS" in {v["id"] for v in r["findings"]}
    assert not r["live_capital_eligible"] and not r["paper_trading_eligible"]


def test_strong_results_never_override_absent_deployment_evidence(market, tmp_path):
    s = evaluate_model(market["anchor"], market["path"], tmp_path / "result", log=lambda *_: None)
    assert s["evaluation"]["models"]["candidate"]["signal_score"] == pytest.approx(100)
    gates = {v["id"]: v for v in s["readiness"]["gates"]}
    for name in ("net_account", "execution_stress", "reproduction", "trial_selection", "forward_confirmation"):
        assert gates[name]["status"] == "NOT_EVALUATED"
    assert not s["readiness"]["live_capital_eligible"]


def test_low_liquidity_concentration_is_actionable_not_capacity_certification(market):
    from qwb.bench import diagnostics, signal
    from qwb.bench.assessment import assess
    from qwb.bench.contracts import REFERENCES

    dates, p = market["dates"], market["protocol"]
    m = 60
    predictions = np.tile(np.arange(m), (len(dates), 1)).astype(float)
    labels = predictions / 10000
    refs = {name: predictions.copy() for name in REFERENCES}
    universe = np.ones_like(predictions, dtype=bool)
    evidence = {"status": "PASS", "sample_status": "historical_oos_declared"}
    ev, path = signal.evaluate(dates, labels, universe, predictions, refs, p, evidence, evidence)
    context = {"liquidity_pct": np.tile((np.arange(m)[::-1] + 1) / m, (len(dates), 1)),
               "market_trailing_mean": np.ones(len(dates))}
    d, _ = diagnostics.evaluate(dates, labels, universe, predictions, refs, p, path, context)
    r = assess(ev, d, evidence, p)
    f = next(v for v in r["findings"] if v["id"] == "LOW_LIQUIDITY_CONCENTRATION")
    assert f["observation"]["top_low_liquidity_share"] == 1
    assert f["observation"]["universe_low_liquidity_share"] == pytest.approx(1 / 3)
    assert f["severity"] == "WARNING" and f["locations"] and f["next_action"]
    assert "不是资金容量" in f["target"]
    assert not r["live_capital_eligible"]
