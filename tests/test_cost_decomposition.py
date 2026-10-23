"""Hand-computed fill-level cost units, zero denominators and v1 compatibility."""

import copy

import pytest

from qwb.portfolio.reconciliation import cost_decomposition


VERSION = "same_fill_cost_decomposition_v2"


def example():
    recipe = {"actual_equity": 20000., "fees": {"commission_bp": 3., "slippage_bp": 2.},
              "cost": {"sell_tax_bp": 10.}, "buy_cost": [.002], "sell_cost": [.003]}
    fills = [
        {"asset_id": "A", "side": "BUY", "quantity": 100, "notional": 1000.,
         "commission": 5., "stamp_tax": 0., "slippage": .2, "total_cost": 5.2},
        {"asset_id": "A", "side": "SELL", "quantity": 50, "notional": 500.,
         "commission": 5., "stamp_tax": .6, "slippage": .15, "total_cost": 5.75},
    ]
    zero = {**fills[0], "quantity": 0, "notional": 0., "commission": 0., "slippage": 0., "total_cost": 0.}
    account = {"audits": [{"decision_id": "d0", "equity_before": 20000., "total_cost": 10.95, "fills": fills},
                           {"decision_id": "d1", "equity_before": 21000., "total_cost": 0., "fills": [zero]}]}
    allocation = {"attempts": [{"decision_id": decision, "status": "target", "assets": ["A"],
                                "recipe": recipe, "intended_proxy_cost": intended}
                               for decision, intended in (("d0", 1.), ("d1", 0.))]}
    return account, allocation


def test_fill_units_match_hand_cost_bridge_without_changing_default_v1():
    account, allocation = example()
    original = copy.deepcopy((account, allocation))
    legacy = cost_decomposition(account, allocation)
    assert legacy == cost_decomposition(account, allocation, schema_version="same_fill_cost_decomposition_v1")
    assert set(legacy) == {"schema_version", "daily", "totals", "scope"}
    result = cost_decomposition(account, allocation, schema_version=VERSION)
    assert (account, allocation) == original
    assert result["schema_version"] == VERSION and result["initial_equity_cny"] == 20000
    assert result["totals"] == legacy["totals"]
    for old, new in zip(legacy["daily"], result["daily"], strict=True):
        assert {key: new[key] for key in old} == old
    buy, sell, zero = result["fills"]
    assert [row["side"] for row in result["fills"]] == ["BUY", "SELL", "BUY"]
    assert buy["cny"]["actual_to_filled_gap"] == pytest.approx(3.2)
    assert sell["cny"]["actual_to_filled_gap"] == pytest.approx(4.25)
    assert buy["cny"]["minimum_commission_premium"] == pytest.approx(4.7)
    assert sell["cny"]["sell_tax_rate_gap"] == pytest.approx(.1)
    assert sell["cny"]["slippage_gap"] == pytest.approx(.05)
    assert sell["cny"]["negative_proxy_impact"] == pytest.approx(-.75)
    assert buy["bp_of_fill_notional"]["actual_cost"] == pytest.approx(52.)
    assert sell["bp_of_fill_notional"]["actual_cost"] == pytest.approx(115.)
    assert result["totals_bp_of_initial_equity"]["actual_cost"] == pytest.approx(5.475)
    assert result["totals_bp_of_initial_equity"]["filled_proxy_cost"] == pytest.approx(1.75)
    assert result["daily"][0]["relative_actual_to_filled_gap"] == pytest.approx(7.45 / 3.5)
    for record in result["fills"]:
        assert abs(record["cny"]["accounting_residual"]) < 1e-12
        for key, value in record["cny"].items():
            assert record["bp_of_initial_equity"][key] == pytest.approx(value / 2)
    assert all(value is None for value in zero["bp_of_fill_notional"].values())
    assert zero["relative_actual_to_filled_gap"] is None
    assert result["daily"][1]["relative_actual_to_filled_gap"] is None


def test_zero_proxy_cost_keeps_actual_cost_and_fixed_initial_equity_basis():
    account, allocation = example()
    account["audits"][1].update(total_cost=5., fills=[{
        "asset_id": "A", "side": "BUY", "quantity": 100, "notional": 1000.,
        "commission": 5., "stamp_tax": 0., "slippage": 0., "total_cost": 5.}])
    allocation["attempts"][1]["recipe"] = {"fees": {"commission_bp": 0., "slippage_bp": 0.},
        "cost": {"sell_tax_bp": 0.}, "buy_cost": [0.], "sell_cost": [0.]}
    result = cost_decomposition(account, allocation, schema_version=VERSION)
    fill = result["fills"][-1]
    assert fill["relative_actual_to_filled_gap"] is None
    assert fill["cny"]["actual_to_filled_gap"] == 5
    assert fill["bp_of_initial_equity"]["actual_to_filled_gap"] == 2.5
    assert result["daily"][1]["bp_of_initial_equity"]["actual_cost"] == 2.5
    assert fill["bp_of_fill_notional"]["actual_cost"] == 50


def test_fill_arithmetic_tampering_and_unknown_version_are_rejected():
    account, allocation = example()
    account["audits"][0]["fills"][0]["total_cost"] += 1
    with pytest.raises(ValueError, match="per-fill"):
        cost_decomposition(account, allocation, schema_version=VERSION)
    with pytest.raises(ValueError, match="version"):
        cost_decomposition(account, allocation, schema_version="future")
    with pytest.raises(ValueError, match="nonempty"):
        cost_decomposition({"audits": []}, {"attempts": []}, schema_version=VERSION)


@pytest.mark.parametrize("equity", [0., -1., float("nan"), float("inf")])
def test_bp_basis_requires_positive_finite_initial_equity(equity):
    account, allocation = example()
    account["audits"][0]["equity_before"] = equity
    with pytest.raises(ValueError, match="initial equity"):
        cost_decomposition(account, allocation, schema_version=VERSION)
