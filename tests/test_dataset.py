import io
import json

import numpy as np
import pandas as pd
import pytest

from qwb import csvimport, daily, dataset, leakcheck, pipeline, runconfig, scenario


def base(tmp_path, n_days=800, n_codes=40, name="base"):
    b = pipeline.synth_bars(n_days=n_days, n_codes=n_codes).drop(columns="up_limit")
    raw = b.to_csv(index=False).encode()
    csvimport.import_csv(io.BytesIO(raw), tmp_path, name)
    return tmp_path / name, b


def test_effective_days_rules():
    cal = pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-05"]))
    d = pd.Series(pd.to_datetime(["2024-01-02", "2024-01-04", "2024-01-05"]))
    ev = dataset.effective_days(d, cal, "evening").dt.strftime("%m%d").tolist()
    nd = dataset.effective_days(d, cal, "next_day")
    assert ev == ["0102", "0105", "0105"]               # non-trading 01-04 moves forward
    assert nd.dt.strftime("%m%d").tolist()[:2] == ["0103", "0105"] and pd.isna(nd.iloc[2])


def test_removing_a_source_cannot_make_next_import_overwrite_an_existing_source(tmp_path):
    root, bars = base(tmp_path)
    combined = tmp_path / "combined"
    dataset.create(root, combined)
    frame = pd.DataFrame({"code": ["000001"], "date": ["2019-01-02"], "value": [1.]})
    first = dataset.add_factor(combined, frame, "next_day", "first", "fixture")
    second = dataset.add_factor(combined, frame.assign(value=2.), "next_day", "second", "fixture")
    path = combined / "sources" / f"{second['id']}.parquet"
    saved = path.read_bytes()
    dataset.remove(combined, first["id"])
    third = dataset.add_factor(combined, frame.assign(value=3.), "next_day", "third", "fixture")
    assert third["id"] != second["id"]
    assert path.read_bytes() == saved
    assert len({s["id"] for s in dataset.spec(combined)["sources"]}) == 2


def test_effective_bar_overlay_cannot_inherit_normal_state_from_replaced_row(tmp_path):
    from qwb.bench.economics import load_normal

    root = tmp_path / "base"
    (root / "bars").mkdir(parents=True)
    bars = pipeline.synth_bars(80, 30).assign(state="normal")
    bars.to_parquet(root / "bars" / "fixture.parquet", index=False)
    (root / "snapshot.json").write_text(json.dumps({"data_snapshot_id": "fixture"}))
    combined = tmp_path / "combined"
    dataset.create(root, combined)
    replacement = bars.head(1).drop(columns="state").copy()
    source = dataset._save(combined, "bars", replacement, "overlay", "fixture", {"priority": "override"}, [])
    loaded = dataset.load(combined)["P"]
    with pytest.raises(ValueError, match="state missing"):
        load_normal(combined, loaded["close"].index, loaded["close"].columns)
    assert f"sources/{source['id']}.parquet" in dataset.input_files(combined)


def test_factor_source_becomes_feature_and_is_timed(tmp_path):
    root, b = base(tmp_path)
    comb = tmp_path / "comb"
    dataset.create(root, comb)
    days = sorted(b["date"].unique())
    rows = pd.DataFrame({"code": "000001", "date": [d.strftime("%Y-%m-%d") for d in days[:50]],
                         "flow": np.arange(50.0)})
    src = dataset.add_csv(comb, rows.to_csv(index=False).encode(), "factor", "next_day",
                          "资金流", "flow.csv")
    assert src["columns"] == ["flow"]
    ds = dataset.load(comb)
    col = f"x_{src['id']}_flow"
    w = ds["P"][col]["000001.SZ"]
    # next_day: the value dated day k is first usable on day k+1
    assert np.isnan(w.iloc[0]) and w.iloc[1] == 0.0 and w.iloc[2] == 1.0
    assert col in daily.feature_names(daily.features(ds["P"]))
    # snapshot changes with the source, and with removing it
    s1 = json.loads((comb / "snapshot.json").read_text("utf-8"))["data_snapshot_id"]
    dataset.remove(comb, src["id"])
    s0 = json.loads((comb / "snapshot.json").read_text("utf-8"))["data_snapshot_id"]
    assert s1 != s0


