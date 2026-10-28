from dataclasses import replace
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from qwb import audit_input, strategy_audit as audit, strategy_imports as imp
from qwb import strategy_audit_ui as view, strategy_audit_store as store, ui


def joinquant_files(*, code=True):
    # The vendor header omits 总资产 before the final weight column.
    text = ('日期,品种,标的,多空,数量,可用数量,收盘价/结算价,市值/价值,仓位占比\n'
            '2024-01-02,股票,合成甲(600000.XSHG),多,100股,0股,10,1000,10000,10%\n'
            '2024-01-02,,Cash,,,,,9000,,0\n'
            '2024-01-03,股票,合成甲(600000.XSHG),多,200股,100股,11,2200,10100,22%\n'
            '2024-01-03,,Cash,,,,,7900,,0\n')
    files = [imp.SourceFile('原始持仓.csv', text.encode('gb18030'))]
    if code:
        files.append(imp.SourceFile('策略.py', b'capital_base=10000\n'))
    return files


def test_joinquant_encoding_header_and_net_changes_preserve_unknown_execution():
    data = imp.normalize(joinquant_files())
    assert data['adapter'] == 'joinquant_v1'
    assert data['quality']['holdings_header_repaired']
    assert data['sources'][0]['encoding'] == 'gb18030'
    assert len(data['snapshots']) == 2
    assert data['snapshots'][0]['equity'] == 10000
    assert len(data['fills']) == 1
    fill = data['fills'][0]
    assert fill['date'] == '2024-01-03' and fill['quantity'] == 100
    assert fill['inferred'] and fill['fee_reported'] is False
    result = audit.analyze(data)
    assert result['mode'] == 'partial'
    assert result['modules']['partial_diagnostics']['reported_fees'] is None
    assert result['modules']['partial_diagnostics']['net_pnl'] is None
    assert result['input_review']['fills_inferred']


def test_missing_code_previews_but_cannot_start_financial_audit():
    data = imp.normalize(joinquant_files(code=False))
    assert audit_input.review(data)['can_analyze'] is False
    with pytest.raises(ValueError, match='策略代码'):
        audit.analyze(data)


def test_code_only_is_a_static_check_and_never_executes(tmp_path):
    target = tmp_path / 'must-not-exist'
    source = f"open({str(target)!r}, 'w').write('bad')\ncapital_base=1000000\n"
    data = imp.normalize([imp.SourceFile('strategy.py', source.encode())])
    result = audit.analyze(data)
    assert result['mode'] == 'static'
    assert result['input_review']['platform'] == '未识别平台'
    assert result['scope'] == 'static_code_only'
    assert result['modules']['partial_diagnostics']['fills'] == []
    assert not target.exists()
    parameters = {p['key']: p for p in result['input_review']['parameters']}
    assert parameters['commission_bp']['value'] == 3.


def test_generic_fills_without_holdings_can_audit_fees_without_faking_equity():
    files = [imp.SourceFile('strategy.py', b'capital_base=10000\n'),
             imp.SourceFile('trades.csv', b'date,code,side,quantity,price,fee\n2024-01-02,600000.XSHG,buy,100,10,5\n')]
    data = imp.normalize(files)
    result = audit.analyze(data)
    assert data['fills'][0]['code'] == '600000.SH'
    assert result['mode'] == 'partial'
    assert result['modules']['partial_diagnostics']['reported_fees'] == 5
    assert result['modules']['partial_diagnostics']['drawdown'] is None


def test_code_period_warning_and_fee_prefill_have_line_provenance():
    files = [f for f in view.sample_files() if not f.name.endswith('.py')]
    files.append(imp.SourceFile('strategy.py', b"start_date='2023-01-01'\nend_date='2025-12-31'\nbuy_cost=.0002\nmin_cost=6\nhold_days=2\n"))
    review = audit_input.review(imp.normalize(files))
    checks = {c['key']: c for c in review['checks']}
    assert checks['period']['status'] == 'review'
    assert '2024-01-02' in checks['period']['detail']
    params = {p['key']: p for p in review['parameters']}
    assert params['commission_bp']['value'] == 2
    assert params['commission_bp']['evidence'][0]['line'] == 3
    assert not params['commission_bp']['confirmed']
    assert params['minimum_commission']['value'] == 6
    assert '用途待核' in params['holding_days']['label']


