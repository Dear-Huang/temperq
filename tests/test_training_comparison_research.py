"""Causality, purging and action-gradient checks for the isolated research runner."""
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

# Research-only comparison of training recipes; torch is not a runtime dependency.
torch = pytest.importorskip("torch")


spec = importlib.util.spec_from_file_location("training_comparison", Path(__file__).parents[1] / "scripts/run_training_comparison.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.modules["run_training_comparison"] = runner
report_spec = importlib.util.spec_from_file_location("comparison_report", Path(__file__).parents[1] / "scripts/report_training_comparison.py")
reporter = importlib.util.module_from_spec(report_spec)
report_spec.loader.exec_module(reporter)


def test_split_purges_longest_labels():
    dates = pd.bdate_range("2018-01-01", "2023-12-31")
    for year in (2021, 2022, 2023):
        train, val, full, test = runner.split_dates(dates, year)
        assert dates[train[-1]+21] < dates[val[0]]
        assert dates[full[-1]+21] < dates[test[0]]
        assert dates[val[-1]+21] < dates[test[0]]


def test_features_cannot_see_future_prices():
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2018-01-01", periods=200)
    close = pd.DataFrame(np.exp(rng.normal(0, .01, (200, 3)).cumsum(axis=0))*10, index=dates)
    p = {"open": close*.99, "close": close, "high": close*1.02, "low": close*.98,
         "adj_factor": close*0+1, "amount": close*1e7, "up_limit": close*1.1}
    a = runner.feature_frames(p)
    changed = {k: v.copy() for k, v in p.items()}
    for key in changed:
        changed[key].iloc[170:] *= 100
    b = runner.feature_frames(changed)
    assert len(a) == 32
    for key in a:
        pd.testing.assert_frame_equal(a[key].iloc[:170], b[key].iloc[:170])


def test_capped_topk_head_has_action_gradient():
    torch.manual_seed(2)
    score = (torch.randn(40)*.0001).requires_grad_()
    mask = torch.ones(40, dtype=torch.bool); mask[0] = False
    w = runner.torch_weights(score, mask, torch.zeros(40), torch.ones(40)*5)
    assert abs(float(w.sum().detach())-.95) < 1e-6
    assert float(w.max().detach()) <= .100001 and w[0] == 0
    assert int((w > 0).sum()) <= 20
    objective = torch.dot(w, torch.linspace(-.03, .03, 40))
    objective.backward()
    assert torch.isfinite(score.grad).all()
    assert float(score.grad.norm()) > 1e-6


def test_calibration_uses_only_passed_validation_labels():
    rng = np.random.default_rng(4)
    predictions = rng.normal(size=(60, 40, 3))
    raw = predictions*.01 + rng.normal(0, .001, predictions.shape)
    mask = np.ones((60, 40), dtype=bool)
    cal = runner.calibration(predictions, raw, mask)
    assert np.allclose(cal["slopes"], .01, atol=.0002)
    assert len(cal["mix"]) == 3


def test_exported_targets_respect_strict_float64_caps():
    rng = np.random.default_rng(8)
    d = {"codes": pd.Index([f"s{i}" for i in range(40)]),
         "u": np.ones((1, 40), dtype=bool), "spread": np.full((1, 40), 5.)}
    targets = runner.targets_for(d, rng.normal(0, .001, (1, 40)), np.array([0]))
    assert targets[0].max() <= .1
    assert targets[0].sum() <= .95


def test_raw_share_audit_stops_at_unmodelled_corporate_action(tmp_path):
    dates = pd.bdate_range("2021-01-04", periods=10)
    codes = pd.Index([f"{600000+i}.SH" for i in range(20)])
    p = {k: pd.DataFrame(v, index=dates, columns=codes)
         for k, v in (("open", 10.), ("close", 10.), ("up_limit", 11.), ("down_limit", 9.), ("adj_factor", 1.))}
    p["adj_factor"].iloc[3:, 0] = 1.01
    d = {"p": p, "dates": dates, "codes": codes, "spread": np.full((10, 20), 5.),
         "normal": np.ones((10, 20), dtype=bool)}
    case = tmp_path / "y2021-s0"; case.mkdir()
    runner.write(case / "B0-targets.json", {str(dates[0].date()): {code: .95/20 for code in codes}})
    result = reporter.raw_share_audit(d, case, "B0")
    assert result["status"] == "failed_qualification"
    assert result["completed_sessions"] == 2
    assert result["reason"] == "UNMODELLED_CORPORATE_ACTION_OR_MISSING_FACTOR"


def test_paired_inference_counts_dates_not_seeds():
    dates = pd.bdate_range("2021-01-04", periods=30)
    rng = np.random.default_rng(1)
    base = rng.normal(0, .005, len(dates))
    rows = [{"date": day, "year": 2021, "seed": seed, "stress_bp": 0,
             "arm": arm, "net_return": r + (0 if arm == "B0" else .001)}
            for day, r in zip(dates, base) for seed in (0, 1, 2) for arm in reporter.ARMS]
    comparisons = reporter.paired_inference(pd.DataFrame(rows), 0)
    assert len(comparisons) == 10
    assert comparisons[0]["supported_positive"]
    same = next(r for r in comparisons if r["arm"] == "B3" and r["reference"] == "B2")
    assert same["familywise_95_interval"] == [0., 0.]
