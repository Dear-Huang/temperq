import json

import pytest
from fastapi.testclient import TestClient

from qwb import experiments as ex,policy_templates as p,strategy_workspace as w,ui
from test_strategy_workspace import setup,create


def test_shared_rules_exclude_account_data_and_reject_personal_or_executable_fields(setup):
    root,sid,vid=create(setup)
    _,spec=w.revision(root,sid,vid)
    exported=p.export(spec)
    text=json.dumps(exported)
    assert all(key not in exported for key in ('cash','holdings','data_feed','signal','metrics','name'))
    assert str(root) not in text and sid not in text and vid not in text
    template_id=p.install(root,text.encode())
    restored,origin=p.read(root,template_id)
    assert restored['policy']==spec['resolved']['policy']
    assert origin['scope']=='rules_only_not_a_performance_claim'
    with pytest.raises(ValueError):p.install(root,json.dumps(dict(exported,cash=100000)).encode())
    with pytest.raises(ValueError):p.install(root,json.dumps(dict(exported,plugin='run.py')).encode())


def test_template_round_trip_keeps_explicit_rules_and_freezes_origin(setup,monkeypatch):
    root,sid,vid=create(setup)
    monkeypatch.setattr(ex,'frozen_source',lambda p:p)
    monkeypatch.setattr(ui,'datasets',lambda p:{'daily':{'path':setup[2]}})
    app=ui.create_app(root,runner=lambda fn:fn());client=TestClient(app)
    exported=client.get(f'/strategies/{sid}/{vid}/policy-template.json')
    assert exported.status_code==200
    response=client.post('/policy-templates/import',data={'token':app.state.token},files={'template':('rules.json',exported.content)},follow_redirects=False)
    template_id=response.headers['location'].split('=')[-1]
    assert '已载入分享模板' in client.get(response.headers['location']).text
    editor=client.get('/strategies/configure',params={'source':'model','dataset':'daily','template':template_id})
    assert '资金变化不会自动覆盖' in editor.text
    policy=exported.json()['policy']
    form={'token':app.state.token,'source':'model','dataset':'daily','name':'用别人的规则','cash':'5000000',
        'preset':'patient','mode':policy['mode'],'cost_model':policy['cost_model'],'policy_template':template_id,
        'explicit_overrides':json.dumps(list(policy['parameters'])),**policy['parameters']}
    response=client.post('/strategies',data=form,follow_redirects=False)
    frozen=client.get(response.headers['location']+'/config.json').json()
    assert frozen['resolved']['policy']==policy
    assert frozen['resolved']['source_template']['template_hash']==ex.object_digest(exported.json())
