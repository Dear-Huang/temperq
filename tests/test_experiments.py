from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from qwb import card, cli, evidence_export, experiments, pipeline, runconfig, ui, worker
from qwb.bench.contracts import create_protocol
from qwb.bench.runner import compare_models, evaluate_model, verify_result
from qwb.jobs import JobSpec, Store

EX = Path(__file__).resolve().parents[1] / "examples"


def test_atomic_json_write_uses_short_exclusive_temporary_names_and_retries(tmp_path, monkeypatch):
    from pathlib import Path

    target = tmp_path / "long-descriptive-candidate-submission.json"
    experiments.write(target, {"old": True})
    replace = Path.replace
    calls = []
    def locked_then_replace(source, destination):
        calls.append(source.name)
        assert source.parent == target.parent and len(source.name) <= 18
        assert source.suffix == ".tmp" and source.is_file()
        assert experiments.read(target) == {"old": True}
        if len(calls) == 1:
            raise PermissionError("transient Windows lock")
        return replace(source, destination)
    monkeypatch.setattr(Path, "replace", locked_then_replace)
    monkeypatch.setattr(experiments.time, "sleep", lambda _duration: None)
    experiments.write(target, {"new": True})
    assert len(calls) == 2 and calls[0] == calls[1]
    assert experiments.read(target) == {"new": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_sync_sidecars_cannot_change_training_model_identity(tmp_path):
    model = tmp_path / "model.txt"
    model.write_text("model", encoding="utf-8")
    original = experiments.inventory(tmp_path)
    model.with_name("model.txt.baiduyun.uploading.cfg").write_text("sync metadata", encoding="utf-8")
    (tmp_path / ".write.tmp").write_text("partial", encoding="utf-8")
    assert experiments.inventory(tmp_path) == original


@pytest.fixture(scope="module")
def research(tmp_path_factory):
    root = tmp_path_factory.mktemp("research")
    data = root / "private-market"
    (data / "bars").mkdir(parents=True)
    bars = pipeline.synth_bars(420, 40)
    bars["state"] = "normal"
    bars["down_limit"] = bars["open"] * .8
    bars["up_limit"] = bars["open"] * 1.2
    bars.to_parquet(data / "bars" / "fixture.parquet", index=False)
    experiments.write(data / "snapshot.json", {"data_snapshot_id": "snap-test-fixture"})
    raw = {"name": "same_name", "task": str(EX / "cn_rank_5d.yaml"),
           "data": {"source": "snapshot", "dir": str(data)},
           "model": {"name": "lightgbm", "num_boost_round": 8, "params": {"num_threads": 2}},
           "train": {"universe": "all", "min_age_days": 20, "min_amount_quantile": 0,
                     "first_test_year": 2020, "final_test_years": [2020], "seeds": [0], "shuffle_null_draws": 2}}
    rc, report = runconfig.resolve(raw, EX)
    assert rc, str(report)
    first = pipeline.run(rc, root, log=lambda *_: None)
    raw["model"] = {"name": "ridge", "alpha": 10}
    rc2, _ = runconfig.resolve(raw, EX)
    second = pipeline.run(rc2, root, log=lambda *_: None)
    protocol = root / "bench" / "protocols" / "fixture.yaml"
    create_protocol(first["output"], protocol, cwd=root, block_days=2, resamples=100, min_days=8)
    a, b = root / "bench-a", root / "bench-b"
    for source, output in ((first["output"], a), (second["output"], b)):
        score = evaluate_model(source, protocol, output, workdir=root, log=lambda *_: None)
        assert score["status"] == "COMPLETED", score["errors"]
    return root, first, second, rc2, protocol, a, b


def test_same_name_versions_and_frozen_bench_lineage(research):
    root, first, second, _, _, a, b = research
    assert first["run_id"] != second["run_id"]
    assert first["experiment_id"] == second["experiment_id"]
    assert first["model_version"] != second["model_version"]
    for run in (first, second):
        record = experiments.verify(Path(run["output"]))
        assert record["run_id"] == run["run_id"]
        meta = experiments.read(Path(run["output"]) / "final_model" / "meta.json")
        assert meta["model_version"] == run["model_version"]
        import pandas as pd
        predictions = pd.read_parquet(Path(run["output"]) / "predictions.parquet")
        assert set(predictions["model_version"]) == {record["prediction_version"]}
    latest = root / "runs" / "same_name"
    assert experiments.frozen_source(latest) == Path(second["output"])
    for result, run in ((a, first), (b, second)):
        score = experiments.read(result / "scorecard.json")
        assert score["lineage"]["run_id"] == run["run_id"]
        assert score["prediction_hash"] == experiments.verify(Path(run["output"]))["artifacts"]["predictions.parquet"]
        assert verify_result(result, inputs=True)["status"] == "PASS"


def test_failed_and_cancelled_attempts_preserve_latest(research, monkeypatch):
    root, _, second, rc, *_ = research
    latest = root / "runs" / "same_name"
    before = experiments.inventory(latest)
    def boom(*args, **kwargs):
        raise MemoryError("simulated out of memory")
    monkeypatch.setattr(pipeline, "_compute", boom)
    with pytest.raises(MemoryError):
        pipeline.run(rc, root, log=lambda *_: None)
    failed = experiments.list_runs(root)[0]
    assert failed["state"] == "failed" and "MemoryError" in failed["error"]
    assert experiments.inventory(latest) == before
    assert experiments.verify(Path(second["output"]))["state"] == "succeeded"
    from qwb.jobs import Cancelled
    def cancel(*args, **kwargs):
        raise Cancelled()
    monkeypatch.setattr(pipeline, "_compute", cancel)
    with pytest.raises(Cancelled):
        pipeline.run(rc, root, log=lambda *_: None)
    assert experiments.list_runs(root)[0]["state"] == "cancelled"


def test_evidence_export_is_bound_and_excludes_private_files(research):
    root, first, second, _, _, a, b = research
    payload = evidence_export.build(Path(first["output"]), a)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert set(archive.namelist()) == {"README.md", "evidence.json", "checksums.json"}
        text = archive.read("evidence.json").decode()
        assert str(root) not in text and "private-market" not in text and "archive_dir" not in text
        evidence = json.loads(text)
        assert evidence["run_id"] == first["run_id"]
        assert evidence["bench"]["live_capital_eligible"] is False
        parsed = card.parse(archive.read("README.md").decode())
        assert parsed["fields"]["evaluation_id"] == evidence["bench"]["evaluation_id"]
    with pytest.raises(ValueError, match="does not belong"):
        evidence_export.build(Path(first["output"]), b)
    with pytest.raises(ValueError, match="does not belong"):
        evidence_export.build(Path(second["output"]), a)


def test_paired_comparison_reports_cost_turnover_and_rejections(research):
    root, _, _, _, _, a, b = research
    result = compare_models(a, b, root / "comparison")
    assert result["signal_decision"]["scope"] == "paired_rank_ic_only"
    assert result["lineage"]["old"]["run_id"] != result["lineage"]["new"]["run_id"]
    operations = result["economic_comparison"]["results"]["extra_0bp_per_side"]["operational_deltas"]
    for key in ("commission", "mean_one_way_turnover", "rejected_orders"):
        assert operations[key]["delta"] == operations[key]["new"] - operations[key]["old"]
    assert "交易诊断" in (root / "comparison" / "report.md").read_text(encoding="utf-8")


def test_ui_history_bench_comparison_and_download(research, monkeypatch):
    root, first, second, _, _, a, _ = research
    monkeypatch.setattr(ui, "hardware", lambda: {"cpu": "fixture", "cores": 2, "mem_gb": 8, "gpus": []})
    app = ui.create_app(root, runner=lambda fn: fn())
    client = TestClient(app)
    token = app.state.token
    assert "same_name" in client.get("/experiments").text
    assert "独立 Bench" in client.get(f'/experiments/{first["run_id"]}').text
    assert client.post(f'/experiments/{first["run_id"]}/bench', data={"token": "bad"}).status_code == 403
    frozen = client.post(f'/experiments/{first["run_id"]}/protocol', data={
        "token": token, "block_days": "2", "resamples": "100", "min_days": "8"}, follow_redirects=False)
    assert frozen.status_code == 303
    assert "返回训练版本" in client.get(frozen.headers["location"]).text
    queued = client.post(f'/experiments/{first["run_id"]}/bench', data={"token": token, "protocol": "fixture.yaml"},
                         follow_redirects=False)
    assert queued.status_code == 303
    assert "查看评价结果" in client.get(queued.headers["location"]).text
    evaluation_id = experiments.list_evaluations(root, first["run_id"])[0]["evaluation_id"]
    assert "对应训练版本" in client.get(f"/evaluations/{evaluation_id}").text
    bundle = client.get(f'/experiments/{first["run_id"]}/evidence.zip?evaluation_id={evaluation_id}')
    assert bundle.status_code == 200 and bundle.content.startswith(b"PK")
    assert "旧版本" in client.get("/experiments/compare").text
    comparison = client.post("/experiments/compare", data={"token": token, "old": evaluation_id, "new": evaluation_id},
                             follow_redirects=False)
    assert comparison.status_code == 303
    assert "查看评价结果" in client.get(comparison.headers["location"]).text
    assert client.get("/experiments/not-a-run").status_code == 404
    assert client.get("/evaluations/../../secret").status_code == 404
    assert client.get(f'/models/{second["run_id"]}/scores.csv').status_code == 200


def test_legacy_import_does_not_rewrite_source(research, tmp_path):
    import shutil
    _, first, *_ = research
    legacy = tmp_path / "legacy"
    shutil.copytree(first["output"], legacy)
    (legacy / "run.json").unlink()
    before = experiments.inventory(legacy)
    record = experiments.import_legacy(legacy, tmp_path)
    assert record["origin"] == "legacy_import" and record["code_hash"] is None
    assert record["original_created_at"] is None
    assert experiments.inventory(legacy) == before
    assert experiments.verify(Path(record["archive_dir"]))["run_id"] == record["run_id"]


def test_changed_prediction_or_manifest_is_rejected(research, tmp_path):
    import shutil
    _, first, *_ = research
    copied = tmp_path / "tampered"
    shutil.copytree(first["output"], copied)
    (copied / "predictions.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="training artifact changed"):
        evidence_export.build(copied)


def test_latest_from_another_workspace_is_preserved_before_rerun(research, tmp_path, monkeypatch):
    import shutil
    _, _, second, resolved, *_ = research
    latest = tmp_path / resolved["output"]
    shutil.copytree(second["output"], latest)
    def fail(*args, **kwargs):
        raise RuntimeError("stop after preservation")
    monkeypatch.setattr(pipeline, "_compute", fail)
    with pytest.raises(RuntimeError, match="stop after preservation"):
        pipeline.run(resolved, tmp_path, log=lambda *_: None)
    preserved = experiments.run_dir(tmp_path, second["run_id"])
    assert experiments.verify(preserved)["run_id"] == second["run_id"]


def test_invalid_evaluation_keeps_verifiable_lineage(research, tmp_path):
    from qwb.bench.contracts import read_yaml
    _, first, _, _, protocol, *_ = research
    changed = read_yaml(protocol)
    changed["data"]["dir"] = str(tmp_path / "missing-data")
    new_protocol = tmp_path / "bad.yaml"
    new_protocol.write_text(yaml.safe_dump(changed), encoding="utf-8")
    out = tmp_path / "invalid"
    score = evaluate_model(first["output"], new_protocol, out, log=lambda *_: None)
    assert score["status"] == "INVALID" and score["lineage"]["run_id"] == first["run_id"]
    assert verify_result(out)["result_status"] == "INVALID"


def test_current_schema_preserves_adv_and_older_schemas_keep_original_universe(research, tmp_path):
    import copy
    import shutil
    from qwb.bench.contracts import validate_protocol
    _, first, *_ = research
    legacy = tmp_path / "anchor"
    shutil.copytree(first["output"], legacy)
    (legacy / "run.json").unlink()
    config_path = legacy / "config.resolved.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["cfg"]["min_adv_yuan"] = 2e8
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    protocol = create_protocol(legacy, tmp_path / "protocol.yaml", cwd=tmp_path, resamples=100)
    assert protocol["schema_version"] == 5 and protocol["universe"]["min_adv_yuan"] == 2e8
    previous = copy.deepcopy(protocol)
    previous["schema_version"] = 4
    previous["anchor"]["files"].pop("data_manifest.json", None)
    validate_protocol(previous)
    old = copy.deepcopy(protocol)
    old["schema_version"] = 3
    old["anchor"]["files"].pop("data_manifest.json", None)
    del old["universe"]["min_adv_yuan"]
    validate_protocol(old)


def test_worker_failure_retry_and_live_recovery_protection(tmp_path, monkeypatch):
    store = Store(tmp_path / "runs" / "jobs")
    jid = store.submit(JobSpec("train", {"action": "train", "resolved": {}}))
    def fail(ctx, cfg, workdir, **kwargs):
        with pytest.raises(ValueError, match="another run"):
            store.recover(jid)
        raise RuntimeError("worker failure")
    monkeypatch.setattr(worker, "execute", fail)
    assert worker.run_job(tmp_path, jid) == "failed"
    store.retry(jid)
    monkeypatch.setattr(worker, "execute", lambda *args, **kwargs: None)
    assert worker.run_job(tmp_path, jid) == "succeeded"


def test_detached_worker_finishes_without_a_ui_process(research, tmp_path):
    import time
    _, _, _, resolved, *_ = research
    store = Store(tmp_path / "runs" / "jobs")
    job_id = store.submit(JobSpec("train", {"action": "train", "resolved": resolved}))
    process_id = worker.launch(tmp_path, job_id)
    deadline = time.monotonic() + 90
    while store.status(job_id)["state"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(.25)
    state = store.status(job_id)
    assert state["state"] == "succeeded", (process_id, state, (store.root / job_id / "worker.log").read_text())
    record = experiments.list_runs(tmp_path)[0]
    assert record["job_id"] == job_id and record["state"] == "succeeded"


def test_runs_cli_and_export_refuses_overwrite(research, monkeypatch, capsys):
    root, first, _, _, _, a, _ = research
    monkeypatch.chdir(root)
    assert cli.main(["runs", "list", "--json"]) == 0
    assert first["run_id"] in capsys.readouterr().out
    assert cli.main(["runs", "verify", first["run_id"]]) == 0
    output = root / "public-evidence.zip"
    assert cli.main(["runs", "export", first["run_id"], "--evaluation", str(a), "--output", str(output)]) == 0
    assert cli.main(["runs", "export", first["run_id"], "--output", str(output)]) == 1


def test_imported_legacy_run_is_listed_once(research, tmp_path, monkeypatch):
    import shutil
    _, first, *_ = research
    work = tmp_path / "work"
    legacy = work / "runs" / "old_model"
    shutil.copytree(first["output"], legacy)
    (legacy / "run.json").unlink()
    experiments.import_legacy(legacy, work)
    monkeypatch.setattr(ui, "hardware", lambda: {"cpu": "fixture", "cores": 2, "mem_gb": 8, "gpus": []})
    page = TestClient(ui.create_app(work, runner=lambda fn: fn())).get("/train").text
    section = page.split("已训练模型", 1)[1].split("</ul>", 1)[0]
    assert section.count("<li") == 1
