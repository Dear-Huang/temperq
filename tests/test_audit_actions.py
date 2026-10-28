from qwb import audit_actions


def result(**modules):
    return {"modules": modules}


TICK = {"status": "verified",
        "accounted_intervals": {"traded_notional": 1e8, "net_pnl": 40000., "tick_scenario_cost": 110000.,
                                "weighted_half_spread_bp": 7.4, "net_after_tick_scenario": -70000.,
                                "profit_covers_scenario": False, "break_even_cost_fraction": .36},
        "price_bands": [{"id": "under_5", "notional_share": .2, "weighted_half_spread_bp": 13.3,
                         "tick_scenario_cost": 42000., "traded_notional": 2e7}]}


def test_every_card_keeps_status_quo_and_states_both_sides():
    fees = {"status": "measured", "reported_fee": 120000., "modeled_fee": 85000.,
            "minimum_premium": 900., "small_unit_count": 25}
    holding = {"status": "verified", "closed_summary": {"matches": 10},
               "closed_age_bands": [{"id": "0_to_2", "label": "0–2自然日", "matches": 9, "net_pnl": -1., "net_after_tick_scenario": -5.,
                                     "fees": 3., "tick_scenario_cost": 4.},
                                    {"id": "over_10", "label": "超过10自然日", "matches": 1, "net_pnl": 2., "net_after_tick_scenario": 1.,
                                     "fees": 1., "tick_scenario_cost": 1.}]}
    cards = audit_actions.cards(result(tick_analysis=TICK, fee_analysis=fees, holding_analysis=holding))
    assert len(cards) == 4
    for card in cards:
        assert card["options"][0]["label"] == "维持原样"
        assert card["saving"] and card["sacrifice"] and card["net"] and card["overturn"]
        assert card["evidence"] in audit_actions.EVIDENCE


def test_cost_card_is_urgent_when_profit_cannot_cover_tick_floor():
    card = audit_actions.cards(result(tick_analysis=TICK))[0]
    assert card["id"] == "tick_floor" and card["state"] == "act"
    assert "37%" not in card["headline"] and "36%" in card["headline"]
    assert "2.7bp" in card["headline"]           # 7.4bp × 0.36


def test_cost_card_holds_when_profit_covers_spread():
    tick = {**TICK, "accounted_intervals": {**TICK["accounted_intervals"], "net_after_tick_scenario": 5000.,
                                            "profit_covers_scenario": True, "break_even_cost_fraction": 1.2}}
    assert audit_actions.cards(result(tick_analysis=tick))[0]["state"] == "hold"


def test_fee_card_direction_and_absence_when_fees_match():
    over = audit_actions.cards(result(fee_analysis={"status": "measured", "reported_fee": 120., "modeled_fee": 80.}))
    assert over and "偏保守" in over[0]["headline"] and over[0]["evidence"] == "recomputed"
    under = audit_actions.cards(result(fee_analysis={"status": "measured", "reported_fee": 80., "modeled_fee": 120.}))
    assert "偏乐观" in under[0]["headline"]
    assert audit_actions.cards(result(fee_analysis={"status": "measured", "reported_fee": 100., "modeled_fee": 100.})) == []


def test_holding_card_is_descriptive_not_a_promise():
    holding = {"status": "verified", "closed_summary": {"matches": 3},
               "closed_age_bands": [{"id": "0_to_2", "label": "0–2自然日", "matches": 3, "net_pnl": -1., "net_after_tick_scenario": -2.,
                                     "fees": 1., "tick_scenario_cost": 1.}]}
    card = audit_actions.cards(result(holding_analysis=holding))[0]
    assert card["evidence"] == "descriptive" and "重放" in card["net"]


def test_missing_modules_give_no_cards():
    assert audit_actions.cards(result()) == []
    assert audit_actions.cards(result(tick_analysis={"status": "not_supplied"})) == []


def band(id, matches, after, fees=10., tick=5.):
    return {"id": id, "label": id, "matches": matches, "net_pnl": after + tick, "net_after_tick_scenario": after,
            "fees": fees, "tick_scenario_cost": tick}


def test_holding_card_holds_when_short_trades_still_make_money():
    holding = {"status": "verified", "closed_summary": {"matches": 20},
               "closed_age_bands": [band("0_to_2", 10, 5000.), band("over_10", 10, -300.)]}
    card = audit_actions.cards(result(holding_analysis=holding))[0]
    assert card["state"] == "hold" and "暂不改" in card["headline"] and "¥5,000" in card["headline"]
    losing = {**holding, "closed_age_bands": [band("0_to_2", 10, -5000.), band("over_10", 10, 300.)]}
    card = audit_actions.cards(result(holding_analysis=losing))[0]
    assert card["state"] == "review" and "拿久一点" in card["headline"]


def test_low_price_card_needs_a_meaningful_share():
    def holding(after):
        closed = {"matches": 6, "fees": 100., "tick_scenario_cost": 200., "net_after_tick_scenario": after}
        return {"status": "verified", "closed_summary": {"matches": 6}, "closed_age_bands": [],
                "entry_price_bands": [{"id": "under_5", "label": "低于5元", "closed": closed}]}
    tick = lambda share: {"status": "not_supplied", "price_bands": [{"id": "under_5", "notional_share": share}]}
    card = audit_actions.cards(result(holding_analysis=holding(-800.), tick_analysis=tick(.2)))[0]
    assert card["id"] == "low_price" and card["state"] == "review" and "¥800" in card["headline"]
    assert audit_actions.cards(result(holding_analysis=holding(-800.), tick_analysis=tick(.01))) == []


def test_cards_point_at_code_lines_when_the_bundle_is_given():
    code = "def initialize(context):\n    g.stock_num = 10\n    run_weekly(weekly_adjustment, weekday=1, time='9:30')\n"
    bundle = {"adapter": "joinquant_v1", "code": {"source": code}, "fills": [], "snapshots": [], "positions": []}
    holding = {"status": "verified", "closed_summary": {"matches": 20},
               "closed_age_bands": [band("0_to_2", 10, -5000.), band("over_10", 10, 300.)]}
    card = audit_actions.cards(result(holding_analysis=holding), bundle)[0]
    assert [(e["line"], e["change"][:11]) for e in card["edits"]] == [(3, "run_monthly")]
    assert audit_actions.cards(result(holding_analysis=holding))[0]["edits"] == []


def test_cost_card_when_declared_slippage_already_covers_the_floor():
    tick = {**TICK, "accounted_intervals": {**TICK["accounted_intervals"], "tick_scenario_cost": 0.,
                                            "tick_floor_cost": 600., "already_charged": 600.}}
    card = audit_actions.cards(result(tick_analysis=tick))[0]
    assert card["state"] == "hold" and "不用再扣" in card["headline"]
    assert card["evidence"] in audit_actions.EVIDENCE and card["options"][0]["label"] == "维持原样"
