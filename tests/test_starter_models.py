from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwb import starter_models as s,experiments as ex,ui


def test_bundled_starter_is_public_data_only_complete_and_tamper_evident(tmp_path):
    card=s.catalog()['public40_lgbm_v1']
    assert card['data_source'].startswith('BaoStock') and len(card['codes'])==40
    assert not card['raw_data_bundled']
    source=s.install(tmp_path,'public40_lgbm_v1')
    assert s.install(tmp_path,'public40_lgbm_v1')==source
    assert (source/'predictions.parquet').is_file()
    assert not any(p.suffix in {'.csv','.pkl','.pickle','.db'} for p in source.rglob('*'))
    meta=ex.read(source/'final_model/meta.json')
    assert meta['n_train']>0 and len(meta['starter_universe'])==40
    assert str(tmp_path) not in (source/'final_model/meta.json').read_text(encoding='utf-8')
    weight=source/'final_model'/meta['files'][0]
    weight.write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError,match='被修改'):s.verify(source)


def test_starter_install_is_explicit_and_available_without_training(tmp_path):
    app=ui.create_app(tmp_path,runner=lambda fn:None);client=TestClient(app)
    assert '使用入门模型' in client.get('/strategies/new').text
    assert not (tmp_path/'runs/starter_public40_lgbm_v1').exists()
    assert client.post('/strategies/starter',data={'token':'bad','model_id':'public40_lgbm_v1'}).status_code==403
    response=client.post('/strategies/starter',data={'token':app.state.token,'model_id':'public40_lgbm_v1'},follow_redirects=False)
    assert response.status_code==303 and 'starter_public40_lgbm_v1' in response.headers['location']
    assert 'starter_public40_lgbm_v1' in client.get(response.headers['location']).text


def test_starter_data_download_returns_to_selected_model_not_training(tmp_path,monkeypatch):
    import html
    from qwb import free_data
    from test_free_data import SDK
    original=free_data.fetch
    def download(feed,**kw):
        return original(feed,start='2025-01-02',end='2025-01-03',sdk=SDK(),
            log=kw['log'],progress=kw['progress'],check_cancel=kw['check_cancel'])
    monkeypatch.setattr(free_data,'fetch',download)
    app=ui.create_app(tmp_path,runner=lambda fn:fn());client=TestClient(app)
    s.install(tmp_path,'public40_lgbm_v1')
    source='starter_public40_lgbm_v1'
    page=client.get('/strategies/new',params={'source':source}).text
    assert '?next_signal='+source+'#free-data' in page
    assert 'name="next_signal"' in client.get('/train',params={'next_signal':source}).text
    response=client.post('/data/free',data={'token':app.state.token,'next_signal':source},follow_redirects=False)
    completed=html.unescape(client.get(response.headers['location']).text)
    assert '/strategies/new?source='+source+'&dataset=free_daily' in completed
    assert '数据已准备，继续配置政策' in completed