def test_joinquant_short_cash_row_keeps_original_weights_and_asset_column():
    files = joinquant_files()
    files[0] = replace(files[0], data=files[0].data.replace(b'9000,,0', b'9000,0').replace(b'7900,,0', b'7900,0'))
    data = imp.normalize(files)
    assert data['snapshots'][0]['cash'] == 9000
    assert data['snapshots'][0]['equity'] == 10000


def test_negative_export_cash_is_preserved_and_flagged():
    files = joinquant_files()
    files[0] = replace(files[0], data=files[0].data.replace(b'9000,,0', b'-1,,0'))
    data = imp.normalize(files)
    assert data['snapshots'][0]['cash'] == -1
    assert next(c for c in audit_input.review(data)['checks'] if c['key'] == 'negative_cash')['status'] == 'review'


def test_files_are_immutable_across_four_pages_and_legacy_addresses(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, 'hardware', lambda: {})
    app = ui.create_app(tmp_path, runner=lambda fn: None)
    c = TestClient(app)
    response = c.post('/strategy-audits/import', data={'token': app.state.token, 'name': '测试策略', 'adapter': 'auto'},
                      files=[('files', (f.name, f.data)) for f in joinquant_files()], follow_redirects=False)
    assert response.status_code == 303
    identity = response.headers['location'].rsplit('/', 1)[1]
    directory = store.root(tmp_path, identity)
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    for _, route, label in view.TABS:
        page = c.get(route + '?strategy=' + identity)
        assert page.status_code == 200
        assert 'id="current-strategy"' in page.text
        assert page.text.count('?strategy=' + identity) >= len(view.TABS)
        assert '聚宽 · 测试策略 · v1' in page.text
    after = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert before == after
    assert c.get('/review').status_code == 200
    assert c.get('/').url.path == '/strategy-governance'
    assert c.get('/settings').status_code == 200
    assert not (tmp_path / 'runs/paper').exists()


def test_my_strategies_four_actions_rerun_only_confirmed_parameters(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, 'hardware', lambda: {})
    app = ui.create_app(tmp_path, runner=lambda fn: None); c = TestClient(app)
    files = view.sample_files(); bundle = imp.normalize(files, opening_cash=10000)
    identity = store.save(tmp_path, bundle, files)
    path = '/strategy-audits/'+identity
    assert c.post(path+'/rerun', data={'token': 'bad'}).status_code == 403
    assert c.post(path+'/rerun', data={'token': app.state.token}, follow_redirects=False).headers['location'] == path
    from qwb.experiments import object_digest
    c.post(path+'/analyze', data={'token': app.state.token, 'input_digest': object_digest(bundle),
        'same_run': 'yes', 'commission_bp': '2', 'minimum_commission': '6', 'max_weight': '40',
        'minimum_triggers': '30', 'validation_fraction': '30', 'purge_days': '25'})
    records = store.root(tmp_path, identity) / 'reports'
    before = {p.name for p in records.iterdir()}
    listing = c.get('/strategy-audits').text
    for name in ('一键审计', '一键归因', '效果验证'):
        assert name in listing
    assert '一键模拟' not in listing
    for route in ('strategy-attribution', 'strategy-comparison'):
        assert f'/{route}?strategy={identity}' in listing
    response = c.post(path+'/rerun', data={'token': app.state.token}, follow_redirects=False)
    assert response.headers['location'] == '/strategy-governance?strategy='+identity
    created = {p.name for p in records.iterdir()} - before
    assert len(created) == 1
    _, _, result = store.report(tmp_path, identity, created.pop())
    assert result['settings'] == {'commission_bp': 2., 'minimum_commission': 6., 'max_weight': .4, 'broker_confirmed': False}
    assert result['period_review']['settings'] == {'validation_fraction': .3, 'minimum_triggers': 30, 'purge_days': 25}
    assert not (tmp_path / 'runs/paper').exists()


