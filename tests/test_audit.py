import io

import numpy as np
import pandas as pd
import pytest

from qwb import audit, csvimport, pipeline


def _data(tmp_path):
    b = pipeline.synth_bars(n_days=800, n_codes=60).drop(columns="up_limit")
    csvimport.import_csv(io.BytesIO(b.to_csv(index=False).encode()), tmp_path, "d")
    return tmp_path / "d", b


def _qlib_pred(b, last_days=300, leak=False):
    """Qlib-style pred: datetime, instrument (SZ000001), score."""
    b = b.sort_values(["code", "date"]).copy()
    ac = b.groupby("code")["close"]
    b["score"] = -ac.pct_change(5)                      # 5-day reversal, planted in synth data
    if leak:   # the label itself, close-based: a look-ahead score
        b["score"] = ac.shift(-6) / ac.shift(-1) - 1
    days = sorted(b["date"].unique())[-last_days:]
    p = b[b["date"].isin(days)].dropna(subset=["score"])
    return pd.DataFrame({"datetime": pd.to_datetime(p["date"]).dt.strftime("%Y-%m-%d"),
                         "instrument": "SZ" + p["code"].str[:6], "score": p["score"]})


def test_reads_qlib_format_and_codes():
    raw = b"datetime,instrument,score\n2024-01-02,SH600000,0.1\n2024-01-02,SZ000001,-0.2\n"
    df = audit.read_predictions(raw)
    assert df["code"].tolist() == ["600000.SH", "000001.SZ"]
    with pytest.raises(audit.AuditError, match="缺少列"):
        audit.read_predictions(b"a,b\n1,2\n")


def test_audit_runs_end_to_end_on_external_scores(tmp_path):
    d, b = _data(tmp_path)
    raw = _qlib_pred(b).to_csv(index=False).encode()
    s = audit.run(raw, "pred.csv", d, 5, "100w_600w", tmp_path / "out", tmp_path, "ext1")
    assert s["days_scored"] >= 250 and s["code_match"] == 1.0
    assert s["ic"]["uploaded"]["final_test"]["mean"] > 0.02          # planted reversal is found
    rep = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    for k in ("外部模型体检", "扣成本后", "Qlib 基准口径", "前视嫌疑检查", "参数体检"):
        assert k in rep, k


def test_leaky_scores_stand_out(tmp_path):
    d, b = _data(tmp_path)
    honest = audit.run(_qlib_pred(b).to_csv(index=False).encode(), "a.csv", d, 5, "100w_600w",
                       tmp_path / "a", tmp_path, "honest")
    leaky = audit.run(_qlib_pred(b, leak=True).to_csv(index=False).encode(), "b.csv", d, 5,
                      "100w_600w", tmp_path / "b", tmp_path, "leaky")
    hi, li = honest["ic"]["uploaded"]["final_test"]["mean"], leaky["ic"]["uploaded"]["final_test"]["mean"]
    assert li > 0.5 > hi                                      # a perfect-foresight score is obvious


def test_too_little_overlap_is_explained(tmp_path):
    d, b = _data(tmp_path)
    raw = _qlib_pred(b, last_days=20).to_csv(index=False).encode()
    with pytest.raises(audit.AuditError, match="至少需要 60 天"):
        audit.run(raw, "x.csv", d, 5, "100w_600w", tmp_path / "x", tmp_path, "x")
