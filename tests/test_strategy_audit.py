from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from qwb import experiments as ex, strategy_audit as audit, strategy_audit_store as store
from qwb import strategy_audit_ui as view, strategy_imports as imp, ui
from qwb.audit_catalog import STAGES


def bundle(*, cash=10000):
    return imp.normalize(view.sample_files(), name='Synthetic audit', opening_cash=cash)


def change_file(files, name, old, new):
    return [replace(f, data=f.data.replace(old.encode(), new.encode())) if f.name == name else f for f in files]


def test_merged_dates_sorted_after_fill_and_summaries_excluded():
    data = bundle()
    assert len(data['fills']) == 3 and len(data['snapshots']) == 2
    assert len(data['positions']) == 3
    assert data['fills'][0]['date'] == data['fills'][1]['date'] == '2024-01-02'
    assert data['fills'][1]['ref'] == {'source': 'fills', 'row': 4}
    assert len(data['sources']) == 4


def test_closed_and_open_positions_reconcile_without_realized_pnl_column():
    result = audit.analyze(bundle())
    account, attr = result['modules']['accounting'], result['modules']['attribution']
    assert account['status'] == attr['status'] == 'verified'
    assert not account['exceptions']
    assert attr['net_pnl'] == pytest.approx(183.9)
    stocks = {r['code']: r for r in attr['stocks']}
    assert stocks['600000.SH']['net_pnl'] == pytest.approx(88.9)
    assert stocks['000001.SZ']['net_pnl'] == pytest.approx(95)
    assert sum(r['net_pnl'] for r in account['daily']) == pytest.approx(183.9)
    assert attr['fees'] == pytest.approx(16.1)


def test_unknown_opening_uses_first_close_excludes_first_day_activity():
    result = audit.analyze(bundle(cash=None))
    account = result['modules']['accounting']
    assert account['status'] == 'verified'
    assert account['baseline'] == 'first_exported_close'
    assert [d['date'] for d in account['daily']] == ['2024-01-03']
    assert result['modules']['attribution']['net_pnl'] == pytest.approx(193.9)


def test_single_snapshot_without_opening_never_claims_verified_pnl():
    data = bundle(cash=None)
    data['snapshots'] = data['snapshots'][:1]
    day = data['snapshots'][0]['date']
    for key in ('fills', 'positions', 'actions'):
        data[key] = [r for r in data[key] if r['date'] == day]
    result = audit.analyze(data)
    assert result['modules']['attribution']['status'] == 'needs_reconciliation'


def test_company_action_share_and_cash_close_both_bridges():
    files = view.sample_files()
    files = change_file(files, 'holdings.csv', '000001.SZ,100,20,21,2100', '000001.SZ,110,20,21,2310')
    files = change_file(files, 'holdings.csv', '10183.9,现金: 8083.9,2100', '10498.9,现金: 8188.9,2310')
    files = change_file(files, 'logs.csv', 'old=100,now=100,cash=0', 'old=100,now=110,cash=105')
    data = imp.normalize(files, opening_cash=10000)
    result = audit.analyze(data)
    assert result['modules']['accounting']['status'] == 'verified'
    assert result['modules']['attribution']['net_pnl'] == pytest.approx(498.9)
    data['actions'] = []
    broken = audit.analyze(data)['modules']['accounting']
    assert broken['status'] == 'needs_reconciliation'
    assert {'inventory', 'cash_flow'} <= {e['key'] for e in broken['exceptions']}


def test_cash_gap_and_quantity_gap_do_not_get_hidden_by_balanced_equity():
    data = bundle()
    data['snapshots'][-1]['cash'] += 10
    data['snapshots'][-1]['equity'] += 10
    result = audit.analyze(data)
    assert result['modules']['attribution']['status'] == 'needs_reconciliation'
    assert 'cash_flow' in {e['key'] for e in result['modules']['accounting']['exceptions']}


def test_minimum_fee_scenario_does_not_change_actual_fees_or_claim_optimal():
    result = audit.analyze(bundle())
    gov = result['modules']['governance']
    assert gov['scenarios'][0]['value'] == pytest.approx(13.77)
    assert gov['scenarios'][0]['scope'] == 'same_fills_fee_scenario'
    assert result['modules']['attribution']['fees'] == pytest.approx(16.1)
    assert 'capabilities' not in result
    assert result['coverage_version'] == 'audit_coverage_v3'
    assert next(c for c in result['coverage'] if c['id'] == 'costs')['optimization_status'] == 'conditional_fee_scenario'


