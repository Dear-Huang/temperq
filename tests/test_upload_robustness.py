"""Uploads as users actually produce them: re-saved by Excel, pasted from forums, mixed instruments."""
import pytest

from qwb import code_parse, securities, strategy_imports as imports


@pytest.mark.parametrize("value", ["2023-01-03", "2023/1/3", "2023-1-3", "2023.1.3", "2023/1/3 15:00",
                                   "2023-01-03 15:30:00", "2023-01-03 16:00:00.000"])
def test_dates_resaved_by_excel_are_read(value):
    assert imports.date_value(value, "日期") == "2023-01-03"


@pytest.mark.parametrize("value", ["2023/13/1", "03/01/2023", "abc", ""])
def test_ambiguous_or_invalid_dates_are_refused_with_the_value(value):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        imports.date_value(value, "日期")


def test_forum_header_is_skipped_and_line_numbers_kept():
    source = "该策略由聚宽用户分享，仅供学习交流使用。\n原文网址：https://www.joinquant.com/post/1\n\ng.stock_num = 10\n"
    tree, skipped = code_parse.parse(source)
    assert skipped == 2 and tree.body[0].lineno == 4


@pytest.mark.parametrize("source", ["invalid python )", "x = (\ny = 2", "", "   "])
def test_real_syntax_errors_are_not_hidden(source):
    assert code_parse.parse(source) == (None, 0)


def test_python2_code_is_named_in_the_note():
    info = imports.inspect_code("def initialize(context):\n    print 'hi'\n")
    assert info["status"] == "unparsed" and "Python 2" in info["note"]


@pytest.mark.parametrize("raw, code, kind, tick", [
    ("510300.XSHG", "510300.SH", "fund", .001), ("159915.XSHE", "159915.SZ", "fund", .001),
    ("511880.XSHG", "511880.SH", "fund", .001), ("113050.XSHG", "113050.SH", "bond", .001),
    ("128035.XSHE", "128035.SZ", "bond", .001), ("600000.XSHG", "600000.SH", "stock", .01),
    ("688981.XSHG", "688981.SH", "stock", .01), ("sh.600000", "600000.SH", "stock", .01)])
def test_exchange_traded_instruments_keep_their_own_rules(raw, code, kind, tick):
    assert securities.security_code(raw) == code
    assert securities.asset_class(code) == kind and securities.tick_yuan(code) == tick
    assert securities.pays_stamp_tax(code) == (kind == "stock")


@pytest.mark.parametrize("raw", ["IF2106.CCFX", "RB2105.XSGE", "830799.BJ"])
def test_futures_and_beijing_are_refused_plainly(raw):
    with pytest.raises(ValueError, match="期货|北交所"):
        securities.security_code(raw)


def test_fund_fills_pay_no_stamp_tax_and_a_tenth_of_the_tick():
    from qwb import strategy_audit as audit
    header = "date,code,side,quantity,price,fee\n"
    trades = header + "2024-01-02,510300.SH,buy,10000,3.500,10.5\n2024-01-03,510300.SH,sell,10000,3.600,10.8\n"
    holdings = "date,code,quantity,price,market_value\n2024-01-02,510300.SH,10000,3.5,35000\n"
    account = "date,equity,cash\n2024-01-02,100000,64989.5\n2024-01-03,100989.2,100989.2\n"
    code = b"def initialize(context):\n    g.stock_num = 1\n"
    files = [imports.SourceFile(n, d.encode()) for n, d in (("trades.csv", trades), ("holdings.csv", holdings), ("account.csv", account))]
    bundle = imports.normalize(files + [imports.SourceFile("s.py", code)], adapter="auto", name="etf")
    result = audit.analyze(bundle)
    fills = result["modules"]["fee_analysis"]["fills"]
    assert all(f["modeled_stamp_tax"] == 0 for f in fills)
    tick = result["modules"]["tick_analysis"]["fills"][0]
    assert tick["tick_half_spread_bp"] == pytest.approx(10000 * .001 / (2 * 3.5))


def test_holdings_past_the_account_rows_are_reported_not_crashed():
    from qwb import audit_input, strategy_audit as audit
    trades = "date,code,side,quantity,price,fee\n2024-01-02,600000.SH,buy,100,10,5\n"
    holdings = ("date,code,quantity,price,market_value\n2024-01-02,600000.SH,100,10,1000\n"
                "2024-01-03,600000.SH,100,10,1000\n2024-01-04,600000.SH,100,10,1000\n")
    account = "date,equity,cash\n2024-01-02,10000,8995\n2024-01-03,9995,8995\n"
    files = [imports.SourceFile(n, d.encode()) for n, d in (("t.csv", trades), ("h.csv", holdings), ("a.csv", account))]
    bundle = imports.normalize(files + [imports.SourceFile("s.py", b"x = 1\n")], adapter="auto", name="cut")
    audit.analyze(bundle)
    check = next(c for c in audit_input.review(bundle)["checks"] if c["key"] == "coverage")
    assert check["status"] == "review" and "2024-01-04" in check["detail"]
