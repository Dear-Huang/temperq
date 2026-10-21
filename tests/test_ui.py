from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from qwb import ui  # noqa: E402

EX = Path(__file__).resolve().parents[1] / "examples"


def client(tmp_path):
    app = ui.create_app(tmp_path, task_dir=EX, runner=lambda fn: fn())   # run inline
    return app, TestClient(app)


def form(app, **over):
    f = {"token": app.state.token, "name": "ui_smoke", "task": "cn_rank_5d",
         "source": "synthetic", "universe": "all", "num_boost_round": "40",
         "num_leaves": "63", "learning_rate": "0.05", "n_seeds": "1",
         "top": "0.1", "commission_bp": "3"}
    f.update(over)
    return f


def test_index_lists_task_cards_only(tmp_path):
    _, c = client(tmp_path)
    page = c.get("/").text
    assert 'value="cn_rank_5d"' in page and 'value="st_task"' in page
    assert "run_smoke" not in page and "qwb_package" not in page   # run configs / manifests


def test_post_needs_token_and_local_host(tmp_path):
    app, c = client(tmp_path)
    assert c.post("/run", data=form(app, token="x")).status_code == 403
    r = c.post("/run", data=form(app), headers={"host": "evil.example:8765"})
    assert r.status_code == 403


def test_bad_name_and_ranges_rejected(tmp_path):
    app, c = client(tmp_path)
    assert "名称只能用" in c.post("/run", data=form(app, name="../x")).text
    assert "种子数" in c.post("/run", data=form(app, n_seeds="50")).text


def test_build_config_writes_only_changes():
    tasks = ui.task_cards(EX)
    cfg, _ = ui.build_config({"name": "a", "task": "cn_rank_5d", "source": "snapshot",
                              "universe": "all", "num_boost_round": "300", "num_leaves": "31",
                              "learning_rate": "0.05", "n_seeds": "3", "top": "0.1",
                              "commission_bp": "3"}, tasks)
    assert cfg["model"] == {"name": "lightgbm", "params": {"num_leaves": 31}}
    assert "train" not in cfg


def test_full_run_from_form(tmp_path):
    pytest.importorskip("lightgbm")
    app, c = client(tmp_path)
    r = c.post("/run", data=form(app), follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert "完成" in page and "扣成本后" in page and "前视检查" in page
    assert "num_boost_round = 40" in page
    saved = yaml.safe_load((tmp_path / "runs" / "ui_configs" / "ui_smoke.yaml").read_text("utf-8"))
    assert saved["model"]["num_boost_round"] == 40
    assert "ui_smoke" in c.get("/").text
