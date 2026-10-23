from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qwb import daily, folds, metrics, pipeline, runconfig, taskcard

EX = Path(__file__).resolve().parents[1] / "examples"
TZ = "Asia/Shanghai"


def test_inner_validation_purges_labels_and_uses_only_outer_training_rows():
    dates = pd.bdate_range("2019-01-01", periods=220)
    card = taskcard.load(EX / "cn_rank_5d.yaml")
    ends = folds.label_end_times(pd.Series(dates), dates, card, TZ)
    outer = np.tile((np.arange(len(dates)) < 190)[:, None], (1, 30))
    train, valid, record = folds.purged_tail_split(dates, ends, outer,
        validation_days=30, min_train_days=100, decision_time="15:30", tz=TZ)
    assert not (train & valid).any()
    assert not ((train | valid) & ~outer).any()
    assert record["purged_rows"] > 0
    assert ends.iloc[np.flatnonzero(train.any(axis=1))].max() <= pd.Timestamp(record["fit_cutoff"])
    assert dates[train.any(axis=1)].max() < dates[valid.any(axis=1)].min()
    assert not record["uses_outer_test_labels"]


def test_changing_outer_test_does_not_change_selected_rounds_or_fold_model(tmp_path):
    pytest.importorskip("lightgbm")
    card = taskcard.load(EX / "cn_rank_5d.yaml")
    panel = daily.panel_from_bars(pipeline.synth_bars(420, 40))
    cfg = dict(daily.DEFAULTS, universe="all", min_age_days=20, first_test_year=2020,
               seeds=[0], num_boost_round=20, selection={"validation_days": 20,
               "min_train_days": 80, "patience": 5}, features=["r1", "r5", "r20"],
               lgbm=dict(daily.DEFAULTS["lgbm"], min_data_in_leaf=30, num_threads=2))
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    first = daily.run(panel, card, cfg, "fixture", TZ, model_dir=before, log=lambda *_: None)
    changed = {key: frame.copy() for key, frame in panel.items()}
    future = panel["close"].index.year >= 2020
    for name in ("open", "close", "high", "low"):
        changed[name].loc[future] *= np.linspace(0.4, 2.0, 40)
    second = daily.run(changed, card, cfg, "fixture", TZ, model_dir=after, log=lambda *_: None)
    assert first["manifests"] == second["manifests"]
    assert (before / "lgbm_fold0_seed0.txt").read_bytes() == (after / "lgbm_fold0_seed0.txt").read_bytes()
    choice = first["manifests"][0]["selection"][0]
    assert 1 <= choice["best_iteration"] <= 20
    assert choice["validation_end"] < "2020-01-01"
    final = daily.fit_final(panel, card, cfg, "fixture", TZ, tmp_path / "final",
                            first["F"], first["U"], log=lambda *_: None)
    assert final["selection"][0]["refit_on_all_matured"]
    assert final["features"] == ["r1", "r5", "r20"]


@pytest.mark.parametrize("model", [
    {"selection": {"validation_days": 0}},
    {"selection": {"patience": True}},
    {"selection": {"unknown": 1}},
    {"selection": {}, "params": {"num_iterations": 1000}},
    {"selection": {}, "params": {"boosting_type": "dart"}},
    {"name": "ridge", "selection": {}},
    {"params": ["num_leaves"]},
    {"num_boost_round": 0},
])
def test_selection_configuration_rejects_ambiguous_or_inoperable_settings(model):
    raw = runconfig.load(EX / "run_smoke.yaml")
    raw["model"] = model
    resolved, report = runconfig.resolve(raw, EX)
    assert resolved is None and not report.ok


def test_newey_west_keeps_missing_calendar_dates_in_lag_products():
    x = np.array([1., 2., np.nan, 3., 2., np.nan, 1., 4., 3., 2., 1., 2.])
    result = metrics.newey_west(x, lag=1)
    count = np.isfinite(x).sum()
    residual = np.where(np.isfinite(x), x - np.nanmean(x), 0)
    expected = np.sqrt((residual @ residual + residual[1:] @ residual[:-1]) / count ** 2)
    assert result["se"] == pytest.approx(expected)
    assert result["se"] != pytest.approx(metrics.newey_west(x[np.isfinite(x)], 1)["se"])


