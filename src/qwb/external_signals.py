"""Append-only external score feeds; user model code is never loaded."""
from __future__ import annotations

import io
from pathlib import Path
import shutil
import uuid

import pandas as pd

from . import experiments as ex, governance as g, scenario, strategy


def clock(now=None):
    stamp=pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz='Asia/Shanghai')
    if stamp.tzinfo is None:raise ValueError('接收时间需要时区')
    return stamp.tz_convert('Asia/Shanghai')


def contract(name,version,codes,*,horizon=5,decision_time='15:30'):
    if not isinstance(name,str) or not name.strip() or len(name)>80:raise ValueError('请填写不超过 80 字的信号名称')
    if not isinstance(version,str) or not version.strip() or len(version)>80:raise ValueError('请声明外部模型版本')
    if type(horizon) is not int or horizon!=5 or decision_time!='15:30':
        raise ValueError('首版仅支持 15:30 决策、次日开盘到第 6 日开盘的五日预测')
    universe=sorted(strategy.stock_code(c) for c in codes)
    if not universe or len(set(universe))!=len(universe):raise ValueError('请声明无重复的完整股票池')
    value={'schema':'external_scores_v1','name':name.strip(),'version':version.strip(),
        'codes':universe,'horizon_days':horizon,'decision_time':decision_time,'timezone':'Asia/Shanghai',
        'task':scenario.task_card(5),'availability':'user_declared_generated_at; platform_records_first_receipt',
        'upstream_leakage_audit':'not_certified'}
    return {**value,'contract_hash':ex.object_digest(value)}


def validate_meta(meta):
    c=meta['external_contract']
    expected=contract(c['name'],c['version'],c['codes'],horizon=c['horizon_days'],decision_time=c['decision_time'])
    if c!=expected:raise ValueError('外部信号契约已改变，请导入为新信号版本')
    return c


def rows(raw,c,stamp):
    if len(raw)>32_000_000:raise ValueError('预测文件超过 32 MB，请分批追加')
    frame=pd.read_csv(io.BytesIO(raw),dtype={'code':str})
    required={'code','decision_time','prediction','generated_at'}
    if set(frame.columns)!=required:raise ValueError('预测 CSV 需且只能有 code,decision_time,prediction,generated_at 四列')
    frame['code']=frame.code.map(strategy.stock_code)
    for key in ('decision_time','generated_at'):
        parsed=[]
        for value in frame[key]:
            t=pd.Timestamp(value)
            if pd.isna(t) or t.tzinfo is None:raise ValueError(key+' 必须明确时区')
            parsed.append(t.tz_convert('Asia/Shanghai'))
        frame[key]=pd.Series(parsed,index=frame.index,dtype='datetime64[ns, Asia/Shanghai]')
    if frame.empty:raise ValueError('预测表为空')
    if (frame.generated_at>frame.decision_time).any():raise ValueError('信号生成晚于声明的 15:30 决策时刻，不能倒填')
    if (frame.decision_time>stamp).any():raise ValueError('不能提前接收未来决策的预测')
    dummy={'close':pd.DataFrame(index=pd.DatetimeIndex(frame.decision_time.dt.normalize().dt.tz_localize(None).unique()),columns=c['codes'])}
    # Reuse exact timing/duplicate/finite-score checks used by governance.
    g.signal_matrix(frame,dummy)
    frame['prediction']=pd.to_numeric(frame.prediction,errors='raise').astype(float)
    for _,group in frame.groupby('decision_time'):
        if set(group.code)!=set(c['codes']):raise ValueError('每个决策日必须覆盖声明的完整股票池，缺失不能当低分')
    frame['decision_time']=frame.decision_time.astype(str)
    frame['generated_at']=frame.generated_at.astype(str)
    return frame.sort_values(['decision_time','code']).reset_index(drop=True)


