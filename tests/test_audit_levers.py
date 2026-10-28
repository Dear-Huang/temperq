from qwb import audit_levers as levers

BIGQUANT = '''from bigmodule import M
def m3_initialize_bigquant_run(context):
    context.set_commission(PerOrder(buy_cost=0.0003, sell_cost=0.0013, min_cost=5))
    stock_count = 2
    context.options["hold_days"] = 2
def m3_handle_data_bigquant_run(context, data):
    for instrument in buy_instruments:
        cash = 1
        if cash > 0:
            context.order_value(instrument, cash)
m = M.bigtrader.v34(rebalance_period_days="""5""", expr_filters="""-- none""")
'''

JOINQUANT = """def initialize(context):
    set_order_cost(OrderCost(open_tax=0, close_tax=0.001, open_commission=0.0003, close_commission=0.0003, min_commission=5),type='fund')
    g.stock_num = 10
    run_weekly(weekly_adjustment, weekday=1, time='9:30', reference_security='000300.XSHG')
def prepare(context):
    initial_list = filter_st_stock(initial_list)
def buy(context):
    value = context.portfolio.cash / (target_num - position_count)
"""

RICEQUANT = """REBALANCE_INTERVAL = 5
MAX_HOLDING_NUM = 10
FILTERS = [
    {'operator': 'less_than', 'lhs': {'type': 'fundamental', 'name': 'pe_ratio'}, 'rhs': 15},
]
def rebalance_first_part(context, bar_dict):
    context.target_position_value = context.portfolio.total_value * 0.99 / len(context.buy_list)
"""


def bundle(code, adapter, start="2024-01-02", end="2024-06-28", buys=()):
    days = [{"date": start}, {"date": end}]
    return {"adapter": adapter, "code": {"source": code}, "snapshots": days, "positions": [], "fills": list(buys)}


def test_locates_the_knobs_on_each_platform():
    bq = levers.locate(bundle(BIGQUANT, "bigquant_v1"))
    assert [(x["name"], x["value"]) for x in bq["rebalance"]] == [("hold_days", 2), ("rebalance_period_days", 5)]
    assert bq["holding_count"][0]["value"] == 2 and bq["fee_setting"][0]["call"] == "PerOrder"
    jq = levers.locate(bundle(JOINQUANT, "joinquant_v1"))
    assert jq["rebalance"][0]["name"] == "run_weekly" and jq["fee_setting"][0]["scope"] == "fund"
    assert jq["price_filter"][0]["target"] == "initial_list" and jq["order_size"][0]["name"] == "value"
    rq = levers.locate(bundle(RICEQUANT, "ricequant_v1"))
    assert rq["rebalance"][0]["value"] == 5 and rq["price_filter"][0]["name"] == "FILTERS"
    assert rq["order_size"][0]["name"] == "target_position_value"


def test_holding_edits_lengthen_the_period():
    edits = levers.holding(levers.locate(bundle(BIGQUANT, "bigquant_v1")), "bigquant_v1")
    assert [e["change"] for e in edits] == ['context.options["hold_days"] = 5', 'm = M.bigtrader.v34(rebalance_period_days="""10""", expr_filters="""-- none""")']
    jq = levers.holding(levers.locate(bundle(JOINQUANT, "joinquant_v1")), "joinquant_v1")
    assert jq[0]["change"].startswith("run_monthly(weekly_adjustment")


def test_price_floor_uses_the_users_variable_name():
    edit = levers.price_floor(levers.locate(bundle(JOINQUANT, "joinquant_v1")), "joinquant_v1")[0]
    assert "initial_list = [s for s in initial_list" in edit["change"] and edit["line"] == 6


def test_fee_edits():
    # BigQuant: whole backtest after the stamp-tax cut, sell cost still carries 10bp.
    edit = levers.fees(levers.locate(bundle(BIGQUANT, "bigquant_v1")), bundle(BIGQUANT, "bigquant_v1"), True, 3)[0]
    assert "sell_cost=0.0008" in edit["change"]
    # JoinQuant: type='fund' does not reach stock trades.
    b = bundle(JOINQUANT, "joinquant_v1", start="2023-01-03", end="2025-06-30")
    edit = levers.fees(levers.locate(b), b, True, 3)[0]
    assert "type='stock'" in edit["change"] and "close_tax=0.0005" in edit["change"] and "偏乐观" in edit["note"]
    # RiceQuant: no fee code; buys show the platform default commission is too high.
    buys = [{"side": "buy", "notional": 10000., "fee": 10.6}]
    b = bundle(RICEQUANT, "ricequant_v1", start="2017-01-03", end="2022-03-04", buys=buys)
    edit = levers.fees(levers.locate(b), b, True, 3)[0]
    assert edit["line"] is None and "佣金率" in edit["change"] and "10.6bp" in edit["note"]
    # Nothing to change when the backtest did not overcharge.
    assert levers.fees(levers.locate(b), b, False, 3) == []


def test_unparsable_code_locates_nothing():
    assert levers.locate(bundle("def broken(:", "joinquant_v1")) == {}
