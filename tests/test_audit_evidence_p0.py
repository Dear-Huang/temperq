"""AUDIT_EVIDENCE_SPEC P0: rule registry, four fee groups, grouped unheld targets, churn inputs, rerun verdicts."""
import pytest

from qwb import audit_actions, audit_fee_model, audit_rules, audit_verification


def test_registry_only_thresholds_adjust_and_reports_store_values():
    assert audit_rules.resolve({"T-HOLD-SHORT": 10})["T-HOLD-SHORT"] == 10
    with pytest.raises(ValueError, match="不能调整"):
        audit_rules.resolve({"F-TICK-STOCK": .02})
    with pytest.raises(ValueError, match="之间"):
        audit_rules.resolve({"T-BUFFER-RANK": 1})
    snap = audit_rules.snapshot(audit_rules.defaults())
    assert snap["version"] == audit_rules.VERSION and "T-CASH-BUFFER" in snap["thresholds"]
    assert all(r.range is not None for r in audit_rules.RULES.values() if r.kind == "threshold")


CONFIG = ("__config__ = {'mod': {'sys_transaction_cost': {'stock_commission_multiplier': 0.25, 'stock_min_commission': 5,"
          " 'etf_commission': {'default': {'commission_rate': 0.0002, 'min_commission': 5}}}}}\n")


def bundle(fills, code=CONFIG):
    return {"adapter": "ricequant_v1", "code": {"source": code}, "fills": fills, "sources": []}


def fill(side, notional, fee, code="510300.SH", day="2024-01-02"):
    return {"date": day, "code": code, "side": side, "notional": notional, "fee": fee, "quantity": 100, "price": notional / 100}


def test_declared_model_converts_the_rqalpha_stock_multiplier():
    # RQAlpha 6.x: base stock commission 0.08% and stamp tax 0.05%, each scaled by its multiplier.
    d = audit_fee_model.declared(bundle([fill("buy", 100000., 20.), fill("buy", 100000., 20., "600000.SH")]))
    assert d["models"]["fund"]["commission_buy"] == .0002
    assert abs(d["models"]["stock"]["commission_buy"] - .0008 * .25) < 1e-12      # CONFIG multiplier 0.25
    assert "RQAlpha 6.x" in d["models"]["stock"]["source"]


def test_self_check_is_a_fact_independent_of_audit_rate():
    b = bundle([fill("buy", 100000., 20.), fill("sell", 101000., 20.2), fill("buy", 10000., 5.)])
    check = audit_fee_model.self_check(b, audit_fee_model.declared(b))
    assert check["status"] == "consistent" and check["priced_fills"] == 3
    b["fills"][0]["fee"] = 30.
    assert audit_fee_model.self_check(b, audit_fee_model.declared(b))["status"] == "inconsistent"


def test_joinquant_fund_scope_and_stamp_tax_check():
    code = ("set_order_cost(OrderCost(open_tax=0, close_tax=0.001, open_commission=0.0003, close_commission=0.0003,"
            " min_commission=5), type='stock')\n")
    b = {"adapter": "joinquant_v1", "code": {"source": code}, "sources": [],
         "fills": [fill("sell", 100000., 130., "600000.SH", "2024-03-01")]}
    stamp = audit_fee_model.stamp_check(b, audit_fee_model.declared(b))
    assert stamp["declared_bp"] == pytest.approx(10.) and stamp["rule_bp"] == [5.] and stamp["mismatched_sells"] == 1


def fee_model(confirmed, broker_bp=1., gap=1330.):
    return {"declared": {"status": "complete", "models": {"fund": {"source": "配置", "line": 17}}, "missing": {}},
            "self_check": {"status": "consistent", "priced_fills": 91}, "stamp_check": None,
            "charged": {"reported": 2678.}, "broker": {"confirmed": confirmed, "commission_bp": broker_bp, "minimum_commission": 5.},
            "broker_check": {"modeled": 1348., "reported": 2678., "gap": gap} if confirmed else None,
            "scenarios": [{"commission_bp": 1., "fee": 1348.}, {"commission_bp": 2., "fee": 2678.}, {"commission_bp": 3., "fee": 4010.}]}