def test_positive_own_ic_does_not_establish_increment_over_equal_reference():
    candidate = np.random.default_rng(1).normal(.1, .01, 200)
    result = metrics.paired_signal_screen(candidate, {"same": candidate}, lag=5)
    assert result["candidate"]["t"] > 2
    assert result["status"] == "NOT_ESTABLISHED"
    assert result["comparisons"]["same"]["status"] == "NO_CHANGE"


def test_missing_daily_ic_cannot_promote_a_model_on_its_surviving_dates():
    candidate = np.full(200, .2)
    candidate[10] = np.nan
    result = metrics.paired_signal_screen(candidate, {"zero": np.zeros(200)}, lag=5)
    assert result["comparisons"]["zero"]["status"] == "IMPROVED"
    assert result["status"] == "NOT_ESTABLISHED"
    assert result["missing_common_dates"] == 1


@pytest.mark.parametrize("target,output", [("event_class", "score"), ("return_rank", "target_weight"),
                                          ("volatility", "score")])
def test_native_runner_does_not_silently_train_returns_for_unimplemented_tasks(target, output):
    raw = runconfig.load(EX / "run_smoke.yaml")
    raw["task"] = taskcard.load(EX / "cn_rank_5d.yaml")
    raw["task"]["target"]["type"] = target
    raw["task"]["output"] = output
    resolved, report = runconfig.resolve(raw, EX)
    assert resolved is None
    assert "UNSUPPORTED_NATIVE_TASK" in report.codes()


def test_invalid_prices_do_not_become_infinite_labels_or_extreme_rank_features():
    panel = daily.panel_from_bars(pipeline.synth_bars(80, 30))
    panel["open"].iloc[11, 0] = 0
    panel["up_limit"].iloc[10, 0] = 0
    panel["adj_factor"].iloc[12, 0] = -1
    labels = daily.labels(panel, taskcard.load(EX / "cn_rank_5d.yaml"))
    assert pd.isna(labels.iloc[10, 0])
    assert pd.isna(daily.features(panel)["to_limit"].iloc[10, 0])
    assert not daily.universe(panel, daily.DEFAULTS).iloc[12, 0]


def test_missing_requested_final_year_is_not_silently_dropped(tmp_path):
    raw = runconfig.load(EX / "run_smoke.yaml")
    raw["train"]["final_test_years"] = [2021, 2022]
    resolved, report = runconfig.resolve(raw, EX)
    assert report.ok
    with pytest.raises(ValueError, match="final test years are absent"):
        pipeline.run(resolved, tmp_path, log=lambda *_: None)


def test_feature_subset_is_explicit_and_missing_feature_is_an_error():
    panel = daily.panel_from_bars(pipeline.synth_bars(80, 30))
    assert daily.feature_names(daily.selected_features(panel, {"features": ["r5", "r1"]})) == ["r1", "r5"]
    with pytest.raises(ValueError, match="absent"):
        daily.selected_features(panel, {"features": ["x_missing"]})


def test_mutating_source_during_fit_fails_the_attempt_instead_of_publishing(tmp_path, monkeypatch):
    import json
    from qwb import experiments

    source = tmp_path / "snapshot"
    (source / "bars").mkdir(parents=True)
    pipeline.synth_bars(420, 40).to_parquet(source / "bars" / "data.parquet", index=False)
    (source / "snapshot.json").write_text(json.dumps({"data_snapshot_id": "same-name"}))
    raw = runconfig.load(EX / "run_smoke.yaml")
    raw.update(data={"source": "snapshot", "dir": str(source)}, model={"name": "ridge"})
    raw["train"].update(first_test_year=2020, final_test_years=[2020], seeds=[0], shuffle_null_draws=1)
    resolved, report = runconfig.resolve(raw, EX)
    assert report.ok
    original = daily.fit_final

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        (source / "snapshot.json").write_text(json.dumps({"data_snapshot_id": "same-name", "changed": True}))
        return result

    monkeypatch.setattr(daily, "fit_final", mutate)
    with pytest.raises(ValueError, match="data changed during"):
        pipeline.run(resolved, tmp_path, log=lambda *_: None)
    attempts = list((tmp_path / "runs/history").iterdir())
    assert len(attempts) == 1
    assert experiments.read(attempts[0] / "run.json")["state"] == "failed"
    assert not (tmp_path / resolved["output"]).exists()
