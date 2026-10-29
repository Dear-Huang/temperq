"""Regressions from the multi-strategy end-to-end run on real RiceQuant, JoinQuant and BigQuant exports.

Each test pins one failure seen on real files, rebuilt here from synthetic text only.
"""
from qwb import audit_attribution, audit_premise, audit_report, strategy_imports as imp


RQ_LOG = """===== console.log =====
D:\\env\\client.py:263: UserWarning: Your account will be expired after  29 days.
  warnings.warn("Your account will be expired")
[2026-08-06 09:31:00.000000] WARN: user_system_log: 订单被撤销：513100.XSHG 当前无成交量
===== engine.log =====
[2026-08-06 09:31:00.000000] WARN: user_system_log: 订单被撤销：513100.XSHG 当前无成交量
[2026-08-07 09:31:00.000000] WARN: user_system_log: 订单创建失败: 可用资金不足。
===== strategy.log =====
INFO:strategy.s22:2025-10-28 09:31:00 CALLBACK trade
"""


def test_ricequant_joined_log_is_a_log_not_a_second_code_file():
    # Was: run log read as strategy code -> "每种材料只接收一个文件" or "代码不能超过 200KB".
    assert imp.is_platform_log(RQ_LOG)
    rows = imp.parse_log(RQ_LOG)
    assert [r["级别"] for r in rows] == ["WARN", "WARN"]      # console/engine duplicate kept once
    assert rows[0]["时间"] == "2026-08-06 09:31:00"
    assert not imp.is_platform_log("import numpy as np\n\ndef initialize(context):\n    pass\n")


def test_ricequant_summary_table_is_kept_for_comparison():
    from qwb.strategy_import_formats import other_platform
    summary = "指标,数值\n策略名,x\n累计收益率,0.1\n最大回撤,0.05\n夏普比率,0.9\n".encode("utf-8-sig")
    files = [imp.SourceFile("回测收益.csv", summary)]
    result = other_platform(files, adapter="ricequant", name="x")
    assert result["platform_summary"]["累计收益率"] == "0.1"
    assert result["sources"][0]["kind"] == "platform_summary"


def snapshots(*equity):
    return [{"date": f"2025-01-{i + 2:02d}", "equity": e} for i, e in enumerate(equity)]


def test_returns_start_from_capital_before_the_first_trade():
    # Was: measured from the first close, dropping day one's loss (-1.42% shown vs platform -3.31%).
    bundle = {"snapshots": snapshots(98000., 99000., 96690.),
              "platform_returns": [{"date": "2025-01-02", "strategy_pct": -2., "benchmark_pct": 1.},
                                   {"date": "2025-01-04", "strategy_pct": -3.31, "benchmark_pct": 2.}]}
    perf = audit_report.performance(bundle)
    assert abs(perf["start_equity"] - 100000.) < 1e-6
    assert abs(perf["total"] - (-.0331)) < 1e-4
    assert abs(perf["benchmark_total"] - .02) < 1e-9          # from NAV 1, not from day one's close
    assert perf["basis"] == "平台净值反推的初始资金"
    plain = audit_report.performance({"snapshots": snapshots(100., 110.)})
    assert plain["basis"].startswith("首日收盘权益")


def test_platform_figures_are_compared_and_sharpe_gap_explained():
    bundle = {"snapshots": snapshots(100., 103., 102.),
              "platform_summary": {"累计收益率": "0.02", "最大回撤": "0.009709", "夏普比率": "0.10"}}
    rows = {r["label"]: r for r in audit_report.platform_check(bundle, audit_report.performance(bundle))}
    assert rows["区间收益"]["agree"] and rows["最大回撤"]["agree"]
    assert not rows["夏普比率"]["agree"] and "无风险" in rows["夏普比率"]["note"]


def test_rounded_zero_has_no_minus_sign():
    assert audit_report._yuan(-.3) == "¥0" and audit_attribution._y(-.3) == "¥0"
    assert audit_report._yuan(-1394.2) == "¥-1,394"


def premise_of(code):
    found = audit_premise.from_code({"code": {"source": code}})
    return found and audit_premise._premise_text(found["direction"] or "higher", found)


def test_etf_rotation_score_is_trend_momentum_with_its_window():
    code = '''g.m_days = 25
def MOM(etf):
    slope, intercept = np.polyfit(x, y, 1)
    r_squared = 1 - ss_res / ss_tot
    return annualized_returns * r_squared
def get_rank(pool):
    df = df.sort_values(by='score', ascending=False)
'''
    assert premise_of(code) == "过去25日趋势动量越高的标的，到下一次调仓前涨得更多"


def test_listing_age_filter_is_not_a_factor_window():
    # Was: timedelta(days=250) in a new-listing filter became "过去250日估值".
    code = '''def filter_new(stocks):
    return [s for s in stocks if today - start < datetime.timedelta(days=250)]
def pick():
    q = query(valuation.code).order_by(valuation.circulating_market_cap.asc())
'''
    assert premise_of(code) == "市值越低的标的，到下一次调仓前涨得更多"


def test_price_ratio_momentum_and_dict_config_sort_and_model_scores():
    momentum = '''N=21
R = (close_data1[-2] + close_data1[-1] - sum(close_data1[-23:-20])) / sum(close_data1[-23:-20])
SEC_LIST.sort(reverse=True)
'''
    assert premise_of(momentum) == "过去21日动量越高的标的，到下一次调仓前涨得更多"
    config = '''{'factor': {'type': 'fundamental', 'name': 'market_cap'}, 'ascending': False, 'weight': 1},'''
    assert audit_premise.from_code({"code": {"source": "rules = [\n" + config + "\n]\n"}})["direction"] == "higher"
    model = '''ranker_prediction = context.data["predictions"]
ranker_prediction = ranker_prediction.sort_values('score', ascending=False)
'''
    assert premise_of(model) == "模型预测分数越高的标的，到下一次调仓前涨得更多"