def test_fee_card_gives_no_direction_until_broker_rate_is_confirmed():
    card = audit_actions.cards({"modules": {"fee_model": fee_model(False)}})[0]
    assert card["evidence"] == "E3c" and "偏" not in card["headline"] and "1bp 约 ¥1,348" in card["finding"]
    card = audit_actions.cards({"modules": {"fee_model": fee_model(True)}})[0]
    assert card["evidence"] == "E1" and "高于合同口径约 ¥1,330" in card["headline"]
    assert audit_actions.cards({"modules": {"fee_model": fee_model(True, 2., 0.)}}) == []


def signals(lots=39):
    return {"switches": 46, "avoided_switches": 16, "buffer_rank": 2, "days": 725, "followed_days": 699,
            "average_hold_days": 15., "cost_per_round_trip": 114.81, "avoided_cost": 1837.03, "closed_lots": lots,
            "whipsaw_lots": 6, "whipsaw_cost": 767., "missed_reasons": {"委托未成交：无成交量": 26},
            "missed_groups": [{"code": "513100.SH", "time": "09:31:00", "reason": "委托未成交：无成交量", "days": 26}]}


def test_churn_card_states_inputs_opening_and_no_cause():
    opening = {"status": "declared_empty", "text": "空仓，现金 ¥100,000"}
    card = audit_actions.cards({"modules": {"signal_mapping": signals()}, "opening_state": opening})[0]
    assert "收益影响需重跑" in card["headline"] and card["evidence"] == "E3" and not card["conditions"]
    assert dict(card["inputs"])["已平仓笔数"] == "39" and card["formula"] == "16 × ¥114.81 = ¥1,837"
    assert "26 天全部是 513100.SH，09:31 下单" in card["finding"] and "迁移" not in card["finding"]
    assert "分钟数据" in card["finding"]
    unknown = audit_actions.cards({"modules": {"signal_mapping": signals(38)}, "opening_state": {"status": "unconfirmed"}})[0]
    assert unknown["evidence"] == "E3c" and "首日持仓成本未确认" in unknown["conditions"][0]


def test_every_card_cites_rules_and_uses_new_levels():
    card = audit_actions.cards({"modules": {"signal_mapping": signals()}})[0]
    assert {r["id"] for r in card["rules"]} >= {"T-BUFFER-RANK", "M-FIFO"}
    assert card["evidence"] in audit_rules.EVIDENCE


def restated(net, drawdown, fills):
    days = [{"date": f"2024-01-{i:02d}", "net_pnl": net / 3} for i in (1, 2, 3)]
    return {"mode": "full", "modules": {"accounting": {"daily": days, "baseline_equity": 1e5},
                                         "fee_analysis": {"fills": []}, "tick_analysis": {"fills": fills}}}


def test_rerun_verdict_is_in_sample_and_weighs_drawdown(monkeypatch):
    route = [{"date": "2024-01-01", "code": "510300.SH", "side": "buy", "quantity": 1000, "accounted_interval": True,
              "ref": None, "fee": 5., "tick_scenario_cost": 0.}]
    a, b = restated(1000., -.1, route), restated(2000., -.2, [{**route[0], "quantity": 1010}])
    real = audit_verification.restate
    monkeypatch.setattr(audit_verification, "restate", lambda r: {**real(r), "drawdown": {"realistic": -.1 if r is a else -.2,
                                                                                         "reported": 0.}})
    out = audit_verification.compare(a, b)
    assert out["state"] == "mixed" and "回撤" in out["verdict"] and "样本内" in out["verdict"] and out["evidence"] == "E4"
    assert out["same_trades"] and "费率敏感性" in out["verdict"]


def test_rerun_version_inherits_parent_cost_scenario():
    from qwb import strategy_audit_ui as ui
    from qwb import strategy_imports as imports
    trades = "date,code,side,quantity,price,fee\n2024-01-02,510300.SH,buy,1000,3.5,5\n"
    b = imports.normalize([imports.SourceFile("t.csv", trades.encode()), imports.SourceFile("s.py", b"x = 1\n")],
                          adapter="auto", name="v2")
    html = ui.overview("id", b, "tok", {"commission_bp": 2., "minimum_commission": 5., "max_weight": .5, "broker_confirmed": True})
    assert 'name="commission_bp" value="2.0"' in html and "沿用上一版报告" in html
    assert 'name="broker_confirmed" value="yes" checked' in html


