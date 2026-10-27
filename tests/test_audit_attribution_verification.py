from qwb import audit_attribution as aa, audit_verification as av


def make_result(net_days=(100., -40., 60.), fee_reported=10., fee_modeled=8., spread=3.):
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    daily = [{"date": d, "net_pnl": v, "equity_change": v, "fees": fee_reported, "gross_turnover": .1}
             for d, v in zip(days[1:], net_days)]
    fills = [{"date": d, "code": "600000.SH", "side": "buy", "fee": fee_reported, "ref": {"source": "fills", "row": i},
              "tick_scenario_cost": spread, "accounted_interval": True} for i, d in enumerate(days[1:])]
    fills.append({"date": days[0], "code": "600000.SH", "side": "buy", "fee": 5., "ref": {"source": "fills", "row": 99},
                  "tick_scenario_cost": 1., "accounted_interval": False})
    fee_fills = [{**f, "modeled_fee": fee_modeled if f["accounted_interval"] else 5.} for f in fills]
    stocks = [{"code": "600000.SH", "gross_pnl": sum(net_days) + 3 * fee_reported, "fees": 3 * fee_reported, "net_pnl": sum(net_days)}]
    return {"mode": "full",
            "period_review": {"partition": {"status": "planned", "development_start": days[1], "development_end": days[2],
                                            "validation_start": days[3], "validation_end": days[3]}},
            "modules": {
                "accounting": {"baseline_equity": 1000., "daily": daily},
                "fee_analysis": {"fills": fee_fills},
                "tick_analysis": {"fills": fills, "accounted_intervals": {"tick_scenario_cost": 3 * spread}},
                "attribution": {"status": "verified", "stocks": stocks, "net_pnl": sum(net_days),
                                "gross_pnl": stocks[0]["gross_pnl"], "fees": 3 * fee_reported},
                "risk_analysis": {"max_drawdown": {"ratio": .04, "peak_date": days[1], "trough_date": days[2], "loss": 40.,
                                                   "recovery_date": days[3], "stocks": [{"code": "600000.SH", "net_pnl": -40.}],
                                                   "contribution_gap": 0., "fees": 10.}},
                "holding_analysis": {"status": "verified", "closed_age_bands": [
                    {"id": "0_to_2", "label": "0–2自然日", "matches": 2, "gross_pnl": 5., "fees": 4., "net_pnl": 1., "net_after_tick_scenario": -2.}],
                    "entry_price_bands": [], "closed_summary": {"matches": 2}, "open_summary": {}},
            }}


def test_restatement_adds_back_overcharged_fees_and_subtracts_spread():
    restated = av.restate(make_result())
    total = av.totals(restated)
    assert total["reported"] == 120.
    assert total["fee_gap"] == 6.             # 3 fills × (10 reported − 8 modeled)
    assert total["spread"] == 9.              # baseline-day fill is excluded
    assert total["realistic"] == 120. + 6. - 9.
    assert restated["unpriced_fills"] == 0


def test_comparison_judges_on_realistic_figures_and_uses_validation_segment():
    original = make_result(net_days=(100., -40., 60.), spread=3.)
    cheaper = make_result(net_days=(95., -40., 62.), spread=1.)    # less gross, much less spread
    cmp = av.compare(original, cheaper)
    assert cmp["comparable"] and cmp["state"] == "better"
    assert cmp["full_gain"] > 0 and cmp["validation_gain"] > 0


def test_comparison_recommends_keeping_original_when_optimized_loses():
    original = make_result()
    worse = make_result(net_days=(50., -60., 10.))
    cmp = av.compare(original, worse)
    assert cmp["state"] == "worse" and "保留原版" in cmp["verdict"]


def test_mixed_when_only_development_segment_improves():
    original = make_result(net_days=(100., -40., 60.))
    early = make_result(net_days=(140., -40., 50.))
    assert av.compare(original, early)["state"] == "mixed"


def test_attribution_closes_and_summarises():
    data = aa.build(make_result())
    assert data["verified"] and all(data["checks"].values())
    assert data["waterfall"][-1]["value"] == data["net"] - data["spread"]
    assert data["by_holding"][0]["after_tick"] == -2.
    assert len(data["summary"]) == 3


def test_attribution_absent_without_full_audit():
    assert aa.build({"mode": "static", "modules": {}}) is None
    assert av.restate({"mode": "static", "modules": {}}) is None
