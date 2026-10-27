import io

import pandas as pd

from qwb import csvimport, data_readiness, ui, workflow_ui
from test_ui import client, form


def test_three_year_names_do_not_mean_three_years_of_observations():
    snap = {"start": "2023-12-29", "end": "2025-01-02", "trading_days": 3, "codes": 40,
            "readiness": {"dates_by_year": {"2023": 1, "2024": 1, "2025": 1},
                          "dates_with_20_price_rows": 3, "provided_columns": []}}
    report = data_readiness.assess(snap)
    assert report["blocked"]
    assert "不等于有 3 年历史" in ' '.join(report["messages"])
    assert "最终检验期太短" in ' '.join(report["messages"])


def test_disjoint_stock_histories_do_not_supply_cross_section():
    bars = pd.DataFrame({"date": ["2023-01-03", "2024-01-02", "2025-01-02"],
                         "code": ["000001.SZ", "600000.SH", "688001.SH"],
                         "open": 10, "close": 10, "amount": 1000000, "adj_factor": 1})
    profile = data_readiness.csv_profile(bars, bars.columns)
    assert profile["dates_with_20_price_rows"] == 0
    snap = {"start": "2023-01-03", "end": "2025-01-02", "codes": 30,
            "trading_days": 3, "readiness": profile}
    assert data_readiness.assess(snap)["blocked"]


def test_compact_readiness_keeps_blockers_open_and_missing_inputs_in_summary():
    report = data_readiness.assess({'start': '2023-01-01', 'end': '2025-01-02', 'codes': 4,
                                  'trading_days': 500})
    rendered = data_readiness.render(report, compact=True)
    assert ' open>' in rendered and '不足 20 只' in rendered
    report = data_readiness.assess({'start': '2018-01-01', 'end': '2025-12-31', 'codes': 40,
                                  'trading_days': 1900, 'readiness': {'provided_columns': []}})
    rendered = data_readiness.render(report, compact=True)
    summary = rendered.split('</summary>')[0]
    assert '缺少复权因子' in summary and '缺少 ST 标记' in summary


def test_import_profile_identifies_missing_adj_and_test_days(tmp_path):
    from test_csvimport import synth_csv
    raw = pd.read_csv(io.BytesIO(synth_csv(n_days=800, chinese=False))).drop(columns=["adj_factor", "is_st"])
    snap = csvimport.import_csv(io.BytesIO(raw.to_csv(index=False).encode()), tmp_path, "input")
    profile = snap["readiness"]
    assert sum(profile["dates_by_year"].values()) == snap["trading_days"] == 800
    assert profile["dates_with_20_price_rows"] == 800
    report = data_readiness.assess(snap)
    assert not report["blocked"]
    assert "缺少 adj_factor" in ' '.join(report["messages"])
    assert "缺少历史 is_st" in ' '.join(report["messages"])


def test_upload_requirements_and_preflight_before_spending_training_time(tmp_path):
    app, c = client(tmp_path)
    page = c.get("/train").text
    for text in ("720", "5–8 年", "calendar.csv", "adj_factor", "amount", "20 只", "1–2 年"):
        assert text in page
    assert "合成数据 · 仅供演示" in c.get("/data-readiness?dataset_id=_trial").text
    assert c.get("/data-readiness?dataset_id=no_such_data").status_code == 400
    assert c.get("/data-readiness?dataset_id=_trial&horizon=999").status_code == 400
    ds = ui.datasets(tmp_path)
    ds["tiny"] = {"path": tmp_path, "snap": {"start": "2018-01-01", "end": "2025-12-31",
                  "codes": 4, "trading_days": 1900, "has_st": True}}
    cfg, error, _ = ui.build_config(form(app, dataset="tiny"), ds)
    assert cfg is None and "不足 20 只" in error


def test_long_history_is_not_promised_to_pass_and_supported_task_is_clear():
    report = data_readiness.assess({"start": "2018-01-01", "end": "2025-12-31",
                                   "trading_days": 1900, "codes": 300}, horizon=20)
    assert not report["blocked"]
    assert "不保证通过" in ' '.join(report["messages"])
    assert "每日纸面交易只支持 5 日" in ' '.join(report["messages"])


def test_missing_inputs_and_small_samples_have_different_verdicts():
    assert workflow_ui.score_summary({"status": "INVALID"})["label"] == "评价输入不合格"
    score = {"status": "COMPLETED", "evaluation": {"comparable": True,
        "models": {"candidate": {"n": 20, "ci95": None, "ci_reason": "insufficient_dates_or_valid_blocks"}}}}
    result = workflow_ui.score_summary(score)
    assert result["label"] == "证据不足" and "日期不足" in result["message"]


def test_negative_excess_is_not_called_absolute_loss():
    m = {"ic": {"lgbm_ensemble": {"final_test": {"mean": .01, "t": .3, "n": 100}}},
         "net_of_cost": {"signals": {"lgbm_ensemble": {
             "net_excess_t": -.5, "net_excess_ann": -.01, "cost_ann": .02}}}}
    verdict = ' '.join(message for _, message in ui.verdict(m))
    assert "未胜过基准" in verdict and "不等于账户绝对亏损" in verdict
