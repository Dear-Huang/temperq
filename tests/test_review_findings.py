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
