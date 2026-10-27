"""Governance code patches preserve signal code and never execute uploads."""
import ast
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from qwb import audit_code, audit_verdicts, strategy_audit as audit, strategy_imports as imports
from qwb import strategy_audit_ui as view, strategy_audit_store as store, ui
from qwb.experiments import object_digest

SOURCE = '''# 合成代码，只用于验证改写
from bigmodule import M
def initialize(context):
    context.max_cash_per_instrument = 0.5
    context.set_commission(PerOrder(buy_cost=0.0003, sell_cost=0.0013, min_cost=5))

def handle_data(context, data):
    ranked = context.data.sort_values('score', ascending=False)
    instruments = list(ranked.instrument[:2])
    for instrument in instruments:
        cash = context.portfolio.cash / 2
        if cash > 0:
            context.order_value(instrument, cash)
    context.order_target(instrument, 0)

m1 = M.input_features(expr="score", expr_filters="price > 2", sql="SELECT score FROM original_signals")
'''


def data(source=SOURCE, settings=None):
    files = [f for f in view.sample_files() if not f.name.endswith('.py')]
    files.append(imports.SourceFile('strategy.py', source.encode('utf-8')))
    bundle = imports.normalize(files, opening_cash=10000)
    result = audit.analyze(bundle, settings or {'commission_bp': 3., 'minimum_commission': 5., 'max_weight': .25})
    return files, bundle, result


def test_generated_code_changes_only_buy_guard_and_stricter_confirmed_cap():
    _, bundle, result = data()
    before = deepcopy(bundle)
    info = audit_code.inspect(bundle, result)
    assert {o['id'] for o in info['options']} == {'small_buy_guard', 'confirmed_cap'}
    candidate = audit_code.propose(bundle, result, ['small_buy_guard', 'confirmed_cap'], minimum_buy_notional=5000.001)
    optimized = candidate['optimized_source']
    ast.parse(optimized)
    assert 'if cash >= 5000.01:' in optimized
    assert 'context.max_cash_per_instrument = 0.25' in optimized
    for untouched in ("ranked = context.data.sort_values('score', ascending=False)",
                      'context.order_target(instrument, 0)',
                      'context.set_commission(PerOrder(buy_cost=0.0003, sell_cost=0.0013, min_cost=5))',
                      'm1 = M.input_features(expr="score", expr_filters="price > 2", sql="SELECT score FROM original_signals")'):
        assert untouched in optimized
    assert len(candidate['changes']) == 2
    assert candidate['performance_improvement'] is None and not candidate['code_executed']
    assert 'costs.minimum_commission' in optimized and 'confirmed_cap' in optimized
    assert bundle == before


@pytest.mark.parametrize('call', [
    'context.order_value(instrument, -cash)',
    'context.order_value(instrument, cash, price=10)',
    'order_value(instrument, cash)',
    'context.order_target_value(instrument, cash)',
])
def test_unrecognized_order_semantics_are_not_automatically_rewritten(call):
    _, bundle, result = data(SOURCE.replace('context.order_value(instrument, cash)', call))
    assert 'small_buy_guard' not in {o['id'] for o in audit_code.inspect(bundle, result)['options']}
    with pytest.raises(ValueError, match='支持'):
        audit_code.propose(bundle, result, ['small_buy_guard'])


@pytest.mark.parametrize('amount', [float('nan'), float('inf'), -1, 0, 10**10])
def test_invalid_thresholds_are_rejected(amount):
    _, bundle, result = data()
    with pytest.raises(ValueError, match='最低买入金额'):
        audit_code.propose(bundle, result, ['small_buy_guard'], minimum_buy_notional=amount)


def test_no_fee_changes_no_hard_rule_relaxation_and_no_executed_upload(tmp_path):
    marker = tmp_path / 'never-executed'
    source = f"open({str(marker)!r}, 'w').write('bad')\n"+SOURCE
    _, bundle, result = data(source, {'commission_bp': 3., 'minimum_commission': 5., 'max_weight': .8})
    assert 'confirmed_cap' not in {o['id'] for o in audit_code.inspect(bundle, result)['options']}
    audit_code.propose(bundle, result, ['small_buy_guard'])
    assert not marker.exists()


