from fastapi.testclient import TestClient

from qwb import ui


def test_home_exposes_experience_entry_and_playbook_cards(tmp_path):
    app = ui.create_app(tmp_path, runner=lambda fn: fn())
    client = TestClient(app)
    home = client.get('/start')
    assert home.status_code == 200
    assert '从你的经验开始' in home.text
    cards = client.get('/playbooks')
    assert cards.status_code == 200
    assert '趋势' in cards.text or '规则卡' in cards.text


def test_playbook_selection_persists_a_draft(tmp_path):
    app = ui.create_app(tmp_path, runner=lambda fn: fn())
    client = TestClient(app)
    response = client.post('/playbooks/select', data={
        'token': app.state.token, 'playbook': 'patient_owner', 'project': ''},
        follow_redirects=False)
    assert response.status_code == 303
    location = response.headers['location']
    assert location.startswith('/playbooks/setup?project=')
    project = location.split('=')[1]
    setup = client.get(location)
    assert setup.status_code == 200 and '低波稳健' in setup.text
    assert client.post('/playbooks/setup', data={'token': app.state.token,
        'project': project, 'action': 'save'}, follow_redirects=False).status_code == 303
    assert client.get('/rules/'+project+'.json').json()['schema'] == 'rule_card_v1'


def test_prepare_data_saves_edits_and_returns_to_same_draft(tmp_path):
    app=ui.create_app(tmp_path,runner=lambda fn:fn())
    client=TestClient(app)
    response=client.post('/playbooks/select',data={'token':app.state.token,'playbook':'low_volatility_daily'},follow_redirects=False)
    project=response.headers['location'].split('=')[1]
    response=client.post('/playbooks/setup',data={'token':app.state.token,'project':project,
        'r0_window':'25','action':'import_data'},follow_redirects=False)
    assert response.headers['location']=='/data?project='+project
    assert client.get('/rules/'+project+'.json').json()['rules'][0]['window']==25
    assert client.get('/projects/'+project,follow_redirects=False).headers['location']=='/playbooks/setup?project='+project
