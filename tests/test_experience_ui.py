"""Navigation must expose real prerequisites without modifying economic settings."""
from fastapi.testclient import TestClient

from qwb import experience_ui, experiments as ex, ui
from test_daily_strategy import fixture


def test_empty_workspace_has_actionable_start_and_no_fake_official_model(tmp_path):
    app = ui.create_app(tmp_path, runner=lambda fn: None)
    client = TestClient(app)
    page = client.get('/start')
    assert page.status_code == 200
    assert '开始训练' in page.text and '用现成的模型' in page.text
    assert 'href="/train#free-data"' in page.text
    assert page.text.index('自己训练一个模型') < page.text.index('下载免费日线')
    assert '全部策略 →' not in page.text
    assert '统一插件安装仍在规划' in page.text
    signal = client.get('/strategies/new').text
    assert '需要兼容的五日模型' in signal
    assert '<button disabled>下一步' in signal
    assert '还没有兼容模型' in signal and '还没有行情数据' in signal
    assert client.get('/static/experience.js').status_code == 200
    assert client.get('/static/experience.css').status_code == 200
    assert '<dialog id="workspace-help"' in page.text
    assert '每一步都能深入' not in page.text


def test_metadata_discovery_respects_hidden_and_incompatible_models(tmp_path, monkeypatch):
    run, _ = fixture(tmp_path, monkeypatch)
    # Discovery requires an archived prediction table, but never executes it.
    (run / 'predictions.parquet').write_bytes(b'metadata discovery only')
    meta = ex.read(run / 'final_model/meta.json')
    meta['run_id'] = 'same-training-identity'
    ex.write(run / 'final_model/meta.json', meta)
    archive = tmp_path / 'runs/history/archive'
    (archive / 'final_model').mkdir(parents=True)
    ex.write(archive / 'final_model/meta.json', meta)
    ex.write(archive / 'run.json', {'name': 'model'})
    (archive / 'predictions.parquet').write_bytes(b'archived copy')
    assert len(experience_ui.signal_options(tmp_path)) == 1
    assert experience_ui.signal_options(tmp_path)['model']['model'] == 'ridge'
    marker = tmp_path / 'runs/workflow/hidden/model.json'
    marker.parent.mkdir(parents=True)
    ex.write(marker, {'deleted_at': 'test'})
    assert experience_ui.signal_options(tmp_path) == {}
    marker.unlink()
    meta = ex.read(run / 'final_model/meta.json')
    meta['card']['label']['end']['day'] = 2
    ex.write(run / 'final_model/meta.json', meta)
    assert experience_ui.signal_options(tmp_path) == {}


def test_corrupt_project_does_not_block_other_onboarding(tmp_path):
    root = tmp_path / 'runs/strategies/broken'
    (root / 'versions/one').mkdir(parents=True)
    ex.write(root / 'identity.json', {'name': 'broken'})
    ex.write(root / 'versions/one/spec.json', {'name': '<script>bad</script>'})
    app = ui.create_app(tmp_path, runner=lambda fn: None)
    page = TestClient(app).get('/start')
    assert page.status_code == 200 and '部分策略元数据无法读取' in page.text
    assert '<script>bad</script>' not in page.text


def test_flow_has_one_current_step_and_no_unavailable_future_link():
    value = experience_ui.flow(3, '/strategies/a/b')
    assert value.count('aria-current="step"') == 1
    assert 'href="/strategies/a/b/rules"' in value
    assert 'href="/strategies/a/b#running"' not in value
    assert '<small>' not in value


def test_model_stage_uses_recorded_signal_evidence_not_account_returns():
    value=experience_ui.signal_report({'model_key':'ridge','signal_screen':{'status':'NOT_ESTABLISHED'},
        'ic':{'ridge':{'final_test':{'mean':.02,'t':.5,'n':100}}},
        'net_of_cost':{'signals':{'ridge':{'net_excess_ann':.9}}},'ledger':{'views':5}})
    assert '排序能力 · 暂不能判断' in value and '+0.020' in value
    assert '本地测试期查看 5 次' in value
    assert '净超额' not in value and '90%' not in value


def test_running_old_version_remains_reachable_beside_new_draft(tmp_path):
    import os
    family = tmp_path / 'runs/strategies' / ('a'*32)
    old = family / 'versions' / ('b'*32)
    new = family / 'versions' / ('c'*32)
    for root, name in ((old, '正在观察的版本'), (new, '新的设置草稿')):
        root.mkdir(parents=True)
        ex.write(root / 'spec.json', {'sid': family.name, 'vid': root.name, 'name': name})
    ex.write(family / 'identity.json', {'name': '策略'})
    ex.write(old / 'runtime.json', {'scope': 'historical_replay'})
    os.utime(old / 'spec.json', (1, 1))
    app = ui.create_app(tmp_path, runner=lambda fn: None)
    value = TestClient(app).get('/start').text
    assert '正在观察的版本' in value and '新的设置草稿' in value
    assert '历史演示' in value