def test_announce_rule_needs_ann_date_and_carries_forward(tmp_path):
    root, b = base(tmp_path)
    comb = tmp_path / "comb"
    dataset.create(root, comb)
    no_ann = pd.DataFrame({"code": ["000001"], "date": ["2019-03-01"], "roe": [0.1]})
    with pytest.raises(dataset.SourceError, match="ann_date"):
        dataset.add_csv(comb, no_ann.to_csv(index=False).encode(), "factor", "announce", "", "x.csv")
    days = sorted(b["date"].unique())
    ann = pd.DataFrame({"code": ["000001", "000001"],
                        "ann_date": [days[10].strftime("%Y%m%d"), days[30].strftime("%Y%m%d")],
                        "roe": [0.10, 0.20]})
    src = dataset.add_csv(comb, ann.to_csv(index=False).encode(), "factor", "announce",
                          "财务", "fin.csv")
    w = dataset.load(comb)["P"][f"x_{src['id']}_roe"]["000001.SZ"]
    assert np.isnan(w.iloc[10])                              # not on the announcement day
    assert w.iloc[11] == pytest.approx(0.10) and w.iloc[30] == pytest.approx(0.10)
    assert w.iloc[31] == pytest.approx(0.20) and w.iloc[60] == pytest.approx(0.20)


def test_bars_source_overlap_check_and_priority(tmp_path):
    root, b = base(tmp_path)
    comb = tmp_path / "comb"
    dataset.create(root, comb)
    sl = b[b["code"] == "000005.SZ"].head(20).copy()
    wrong = sl.assign(close=sl["close"] * 1.5, high=sl["high"] * 1.5, open=sl["open"] * 1.5,
                      low=sl["low"] * 1.5)
    with pytest.raises(dataset.SourceError, match="收盘价相差"):
        dataset.add_csv(comb, wrong.to_csv(index=False).encode(), "bars", "", "x", "w.csv", "fill")
    newcode = sl.assign(code="000999.SZ")
    src = dataset.add_csv(comb, newcode.to_csv(index=False).encode(), "bars", "", "补充", "n.csv",
                          "fill")
    P = dataset.load(comb)["P"]
    assert "000999.SZ" in P["close"].columns and src["priority"] == "fill"


def test_st_source_and_combined_training_with_leak_check(tmp_path):
    pytest.importorskip("lightgbm")
    root, b = base(tmp_path)
    comb = tmp_path / "comb"
    dataset.create(root, comb)
    # a factor that leaks the future 5-day return would be a disaster; this one is honest:
    # yesterday's close-to-close return, published the next morning
    b = b.sort_values(["code", "date"])
    b["prev"] = b.groupby("code")["close"].pct_change()
    f = b[["code", "date", "prev"]].dropna()
    f["date"] = pd.to_datetime(f["date"]).dt.strftime("%Y-%m-%d")
    f["code"] = f["code"].str[:6]
    dataset.add_csv(comb, f.to_csv(index=False).encode(), "factor", "evening", "昨收益", "f.csv")
    st = pd.DataFrame({"code": "000003", "date": [d.strftime("%Y-%m-%d") for d in
                                                   sorted(b["date"].unique())[:100]], "is_st": 1})
    dataset.add_csv(comb, st.to_csv(index=False).encode(), "st", "", "ST", "st.csv")
    assert dataset.has_st(comb)
    cfg = {"name": "comb_run", "task": scenario.task_card(5),
           "data": {"source": "snapshot", "dir": str(comb)},
           "model": {"name": "lightgbm", "num_boost_round": 30},
           "train": {"universe": "all_ex_st", "min_age_days": 20, "first_test_year": 2021,
                     "final_test_years": [2021], "seeds": [0], "shuffle_null_draws": 2},
           "output": "out"}
    rc, rep = runconfig.resolve(cfg, tmp_path)
    assert rc is not None, str(rep)
    s = pipeline.run(rc, tmp_path, log=lambda *_: None)
    assert s["leak_check"]["ok"], s["leak_check"]["violations"]
    assert s["leak_check"]["items"] == len(daily.FEATURES) + 2      # + 1 added factor + universe


def test_text_labels_become_indicator_columns_and_free_text_is_skipped(tmp_path):
    root, b = base(tmp_path)
    comb = tmp_path / "comb"
    dataset.create(root, comb)
    days = [d.strftime("%Y-%m-%d") for d in sorted(b["date"].unique())[:60]]
    rows = pd.DataFrame({"code": "000001", "date": days,
                         "情绪": ["利好", "利空", "中性"] * 20,
                         "正文": [f"公告全文第{i}段……" for i in range(60)],
                         "热度": np.arange(60.0)})
    src = dataset.add_csv(comb, rows.to_csv(index=False).encode(), "factor", "evening",
                          "舆情", "news.csv")
    cols = src["columns"]
    assert "热度" in cols
    assert any(c.endswith("利好") for c in cols) and any(c.endswith("利空") for c in cols)
    assert not any("正文" in c for c in cols)
    assert any("正文" in n for n in src["notes"]) and any("3 种取值" in n for n in src["notes"])
    P = dataset.load(comb)["P"]
    good = next(k for k in P if k.startswith("x_") and k.endswith("利好"))
    w = P[good]["000001.SZ"].dropna()
    assert set(w.unique()) <= {0.0, 1.0} and w.sum() == 20
