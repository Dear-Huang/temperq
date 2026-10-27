"""Synthetic rule signal through history, confirmation and continuing accounts."""
from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qwb import daily_data, experiments as ex, pipeline, rule_cards, rule_signals, strategy_workspace as ws, ui
from qwb import strategy_projects as projects


@pytest.fixture
def environment(tmp_path):
    dates = pd.bdate_range('2024-01-01', periods=210)
    bars = pipeline.synth_bars(len(dates), 24)
    bars['date'] = np.repeat(dates, 24)
    bars['code'] = np.tile([f'{i:06d}.SZ' for i in range(1, 25)], len(dates))
    bars['down_limit'] = bars['close'] * .8
    bars['is_st'] = 0
    calendar = pd.DataFrame({'date': pd.bdate_range(dates[0], periods=250).strftime('%Y-%m-%d'), 'is_open': 1}).to_csv(index=False).encode()
    feed = tmp_path/'data/test_rules'
    # Synthetic unit-test fixture only, never a real user account or data source.
    daily_data.update(feed, bars[bars.date <= dates[179]].to_csv(index=False).encode(), calendar, source='unit_test_fixture')
    return tmp_path, feed, bars, dates, calendar


def test_actual_rules_and_live_append_share_calculation(environment):
    root, feed, bars, dates, calendar = environment
    source = rule_signals.create(root, feed, rule_cards.template('low_volatility_daily'))
    assert not list((source/'final_model').glob('*.pkl'))
    sid, vid = ws.create(root, source, feed, name='规则测试', cash=100000, cost_model='commission_only',
        overrides={'n_hold': 3, 'hold_until_rank': 4, 'max_weight': .4})
    out = ws.evaluate(root, sid, vid, start=str(dates[140].date()), end=str(dates[178].date()))
    assert ex.read(out/'result.json')['status'] == 'SUCCEEDED'
    ws.start(root, sid, vid, now=str(dates[179].date())+'T16:00:00+08:00')
    assert ws.tick(root, sid, vid, now=str(dates[179].date())+'T17:30:00+08:00')['new_sessions'] == 1
    daily_data.update(feed, bars.to_csv(index=False).encode(), calendar, source='unit_test_fixture')
    result = ws.tick(root, sid, vid, now=str(dates[-1].date())+'T17:30:00+08:00')
    assert result['new_sessions'] == 30
    records = ws.inspect(root, sid, vid)['records']
    assert len(records) == 31 and any(record['fills'] for record in records)
    assert ws.tick(root, sid, vid, now=str(dates[-1].date())+'T17:30:00+08:00')['new_sessions'] == 0
    frozen, spec = ws.revision(root, sid, vid)
    meta, panel, _, st, members, _ = ws.inputs(root, frozen, spec)
    live = ws._live_scores(meta, panel, st, members, pd.DatetimeIndex([dates[-1]]))
    expected, _, _ = rule_signals.calculate(panel, rule_cards.template('low_volatility_daily'), meta['cfg'], st, members)
    assert live[str(dates[-1].date())] == expected.loc[dates[-1]].dropna().to_dict()


@pytest.mark.parametrize('doc', rule_cards.catalog(), ids=lambda d: d['id'])
def test_all_templates_freeze_real_scores(environment, doc):
    root, feed, *_ = environment
    source = rule_signals.create(root, feed, doc)
    meta = ex.read(source/'final_model/meta.json')
    assert meta['model'] == 'rule_card' and meta['rule_card'] == doc
    assert not pd.read_parquet(source/'predictions.parquet').empty
    assert ex.read(source/'metrics.json')['rule_card']['rules'][0]['origin'] == 'template'


def test_ui_import_requires_confirmation_and_export_is_private(environment):
    root, feed, *_ = environment
    app = ui.create_app(root, runner=lambda fn: fn())
    client = TestClient(app)
    token = app.state.token
    response = client.post('/playbooks/select', data={'token': token, 'playbook': 'low_volatility_daily'}, follow_redirects=False)
    pid = response.headers['location'].split('=')[1]
    old = projects.read(root, pid)['rule_document']
    incoming = rule_cards.template('volume_price_daily')
    response = client.post('/rules/import', data={'token': token, 'project': pid}, files={'rules': ('rules.json', rule_cards.dumps(incoming))}, follow_redirects=False)
    assert response.status_code == 303
    assert projects.read(root, pid)['rule_document'] == old
    assert client.get('/projects/'+pid,follow_redirects=False).headers['location']=='/rules/confirm?project='+pid
    assert not (root/'runs/rule_attempts').exists()
    draft = projects.read(root, pid)
    assert client.post('/rules/confirm', data={'token': token, 'project': pid, 'pending_hash': 'stale'}).status_code == 400
    client.post('/rules/confirm', data={'token': token, 'project': pid, 'pending_hash': ex.object_digest(draft['pending_rules'])})
    assert all(r['origin'] == 'imported' for r in projects.read(root,pid)['rule_document']['rules'])
    response = client.post('/playbooks/setup', data={'token': token, 'project': pid, 'dataset': 'test_rules', 'action': 'preview'}, follow_redirects=False)
    assert response.status_code == 303
    draft = projects.read(root,pid)
    assert draft['source'].startswith('rule_')
    assert '下一步：设置规则' in client.get('/projects/'+pid).text
    source = root/'runs'/draft['source']
    sid,vid=ws.create(root,source,feed,name='private-name',cash=100000,cost_model='commission_only',overrides={'n_hold':3,'hold_until_rank':4,'max_weight':.4})
    frozen,_=ws.revision(root,sid,vid)
    ex.write(frozen/'model-evaluation.json',ex.read(source/'metrics.json'))
    for format in ('json','md'):
        response=client.get(f'/strategies/{sid}/{vid}/scorecard.{format}')
        assert response.status_code==200 and 'imported' in response.text
        assert 'private-name' not in response.text and str(root) not in response.text and 'holdings' not in response.text
    invalid=deepcopy(incoming);invalid['fee']=0
    assert client.post('/rules/import',data={'token':token},files={'rules':('bad.json',json.dumps(invalid))}).status_code==400
    response=client.post('/rules/revise',data={'token':token,'sid':sid,'vid':vid},follow_redirects=False)
    revised=response.headers['location'].split('=')[1]
    client.post('/playbooks/setup',data={'token':token,'project':revised,'dataset':'test_rules','action':'preview'})
    next_source=root/'runs'/projects.read(root,revised)['source']
    assert ex.read(next_source/'metrics.json')['rule_attempts']==2


