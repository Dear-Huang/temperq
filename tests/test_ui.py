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
    f = {"token": app.state.token, "name": "ui_trial", "task": "cn_rank_5d",
         "dataset": ui.SYNTHETIC, "num_boost_round": "40", "n_seeds": "1"}
    f.update(over)
    return f


def test_index_shows_four_steps_and_hardware(tmp_path):
    _, c = client(tmp_path)
    page = c.get("/").text
    for s in ("要打造什么模型", "用什么数据", "参数（可以不改）", "开始训练"):
        assert s in page
    assert "全 A 股选股" in page and "试运行数据" in page and "什么显卡" in page
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
    assert "没有 ST 标记" in c.post("/run", data=form(app, task="st_task")).text


def test_empty_advanced_fields_keep_defaults(tmp_path):
    ds = ui.datasets(tmp_path)
    cfg, err, _ = ui.build_config({"name": "a", "task": "cn_rank_5d", "dataset": ui.SYNTHETIC,
                                   "num_leaves": "31"}, EX, ds)
    assert err == "" and cfg["model"] == {"name": "lightgbm", "params": {"num_leaves": 31}}
    # trial data has no ST flags: universe falls back to all (the default, so not written);
    # test years follow the data range
    assert "universe" not in cfg["train"] and cfg["train"]["final_test_years"] == [2021]


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