def current(feed):
    feed=Path(feed).resolve()
    c=ex.read(feed/'contract.json')
    pointer=ex.read(feed/'current.json')
    root=(feed/pointer['snapshot']).resolve()
    if not root.is_relative_to(feed/'snapshots'):raise ValueError('外部信号快照路径无效')
    info=ex.read(root/'snapshot.json')
    if info['contract_hash']!=c['contract_hash']:raise ValueError('外部信号版本不一致')
    if set(info['files'])!={'predictions.parquet','receipts.json'}:raise ValueError('外部信号快照文件无效')
    if any((root/k).is_symlink() or ex.digest(root/k)!=v for k,v in info['files'].items()):raise ValueError('外部信号存档被修改')
    return c,pd.read_parquet(root/'predictions.parquet'),ex.read(root/'receipts.json')


def append(feed,raw,*,now=None):
    feed=Path(feed);stamp=clock(now)
    with ex.output_lock(feed/'current.json'):
        c=ex.read(feed/'contract.json');incoming=rows(raw,c,stamp)
        receipts={}
        if (feed/'current.json').is_file():
            _,old,receipts=current(feed)
            a=old.set_index(['decision_time','code']).sort_index()
            b=incoming.set_index(['decision_time','code']).sort_index()
            overlap=a.index.intersection(b.index)
            if not a.loc[overlap].equals(b.loc[overlap]):raise ValueError('旧预测或生成时点被修改；请建立新信号版本')
            new=b.loc[~b.index.isin(a.index)].reset_index()
            if new.empty:return {'added_rows':0,'end':old.decision_time.max()}
            if new.decision_time.min()<=old.decision_time.max():raise ValueError('只能末尾追加新决策日，不能补洞或改历史')
            incoming=pd.concat([old,new],ignore_index=True)
            added=len(new)
        else:added=len(incoming)
        for day in incoming.decision_time.str[:10].unique():receipts.setdefault(day,str(stamp))
        identity=ex.object_digest({'contract':c['contract_hash'],'rows':incoming.to_dict('records'),'receipts':receipts}).split(':')[-1]
        root=feed/'snapshots'/identity
        root.mkdir(parents=True,exist_ok=False)
        incoming.to_parquet(root/'predictions.parquet',index=False)
        ex.write(root/'receipts.json',receipts)
        ex.write(root/'snapshot.json',{'contract_hash':c['contract_hash'],
            'files':{name:ex.digest(root/name) for name in ('predictions.parquet','receipts.json')}})
        ex.write(feed/'current.json',{'snapshot':'snapshots/'+identity})
        return {'added_rows':added,'end':incoming.decision_time.max()}


def create(workdir,c,raw,*,now=None):
    workdir=Path(workdir);sid=uuid.uuid4().hex
    feed=workdir/'signals'/sid
    feed.mkdir(parents=True)
    ex.write(feed/'contract.json',c)
    source=workdir/'runs'/('external_'+sid)
    try:
        append(feed,raw,now=now)
        _,frame,_=current(feed)
        (source/'final_model').mkdir(parents=True)
        ex.write(source/'final_model/contract.json',c)
        ex.write(source/'final_model/meta.json',{'model':'external_scores','files':['contract.json'],
            'features':[],'card':c['task'],'cfg':{'universe':'all'},'data_end':frame.decision_time.str[:10].max(),
            'external_contract':c,'signal_feed':str(feed.resolve()),'run_id':sid})
        frame.to_parquet(source/'predictions.parquet',index=False)
        return source
    except BaseException:
        shutil.rmtree(feed,ignore_errors=True)
        if source.exists():shutil.rmtree(source,ignore_errors=True)
        raise


def live_scores(meta,panel,days,*,now=None):
    c=validate_meta(meta)
    actual,frame,receipts=current(meta['signal_feed'])
    if c!=actual:raise ValueError('外部信号契约改变，须建立新版本')
    signals=g.signal_matrix(frame,panel)
    stamp=clock(now);result={}
    for day in days:
        key=str(day.date())
        if day not in signals.index:raise ValueError(f'{key} 等待外部预测；不会用旧排名或最终模型代填')
        if clock(receipts[key])>stamp:raise ValueError('外部信号在本次检查时尚未收到')
        result[key]=signals.loc[day].dropna().to_dict()
    return result
