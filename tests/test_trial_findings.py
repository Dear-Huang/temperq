"""Regressions from an independent trial (strategy 91, a T+0 ETF intraday strategy on local RiceQuant)."""
import io
import json
import zipfile

import pytest

from qwb import audit_premise, audit_report, audit_slippage, audit_timing, strategy_audit as audit, strategy_imports as imp

CODE = b"def initialize(context):\n    pass\n"


def t0_bundle(code="513030.SH"):
    """Buy in the morning, sell part in the afternoon of the same day, from an empty opening."""
    files = [
        imp.SourceFile("trades.csv", (f"date,time,code,side,quantity,price,fee\n"
                                      f"2024-01-02,09:31:00,{code},buy,1000,1.00,5\n"
                                      f"2024-01-02,14:59:00,{code},sell,400,1.01,5\n").encode()),
        imp.SourceFile("holdings.csv", f"date,code,quantity,price\n2024-01-02,{code},600,1.01\n".encode()),
        imp.SourceFile("account.csv", b"date,equity,cash\n2024-01-02,1596.0,990.0\n"),
        imp.SourceFile("code.py", CODE),
    ]
    return imp.normalize(files, opening_cash=1596.0)


def test_tq01_same_day_resale_of_an_etf_is_not_an_inventory_error():
    account = audit.accounting(t0_bundle())
    assert account["status"] == "verified"
    assert not [e for e in account["exceptions"] if e["key"] == "prior_inventory_for_sales"]
    assert account["same_day_resales"][0]["asset"] == "fund"


def test_tq01_same_day_resale_of_a_stock_still_breaks_t_plus_one():
    account = audit.accounting(t0_bundle("600000.SH"))
    assert [e for e in account["exceptions"] if e["key"] == "prior_inventory_for_sales"]


def test_tq02_engine_config_json_supplies_fees_and_slippage():
    config = {"mod": {"sys_simulation": {"slippage_model": "strategy_lab.bridge.FixedSpreadSlippage", "slippage": .001},
                      "sys_transaction_cost": {"etf_commission": {"default": {"commission_rate": 5e-05, "min_commission": .1}}}}}
    from qwb.audit_fee_model import declared
    bundle = {"code": {"source": "x = 1\n"}, "engine_config": config, "adapter": "ricequant_v1"}
    assert declared(bundle)["models"]["fund"]["commission_buy"] == 5e-05
    slip = audit_slippage.declared(bundle)
    assert slip["settings"][0]["model"] == "FixedSpreadSlippage" and "运行配置" in audit_slippage.describe(slip)
    assert "没有读到" in audit_slippage.describe(None)


def test_tq04_fills_only_move_later_and_never_past_the_close():
    assert audit_timing.shift("14:59", 1) == "15:00" and audit_timing.shift("14:59", 5) is None
    assert audit_timing.shift("11:29", 5) == "13:04"


def test_tq06_default_cap_is_the_same_everywhere():
    from qwb import audit_input
    params = {p["key"]: p for p in audit_input.parameter_candidates({"code": {"candidates": []}})}
    assert params["max_weight"]["value"] == 1.


def test_tq07_single_name_strategy_is_not_tested_for_a_cross_sectional_premise():
    scores = [{"date": f"2024-01-{d:02d}", "code": "513030.SH", "score": float(d), "selected": True} for d in range(2, 28)]
    out = audit_premise.analyze({"scores": scores, "prices": [], "code": {"source": "x = 1\n"}})
    assert out["status"] == "not_applicable" and "不适用" in out["verdict"]


def test_tq09_native_summary_xlsx_is_read():
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             '<row r="1"><c r="A1" t="inlineStr"><is><t>收益率</t></is></c><c r="B1" t="inlineStr"><is><t>0.5</t></is></c></row>'
             '</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    assert imp.xlsx_pairs(buf.getvalue()) == [("收益率", "0.5")]


def test_tq08_and_tq11_messages_name_the_file_and_the_sample_zip_imports():
    from qwb import strategy_audit_ui as view
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in view.sample_files():
            z.writestr(f.name, f.data)
    bundle = imp.normalize([imp.SourceFile("synthetic-strategy.zip", buf.getvalue())], opening_cash=10000)
    assert bundle["fills"]
    with pytest.raises(ValueError, match="empty.csv"):
        imp.normalize([imp.SourceFile("empty.csv", b""), imp.SourceFile("c.py", CODE)])


def test_batch_p02_equity_etf_is_t_plus_one_and_cross_border_is_t_zero():
    from qwb.securities import same_day_resale
    assert same_day_resale("510300.SH") is False and same_day_resale("513030.SH") is True
    assert same_day_resale("518880.SH") is True and same_day_resale("600000.SH") is False
    account = audit.accounting(t0_bundle("510300.SH"))
    assert [e for e in account["exceptions"] if e["key"] == "prior_inventory_for_sales"]


def test_batch_p06_p07_failed_exits_and_idle_stretches():
    from qwb import audit_activity
    days = [f"2024-01-{d:02d}" for d in range(2, 30)]
    snaps = [{"date": d, "cash": 100. if i < 25 else 10., "equity": 100.} for i, d in enumerate(days)]
    positions = [{"date": d, "code": "A.SH", "quantity": 100} for d in days[25:]]
    orders = [{"date": d, "code": "A.SH", "side": "sell", "quantity": 100, "filled": 0, "reason": "无成交量"} for d in days[25:]]
    out = audit_activity.analyze({"snapshots": snaps, "positions": positions, "orders": orders}, {"status": "verified"})
    assert out["exits"]["failed"] == 3 and out["exits"]["longest_run"] == 3
    assert out["idle"]["longest"] == 25 and "低回撤可能来自" in out["idle"]["verdict"]


def test_batch_p05_all_profitable_groups_are_not_called_losses():
    from qwb import audit_attribution
    lines = audit_attribution.summary(100., 110., 10., None, {"stocks": 1, "winners": 1, "losers": 0, "top": [], "noun": "标的"},
                                      [{"label": "超过10自然日", "matches": 4, "after_tick": 18471.}], None)
    assert not any("亏得最多" in l for l in lines) and any("贡献最少" in l for l in lines)


def test_long_idle_keeps_the_first_screen_from_saying_pass():
    from datetime import date, timedelta
    days = [(date(2024, 1, 2) + timedelta(days=i)).isoformat() for i in range(40)]
    bundle = {"fills": [{"date": days[0]}], "snapshots": [{"date": d, "equity": 100., "cash": 100.} for d in days],
              "platform_summary": {"累计收益率": "0", "最大回撤": "0"}}
    result = {"modules": {"accounting": {"status": "verified"},
                          "activity": {"idle": {"longest": 40, "verdict": "长期空仓"}}}}
    first = audit_report.answers(bundle, result, audit_report.performance(bundle), None, [])[0]
    assert first["tone"] != "pass" and "长期空仓" in first["notes"]
