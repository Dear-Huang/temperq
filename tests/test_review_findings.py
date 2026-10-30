"""Regressions from two independent reviews (guojiu small-cap re-migration; 74 and guojiu follow-up)."""
from qwb import audit_actions, audit_fee_model, audit_premise, audit_report, audit_signals


def premise_bundle(drop_losers=False, n_days=30):
    """Five candidates a day; the lowest score always falls 10% next period. Dropping it biases the IC upward."""
    days = [f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n_days)]
    scores, prices = [], []
    for i, d in enumerate(days):
        for k, code in enumerate(("A.SH", "B.SH", "C.SH", "D.SH", "E.SH")):
            scores.append({"date": d, "code": code, "score": float(k), "selected": k >= 3})
            price = 10. * (1 + (.01 * (k - 2) + (.003 if (i + k) % 2 else -.003))) ** i
            if not (drop_losers and code == "A.SH" and i > 0):
                prices.append({"date": d, "code": code, "close": price})
    return {"scores": scores, "prices": prices, "code": {"source": "x = 1\n"}}


def test_p0_missing_forward_prices_block_the_conclusion():
    full = audit_premise.analyze(premise_bundle())
    assert full["price_coverage"]["missing_share"] == 0 and full["status"] != "incomplete_prices"
    cut = audit_premise.analyze(premise_bundle(drop_losers=True))
    assert cut["status"] == "incomplete_prices" and "不下结论" in cut["verdict"]
    assert cut["price_coverage"]["incomplete_periods"] > 0


def test_negated_factor_in_the_score_flips_the_wording():
    code = '''def pick(frame):
    frame = frame.sort_values("market_cap").head(12)
    rows = [{"score": -float(r.market_cap)} for r in frame.itertuples()]
'''
    found = audit_premise.from_code({"code": {"source": code}})
    assert found["factors"][0]["key"] == "size" and found["factors"][0]["negated"]
    assert audit_premise._premise_text("higher", found).startswith("市值越低")


def test_one_ic_period_renders():
    assert audit_report._t(None) == "不可计算" and audit_report._t(1.234) == "1.23"


def test_rank1_changes_are_not_target_set_changes():
    days = [f"2024-01-{d:02d}" for d in range(2, 26)]
    scores, positions = [], []
    for i, d in enumerate(days):
        order = ["A.SH", "B.SH", "C.SH", "D.SH"] if i % 2 else ["A.SH", "C.SH", "B.SH", "D.SH"]
        for r, c in enumerate(order, 1):
            scores.append({"date": d, "code": c, "rank": r, "selected": r <= 2})
        positions += [{"date": d, "code": c} for c in order[:2]]
    out = audit_signals.analyze({"scores": scores, "positions": positions, "fills": [], "orders": []},
                                {"accounting": {"status": "verified", "daily": [{"date": days[0]}]}})
    assert out["top_switches"] == 0 and out["switches"] > 0 and out["target_size"] == 2


def test_cash_short_is_a_missed_target_not_an_empty_account():
    card = audit_actions._cash_buffer_card({"cash_short_orders": 3, "cash_short_gap": 10., "cash_short_days": 3,
                                            "cash_short_examples": [{"date": "2024-01-02", "available": 1., "required": 2.}]}, {})
    assert "空仓" not in card["finding"] and "没买到" in card["finding"]


def test_rqalpha_stock_fees_from_config():
    bundle = {"code": {"source": "x = 1\n"}, "adapter": "ricequant_v1",
              "engine_config": {"mod": {"sys_transaction_cost": {"stock_commission_multiplier": .3125,
                                                                   "stock_min_commission": 5, "tax_multiplier": 2}}}}
    stock = audit_fee_model.declared(bundle)["models"]["stock"]
    assert abs(stock["commission_buy"] - .00025) < 1e-12 and abs(stock["tax_sell"] - .001) < 1e-12


def test_supplied_corporate_actions_replace_inferred_ones():
    from qwb import strategy_imports as imp
    files = [
        imp.SourceFile("trades.csv", b"date,time,code,side,quantity,price,fee\n2024-01-02,09:31:00,600000.SH,buy,1000,10,5\n"),
        imp.SourceFile("holdings.csv", b"date,code,quantity,price\n2024-01-02,600000.SH,1000,10\n2024-01-03,600000.SH,1000,10\n"),
        imp.SourceFile("account.csv", b"date,equity,cash\n2024-01-02,10000,0\n2024-01-03,10300,300\n"),
        imp.SourceFile("actions.csv", b"date,code,kind,quantity_change,cash\n2024-01-03,600000.SH,dividend,0,300\n"),
        imp.SourceFile("code.py", b"def initialize(context):\n    pass\n"),
    ]
    bundle = imp.normalize(files, opening_cash=10005)
    assert [a["source"] for a in bundle["actions"]] == ["supplied"]


def _signal_case(selected, held_code):
    days = [f"2024-01-{d:02d}" for d in range(2, 28)]
    scores, positions = [], []
    for d in days:
        for r, c in enumerate(("A.SH", "B.SH", "C.SH"), 1):
            row = {"date": d, "code": c, "rank": r}
            if selected is not None:
                row["selected"] = c in selected
            scores.append(row)
        if held_code:
            positions.append({"date": d, "code": held_code})
    return audit_signals.analyze({"scores": scores, "positions": positions, "fills": [], "orders": []},
                                 {"accounting": {"status": "verified", "daily": [{"date": days[0]}]}})


