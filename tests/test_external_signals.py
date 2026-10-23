"""Declared timing, immutable appends, and governance without loading model code."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qwb import external_signals as s,experiments as ex,strategy_workspace as w,ui
from test_strategy_workspace import setup


def raw(panel,start,end):
    days=panel['close'].loc[start:end].index
    return pd.DataFrame([{'code':code,'decision_time':f'{d:%Y-%m-%d}T15:30:00+08:00',
        'prediction':float(3-i),'generated_at':f'{d:%Y-%m-%d}T15:20:00+08:00'}
        for d in days for i,code in enumerate(panel['close'].columns)]).to_csv(index=False).encode()


def test_external_feed_rejects_ambiguous_or_future_rows_and_modified_history(setup):
    root,_,_,panel=setup
    c=s.contract('外部','v1',list(panel['close'].columns))
    data=raw(panel,'2026-10-09','2026-10-22')
    source=s.create(root,c,data,now='2026-10-22T18:00:00+08:00')
    meta=ex.read(source/'final_model/meta.json');feed=meta['signal_feed']
    assert s.append(feed,data,now='2026-10-22T19:00:00+08:00')['added_rows']==0
    bad=data.replace(b'15:20:00',b'15:31:00')
    with pytest.raises(ValueError,match='生成晚于'):s.append(feed,bad,now='2026-10-22T19:00:00+08:00')
    with pytest.raises(ValueError,match='未来决策'):
        s.append(feed,raw(panel,'2026-10-23','2026-10-23'),now='2026-10-22T19:00:00+08:00')
    frame=pd.read_csv(__import__('io').BytesIO(data));frame.loc[0,'prediction']=100
    with pytest.raises(ValueError,match='旧预测'):s.append(feed,frame.to_csv(index=False).encode(),now='2026-10-22T19:00:00+08:00')
    frame=pd.read_csv(__import__('io').BytesIO(data)).iloc[1:]
    with pytest.raises(ValueError,match='完整股票池'):s.append(feed,frame.to_csv(index=False).encode(),now='2026-10-22T19:00:00+08:00')
    assert s.current(feed)[2]['2026-10-09']=='2026-10-22 18:00:00+08:00'


def test_external_source_replays_and_only_consumes_received_new_predictions(setup):
    root,_,feed,panel=setup
    c=s.contract('别处训练的模型','model-1',list(panel['close'].columns))
    source=s.create(root,c,raw(panel,'2026-10-09','2026-10-22'),now='2026-10-22T18:00:00+08:00')
    sid,vid=w.create(root,source,feed,name='外部信号策略',cash=100000,
        overrides={'n_hold':1,'hold_until_rank':1,'max_weight':1})
    w.evaluate(root,sid,vid,start='2026-10-09',end='2026-10-22')
    w.start(root,sid,vid,now='2026-10-23T10:00:00+08:00')
    with pytest.raises(ValueError,match='等待外部预测'):
        w.tick(root,sid,vid,now='2026-10-23T18:00:00+08:00')
    assert not w.inspect(root,sid,vid)['records']
    meta=ex.read(source/'final_model/meta.json')
    s.append(meta['signal_feed'],raw(panel,'2026-10-23','2026-10-23'),now='2026-10-23T18:10:00+08:00')
    with pytest.raises(ValueError,match='尚未收到'):
        w.tick(root,sid,vid,now='2026-10-23T18:05:00+08:00')
    assert w.tick(root,sid,vid,now='2026-10-23T18:15:00+08:00')['new_sessions']==1
    records=w.inspect(root,sid,vid)['records']
    assert records[0]['scope']=='forward_paper'
    assert w.tick(root,sid,vid,now='2026-10-23T18:20:00+08:00')['new_sessions']==0
    # Receipt after the execution deadline remains a late reconstruction.
    s.append(meta['signal_feed'],raw(panel,'2026-10-26','2026-10-27'),now='2026-10-27T18:00:00+08:00')
    w.tick(root,sid,vid,now='2026-10-27T18:00:00+08:00')
    assert w.inspect(root,sid,vid)['records'][1]['scope']=='late_reconstruction'


def test_import_wizard_uses_same_policy_flow_and_csrf(setup,monkeypatch):
    root,_,feed,panel=setup
    monkeypatch.setattr(ui,'datasets',lambda p:{'daily':{'path':feed}})
    app=ui.create_app(root,runner=lambda fn:fn());client=TestClient(app)
    form={'token':app.state.token,'name':'外部 CSV','version':'v1','codes':','.join(panel['close'].columns),'horizon':'5','decision_time':'15:30'}
    data=raw(panel,'2026-10-09','2026-10-22')
    assert client.get('/signals/import').status_code==200
    assert client.post('/signals/import',data=dict(form,token='bad'),files={'predictions':('p.csv',data)}).status_code==403
    response=client.post('/signals/import',data=form,files={'predictions':('p.csv',data)},follow_redirects=False)
    assert response.status_code==303,response.text
    assert '下一步：选择行情并配置政策' in client.get(response.headers['location']).text
    source='external_'+response.headers['location'].split('/')[-1]
    assert client.get('/strategies/configure',params={'source':source,'dataset':'daily'}).status_code==200