def test_pasted_code_and_missing_code_gates_are_enforced_on_server(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, 'hardware', lambda: {})
    app = ui.create_app(tmp_path, runner=lambda fn: None)
    c = TestClient(app)
    response = c.post('/strategy-audits/import', data={'token': app.state.token, 'name': '静态',
        'adapter': 'auto', 'code_text': 'capital_base=1000000'}, follow_redirects=False)
    assert response.status_code == 303
    identity = response.headers['location'].rsplit('/', 1)[1]
    _, data = store.read(tmp_path, identity)
    from qwb.experiments import object_digest
    report = c.post(response.headers['location'] + '/analyze', data={'token': app.state.token,
        'input_digest': object_digest(data), 'same_run': 'yes', 'commission_bp': '3',
        'minimum_commission': '5', 'max_weight': '50'})
    assert report.status_code == 200
    assert '代码静态检查' in report.text
    evidence = c.get(report.url.path + '/evidence.json').json()
    assert evidence['result']['mode'] == 'static'
    assert 'accounting' not in evidence['result']['modules']
    assert c.get(report.url.path + '/stocks.csv').status_code == 404


def test_import_hub_templates_supplement_confirmation_and_shared_results(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, 'hardware', lambda: {})
    app = ui.create_app(tmp_path, runner=lambda fn: None)
    c = TestClient(app)
    response = c.post('/strategy-audits/import', data={'token': app.state.token, 'name': '逐步补齐'},
                      files=[('files', (f.name, f.data)) for f in joinquant_files(code=False)], follow_redirects=False)
    path = response.headers['location']; identity = path.rsplit('/', 1)[-1]
    _, bundle = store.read(tmp_path, identity)
    original_files = {p.name: p.read_bytes() for p in store.root(tmp_path, identity).iterdir() if p.is_file()}
    preview = c.get(path).text
    assert '策略导入 · 核对与补齐' in preview
    assert f'{path}#material-code' in preview and f'{path}/templates/code.zip' in preview
    assert 'name="kind" value="code"' in preview
    draft = c.get('/strategy-governance?strategy='+identity).text
    assert '导入尚未确认' in draft
    assert 'id="input-consistency"' not in draft
    assert 'name="files"' not in draft
    assert '我的策略' in draft and '策略导入' in draft
    download = c.get(path+'/templates/positions.zip')
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert '总资产' in archive.read('positions.csv').decode('utf-8-sig')
        assert 'Cash' in archive.read('填写说明.txt').decode('utf-8-sig')
    with zipfile.ZipFile(io.BytesIO(c.get('/strategy-audits/templates/fills.zip?adapter=bigquant').content)) as archive:
        assert '交易费用' in archive.read('fills.csv').decode('utf-8-sig')
    assert c.get('/strategy-audits/templates/fills.zip?adapter=unknown').status_code == 404
    from qwb.experiments import object_digest
    fields = {'token': app.state.token, 'input_digest': object_digest(bundle), 'kind': 'code',
              'code_text': 'capital_base=10000\n'}
    assert c.post(path+'/materials', data={**fields, 'token': 'bad'}).status_code == 403
    assert c.post(path+'/materials', data={**fields, 'input_digest': 'bad'}).status_code == 400
    update = c.post(path+'/materials', data=fields, follow_redirects=False)
    assert update.status_code == 303, update.text
    updated_path = update.headers['location']; updated = updated_path.rsplit('/', 1)[-1]
    record, new_bundle = store.read(tmp_path, updated)
    assert record['parent_id'] == identity and record['strategy_id'] == identity
    assert record['version'] == 2
    assert new_bundle['positions'] == bundle['positions']
    assert store.source_files(tmp_path, updated)[0].data == joinquant_files(code=False)[0].data
    assert [r['id'] for r in store.history(tmp_path, latest_only=True)] == [updated]
    assert len(store.history(tmp_path)) == 2
    assert original_files == {p.name: p.read_bytes() for p in store.root(tmp_path, identity).iterdir() if p.is_file()}
    assert '导入尚未确认' in c.get('/strategy-governance?strategy='+updated).text
    confirmed = c.post(updated_path+'/analyze', data={'token': app.state.token, 'input_digest': object_digest(new_bundle),
        'same_run': 'yes', 'commission_bp': '3', 'minimum_commission': '5', 'max_weight': '50',
        'destination': 'workspace'}, follow_redirects=False)
    assert confirmed.headers['location'] == '/strategy-governance?strategy='+updated
    governance = c.get(confirmed.headers['location']).text
    assert '本次审计发现' in governance and '未知，不按0元处理' in governance
    assert 'id="input-consistency"' not in governance and 'name="files"' not in governance
    for _, route, _ in view.TABS:
        page = c.get(route+'?strategy='+updated)
        assert page.status_code == 200
        assert f'value="{updated}" selected' in page.text
        assert '聚宽 · 逐步补齐 · v2' in page.text
    assert not (tmp_path / 'runs/paper').exists()


