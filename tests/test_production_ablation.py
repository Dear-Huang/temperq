"""Frozen-map comparisons must use the same inputs and retain rejections."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_utility_production_ablation import frozen_maps, POLICIES


@pytest.mark.parametrize("raw", [[-1e-12, .3], [1e-11, .3], [-.1, .3]])
def test_four_maps_share_one_frozen_solve_and_keep_material_failures(raw):
    inputs = {"previous_weights": [.2, .2], "lower": [0., 0.], "upper": [.4, .4],
        "budget": .9, "max_turnover": 2., "tolerance": 1e-6,
        "receivable_weight": .05, "min_cash": .05, "max_weight": .4}
    compared = frozen_maps(raw, inputs, ("A", "B"), buffer=1e-12, dust_threshold=1e-10)
    assert compared["raw_weights"] == raw and compared["inputs"] == inputs
    assert set(compared["policies"]) == set(POLICIES)
    for value in compared["policies"].values():
        if value["status"] == "accepted":
            assert value["max_absolute_adjustment"] <= inputs["tolerance"]
            assert value["constraint_residual"] <= inputs["tolerance"]
            assert all(weight >= 0 for weight in value["weights"])
        else:
            assert value["reason"]
    if raw[0] < -.01:
        assert all(value["status"] == "rejected" for value in compared["policies"].values())
    elif raw[0] < 0:
        assert compared["policies"]["strict_raw"]["status"] == "rejected"
        assert all(compared["policies"][name]["status"] == "accepted" for name in POLICIES[1:])
    else:
        assert compared["policies"]["torch_box_budget_turnover_v1"]["weights"][0] > 0
        assert compared["policies"]["torch_box_budget_turnover_dust_v1"]["weights"][0] == 0
