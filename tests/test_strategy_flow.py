"""Strategy progress survives navigation without reusing another version's evidence."""
from fastapi.testclient import TestClient

from qwb import experiments as ex, strategy_projects as projects, strategy_workspace as ws, ui
from test_strategy_workspace import setup, evaluated


def test_draft_survives_app_restart_and_is_not_a_trading_version(setup):
    root,_,_,_=setup
    app=ui.create_app(root,runner=lambda fn:None)
    client=TestClient(app)
    assert client.post('/projects',data={'token':'bad'}).status_code==403
    response=client.post('/projects',data={'token':app.state.token,'route':'train'},follow_redirects=False)
    pid=response.headers['location'].split('project=')[1]
    draft=projects.read(root,pid)
    assert not (root/'runs/strategies'/pid).exists()
    reopened=TestClient(ui.create_app(root,runner=lambda fn:None))
    assert 'href="/start"' in reopened.get('/settings').text
    assert draft['name'] in reopened.get('/start').text
    assert reopened.get('/projects/'+pid,follow_redirects=False).headers['location']=='/train?project='+pid
    assert not (root/'runs/strategies'/pid).exists()


def test_changing_model_inherits_rules_but_requires_new_evidence(setup,monkeypatch):
    root,sid,vid=evaluated(setup,n_hold=2,hold_until_rank=3,max_weight=.6)
    ws.start(root,sid,vid,historical_start='2026-10-09',now='2026-10-23T18:00:00+08:00')
    ws.tick(root,sid,vid,now='2026-10-22T18:00:00+08:00')
    old=root/'runs/strategies'/sid/'versions'/vid
    original={p.name:p.read_bytes() for p in [old/'runtime.json',old/'ledger.sqlite3',old/'spec.json']}
    monkeypatch.setattr(ui,'datasets',lambda _: {'daily':{'path':setup[2],'snap':ex.read(setup[2]/'snapshot.json')}})
    app=ui.create_app(root,runner=lambda fn:None);client=TestClient(app);token=app.state.token
    response=client.post(f'/strategies/{sid}/{vid}/change-model',data={'token':token},follow_redirects=False)
    pid=response.headers['location'].split('project=')[1]
    response=client.post('/projects/'+pid+'/select',data={'token':token,'source':'model','dataset':'daily'},follow_redirects=False)
    assert response.status_code==303,response.text.split('<main>')[-1].split('</main>')[0]
    model=client.get('/projects/'+pid)
    assert model.status_code==200 and '模型检验' in model.text
    rules=client.get('/projects/'+pid+'/rules')
    assert 'name="n_hold"' in rules.text and 'value="2"' in rules.text
    assert f'href="/projects/{pid}/model"' in rules.text
    assert '模型检验' in client.get('/projects/'+pid+'/model').text
    reopened=TestClient(ui.create_app(root,runner=lambda fn:None))
    assert reopened.get('/projects/'+pid,follow_redirects=False).headers['location']=='/projects/'+pid+'/rules'
    old_spec=ex.read(old/'spec.json')
    values=old_spec['resolved']['policy']['parameters']
    form={'token':token,'project':pid,'source':'model','dataset':'daily','replace_model':'yes',
          'sid':sid,'parent':vid,'name':'新的规则版本','cash':old_spec['cash'],
          'preset':'patient','mode':'fixed','cost_model':'struct_v1',**values}
    response=client.post('/strategies',data=form,follow_redirects=False)
    assert response.status_code==303
    new_vid=response.headers['location'].split('/')[-1]
    assert new_vid!=vid
    state=ws.inspect(root,sid,new_vid)
    assert state['evaluations']==[] and state['runtime'] is None
    assert state['spec']['resolved']['policy']['parameters']==values
    assert '当前版本需要完成规则检验' in client.get(response.headers['location']+'/deploy').text
    assert {name:(old/name).read_bytes() for name in original}==original
    assert '新的规则版本' in client.get('/start').text and '历史演示' in client.get('/start').text
    # Refreshing/reposting the completed draft cannot create duplicate versions.
    assert client.post('/strategies',data=form,follow_redirects=False).headers['location']==response.headers['location']


def test_model_check_blocks_bad_predictions_before_rules(setup,monkeypatch):
    root,run,_,_=setup
    monkeypatch.setattr(ui,'datasets',lambda _: {'daily':{'path':setup[2],'snap':ex.read(setup[2]/'snapshot.json')}})
    app=ui.create_app(root,runner=lambda fn:None);client=TestClient(app)
    pid=projects.create(root,route='select')
    (run/'predictions.parquet').write_bytes(b'broken')
    result=client.post('/projects/'+pid+'/select',data={'token':app.state.token,'source':'model','dataset':'daily'})
    assert result.status_code==400
    assert 'source' not in projects.read(root,pid)


def test_data_download_resumes_in_same_strategy_without_starting_training(setup):
    root,_,_,_=setup
    app=ui.create_app(root,runner=lambda fn:None);client=TestClient(app)
    pid=projects.create(root)
    response=client.post('/data/free',data={'token':app.state.token,'project':pid,'start':'2021-01-01'},follow_redirects=False)
    assert response.status_code==303
    route=response.headers['location']
    reopened=ui.create_app(root,runner=lambda fn:None);other=TestClient(reopened)
    assert other.get('/projects/'+pid,follow_redirects=False).headers['location']==route
    blocked=other.post('/run',data={'token':reopened.state.token,'project':pid},follow_redirects=False)
    assert blocked.headers['location']==route
    assert len(list((root/'runs/strategy_drafts').glob('*.json')))==1