def test_empty_candidates_are_cash_but_missing_inputs_block(environment):
    root,feed,bars,dates,calendar=environment
    doc=rule_cards.template('volume_price_daily')
    doc['rules']=[dict(doc['rules'][0],required=True,value=10)]
    doc['rules'][0].pop('description')
    source=rule_signals.create(root,feed,doc)
    assert pd.read_parquet(source/'predictions.parquet').empty
    sid,vid=ws.create(root,source,feed,name='empty-test',cash=100000,cost_model='commission_only',
        overrides={'n_hold':3,'hold_until_rank':4,'max_weight':.4})
    client=TestClient(ui.create_app(root,runner=lambda fn:fn()))
    page=client.get(f'/strategies/{sid}/{vid}')
    assert page.status_code==200 and f'name="start" value="{dates[59]:%Y-%m-%d}"' in page.text
    assert 'value="nan"' not in page.text
    result=ws.evaluate(root,sid,vid,start=str(dates[140].date()),end=str(dates[178].date()))
    assert ex.read(result/'result.json')['metrics']['selected']['mean_exposure']==0
    ws.start(root,sid,vid,now=str(dates[179].date())+'T16:00:00+08:00')
    ws.tick(root,sid,vid,now=str(dates[179].date())+'T17:30:00+08:00')
    frozen,spec=ws.revision(root,sid,vid)
    meta,panel,_,st,members,_=ws.inputs(root,frozen,spec)
    assert ws._live_scores(meta,panel,st,members,pd.DatetimeIndex([dates[179]]))=={str(dates[179].date()):{}}
    panel['close'].loc[dates[179],panel['close'].columns[0]]=np.nan
    panel['open'].loc[dates[179],panel['open'].columns[0]]=np.nan
    # A missing required comparison input is unknown, even with a valid close.
    panel['close'].loc[dates[179],panel['close'].columns[0]]=10
    panel['adj_factor'].loc[dates[174],panel['close'].columns[0]]=np.nan
    with pytest.raises(ValueError,match='规则输入缺失'):
        ws._live_scores(meta,panel,st,members,pd.DatetimeIndex([dates[179]]))


def test_longer_window_has_explicit_history_and_remains_comparable(environment):
    root,feed,*_=environment
    doc=rule_cards.template('low_volatility_daily')
    source=rule_signals.create(root,feed,doc)
    doc['rules'][0]['window']=100
    doc['rules'][0].pop('description')
    candidate=rule_signals.create(root,feed,doc,parent=source.name)
    assert ex.read(candidate/'final_model/meta.json')['cfg']['min_age_days']==101
    assert ex.read(candidate/'metrics.json')['paired_previous']['rank_ic_difference']['n']>0


@pytest.mark.parametrize('mode',['fixed','ewma','ml'])
def test_empty_candidate_transition_exits_positions(environment,mode):
    root,feed,_,dates,_=environment
    source=rule_signals.create(root,feed,rule_cards.template('low_volatility_daily'))
    sid,vid=ws.create(root,source,feed,name='cash-exit',cash=100000,cost_model='commission_only',
        overrides={'n_hold':3,'hold_until_rank':4,'max_weight':.4})
    frozen,spec=ws.revision(root,sid,vid)
    _,panel,snap,_,_,calendar=ws.inputs(root,frozen,spec)
    signals=ws.history(frozen,panel)
    signals.loc[dates[145]:]=np.nan
    arm=ws.replay(frozen,spec,panel,signals,snap,start=dates[140],end=dates[155],mode=mode,calendar=calendar)
    assert arm['path'][2]['exposure']>0
    assert arm['path'][-1]['exposure']==0
    assert not arm['positions'][-1]['state']['positions']


def test_stale_preview_cannot_replace_newer_draft(environment,monkeypatch):
    root,feed,*_=environment
    app=ui.create_app(root,runner=lambda fn:fn())
    client=TestClient(app)
    response=client.post('/playbooks/select',data={'token':app.state.token,'playbook':'low_volatility_daily'},follow_redirects=False)
    pid=response.headers['location'].split('=')[1]
    create=rule_signals.create
    def changed_while_building(*args,**kwargs):
        source=create(*args,**kwargs)
        projects.update(root,pid,rule_document=rule_cards.template('short_reversal_daily'),rule_build=None)
        return source
    monkeypatch.setattr(rule_signals,'create',changed_while_building)
    response=client.post('/playbooks/setup',data={'token':app.state.token,'project':pid,'dataset':'test_rules','action':'preview'})
    assert response.status_code==400 and '草稿已变化' in response.text
    assert not projects.read(root,pid).get('source')
    assert projects.read(root,pid)['rule_document']['id']=='short_reversal_daily'