def test_static_code_never_executes_or_activates_literals(tmp_path):
    destination = tmp_path / 'must-not-exist'
    code = f"open({str(destination)!r}, 'w').write('bad')\ncontext.stock_count=2\n"
    result = imp.inspect_code(code)
    assert not destination.exists()
    assert result['candidates'][0]['scope'] == 'unconfirmed_literal'
    assert result['candidates'][0]['line'] == 2
    assert imp.inspect_code('invalid python )')['status'] == 'unparsed'


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', 'not-a-number'])
def test_bad_numeric_fails_closed(value):
    files = change_file(view.sample_files(), 'trades.csv', ',100,11,1100,6.1', f',100,11,1100,{value}')
    with pytest.raises(ValueError):
        imp.normalize(files)


def test_rejects_unknown_material_missing_dates_duplicate_snapshots_and_paths():
    with pytest.raises(ValueError, match='表头'):
        imp.normalize([imp.SourceFile('unknown.csv', b'a,b\n1,2')])
    files = change_file(view.sample_files(), 'holdings.csv', '2024-01-03', '2024-01-04')
    with pytest.raises(ValueError, match='缺少对应'):
        imp.normalize(files)
    files = change_file(view.sample_files(), 'holdings.csv', '2024-01-03', '2024-01-02')
    with pytest.raises(ValueError, match='重复'):
        imp.normalize(files)
    files = [replace(view.sample_files()[0], name='../trades.csv')]
    with pytest.raises(ValueError, match='文件名'):
        imp.normalize(files)
    with pytest.raises(ValueError, match='8MB'):
        imp.normalize([imp.SourceFile('large.csv', b'x'*(imp.MAX_FILE_BYTES+1))])
    with pytest.raises(ValueError, match='CSV 格式'):
        imp.normalize([imp.SourceFile('broken.csv', b'one,two\n"unfinished,quote')])


def test_external_attribution_is_preserved_but_never_silently_verified():
    files = view.sample_files() + [imp.SourceFile('attribution.csv', '行业名称,组合收益率,基准收益率\n行业A,100,20\n'.encode())]
    data = imp.normalize(files, opening_cash=10000)
    result = audit.analyze(data)
    assert data['external_attribution'][0]['组合收益率'] == '100'
    assert result['modules']['attribution']['external_report_status'] == 'unverified_external'
    assert result['modules']['attribution']['net_pnl'] == pytest.approx(183.9)


def test_settings_change_report_digest_not_accounting():
    data = bundle()
    first = audit.analyze(data)
    second = audit.analyze(data, {'commission_bp': 3., 'minimum_commission': 0., 'max_weight': .1})
    assert first['input_digest'] == second['input_digest']
    assert first['result_digest'] != second['result_digest']
    assert first['modules']['accounting'] == second['modules']['accounting']
    assert second['modules']['governance']['items'][0]['status'] == 'review'
    with pytest.raises(ValueError):
        audit.analyze(data, {'commission_bp': float('nan'), 'minimum_commission': 5, 'max_weight': .5})


def test_all_stages_have_actions_units_and_honest_implementation_status():
    result = audit.analyze(bundle())
    cards = {c['id']: c for c in result['coverage']}
    assert len(cards) == len(STAGES) == 15
    assert cards['account_integrity']['status'] == 'verified'
    assert cards['rebalance']['status'] == 'partial'
    assert cards['execution']['status'] == 'partial'
    for key in ('capacity', 'completion', 'signal_age'):
        assert cards[key]['status'] == 'not_implemented'
        assert cards[key]['metrics'] == []
    for card in cards.values():
        assert card['candidate_action'] and card['tradeoff'] and card['needs']
        assert card['optimization_status'] == ('conditional_fee_scenario' if card['id'] == 'costs' else 'not_replayed')
        assert card['algorithms']
        assert all({'label', 'value', 'unit'} == set(m) for m in card['metrics'])
    cap = cards['risk_exposure']['metrics'][0]
    assert cap['value'] == pytest.approx(2100/10183.9)
    assert cards['costs']['metrics'][0]['value'] == pytest.approx(16.1)
    assert result == audit.analyze(bundle())


