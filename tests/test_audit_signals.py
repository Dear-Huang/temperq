"""Daily ranking and order lifecycle: did holdings follow the signal, and why not."""
from qwb import audit_signals, strategy_imports as imports

CODE = b"def init(context):\n    pass\n"


def files(*pairs):
    return [imports.SourceFile(n, d.encode("utf-8") if isinstance(d, str) else d) for n, d in pairs]


def test_ranking_and_orders_import_from_rqalpha_exports():
    ranks = ("﻿datetime,candidates,rank,order_book_id,selected\n"
             "2024-01-02 09:31:00,510300.XSHG|159915.XSHE,1,510300.XSHG,True\n"
             "2024-01-02 09:31:00,510300.XSHG|159915.XSHE,2,159915.XSHE,False\n")
    orders = ("﻿time,event,order_id,code,side,quantity,filled_quantity,status,message\n"
              "2024-01-02 09:31:00,EVENT.ORDER_CREATION_PASS,1,510300.XSHG,SIDE.BUY,1000,0,ORDER_STATUS.ACTIVE,\n"
              "2024-01-02 09:31:00,EVENT.ORDER_UNSOLICITED_UPDATE,1,510300.XSHG,SIDE.BUY,1000,0,ORDER_STATUS.REJECTED,"
              "订单创建失败: 可用资金不足。当前资金: 3999.5，510300.XSHG 下单所需资金: 4001.2\n")
    bundle = imports.normalize(files(("每日股票排名.csv", ranks), ("委托详情.csv", orders), ("代码.txt", CODE)),
                               adapter="auto", name="rq")
    assert bundle["adapter"] == "ricequant_v1"
    assert [(s["code"], s["rank"], s["selected"]) for s in bundle["scores"]] == [("510300.SH", 1, True), ("159915.SZ", 2, False)]
    order = bundle["orders"][0]
    assert order["status"] == "rejected" and order["reason"] == "可用资金不足"
    assert order["available_cash"] == 3999.5 and order["required_cash"] == 4001.2


def account(days):
    return {"status": "verified", "daily": [{"date": d} for d in days]}


def test_buffer_counts_switches_a_top_two_rule_skips():
    days = [f"2024-01-{i:02d}" for i in range(1, 31)]
    # A and B trade first place every few days; B never leaves the top two.
    scores, positions = [], []
    for i, d in enumerate(days):
        first = "510300.SH" if (i // 3) % 2 == 0 else "159915.SZ"
        second = "159915.SZ" if first == "510300.SH" else "510300.SH"
        scores += [{"date": d, "code": first, "rank": 1, "selected": True},
                   {"date": d, "code": second, "rank": 2, "selected": False}]
        positions.append({"date": d, "code": first})
    bundle = {"scores": scores, "positions": positions, "fills": [], "orders": []}
    out = audit_signals.analyze(bundle, {"accounting": account(days), "holding_analysis": {"closed": []}})
    assert out["switches"] == 9 and out["buffered_switches"] == 0 and out["avoided_switches"] == 9
    assert out["followed_days"] == 30


def test_missed_days_carry_the_order_reason():
    days = [f"2024-02-{i:02d}" for i in range(1, 25)]
    scores = [{"date": d, "code": "510300.SH", "rank": 1, "selected": True} for d in days]
    positions = [{"date": d, "code": "510300.SH"} for d in days[1:]]   # first day not held
    orders = [{"date": days[0], "code": "510300.SH", "status": "rejected", "filled": 0, "quantity": 100,
               "reason": "可用资金不足", "available_cash": 99.5, "required_cash": 100.2}]
    out = audit_signals.analyze({"scores": scores, "positions": positions, "fills": [], "orders": orders},
                                {"accounting": account(days), "holding_analysis": {"closed": []}})
    assert out["missed_reasons"] == {"委托未成交：可用资金不足": 1}
    assert out["cash_short_orders"] == 1 and out["cash_short_days"] == 1


def test_no_ranking_or_unverified_account_gives_nothing():
    assert audit_signals.analyze({"scores": []}, {"accounting": account(["2024-01-02"])}) is None
    scores = [{"date": "2024-01-02", "code": "510300.SH", "rank": 1}]
    assert audit_signals.analyze({"scores": scores}, {"accounting": {"status": "needs_reconciliation"}}) is None