def test_supplement_rejects_wrong_category_and_retains_old_report(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, 'hardware', lambda: {})
    app = ui.create_app(tmp_path, runner=lambda fn: None); c = TestClient(app)
    files = view.sample_files()
    bundle = imp.normalize(files, opening_cash=10000)
    identity = store.save(tmp_path, bundle, files)
    result = audit.analyze(bundle)
    report_id = store.save_report(tmp_path, identity, result)
    path = '/strategy-audits/'+identity
    from qwb.experiments import object_digest
    form = {'token': app.state.token, 'input_digest': object_digest(bundle), 'kind': 'scores'}
    invalid = c.post(path+'/materials', data=form, files={'files': ('logs.csv', b'\xe6\x97\xb6\xe9\x97\xb4,\xe7\xba\xa7\xe5\x88\xab,\xe5\x86\x85\xe5\xae\xb9\n2024-01-03,INFO,test\n')})
    assert invalid.status_code == 400 and '文件内容与所选材料不一致' in invalid.text
    empty = c.post(path+'/materials', data=form, files={'files': ('scores.csv', b'date,code,score\n')})
    assert empty.status_code == 400 and '尚未填写' in empty.text
    response = c.post(path+'/materials', data=form,
        files={'files': ('scores.csv', b'date,code,score,available_at\n2024-01-03,000001.SZ,0.8,2024-01-03T08:00:00+08:00\n')}, follow_redirects=False)
    assert response.status_code == 303, response.text
    updated = response.headers['location'].rsplit('/', 1)[-1]
    _, new_bundle = store.read(tmp_path, updated)
    assert len(new_bundle['scores']) == 1
    assert new_bundle['opening'] == bundle['opening']
    assert store.report(tmp_path, identity, report_id)[2] == result
    assert len(store.history(tmp_path)) == 2
    with zipfile.ZipFile(io.BytesIO(c.get(path+'/templates/fills.zip').content)) as archive:
        assert '交易费用' in archive.read('fills.csv').decode('utf-8-sig')


def test_joinquant_real_trade_supplement_replaces_inference():
    from qwb import audit_materials as materials
    files = joinquant_files()
    bundle = imp.normalize(files)
    actual = imp.SourceFile('actual.csv', b'date,code,side,quantity,price,fee\n2024-01-03,600000.SH,buy,100,11,5\n')
    _, updated = materials.replace_files(files, bundle, [actual], 'fills')
    assert not updated['quality']['fills_inferred']
    assert updated['fills'][0]['fee'] == 5
    assert len(updated['positions']) == len(bundle['positions'])
