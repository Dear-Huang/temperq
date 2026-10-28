"""Causal matching and independent accounting checks, with synthetic accounts only."""
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

# Research-only comparison of training recipes; torch is not a runtime dependency.
torch = pytest.importorskip("torch")

scripts = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(scripts))
try:
    import diagnose_training_comparison as diag
finally:
    sys.path.pop(0)


def test_rank_smoothing_is_causal_and_alpha_one_preserves_baseline():
    rng = np.random.default_rng(11)
    pred = rng.normal(size=(20, 30))
    eligible = np.ones((20, 30), dtype=bool)
    spread = np.ones((20, 30))*5
    test = np.arange(20)
    a = diag.smooth_targets(pred, eligible, spread, test, .3)
    changed = pred.copy(); changed[11:] *= -100
    b = diag.smooth_targets(changed, eligible, spread, test, .3)
    for t in (0, 5, 10):
        np.testing.assert_array_equal(a[t], b[t])
    actual = diag.smooth_targets(pred, eligible, spread, test, 1.)
    expected = diag.r.targets_for({"codes":pd.Index(range(30)), "u":eligible, "spread":spread}, pred, test, standard=True)
    for t in actual:
        np.testing.assert_array_equal(actual[t], expected[t])


def test_independent_numpy_water_filling_matches_original_head():
    rng = np.random.default_rng(2)
    score = rng.normal(0, .001, 40).astype("float32")
    spread = rng.uniform(1, 20, 40).astype("float32")
    valid = torch.ones(40, dtype=torch.bool)
    previous = torch.zeros(40)
    penalty = (.0003+spread/10000+.0005)
    value = score*5-penalty
    ids = torch.topk(torch.from_numpy(value), 20).indices.numpy()
    scale = np.std(value, ddof=0, dtype=np.float32)*.2
    expected = np.zeros(40)
    expected[ids] = diag.numpy_capped_weights(value/scale, ids)
    actual = diag.r.torch_weights(torch.from_numpy(score), valid, previous, torch.from_numpy(spread)).numpy()
    np.testing.assert_allclose(actual, expected, atol=2e-6)


def test_fee_audit_reconciles_cash_and_statutory_tax_change(tmp_path):
    dates = pd.bdate_range("2023-08-23", periods=6)
    codes = pd.Index(["600000.SH"])
    p = {k:pd.DataFrame(v, index=dates, columns=codes) for k,v in
         (("open",10.), ("close",10.), ("adj_factor",1.), ("up_limit",11.), ("down_limit",9.))}
    d = {"p":p, "dates":dates, "codes":codes, "spread":np.full((6,1),10.), "normal":np.ones((6,1),dtype=bool)}
    targets = {0:np.array([.95]), 1:np.array([0.]), 2:np.array([.95]), 3:np.array([0.])}
    case = tmp_path / "y2023-s0"; case.mkdir()
    for arm in ("B0", "B0-cost", "B1", "B2"):
        for stress in (0,10):
            path, fills, _ = diag.replay(d, targets, np.arange(6), stress)
            path.to_parquet(case / f"{arm}-slip{stress}-path.parquet", index=False)
            fills.to_parquet(case / f"{arm}-slip{stress}-fills.parquet", index=False)
    rows, examples = diag.audit_fees(case,d)
    assert max(r["max_cash_flow_error_yuan"] for r in rows) < 1e-6
    assert all(r["max_fill_fee_error_yuan"] < 1e-8 for r in rows)
    assert any(r["example"] == "pre_tax_cut_sell" and r["stamp_tax_bp"] == 10 for r in examples)
    assert any(r["example"] == "post_tax_cut_sell" and r["stamp_tax_bp"] == 5 for r in examples)
    corrupt = pd.read_parquet(case / "B0-slip0-fills.parquet")
    corrupt.loc[0, "total_cost"] += 1
    corrupt.to_parquet(case / "B0-slip0-fills.parquet", index=False)
    with pytest.raises(ValueError, match="fill fee mismatch"):
        diag.audit_fees(case,d)