@pytest.mark.parametrize('cookie', ['', '# coding: gbk\n'])
def test_downloadable_chinese_code_has_valid_byte_encoding(cookie):
    files, _, _ = data()
    files[-1] = imports.SourceFile('策略.txt', (cookie+SOURCE).encode('gb18030'))
    bundle = imports.normalize(files, opening_cash=10000)
    result = audit.analyze(bundle)
    candidate = audit_code.propose(bundle, result, ['small_buy_guard'])
    ast.parse(candidate['optimized_source'].encode(candidate['encoding']))


def test_verdicts_distinguish_real_discrepancies_from_missing_evidence():
    _, bundle, result = data()
    rows = {r['id']: r for r in audit_verdicts.conclusions(bundle, result)}
    assert len(rows) == 15
    assert rows['costs']['state'] == 'evidence'  # Fee basis is not in the old export.
    for fill in bundle['fills']:
        fill.update(commission_basis='fill', commission_evidence='synthetic contract')
    confirmed = audit.analyze(bundle, result['settings'])
    assert next(r for r in audit_verdicts.conclusions(bundle, confirmed) if r['id'] == 'costs')['state'] == 'adjust'
    assert rows['account_integrity']['state'] == 'pass'
    assert rows['signal_contract']['state'] == 'evidence'
    assert rows['validation']['state'] == 'evidence'
    changed = deepcopy(result)
    changed['modules']['governance']['items'][0]['observed'] = 3
    changed['modules']['accounting']['summary'][0]['failures'] = 1
    rows = {r['id']: r for r in audit_verdicts.conclusions(bundle, changed)}
    assert rows['constraint_tradeoffs']['state'] == rows['account_integrity']['state'] == 'adjust'


def test_browser_contract_select_below_header_conclusions_and_code_download(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, 'hardware', lambda: {})
    app = ui.create_app(tmp_path, runner=lambda fn: None); c = TestClient(app)
    files, bundle, result = data()
    identity = store.save(tmp_path, bundle, files)
    report_id = store.save_report(tmp_path, identity, result)
    original_path = store.root(tmp_path, identity) / 'reports' / report_id / 'result.json'
    original = original_path.read_bytes()
    html = c.get('/strategy-governance?strategy='+identity).text
    assert 'id="current-strategy"' not in html.split('</header>', 1)[0]
    assert 'id="current-strategy"' in html.split('<main>', 1)[1]
    assert '治理诊断概览' not in html and '基础测量' not in html
    assert '核对预测的含义' in html and 'verdict-adjust' in html
    assert '生成优化代码候选' in html
    endpoint = f'/strategy-audits/{identity}/reports/{report_id}/code-candidates'
    form = {'token': app.state.token, 'result_digest': result['result_digest'],
            'changes': ['small_buy_guard', 'confirmed_cap'], 'minimum_buy_notional': '5000'}
    assert c.post(endpoint, data={**form, 'token': 'bad'}).status_code == 403
    assert c.post(endpoint, data={**form, 'result_digest': 'stale'}).status_code == 400
    generated = c.post(endpoint, data=form, follow_redirects=False)
    assert generated.status_code == 303, generated.text
    path = generated.headers['location']
    assert '待原平台回测' in c.get(path).text
    source = c.get(path+'/optimized.py').content.decode('utf-8-sig')
    assert 'if cash >= 5000.0:' in source
    assert 'original.py' in c.get(path+'/changes.patch').content.decode('utf-8')
    assert original_path.read_bytes() == original
    assert '已生成的代码候选' in c.get('/strategy-governance?strategy='+identity).text
    candidate_id = path.rsplit('/', 1)[-1]
    candidate = store.code_candidate(tmp_path, identity, report_id, candidate_id)
    assert candidate['candidate_digest'] == object_digest({k: v for k, v in candidate.items() if k != 'candidate_digest'})
    record = original_path.parent / 'code_candidates' / candidate_id / 'candidate.json'
    record.write_text(record.read_text(encoding='utf-8').replace('5000.0', '5001.0'), encoding='utf-8')
    assert c.get(path+'/optimized.py').status_code == 404
    assert not (tmp_path / 'runs/paper').exists()
