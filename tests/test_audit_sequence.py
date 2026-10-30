"""Order of events: sale proceeds used by a buy in the same minute, signals later than fills, code hints."""
from qwb import audit_sequence


def test_buy_paid_by_a_same_minute_sale_is_counted():
    bundle = {"snapshots": [{"date": "2024-01-02", "cash": 100.}, {"date": "2024-01-03", "cash": 50.}],
              "fills": [{"date": "2024-01-03", "time": "09:31:00", "code": "A.SH", "side": "sell", "quantity": 100, "price": 10., "fee": 5.},
                        {"date": "2024-01-03", "time": "09:31:00", "code": "B.SH", "side": "buy", "quantity": 100, "price": 9., "fee": 5.}],
              "orders": [{"date": "2024-01-03", "code": "A.SH", "side": "sell", "quantity": 200, "filled": 100}]}
    out = audit_sequence.funding(bundle)
    assert out["dependent"] == 1 and out["rows"][0]["from_sale"] == 805. and out["partial_sell_days"] == 1
    assert "卖单只成交了一部分" in out["verdict"]


def test_signal_later_than_fill_is_flagged():
    bundle = {"scores": [{"date": "2024-01-03", "code": "B.SH", "selected": True, "available_at": "2024-01-03 09:35:00"}],
              "fills": [{"date": "2024-01-03", "time": "09:31:00", "code": "B.SH", "side": "buy"}]}
    out = audit_sequence.signal_after_fill(bundle)
    assert out["late"] == 1 and "还不存在的信号" in out["verdict"]


def test_code_hint_for_daily_history_ending_now_at_the_open():
    code = '''def initialize(context):
    run_daily(trade, '9:30')

def trade(context):
    df = get_price('510300.XSHG', end_date=context.current_dt, frequency='daily', fields=['close'], count=5)
'''
    out = audit_sequence.code_hints({"code": {"source": code}})
    assert out["hints"] and out["hints"][0]["line"] == 5 and "需人工核" in out["verdict"]
    clean = code.replace("context.current_dt", "context.previous_date")
    assert not audit_sequence.code_hints({"code": {"source": clean}})["hints"]


def test_fields_fixed_at_the_open_are_not_hinted():
    code = """def initialize(context):
    run_daily(trade, '9:30')

def trade(context):
    df = get_price('510300.XSHG', end_date=context.current_dt, frequency='daily', fields=['open', 'pre_close'], count=1)
"""
    assert not audit_sequence.code_hints({"code": {"source": code}})["hints"]