def test_n01_explicit_empty_selection_is_not_a_missed_buy():
    out = _signal_case(selected=set(), held_code=None)
    assert out["empty_target_days"] == out["days"] and out["days"] - out["followed_days"] - out["retained_days"] == 0
    held = _signal_case(selected=set(), held_code="B.SH")
    assert held["empty_target_but_held_days"] == held["days"]


def test_n02_explicit_target_is_never_excused_as_retention():
    out = _signal_case(selected={"A.SH"}, held_code="B.SH")
    assert out["retained_days"] == 0 and out["days"] - out["followed_days"] == out["days"]
    ranked_only = _signal_case(selected=None, held_code="B.SH")
    assert ranked_only["retained_days"] == ranked_only["days"]


def test_n05_a_retry_that_filled_nothing_is_not_a_top_up():
    from qwb import audit_completion
    days = [f"2024-02-{d:02d}" for d in range(1, 12)]
    snaps = [{"date": d, "equity": 100., "cash": 30.} for d in days]
    positions = [{"date": d, "code": "A.SH", "quantity": 100} for d in days]
    orders = [{"date": days[0], "code": "A.SH", "side": "buy", "quantity": 300, "filled": 100, "reason": ""}]
    orders += [{"date": d, "code": "A.SH", "side": "buy", "quantity": 200, "filled": 0, "reason": "无成交量"} for d in days[1:7]]
    out = audit_completion.analyze({"orders": orders, "snapshots": snaps, "positions": positions,
                                    "fills": [{"date": days[0], "code": "A.SH"}]}, {"status": "verified"})
    assert out["status"] == "found" and out["worst"]["days_without_order"] == 10 and out["worst"]["retries_unfilled"] == 6


def test_n04_target_weights_against_holdings_and_lot_size():
    from qwb import audit_allocation
    days = [f"2024-03-{d:02d}" for d in range(1, 21)]
    snaps = [{"date": d, "equity": 10000., "cash": 4000.} for d in days]
    positions = [{"date": d, "code": "511010.SH", "quantity": 600, "price": 10., "market_value": 6000.} for d in days]
    targets = [{"date": days[0], "code": "511010.SH", "target_weight": .7}]
    out = audit_allocation.analyze({"target_weights": targets, "snapshots": snaps, "positions": positions}, {"status": "verified"})
    row = out["names"][0]
    assert abs(row["actual"] - .6) < 1e-9 and row["over_days"] == 0           # 0.6 vs 0.7 is within 15% of target
    assert abs(row["lot_share"] - .1) < 1e-9


def test_fulfilment_splits_pnl_by_prior_target_and_measures_overnight():
    from qwb import audit_fulfilment
    days = ["2024-04-01", "2024-04-02", "2024-04-03"]
    snaps = [{"date": d, "equity": 1000., "cash": 0.} for d in days]
    positions = [{"date": d, "code": "B.SH", "quantity": 100, "price": 10., "market_value": 1000.} for d in days]
    scores = [{"date": d, "code": c, "score": 1., "selected": c == "A.SH"} for d in days for c in ("A.SH", "B.SH")]
    contributions = [{"date": "2024-04-02", "code": "B.SH", "net_pnl": 50.}, {"date": "2024-04-03", "code": "B.SH", "net_pnl": 30.}]
    minutes = [{"date": d, "code": "B.SH", "time": "09:31", "open": 10.2, "close": 10.2} for d in days[1:]]
    out = audit_fulfilment.analyze({"snapshots": snaps, "positions": positions, "scores": scores, "minute_prices": minutes},
                                   {"status": "verified", "contributions": contributions})
    assert out["by_target"]["outside"] == 80. and out["by_target"]["inside"] == 0. and out["status"] == "flagged"
    assert abs(out["overnight"]["gap_pnl"] - 40.) < 1e-9          # 2 nights x 100 shares x (10.2 - 10.0)


def test_capital_floor_proves_a_small_account_cannot_hold_the_target():
    from qwb import audit_capital
    assert abs(audit_capital.floor([.7, .3], [120., 4.], 10_000.) - 1.4) < 1e-6 or \
        audit_capital.floor([.7, .3], [120., 4.], 10_000.) > 1.     # one lot of the 120-yuan fund costs 12,000
    assert audit_capital.floor([.7, .3], [120., 4.], 1_000_000.) < .02


def test_rank1_test_separates_from_ic():
    out = audit_premise.analyze(premise_bundle())
    assert out["top_excess_mean"] is not None and out["top_excess_t"] is not None


def test_intraday_day_that_ends_flat_is_not_idle():
    from qwb import audit_activity
    days = [f"2024-05-{d:02d}" for d in range(1, 31)]
    snaps = [{"date": d, "cash": 100., "equity": 100.} for d in days]
    fills = [{"date": d} for d in days]                              # traded every day, flat at every close
    out = audit_activity.analyze({"snapshots": snaps, "positions": [], "fills": fills, "orders": []}, {"status": "verified"})
    assert out["idle"]["longest"] == 0