def test_minimum_and_cash_buffer_cards_follow_registry_thresholds():
    fees = {"status": "measured", "minimum_premium": 300., "small_unit_count": 10}
    default = audit_actions.cards({"modules": {"fee_analysis": fees}})[0]
    lowered = audit_actions.cards({"modules": {"fee_analysis": fees},
                                   "rules": {"thresholds": {"T-MIN-COMM-MATERIAL": 200.}}})[0]
    assert default["state"] == "hold" and lowered["state"] == "review"
    sig = {**signals(), "cash_short_orders": 5, "cash_short_days": 5, "cash_short_gap": 3.,
           "cash_short_examples": [{"date": "2024-01-02", "code": "510300.SH", "available": 99.5, "required": 100.2}]}
    card = next(c for c in audit_actions.cards({"modules": {"signal_mapping": sig}, "rules": {"thresholds": {"T-CASH-BUFFER": .02}}})
                if c["id"] == "cash_buffer")
    assert "2%" in card["headline"] and card["options"][1]["label"] == "下单金额乘 0.98"


def test_rules_document_matches_registry():
    from pathlib import Path
    doc = Path(__file__).parents[1] / "docs" / "AUDIT_RULES.md"
    assert doc.read_text(encoding="utf-8") == audit_rules.document(), \
        "docs/AUDIT_RULES.md 与 audit_rules 不一致：运行 audit_rules.document() 重新生成"


def test_threshold_form_lists_only_thresholds_and_round_trips():
    from qwb import strategy_audit_ui as ui
    html = ui.threshold_fields({"T-HOLD-SHORT": 10})
    assert 'name="t:T-HOLD-SHORT" value="10"' in html and 'threshold-changed' in html
    assert "F-TICK" not in html and "F-STAMP" not in html
    assert 'name="t:T-CASH-BUFFER" value="1"' in html            # 1% shown as a percentage
    form = {"t:T-HOLD-SHORT": "10", "t:T-CASH-BUFFER": "2", "t:T-PRICE-LOW": "5"}
    assert ui.threshold_values(form) == {"T-HOLD-SHORT": 10., "T-CASH-BUFFER": .02}


def test_changed_threshold_changes_the_card_and_is_stored():
    from qwb import strategy_audit as audit, strategy_imports as imports
    trades = ("date,code,side,quantity,price,fee\n2024-01-02,600000.SH,buy,1000,10,5\n2024-01-09,600000.SH,sell,1000,10.1,10\n"
              "2024-01-10,600000.SH,buy,1000,10,5\n2024-01-12,600000.SH,sell,1000,10.2,10\n")
    holdings = "date,code,quantity,price,market_value\n" + "".join(
        f"2024-01-{d:02d},600000.SH,1000,10,10000\n" for d in (2, 3, 4, 5, 8, 10, 11))
    days = ["02", "03", "04", "05", "08", "09", "10", "11", "12"]
    account = "date,equity,cash\n" + "".join(f"2024-01-{d},100000,{90000 if d not in ('09', '12') else 100000}\n" for d in days)
    files = [imports.SourceFile(n, t.encode()) for n, t in (("t.csv", trades), ("h.csv", holdings), ("a.csv", account))]
    bundle = imports.normalize(files + [imports.SourceFile("s.py", b"x = 1\n")], adapter="auto", name="t", opening_cash=100000.)
    base = {"commission_bp": 3., "minimum_commission": 5., "max_weight": 1.}
    r = audit.analyze(bundle, {**base, "thresholds": {"T-HOLD-SHORT": 3}})
    assert r["rules"]["thresholds"]["T-HOLD-SHORT"] == 3 and r["settings"]["thresholds"] == {"T-HOLD-SHORT": 3}
    with pytest.raises(ValueError):
        audit.analyze(bundle, {**base, "thresholds": {"F-TICK-STOCK": .02}})


def test_rerun_lifts_card_to_e4_and_never_says_verified():
    card = audit_actions.cards({"modules": {"signal_mapping": signals()}})
    out = audit_actions.attach_reruns(card, {"rank_churn": [{"state": "worse", "verdict": "没有好于原版（同区间复跑，样本内）",
                                                             "version": 2, "id": "v2", "parent": "v1"}]})[0]
    assert out["evidence"] == "E4" and out["state"] == "hold"
    assert out["headline"].startswith("同区间复跑更差（样本内）") and "已验证" not in out["headline"]
    assert out["reruns"][0]["label"] == "更差"
    untouched = audit_actions.attach_reruns(audit_actions.cards({"modules": {"signal_mapping": signals()}}), {})[0]
    assert untouched["evidence"] in {"E3", "E3c"} and "reruns" not in untouched

