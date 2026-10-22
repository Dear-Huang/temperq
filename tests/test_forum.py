from test_site import client


def test_forum_publish_reply_and_persistence(tmp_path):
    app, c = client(tmp_path)
    assert c.get('/api/forum').json() == {'topics': []}
    data = {'title': '我的训练配方研究', 'category': 'recipes',
            'body': '这里记录训练方法与样本外检验的完整过程。', 'recipe': '<script>unsafe()</script>'}
    assert c.post('/api/forum', data=data).status_code == 401
    c.get('/login?next=/community/new')
    session = c.get('/api/site').json()
    assert session['user'] == 'local-dev'
    data['token'] = session['csrf']
    assert c.post('/api/forum', data=data | {'token': 'bad'}).status_code == 403
    result = c.post('/api/forum', data=data)
    assert result.status_code == 200
    tid = result.json()['id']
    assert c.get('/api/forum').json()['topics'][0]['reply_count'] == 0
    reply = {'token': session['csrf'], 'body': '<img onerror=alert(1)>'}
    assert c.post(f'/api/forum/{tid}/replies', data=reply).status_code == 200
    detail = c.get(f'/api/forum/{tid}').json()
    assert detail['recipe'] == data['recipe'] and detail['replies'][0]['body'] == reply['body']
    assert c.get('/api/forum').json()['topics'][0]['reply_count'] == 1
    assert app.state.db.execute('select count(*) from topics').fetchone()[0] == 1
    c.get('/logout')
    assert c.post(f'/api/forum/{tid}/replies', data=reply).status_code == 401
    assert c.get('/api/forum/999999').status_code == 404
    assert c.post('/api/forum/999999/replies', data=reply).status_code == 401


def test_forum_validation_and_model_ownership(tmp_path):
    app, c = client(tmp_path)
    c.get('/login/dev')
    token = c.get('/api/site').json()['csrf']
    data = {'token': token, 'title': '测试研究标题', 'category': 'models', 'body': '完整的测试正文至少需要十个字。'}
    assert c.post('/api/forum', data=data | {'title': '  '}).status_code == 422
    assert c.post('/api/forum', data=data | {'category': 'invalid'}).status_code == 422
    assert c.post('/api/forum', data=data | {'share_id': 999}).status_code == 403
    r = c.post('/share', data={'repo': 'u/model', 'token': token}, follow_redirects=False)
    sid = int(r.headers['location'].split('/')[-1])
    result = c.post('/api/forum', data=data | {'share_id': sid})
    tid = result.json()['id']
    assert c.get(f'/api/forum/{tid}').json()['model']['repo'] == 'u/model'
    app.state.db.execute('update shares set owner=? where id=?', ('another-user', sid))
    app.state.db.commit()
    assert c.post('/api/forum', data=data | {'share_id': sid}).status_code == 403
    assert c.post(f'/api/forum/{tid}/replies', data={'token': token, 'body': '   '}).status_code == 422


def test_forum_login_return_path_is_local_only(tmp_path):
    _, c = client(tmp_path)
    assert c.get('/login?next=/community/new').url.path == '/community/new'
    c.get('/logout')
    assert c.get('/login?next=//evil.example').url.path == '/'
