import io
import re

import pandas as pd
from fastapi.testclient import TestClient

from qwb import csvimport as ci, experiments as ex, pipeline, ui, daily_data


def client(tmp_path,monkeypatch):
    monkeypatch.setattr(ui,'hardware',lambda:{})
    app=ui.create_app(tmp_path,runner=lambda fn:None)
    return TestClient(app),app.state.token


def test_missing_prices_retains_mapping_and_gives_two_exits(tmp_path,monkeypatch):
    c,token=client(tmp_path,monkeypatch)
    response=c.post('/data/csv',data={'token':token,'name':'mine'},files={'file':('a.csv','证券,日,成交量\n000001,2024-01-02,100\n'.encode())},follow_redirects=False)
    route=response.headers['location']
    response=c.post(route+'/check',data={'token':token,'name':'mine','column_0':'code','column_1':'date','column_2':'volume','amount_unit':'thousand'})
    assert response.status_code==400
    assert '还缺：开盘价、最高价、最低价、收盘价、成交额' in response.text
    assert '成交量不能代替成交额' in response.text
    assert '下载 CSV 模板' in response.text and '改用免费数据' in response.text
    assert '<option value="thousand" selected>' in response.text
    assert '<option value="volume" selected>' in response.text
    assert not (tmp_path/'data/mine').exists()


def test_custom_mapping_units_calendar_and_save(tmp_path,monkeypatch):
    c,token=client(tmp_path,monkeypatch)
    bars=pipeline.synth_bars(n_days=800,n_codes=3)
    keep=ci.REQUIRED+['adj_factor']
    raw_frame=bars[keep].copy()
    raw_frame['amount']/=1000
    columns=['证券','交易日','O','H','L','C','千元成交额','复权']
    raw_frame.columns=columns
    mapping=dict(zip(columns,keep))
    calendar=pd.DataFrame({'date':pd.date_range(bars.date.min(),bars.date.max()).strftime('%Y-%m-%d')})
    calendar['is_open']=calendar.date.isin(set(bars.date.astype(str))).astype(int)
    response=c.post('/data/csv',data={'token':token,'name':'mine'},files={'file':('a.csv',raw_frame.to_csv(index=False).encode())},follow_redirects=False)
    route=response.headers['location']
    fields={'token':token,'name':'mine','amount_unit':'thousand',**{'column_'+str(i):mapping[k] for i,k in enumerate(columns)}}
    checked=c.post(route+'/check',data=fields,files={'calendar':('calendar.csv',calendar.to_csv(index=False).encode())})
    assert checked.status_code==200 and '检查完成' in checked.text
    assert not (tmp_path/'data/mine').exists()
    checked_hash=re.search(r'name="checked_hash" value="([^"]+)"',checked.text).group(1)
    assert c.post(route+'/save',data={'token':token,'checked_hash':'stale'}).status_code==400
    saved=c.post(route+'/save',data={'token':token,'checked_hash':checked_hash},follow_redirects=False)
    assert saved.status_code==303
    feed=tmp_path/'data/mine';root=daily_data.current(feed)
    result=pd.concat([pd.read_parquet(p) for p in (root/'bars').glob('*.parquet')]).sort_values(['code','date'])
    pd.testing.assert_series_equal(result.reset_index(drop=True).amount,bars.sort_values(['code','date']).reset_index(drop=True).amount,check_names=False)
    assert ex.read(feed/'csv_mapping.json')=={'columns':mapping,'amount_unit':'thousand'}
    assert (root/'calendar.csv').is_file()
    assert '数据' in c.get('/data').text
    before=(feed/'current.json').read_bytes()
    # The same source auto-fills its custom headers/unit, checks without
    # publishing, and rejects revisions to an already published date.
    response=c.post('/data/csv',data={'token':token,'name':'mine','append':'mine'},files={'file':('again.csv',raw_frame.to_csv(index=False).encode())},follow_redirects=False)
    route=response.headers['location']
    preview=c.get(route)
    assert '<option value="thousand" selected>' in preview.text
    checked=c.post(route+'/check',data=fields,files={'calendar':('calendar.csv',calendar.to_csv(index=False).encode())})
    assert checked.status_code==200,checked.text.split('<main>')[-1].split('</main>')[0][:1200]
    assert (feed/'current.json').read_bytes()==before
    changed=raw_frame.copy();changed.iloc[0,2]*=2
    response=c.post('/data/csv',data={'token':token,'name':'mine','append':'mine'},files={'file':('revision.csv',changed.to_csv(index=False).encode())},follow_redirects=False)
    failed=c.post(response.headers['location']+'/check',data=fields,files={'calendar':('calendar.csv',calendar.to_csv(index=False).encode())})
    assert failed.status_code==400 and '历史数据修订' in failed.text
    assert (feed/'current.json').read_bytes()==before


def test_duplicate_mapping_and_invalid_units_cannot_save(tmp_path):
    raw=b'a,b\n1,2\n'
    import pytest
    with pytest.raises(ci.ImportError_,match='只能选择一次'):
        ci.read(io.BytesIO(raw),mapping={'a':'code','b':'code'})
    with pytest.raises(ci.ImportError_,match='文件列已变化'):
        ci.read(io.BytesIO(raw),mapping={'a':'code'})
    assert not (tmp_path/'data').exists()
