"""A terminal must describe ledger state without inventing live or forward evidence."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from qwb import deployment_ui as d, experiments as ex, policy, strategy_workspace as w, ui
from test_strategy_workspace import setup, evaluated  # noqa: F401


def recorded():
    spec = {'sid': 'a'*32, 'vid': 'b'*32, 'name': '测试策略', 'cash': 100,
        'spec_hash': 'sha256:frozen', 'resolved': policy.resolve('patient'),
        'signal': {'model_hash': 'sha256:model', 'final_model_data_end': '2026-10-01'}}
    decision = {'date': '2026-10-12', 'execution_date': '2026-10-13', 'target_exposure': .8,
        'weights': {'600000.SH': .8}, 'risk_reason': 'FIXED_EXPOSURE',
        'reasons': [{'reason': 'PARTIAL_ADJUSTMENT'}]}
    records = []
    for i, (equity, cash, quantity, mark, cost) in enumerate(((200, 100, 1, 100, 0), (220, 20, 2, 100, 2), (198, 18, 2, 90, 3))):
        day = f'2026-10-{12+i}'
        fill = {'asset_id': '600000.SH', 'side': 'BUY', 'quantity': 1 if i==1 else 0,
            'requested_quantity': 1, 'open_price': mark, 'notional': mark if i==1 else 0,
            'reason': 'FILLED' if i==1 else 'INSUFFICIENT_CASH', 'unfilled_quantity': int(i==2),
            'commission': 1, 'stamp_tax': 0 if i==1 else 1, 'struct_spread': 1 if i==1 else 0,
            'slippage': 0 if i==1 else 1, 'total_cost': cost}
        records.append({'day': day, 'scope': 'forward_paper' if i==2 else 'late_reconstruction',
            'path': {'equity': equity, 'cash': cash, 'exposure': 1-cash/equity, 'total_cost': cost,
                'net_return': 0 if i==0 else equity/records[-1]['path']['equity']-1},
            'state': {'positions': [{'asset_id': '600000.SH', 'quantity': quantity, 'sellable_quantity': 1,
                'mark': mark, 'mark_time': day+'T15:00:00+08:00'}]},
            'decision': decision if i==0 else None, 'constraint_violations': [], 'fills': [fill] if i else []})
    return {'spec': spec, 'runtime': {'scope': 'forward_paper', 'enabled': False, 'start_date': '2026-10-12'},
        'records': records, 'evaluations': [], 'health': {'state': 'UP_TO_DATE', 'message': '已记账'}}


def test_metrics_use_marked_initial_equity_not_cash_and_do_not_relabel_late_records():
    source = recorded(); original = deepcopy(source); result = d.snapshot(source)
    assert source==original
    assert result['metrics']['total_return']==pytest.approx(-.01)  # 198/200, not 198/100
    assert result['metrics']['daily_pnl']==-22
    assert result['metrics']['max_drawdown']==pytest.approx(-.1)
    assert result['metrics']['return_intervals']==2
    assert result['scope_counts']=={'forward_paper': 1, 'late_reconstruction': 2, 'historical_replay': 0}
    assert [r['scope'] for r in result['series']]==[r['scope'] for r in source['records']]
    assert result['holdings'][0]['value']==180
    assert result['holdings'][0]['weight']==pytest.approx(180/198)
    assert result['holdings'][0]['mark_change']==pytest.approx(-.1)
    assert result['total_cost']==sum(result['costs'].values())==5
    assert result['fill_count']==2 and result['fills'][0]['quantity']==0
    assert result['decision']['execution_recorded']
    assert result['targets'][0]['actual']==pytest.approx(180/198)


def test_no_records_never_use_backtest_or_reference_equity_to_fill_terminal():
    source = recorded(); source['records']=[]
    source['evaluations']=[{'status': 'SUCCEEDED', 'metrics': {'selected': {'total_return': 9.}}}]
    result = d.snapshot(source)
    assert result['series']==result['holdings']==result['fills']==[]
    assert result['metrics']['equity'] is result['metrics']['total_return'] is result['metrics']['daily_pnl'] is None
    assert result['scope_counts']['forward_paper']==0 and result['decision'] is None
    source['records']=recorded()['records'][:1]
    result=d.snapshot(source)
    assert result['metrics']['daily_return'] is None and result['metrics']['return_intervals']==0


def test_demo_and_blocking_remain_visible_independent_of_the_curve():
    source=recorded(); source['runtime']['scope']='historical_replay'
    for row in source['records']:row['scope']='historical_replay'
    source['health']={'state': 'BLOCKED', 'message': '公司行动停止'}
    result=d.snapshot(source)
    assert result['historical_demo'] and result['scope_counts']['forward_paper']==0
    assert result['health']['state']=='BLOCKED' and '运行受阻' in result['next_action']
    assert len(result['series'])==3


def test_polling_is_read_only_and_new_actions_preserve_csrf_jobs_and_frozen_identity(setup,monkeypatch):
    root,sid,vid=evaluated(setup)
    loc,_=w.revision(root,sid,vid)
    app=ui.create_app(root,runner=lambda fn:None); client=TestClient(app); base=f'/strategies/{sid}/{vid}'
    assert client.get(base+'/dashboard').status_code==200
    assert client.get(base+'/dashboard/snapshot.json').json()['runtime'] is None
    assert client.post(base+'/dashboard/actions/tick',data={'token':app.state.token}).status_code==409
    w.start(root,sid,vid,historical_start='2026-10-09')
    w.tick(root,sid,vid,now='2026-10-22T18:00:00+08:00')
    identity=w.implementation(); before=(loc/'ledger.sqlite3').read_bytes()
    monkeypatch.setattr(w,'refresh_and_tick',lambda *a,**k:pytest.fail('A GET must never tick'))
    for _ in range(2):
        response=client.get(base+'/dashboard/snapshot.json')
        assert response.headers['cache-control']=='no-store'
        result=response.json()
        assert result['historical_demo'] and result['scope_counts']['forward_paper']==0
        assert str(root) not in json.dumps(result)  # presentation API excludes private local paths
    assert (loc/'ledger.sqlite3').read_bytes()==before and w.implementation()==identity
    assert client.post(base+'/dashboard/actions/automation',data={'token':'bad','enabled':'yes'}).status_code==403
    toggle=client.post(base+'/dashboard/actions/automation',data={'token':app.state.token,'enabled':'yes'},follow_redirects=False)
    assert toggle.status_code==303 and toggle.headers['location']==base+'/dashboard'
    assert w.inspect(root,sid,vid)['runtime']['enabled']
    run=client.post(base+'/dashboard/actions/tick',data={'token':app.state.token},follow_redirects=False)
    assert run.status_code==303 and run.headers['location'].startswith('/job/')
    assert '返回部署看板' in client.get(run.headers['location']).text
    assert (loc/'ledger.sqlite3').read_bytes()==before
    assert '打开部署看板' in client.get(base).text
    assert base+'/dashboard' in client.get('/start').text


def test_terminal_escapes_strategy_name_and_keeps_static_assets_local(tmp_path,monkeypatch):
    source=recorded(); source['spec']['name']='</script><script>alert(1)</script>'
    monkeypatch.setattr(w,'inspect',lambda *a:source)
    app=ui.create_app(tmp_path,runner=lambda fn:None); client=TestClient(app)
    result=client.get('/strategies/'+('a'*32)+'/'+('b'*32)+'/dashboard')
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    assert '</script><script>alert(1)</script>' not in result.text
    assert '\\u003c/script>' in result.text and '&lt;/script&gt;' in result.text
    assert client.get('/static/deployment.js').status_code==200
    assert client.get('/static/deployment.css').status_code==200


def test_successful_forward_activation_enters_empty_deployment_terminal(setup):
    root,sid,vid=evaluated(setup)
    app=ui.create_app(root,runner=lambda fn:None); client=TestClient(app); base=f'/strategies/{sid}/{vid}'
    response=client.post(base+'/start',data={'token':app.state.token},follow_redirects=False)
    assert response.status_code==303 and response.headers['location']==base+'/dashboard'
    state=client.get(base+'/dashboard/snapshot.json').json()
    assert state['runtime']['scope']=='forward_paper' and not state['historical_demo']
    assert state['metrics']['equity'] is None and state['series']==[]
    assert not state['runtime']['enabled']
