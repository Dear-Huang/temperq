import io
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from qwb import pipeline, ui  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples"


def client(tmp_path):
    (tmp_path / "examples").mkdir()
    for f in ("cn_rank_5d.yaml", "st_task.yaml", "csv_template.csv"):
        (tmp_path / "examples" / f).write_bytes((EX / f).read_bytes())
    app = ui.create_app(tmp_path, runner=lambda fn: fn())   # jobs run inline
    return app, TestClient(app)


def form(app, **over):
    f = {"token": app.state.token, "name": "ui_trial", "market": "cn_a", "horizon": "5",
         "universe": "all", "capital": "100w_600w", "model": "lightgbm",
         "dataset": ui.SYNTHETIC, "num_boost_round": "40", "n_seeds": "1"}
    f.update(over)
    return f


def test_index_shows_four_steps_scenario_and_hardware(tmp_path):
    _, c = client(tmp_path)
    page = c.get("/").text
    for s in ("要打造什么模型", "用什么数据", "由第 1 步推出", "开始训练"):
        assert s in page
    for s in ("用在哪个市场", "持有期", "在哪些股票里选", "打算投入多少资金", "用什么模型",
              "0–30 万", "5 亿及以上", "沪深 300", "LightGBM", "Ridge", "Kronos", "规划中"):
        assert s in page, s
    assert 'value="kronos" form="main" disabled' in page          # planned: cannot be picked
    assert "试运行数据" in page and "什么显卡" in page
    assert c.get("/template.csv").status_code == 200


def test_posts_need_token_and_local_host(tmp_path):
    app, c = client(tmp_path)
    assert c.post("/run", data=form(app, token="x")).status_code == 403
    assert c.post("/run", data=form(app), headers={"host": "evil.example:8765"}).status_code == 403
    assert c.post("/data/csv", data={"token": "x", "name": "a"},
                  files={"file": ("a.csv", b"x")}).status_code == 403
    assert c.post("/data/tushare", data={"token": "x"}).status_code == 403


def test_bad_inputs_get_plain_messages(tmp_path):
    app, c = client(tmp_path)
    assert "名称只能用" in c.post("/run", data=form(app, name="../x")).text
    assert "种子数" in c.post("/run", data=form(app, n_seeds="50")).text
    assert "请选择数据" in c.post("/run", data=form(app, dataset="nope")).text
    assert "没有 ST 标记" in c.post("/run", data=form(app, universe="st")).text
    assert "没有指数成分" in c.post("/run", data=form(app, universe="csi300")).text
    assert "还没有接入" in c.post("/run", data=form(app, model="kronos")).text
    assert "只支持 A 股" in c.post("/run", data=form(app, market="us")).text
    assert "至少 30 只" in c.post("/run", data=form(app, universe="list",
                                                   universe_list="000001 600000")).text


def test_scenario_drives_config():
    ds = ui.datasets(Path("."))
    base = {"name": "a", "market": "cn_a", "dataset": ui.SYNTHETIC, "universe": "all"}
    cfg, err, _ = ui.build_config(dict(base, horizon="20", capital="1e_5e"), ds)
    assert err == ""
    assert cfg["task"]["target"]["horizon_days"] == 20
    assert cfg["train"]["portfolio"]["n_hold"] == 100
    assert cfg["train"]["min_adv_yuan"] == round(5e8 / 100 / 0.05)
    assert cfg["train"]["scenario"]["capital"] == "1 亿–5 亿"
    small, _, _ = ui.build_config(dict(base, capital="lt30w"), ds)
    assert small["train"]["portfolio"]["n_hold"] == 10
    assert small["train"]["min_adv_yuan"] < cfg["train"]["min_adv_yuan"]
    # step 3 override: fewer names -> bigger positions -> higher liquidity floor
    few, _, _ = ui.build_config(dict(base, capital="lt30w", n_hold="5"), ds)
    assert few["train"]["min_adv_yuan"] == 2 * small["train"]["min_adv_yuan"]
    r, _, _ = ui.build_config(dict(base, model="ridge", num_leaves="31"), ds)
    assert r["model"] == {"name": "ridge"} and r["train"]["seeds"] == [0]   # lgbm params ignored
    lg, _, _ = ui.build_config(dict(base, num_leaves="31"), ds)
    assert lg["model"] == {"name": "lightgbm", "params": {"num_leaves": 31}}
    codes = " ".join(f"{600000 + i}" for i in range(30))
    lst, err, _ = ui.build_config(dict(base, universe="list", universe_list=codes), ds)
    assert err == "" and len(lst["train"]["universe_list"]) == 30
    assert lst["train"]["universe_list"][0] == "600000.SH"


