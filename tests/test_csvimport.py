import io
from pathlib import Path

import pandas as pd
import pytest

from qwb import csvimport, pipeline, runconfig

EX = Path(__file__).resolve().parents[1] / "examples"


def synth_csv(n_days=1000, n_codes=60, chinese=True, st_codes=()):
    b = pipeline.synth_bars(n_days=n_days, n_codes=n_codes)
    b["code"] = b["code"].str[:6]                        # users often drop the suffix
    b["date"] = pd.to_datetime(b["date"]).dt.strftime("%Y%m%d")
    b["is_st"] = b["code"].isin(st_codes).astype(int)
    b = b.drop(columns=["up_limit"]).sample(frac=1, random_state=0)   # unsorted on purpose
    if chinese:
        b = b.rename(columns={"code": "代码", "date": "日期", "open": "开盘", "high": "最高",
                              "low": "最低", "close": "收盘", "amount": "成交额"})
    return b.to_csv(index=False).encode("utf-8")


def test_template_parses():
    df = csvimport.read(EX / "csv_template.csv")
    bars, st, notes = csvimport.to_bars(df)
    assert bars["code"].tolist()[:2] == ["000001.SZ", "000001.SZ"]
    assert set(bars["code"]) == {"000001.SZ", "600000.SH"}
    assert st is not None


def test_missing_column_message():
    with pytest.raises(csvimport.ImportError_, match="缺少必填列：amount"):
        csvimport.read(io.BytesIO(b"code,date,open,high,low,close\n1,2024-01-02,1,1,1,1\n"))


def test_too_few_years_and_duplicate_rows(tmp_path):
    raw = synth_csv(n_days=300)
    with pytest.raises(csvimport.ImportError_, match="至少需要 3"):
        csvimport.import_csv(io.BytesIO(raw), tmp_path, "short")
    dup = raw + raw.split(b"\n", 2)[1] + b"\n"
    with pytest.raises(csvimport.ImportError_, match="重复"):
        csvimport.import_csv(io.BytesIO(dup), tmp_path, "dup")


def test_limit_prices_follow_rule_pack_and_st_alignment(tmp_path):
    raw = synth_csv(n_days=800, st_codes=("000003",))
    snap = csvimport.import_csv(io.BytesIO(raw), tmp_path, "d1")
    assert snap["has_st"] and snap["codes"] == 60
    bars = pd.concat([pd.read_parquet(p) for p in (tmp_path / "d1" / "bars").glob("*.parquet")],
                     ignore_index=True)
    b = bars.sort_values(["code", "date"]).reset_index(drop=True)
    pre = b.groupby("code")["close"].shift()
    row = b[(b.code == "000003.SZ") & pre.notna()].iloc[5]
    p = pre.loc[row.name]
    assert row.up_limit == pytest.approx(round(p * 1.05 + 1e-9, 2), abs=0.011)   # ST: 5%
    row = b[(b.code == "000010.SZ") & pre.notna()].iloc[5]
    assert row.up_limit == pytest.approx(round(pre.loc[row.name] * 1.10 + 1e-9, 2), abs=0.011)
    st = pd.read_parquet(tmp_path / "d1" / "st_status.parquet")
    assert set(st.code) == {"000003.SZ"}


def test_imported_csv_trains_end_to_end(tmp_path):
    pytest.importorskip("lightgbm")
    snap = csvimport.import_csv(io.BytesIO(synth_csv()), tmp_path / "data", "mine")
    # is_st column given (all zero): ST info is present, just no ST stocks
    assert snap["trading_days"] == 1000 and snap["has_st"]
    cfg = {"name": "csv_e2e", "task": str(EX / "cn_rank_5d.yaml"),
           "data": {"source": "snapshot", "dir": str(tmp_path / "data" / "mine")},
           "model": {"name": "lightgbm", "num_boost_round": 30},
           "train": {"universe": "all", "min_age_days": 20, "first_test_year": 2021,
                     "final_test_years": [2022], "seeds": [0], "shuffle_null_draws": 2},
           "output": "out"}
    rc, rep = runconfig.resolve(cfg, tmp_path)
    assert rc is not None, str(rep)
    s = pipeline.run(rc, tmp_path, log=lambda *_: None)
    assert s["data_snapshot_id"] == snap["data_snapshot_id"]
    assert s["net_of_cost"] is not None and s["leak_check"]["ok"]
    assert s["ledger"]["views"] == 1

    # second scoring of the same test period on real (non-synthetic) data: warned
    s = pipeline.run(rc, tmp_path, log=lambda *_: None)
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert s["ledger"]["views"] == 2
    assert "已被评估 2 次" in report and "不能当作独立确认" in report
    assert "同一测试期的全部评估记录" in report and "| 本次 |" in report