def test_bad_account_is_not_a_verified_stage():
    data = bundle()
    data['snapshots'][-1]['cash'] += 10
    result = audit.analyze(data)
    card = next(c for c in result['coverage'] if c['id'] == 'account_integrity')
    assert card['status'] == 'review'
    assert card['metrics'][-1]['value'] > 0


def test_store_append_only_and_tamper_detection(tmp_path):
    files = view.sample_files(); data = bundle()
    identity = store.save(tmp_path, data, files)
    result = audit.analyze(data)
    a = store.save_report(tmp_path, identity, result)
    b = store.save_report(tmp_path, identity, result)
    assert a != b
    assert store.report(tmp_path, identity, a)[2] == result
    raw = store.root(tmp_path, identity) / 'source-0.bin'
    raw.write_bytes(raw.read_bytes()+b'\n')
    with pytest.raises(ValueError, match='摘要'):
        store.report(tmp_path, identity, a)
    assert not store.history(tmp_path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, 'hardware', lambda: {})
    app = ui.create_app(tmp_path, runner=lambda fn: None)
    return app, TestClient(app), tmp_path


def upload(client, *, name='Synthetic audit', token=None):
    app, c, _ = client
    return c.post('/strategy-audits/import', data={
        'token': token if token is not None else app.state.token, 'name': name,
        'adapter': 'bigquant', 'opening_empty': 'yes', 'opening_cash': '10000'},
        files=[('files', (f.name, f.data, 'text/plain')) for f in view.sample_files()], follow_redirects=False)


