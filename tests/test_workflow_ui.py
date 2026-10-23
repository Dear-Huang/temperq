from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from qwb import account_advice, experiments, paper, paper_auto, ui, workflow_ui
from test_daily_strategy import fixture


def setup(tmp_path, monkeypatch):
    source, panel = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(experiments, "frozen_source", lambda path: path)
    pending = []
    app = ui.create_app(tmp_path, runner=pending.append)
    return source, panel, app, TestClient(app)


def test_retrain_keeps_source_and_records_parent(tmp_path, monkeypatch):
    source, _, app, client = setup(tmp_path, monkeypatch)
    before = experiments.digest(source / "final_model" / "ridge.json")
    page = client.get("/workflow/model")
    assert "待跑基准" in page.text and "完整重训为新版本" in page.text
    assert client.post("/workflow/model/retrain", data={"token": "bad"}).status_code == 403
    response = client.post("/workflow/model/retrain", data={"token": app.state.token,
                           "name": "model_v2", "alpha": 2}, follow_redirects=False)
    assert response.status_code == 303
    jid = response.headers["location"].split("/")[-1]
    payload = app.state.store.spec(jid)["config"]
    assert payload["resolved"]["cfg"]["ridge_alpha"] == 2
    assert payload["ui"]["parent"] == str(source)
    assert payload["resolved"]["output"] != str(source)
    assert experiments.digest(source / "final_model" / "ridge.json") == before
    assert "未使用过" in client.post("/workflow/model/retrain", data={"token": app.state.token,
           "name": "model", "alpha": 2}).text


def test_delete_reversible_without_removing_account_or_evidence(tmp_path, monkeypatch):
    source, _, app, client = setup(tmp_path, monkeypatch)
    paper.initialize(tmp_path, "observed", source, 100000, [])
    response = client.post("/workflow/model/visibility", data={"token": app.state.token}, follow_redirects=False)
    assert response.status_code == 303 and workflow_ui.hidden(tmp_path, source)
    assert (source / "final_model" / "ridge.json").is_file()
    assert paper.inspect(tmp_path, "observed")["config"]["cash"] == 100000
    client.post("/workflow/model/visibility", data={"token": app.state.token, "restore": "yes"}, follow_redirects=False)
    assert not workflow_ui.hidden(tmp_path, source)


def test_paper_requires_evaluation_then_actual_holdings_use_frozen_account(tmp_path, monkeypatch):
    source, _, app, client = setup(tmp_path, monkeypatch)
    form = {"token": app.state.token, "name": "observed", "cash": 100000,
            "feed": str(tmp_path / "data"), "observe_only": "yes", "automatic": "yes"}
    assert "请先跑完基准" in client.get("/workflow/model/paper").text
    assert "请先完成独立基准评价" in client.post("/workflow/model/paper", data=form).text
    monkeypatch.setattr(workflow_ui, "evaluation_summary", lambda *_: {
        "label": "证据不足", "score": {"status": "COMPLETED"}, "evaluation_id": "a"*32})
    response = client.post("/workflow/model/paper", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert paper_auto.settings(tmp_path, "observed")["enabled"]
    assert "输入真实仓位" in client.get(response.headers["location"]).text
    before = deepcopy(paper.inspect(tmp_path, "observed"))
    # Changing the training source must not change this account's frozen inference.
    experiments.write(source / "final_model" / "ridge.json", {"coef": [1000]})
    result = client.post("/holdings/observed", data={"token": app.state.token, "cash": 80000,
                         "execution_day": "2026-11-02", "supported_only": "yes"}, follow_redirects=False)
    assert "计划执行日" in result.text  # outside supplied calendar
    # Use a fixture panel ending earlier so a future, declared session is available.
    original = paper.load_inputs
    def inputs(*args):
        meta, panel, snapshot, st, members, calendar = original(*args)
        panel = {key: frame.iloc[:-1] for key, frame in panel.items()}
        return meta, panel, snapshot, st, members, calendar
    monkeypatch.setattr(paper, "load_inputs", inputs)
    result = client.post("/holdings/observed", data={"token": app.state.token, "cash": 80000,
                         "execution_day": "2026-10-30", "supported_only": "yes"}, follow_redirects=False)
    assert result.status_code == 303, result.text
    report = client.get(result.headers["location"])
    assert "返回纸面交易" in report.text and "修改真实仓位" in report.text
    assert "启用前向模拟账户" not in report.text
    assert paper.inspect(tmp_path, "observed") == before
    advice = next((tmp_path / "runs" / "advice").glob("*.json"))
    assert experiments.read(advice)["model_hash"] == before["config"]["model_hash"]


def test_daily_auto_once_per_day_persisted_across_restart(tmp_path, monkeypatch):
    source, _, _, _ = setup(tmp_path, monkeypatch)
    paper.initialize(tmp_path, "observed", source, 100000, [])
    paper_auto.configure(tmp_path, "observed", True)
    calls = []
    monkeypatch.setattr(paper, "tick", lambda *args, **kw: calls.append(kw["now"]) or
                        {"state": "WAITING_DATA", "message": "等待今日数据"})
    assert paper_auto.check(tmp_path, "observed", now="2026-10-23T17:29:00+08:00") is None
    assert paper_auto.check(tmp_path, "observed", now="2026-10-23T17:30:00+08:00")["state"] == "WAITING_DATA"
    assert paper_auto.check(tmp_path, "observed", now="2026-10-23T18:00:00+08:00") is None
    assert len(calls) == 1
    paper_auto.configure(tmp_path, "observed", False)
    assert paper_auto.check(tmp_path, "observed", now="2026-10-26T18:00:00+08:00") is None
    assert len(calls) == 1


def test_failed_auto_is_visible_and_does_not_fake_ledger(tmp_path, monkeypatch):
    source, _, _, _ = setup(tmp_path, monkeypatch)
    paper.initialize(tmp_path, "observed", source, 100000, [])
    paper_auto.configure(tmp_path, "observed", True)
    def fail(*args, **kwargs):
        raise ValueError("bad feed")
    monkeypatch.setattr(paper, "tick", fail)
    result = paper_auto.check(tmp_path, "observed", now="2026-10-23T18:00:00+08:00")
    assert result["state"] == "BLOCKED"
    assert not paper.inspect(tmp_path, "observed")["sessions"]


@pytest.mark.parametrize("status,comparable,level,ready", [
    ("INVALID", True, "SIGNAL_RESEARCH_CANDIDATE", False),
    ("COMPLETED", False, "SIGNAL_RESEARCH_CANDIDATE", False),
    ("COMPLETED", True, "RESEARCH_ONLY", False),
    ("COMPLETED", True, "SIGNAL_RESEARCH_CANDIDATE", True),
])
def test_verdict_never_means_live_approval(status, comparable, level, ready):
    summary = workflow_ui.score_summary({"status": status, "evaluation": {"comparable": comparable},
                                         "readiness": {"level": level}})
    assert summary["ready"] is ready
    assert summary["label"] != "可实盘上线"
