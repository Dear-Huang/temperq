"""Fixed samples of real platform exports (headers and quirks copied from user-provided files)."""
from qwb import strategy_imports as imports

# JoinQuant 交易详情 (GBK): sells carry negative quantities; cancelled orders fill 0 shares.
JQ_TRADES = (
    "日期,委托时间,品种,标的,交易类型,下单类型,成交数量,成交价,成交额,委托数量,委托价格,平仓盈亏,手续费,状态,最后更新时间\n"
    "2008-11-10,14:40:00,股票,路翔股份(002192.XSHE),买,市价单,2600股,7.49,19474,2600股,-,0,5.84,全部成交,2008-11-10 14:40:00\n"
    "2008-11-13,14:40:00,股票,路翔股份(002192.XSHE),卖,市价单,-2600股,7.80,-20280,-2600股,-,806,25.36,全部成交,2008-11-13 14:40:00\n"
    "2008-11-14,14:40:00,股票,龙生股份(002625.XSHE),卖,市价单,-0股,--,0,-16950股,-,0,0,已撤单,2008-11-14 14:40:00\n"
).encode("gbk")
JQ_POSITIONS = (
    "日期,品种,标的,多空,数量,可用数量,收盘价/结算价,市值/价值,盈亏/逐笔浮盈,开仓均价,持仓均价（期货）,保证金,当日盈亏,今手数,盈亏占比,仓位占比\n"
    "2008-11-10,股票,路翔股份(002192.XSHE),多,2600股,0股,7.35,19110,-364,7.49,-,0,-364,2600股,-1.87%,99711.62,19.17%\n"
    "2008-11-10,,Cash,,,,,80601.62,,,,,,,,0\n"
).encode("gbk")

# RiceQuant: UTF-8 with BOM, a space after every comma, timestamps with 15:30:00.
RQ_POSITIONS = (
    "﻿时间,合约代码,合约名称,最新价,多/空,仓位,开仓均价,市值,累计盈亏,保证金,费用\n"
    "2017-01-03 15:30:00, 601398.XSHG, 工商银行, 4.43, --, 22300, 4.43, 98789.0, 0, --, 79.0312\n"
    "2017-01-04 15:30:00, 601398.XSHG, 工商银行, 4.45, --, 22300, 4.43, 99235.0, 446.0, --, 0\n"
).encode("utf-8")
RQ_ACCOUNT = (
    "﻿日期,账户类型,可用资金,总权益,市值,保证金,费用\n"
    "2017-01-03 15:30:00, 股票, 901131.97, 999920.97, 98789.0, --, 79.0312\n"
    "2017-01-04 15:30:00, 股票, 901131.97, 1000366.97, 99235.0, --, 0\n"
).encode("utf-8")
RQ_TRADES = (
    "\ufeff时间,合约代码,合约名称,买/卖,开/平,成交量,成交价,费用\n"
    "2017-01-03 15:00:00, 601398.XSHG, 工商银行, 买, 开, 22300, 4.43, 79.0312\n"
    "2017-01-05 15:00:00, 601398.XSHG, 工商银行, 卖, 平, 22300, 4.50, 130.36\n"
).encode("utf-8")
CODE = b"def initialize(context):\n    set_order_cost(OrderCost(open_commission=0.0003, min_commission=5), type='stock')\n"


def files(*pairs):
    return [imports.SourceFile(name, data) for name, data in pairs]


def test_joinquant_transactions_are_real_fills_with_direction_from_trade_type():
    bundle = imports.normalize(files(("transaction.csv", JQ_TRADES), ("position.csv", JQ_POSITIONS), ("代码.txt", CODE)),
                               adapter="auto", name="聚宽")
    assert bundle["adapter"] == "joinquant_v1"
    assert not bundle["quality"]["fills_inferred"]
    assert [(f["side"], f["quantity"]) for f in bundle["fills"]] == [("buy", 2600), ("sell", 2600)]  # cancelled row dropped
    assert bundle["fills"][1]["fee"] == 25.36 and bundle["fills"][0]["code"] == "002192.SZ"


def test_ricequant_holdings_and_account_import_and_infer_trades():
    bundle = imports.normalize(files(("持仓详情.csv", RQ_POSITIONS), ("账户详情.csv", RQ_ACCOUNT), ("代码.txt", CODE)),
                               adapter="auto", name="米筐")
    assert bundle["adapter"] == "ricequant_v1"
    assert bundle["positions"][0]["code"] == "601398.SH" and bundle["positions"][0]["quantity"] == 22300
    assert [s["equity"] for s in bundle["snapshots"]] == [999920.97, 1000366.97]
    assert bundle["snapshots"][0]["reported_day_fee"] == 79.0312
    assert bundle["quality"]["fills_inferred"]


def test_platforms_cannot_be_mixed_in_one_backtest():
    import pytest
    with pytest.raises(ValueError, match="不同平台"):
        imports.normalize(files(("transaction.csv", JQ_TRADES), ("持仓详情.csv", RQ_POSITIONS), ("代码.txt", CODE)),
                          adapter="auto", name="混合")


def test_ricequant_trade_details_are_real_fills():
    bundle = imports.normalize(files(("交易详情.csv", RQ_TRADES), ("持仓详情.csv", RQ_POSITIONS), ("账户详情.csv", RQ_ACCOUNT),
                                     ("代码.txt", CODE)), adapter="auto", name="米筐")
    assert bundle["adapter"] == "ricequant_v1" and not bundle["quality"]["fills_inferred"]
    assert [(f["side"], f["quantity"], f["price"], f["fee"]) for f in bundle["fills"]] == [
        ("buy", 22300, 4.43, 79.0312), ("sell", 22300, 4.5, 130.36)]
    assert bundle["fills"][0]["code"] == "601398.SH" and bundle["fills"][0]["time"] == "15:00:00"