def run_report(client):
    app, c, workdir = client
    response = upload(client)
    assert response.status_code == 303, response.text
    path = response.headers['location']; identity = path.rsplit('/', 1)[1]
    _, data = store.read(workdir, identity)
    response = c.post(path+'/analyze', data={'token': app.state.token, 'input_digest': ex.object_digest(data),
        'same_run': 'yes', 'commission_bp': '3', 'minimum_commission': '5', 'max_weight': '50'}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return path, response.headers['location']


def test_web_full_import_inspect_report_export_and_handoff(client):
    app, c, workdir = client
    assert c.get('/strategy-audits/new').status_code == 200
    path, report = run_report(client)
    assert '代码中识别的参数候选' in c.get(path).text
    assert '已有报告' in c.get(path).text
    page = c.get(report)
    assert page.status_code == 200 and '账目已核对' in page.text
    assert '183.90' in page.text and '治理诊断与候选动作' in page.text
    assert page.text.count('class="audit-stage"') == 15
    assert '本地树模型训练' not in page.text and 'href="/train"' not in page.text
    assert '训练' not in page.text
    evidence = c.get(report+'/evidence.json').json()
    assert evidence['result']['schema_version'] == audit.VERSION
    assert c.get(report+'/stocks.csv').text.count('600000.SH') == 1
    assert 'date,code' in c.get(report+'/daily.csv').text
    assert 'modeled_stamp_tax' in c.get(report+'/fees.csv').text
    assert 'effective_names' in c.get(report+'/risk.csv').text
    assert 'tick_half_spread_bp' in c.get(report+'/tick-costs.csv').text
    assert 'right_censored' in c.get(report+'/holding-lots.csv').text
    assert '检查内容' in page.text and '本次范围' in page.text
    assert '费用核验与可计算建议' in page.text and '最大快照回撤' in page.text
    assert '价位成本：收益能否覆盖半档价差情景' in page.text
    assert '实际持有期与收益覆盖' in page.text
    assert c.get(report+'/unknown.csv').status_code == 404
    plan = c.get(path+'/plan')
    assert plan.status_code == 200 and '历史期末持仓' in plan.text
    assert '000001.SZ,100,0' in plan.text
    assert '000001.SZ,21.0,conditional' in plan.text
    assert 'name="observed_at" type="datetime-local" value=""' in plan.text
    assert 'name="signal_available_at" type="datetime-local" value=""' in plan.text
    assert not (workdir/'runs/paper').exists()
    assert not (workdir/'runs/strategies').exists()
    assert not (workdir/'runs/governance_reviews').exists()


def test_workspace_flow_keeps_strategy_and_account_handoff_reachable(client):
    _, c, workdir = client
    path, _ = run_report(client)
    identity = path.rsplit('/', 1)[1]
    for route in ('strategy-governance', 'strategy-attribution', 'strategy-comparison', 'strategy-simulation'):
        response = c.get('/'+route+'?strategy='+identity)
        assert response.status_code == 200
        assert 'aria-label="策略处理流程"' in response.text
        assert response.text.count('aria-current="step"') == 1
        assert response.text.count('id="current-strategy"') == 1
        assert '<main' in response.text
    simulation = c.get('/strategy-simulation?strategy='+identity).text
    assert '<h1>账户方案</h1>' in simulation
    assert 'action="/strategy-simulation"' in simulation
    assert path+'/plan' in simulation
    assert not (workdir/'runs/paper').exists()


def test_import_security_host_token_errors_and_html_escape(client):
    app, c, workdir = client
    assert upload(client, token='bad').status_code == 403
    assert not store.history(workdir)
    assert c.post('/strategy-audits/import', headers={'host': 'evil.example'}).status_code == 403
    path = upload(client, name='<script>alert(1)</script>').headers['location']
    assert '<script>alert(1)</script>' not in c.get(path).text
    assert '&lt;script&gt;' in c.get(path).text
    bad = c.post('/strategy-audits/import', data={'token': app.state.token},
        files=[('files', ('unknown.csv', b'wrong,columns\n1,2', 'text/csv'))])
    assert bad.status_code == 400


def test_stale_confirmation_and_tampered_report_rejected(client):
    app, c, workdir = client
    path, report = run_report(client)
    assert c.post(path+'/analyze', data={'token': 'bad'}).status_code == 403
    assert c.post(path+'/analyze', data={'token': app.state.token, 'input_digest': 'stale'}).status_code == 400
    identity = path.rsplit('/', 1)[1]; report_id = report.rsplit('/', 1)[1]
    target = store.root(workdir, identity)/'reports'/report_id/'result.json'
    result = ex.read(target); result['settings']['commission_bp'] = 0; ex.write(target, result)
    assert c.get(report).status_code == 404
    assert c.get('/strategy-audits/not-an-id').status_code == 404


def test_bounded_multipart_request(client, monkeypatch):
    app, c, _ = client
    monkeypatch.setattr(imp, 'MAX_TOTAL_BYTES', 10)
    response = c.post('/strategy-audits/import', data={'token': app.state.token},
        files=[('files', ('large.csv', b'x'*1_000_011, 'text/csv'))])
    assert response.status_code == 400


def test_training_entries_hidden_from_audit_navigation_and_home(client):
    _, c, _ = client
    for route in ('/review', '/review/new', '/strategy-audits', '/strategy-audits/new'):
        page = c.get(route)
        assert page.status_code == 200
        for target in ('/train', '/models', '/start'):
            assert f'href="{target}"' not in page.text
        assert '训练' not in page.text
    assert c.get('/start').status_code == 200


def test_legacy_report_read_only_adds_catalog_but_hides_old_training_capability(client):
    _, c, workdir = client
    files, data = view.sample_files(), bundle()
    identity = store.save(workdir, data, files)
    result = audit.analyze(data)
    result.pop('coverage'); result.pop('coverage_version')
    result['capabilities'] = [{'id': 'local_tree', 'title': '本地树模型训练'}]
    result['result_digest'] = ex.object_digest({k: v for k, v in result.items() if k != 'result_digest'})
    report_id = store.save_report(workdir, identity, result)
    target = store.root(workdir, identity)/'reports'/report_id/'result.json'
    original_bytes = target.read_bytes()
    response = c.get(f'/strategy-audits/{identity}/reports/{report_id}')
    assert response.status_code == 200 and '逐环节审计' in response.text
    assert response.text.count('class="audit-stage"') == 15
    assert '本地树模型训练' not in response.text
    assert target.read_bytes() == original_bytes


def test_fee_schedule_gap_is_not_saved_profit_and_stress_uses_accounted_intervals():
    result = audit.analyze(bundle(cash=None))
    fees = result['modules']['fee_analysis']
    assert fees['priced_fill_count'] == 3
    # Sample reports the old 10bp tax in 2024; frozen rule is 5bp, never tuned away.
    sell = next(f for f in fees['fills'] if f['side'] == 'sell')
    assert sell['modeled_commission'] == 5
    assert sell['modeled_stamp_tax'] == pytest.approx(.55)
    assert sell['reported_minus_modeled'] == pytest.approx(.55)
    assert fees['reported_fee'] == pytest.approx(16.1)
    assert fees['breakpoint_notional'] == pytest.approx(16666.6666667)
    assert fees['conditional_batch_saving'] == 0
    stress = fees['cost_stress']
    assert stress['traded_notional'] == 1100  # exclude first-close baseline fills
    assert stress['net_pnl'] == pytest.approx(193.9)
    assert stress['scenarios'][0]['extra_cost'] == pytest.approx(.55)
    assert stress['positive_profit_exhausted_at_extra_bp'] == pytest.approx(193.9/1100*10000)


def test_fee_comparison_keeps_canceling_gaps_and_rule_coverage_visible():
    data = bundle()
    for fill in data['fills']:
        fill.update(commission_basis='fill', commission_evidence='synthetic per-fill contract')
    data['fills'][0]['fee'] += 1
    data['fills'][1]['fee'] -= 1
    fees = audit.analyze(data)['modules']['fee_analysis']
    assert fees['mismatch_count'] == 3
    assert fees['reported_minus_modeled'] == pytest.approx(.55)
    assert fees['absolute_gap'] == pytest.approx(2.55)
    data['fills'][-1]['date'] = '1900-01-01'
    fees = audit.analyze(data)['modules']['fee_analysis']
    assert fees['unpriced_fill_count'] == 1
    assert fees['fills'][-1]['modeled_fee'] is None


def test_fee_schedule_follows_effective_date_and_zero_commission_is_finite():
    from qwb.audit_diagnostics import fee_analysis
    data = bundle()
    data['fills'] = [dict(data['fills'][-1], date=day) for day in ('2023-08-25', '2023-08-28')]
    result = fee_analysis(data, audit.accounting(data), {'commission_bp': 0., 'minimum_commission': 5.})
    assert [f['modeled_stamp_tax'] for f in result['fills']] == pytest.approx([1.1, .55])
    assert result['breakpoint_notional'] is None


def test_batch_saving_only_matches_clock_security_side_and_requires_confirmation():
    from qwb.audit_diagnostics import fee_analysis
    data = bundle()
    first = data['fills'][0]
    assert first['side'] == 'buy'
    rows = [dict(first, quantity=50, notional=500, ref={'source': 'fills', 'row': i}) for i in range(1, 3)]
    data['fills'] = rows + [dict(rows[0], time='10:30'), dict(rows[0], side='sell'),
                            dict(rows[0], code='000002.SZ'), dict(rows[0], time='--')]
    cost = fee_analysis(data, audit.accounting(data), {'commission_bp': 3., 'minimum_commission': 5.})
    assert len(cost['batch_candidates']) == 1
    candidate = cost['batch_candidates'][0]
    assert candidate['fill_count'] == 2
    assert candidate['conditional_saving'] == 5
    assert candidate['adverse_price_budget_bp'] == 50
    assert cost['unknown_clock_fills'] == 1
    assert '确认' in cost['batch_conditions']
    data['fills'][1]['date'] = '2024-01-03'
    assert not fee_analysis(data, audit.accounting(data), {'commission_bp': 3., 'minimum_commission': 5.})['batch_candidates']


def drawdown_bundle():
    data = bundle()
    data['fills'] = [data['fills'][0]]
    data['actions'] = []
    data['snapshots'], data['positions'] = [], []
    for day, price in zip(('2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05', '2024-01-06'), (10, 9, 11, 10, 12)):
        data['snapshots'].append({'date': day, 'equity': 8995+100*price, 'cash': 8995, 'market_value': 100*price, 'ref': {}})
        data['positions'].append({'date': day, 'code': '600000.SH', 'quantity': 100, 'price': price, 'market_value': 100*price, 'ref': {}})
    return data


def test_drawdown_attribution_includes_opening_fees_closes_and_recovers():
    result = audit.analyze(drawdown_bundle())
    risk = result['modules']['risk_analysis']
    assert risk['status'] == 'verified'
    dd = risk['max_drawdown']
    assert dd['ratio'] == pytest.approx(.0105)
    assert dd['peak_date'] is None and dd['trough_date'] == '2024-01-03'
    assert dd['recovery_date'] == '2024-01-04'
    assert dd['loss'] == 105 and dd['fees'] == 5
    assert dd['contribution_gap'] == 0
    assert dd['stocks'][0]['gross_pnl'] == -100
    assert dd['stocks'][0]['net_pnl'] == -105
    assert risk['min_effective_names'] == 1  # high cash is not diversification
    assert risk['daily'][0]['cash_weight'] == pytest.approx(8995/9995)


def test_unknown_opening_drawdown_does_not_invent_opening_and_invalid_account_is_draft():
    data = drawdown_bundle()
    data['opening'] = {'kind': 'first_close', 'cash': None}
    risk = audit.analyze(data)['modules']['risk_analysis']
    dd = risk['max_drawdown']
    assert dd['peak_date'] == '2024-01-02'
    assert dd['loss'] == 100 and dd['fees'] == 0
    assert dd['contribution_gap'] == 0
    data['snapshots'][1]['cash'] += 10
    result = audit.analyze(data)
    assert result['modules']['risk_analysis']['status'] == 'needs_reconciliation'
    assert next(c for c in result['coverage'] if c['id'] == 'risk_exposure')['algorithms'][0]['inputs'] == 'needs_reconciliation'


def test_cap_repair_is_marked_lower_bound_not_an_executable_sale():
    result = audit.analyze(bundle(), {'commission_bp': 3., 'minimum_commission': 5., 'max_weight': .1})
    cap = result['modules']['risk_analysis']['cap_repair']
    assert cap['marked_sale_lower_bound'] == pytest.approx(2100-.1*10183.9)
    assert len(cap['positions']) == 1
    assert '可卖数量' in cap['meaning']


def test_same_day_two_way_is_not_netting_or_avoidable_turnover():
    from qwb.audit_diagnostics import trading_behavior
    data = bundle()
    data['fills'].append(dict(data['fills'][0], side='sell', notional=500, quantity=50))
    behavior = trading_behavior(data)
    assert behavior['two_way_notional'] == 1000
    assert behavior['two_way_share'] == pytest.approx(1000/4600)
    assert len(behavior['same_day_two_way']) == 1
    assert '不是可避免' in behavior['meaning']


def test_implementation_and_input_status_independent_and_deterministic():
    result = audit.analyze(bundle())
    stages = {c['id']: c for c in result['coverage']}
    assert stages['costs']['algorithms'][0]['implementation'] == 'connected'
    assert stages['costs']['algorithms'][0]['inputs'] == 'ready'
    assert stages['costs']['algorithms'][1]['implementation'] == 'not_connected'
    assert stages['costs']['algorithms'][1]['inputs'] == 'missing'
    assert stages['constraint_tradeoffs']['algorithms'][1]['implementation'] == 'available'
    assert stages['signal_age']['algorithms'][0]['implementation'] == 'not_implemented'
    assert audit.analyze(bundle()) == result


def test_report_handoff_preserves_confirmed_cap_and_legacy_report_is_not_recomputed(client):
    app, c, workdir = client
    path = upload(client).headers['location']
    identity = path.rsplit('/', 1)[1]
    _, data = store.read(workdir, identity)
    result = audit.analyze(data, {'commission_bp': 3., 'minimum_commission': 5., 'max_weight': .1})
    report_id = store.save_report(workdir, identity, result)
    response = c.get(path+'/plan?report_id='+report_id)
    assert 'name="max_weight" type="number" value="10.0"' in response.text
    assert c.get(path+'/plan?report_id=not-a-report').status_code == 404
    for key in ('fee_analysis', 'risk_analysis', 'trading_behavior', 'tick_analysis', 'holding_analysis'):
        result['modules'].pop(key)
    result.pop('coverage'); result.pop('coverage_version')
    result['result_digest'] = ex.object_digest({k: v for k, v in result.items() if k != 'result_digest'})
    legacy_id = store.save_report(workdir, identity, result)
    target = store.root(workdir, identity)/'reports'/legacy_id/'result.json'
    raw = target.read_bytes()
    legacy_path = path+'/reports/'+legacy_id
    assert '未运行新增费用与风险检查' in c.get(legacy_path).text
    assert c.get(legacy_path+'/fees.csv').status_code == 404
    assert target.read_bytes() == raw


def test_report_page_leads_with_summary_and_exports_document(client):
    _, c, _ = client
    _, report = run_report(client)
    page = c.get(report).text
    assert page.index('结论摘要') < page.index('回测长度与内部验证')
    assert 'report.html' in page
    doc = c.get(report + '/report.html')
    assert doc.status_code == 200 and doc.text.startswith('<!doctype html>')
    assert '一、结论摘要' in doc.text and '七、审计范围与限制' in doc.text
    download = c.get(report + '/report.html?download=1')
    assert 'attachment' in download.headers['content-disposition']