def test_trial_run_end_to_end_with_verdict(tmp_path):
    pytest.importorskip("lightgbm")
    app, c = client(tmp_path)
    r = c.post("/run", data=form(app), follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "完成" in page and "一句话结论" in page and "试运行数据" in page
    assert "扣成本后" in page and "前视检查" in page and "num_boost_round = 40" in page
    saved = yaml.safe_load((tmp_path / "runs" / "ui_configs" / "ui_trial.yaml").read_text("utf-8"))
    assert saved["model"]["num_boost_round"] == 40
    assert "ui_trial" in c.get("/").text


def test_csv_upload_then_train(tmp_path):
    pytest.importorskip("lightgbm")
    app, c = client(tmp_path)
    b = pipeline.synth_bars(n_days=800, n_codes=40).drop(columns="up_limit")
    raw = b.to_csv(index=False).encode()
    r = c.post("/data/csv", data={"token": app.state.token, "name": "mine"},
               files={"file": ("mine.csv", io.BytesIO(raw), "text/csv")})
    assert "导入成功" in r.text and "40 只股票" in r.text
    assert "mine：" in c.get("/").text
    bad = c.post("/data/csv", data={"token": app.state.token, "name": "bad"},
                 files={"file": ("bad.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")})
    assert "缺少必填列" in bad.text
    r = c.post("/run", data=form(app, name="on_csv", dataset="mine"), follow_redirects=False)
    page = c.get(r.headers["location"]).text
    assert "完成" in page and "一句话结论" in page


def test_tushare_form_validates_before_any_request(tmp_path):
    app, c = client(tmp_path)
    t = app.state.token
    assert "请填写 Tushare token" in c.post("/data/tushare", data={
        "token": t, "name": "x", "api_token": "", "start_year": "2018", "end_year": "2025"}).text
    assert "至少 3 年" in c.post("/data/tushare", data={
        "token": t, "name": "x", "api_token": "a" * 32, "start_year": "2024",
        "end_year": "2025"}).text


def test_markdown_render_escapes_html():
    h = ui.md_to_html("# T\n| a | b |\n|---|---|\n| <script> | **x** |\n- item `c`")
    assert "<script>" not in h and "&lt;script&gt;" in h and "<b>x</b>" in h
    assert "<table>" in h and "<li>item <code>c</code></li>" in h


def test_combined_dataset_flow(tmp_path):
    pytest.importorskip("lightgbm")
    import pandas as pd
    app, c = client(tmp_path)
    t = app.state.token
    b = pipeline.synth_bars(n_days=800, n_codes=40).drop(columns="up_limit")
    c.post("/data/csv", data={"token": t, "name": "mine"},
           files={"file": ("mine.csv", io.BytesIO(b.to_csv(index=False).encode()), "text/csv")})
    assert "加更多数据来源" in c.get("/").text
    page = c.get("/data/mine").text
    assert "新建组合数据" in page
    assert c.post("/data/mine/combine", data={"token": "x", "name": "mine_plus"}).status_code == 403
    r = c.post("/data/mine/combine", data={"token": t, "name": "mine_plus"}, follow_redirects=False)
    assert r.headers["location"] == "/data/mine_plus"
    page = c.get("/data/mine_plus").text
    assert "这份数据是什么" in page and "数据什么时候能拿到" in page and "moneyflow" in page
    f = b[["code", "date"]].assign(flow=range(len(b)))
    f["date"] = pd.to_datetime(f["date"]).dt.strftime("%Y-%m-%d")
    r = c.post("/data/mine_plus/source",
               data={"token": t, "kind": "factor", "rule": "next_day", "label": "flow"},
               files={"file": ("f.csv", io.BytesIO(f.to_csv(index=False).encode()), "text/csv")},
               follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/data/mine_plus").text
    assert "flow" in page and "次日开盘后" in page
    assert "另加 1 个数据来源" in c.get("/").text
    bad = c.post("/data/mine_plus/source", data={"token": t, "kind": "factor", "rule": "announce"},
                 files={"file": ("g.csv", io.BytesIO(f.to_csv(index=False).encode()), "text/csv")})
    assert "ann_date" in bad.text
    r = c.post("/run", data=form(app, name="on_combined", dataset="mine_plus"),
               follow_redirects=False)
    page = c.get(r.headers["location"]).text
    assert "完成" in page and "一句话结论" in page
    assert "12 天 × 12 项" not in page and "前视检查" in page
    assert c.post("/data/mine_plus/remove", data={"token": t, "sid": "s1"},
                  follow_redirects=False).status_code == 303
    assert "还没有添加来源" in c.get("/data/mine_plus").text
    assert c.post("/data/mine_plus/tushare", data={"token": t, "api_token": "a" * 32}).text.count("至少勾选") == 1
