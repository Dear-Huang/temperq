"""Failure retention and deployment boundaries beyond the happy path."""
import pandas as pd
import pytest

from qwb import experiments as ex, strategy_workspace as w
from qwb.jobs import Cancelled
from test_strategy_workspace import setup,create,evaluated  # noqa: F401


def test_feed_pointer_follows_append_and_calendar_controls_next_open(setup,monkeypatch):
    root,run,original,panel=setup
    predictions=pd.read_parquet(run/"predictions.parquet")
    predictions.loc[predictions.decision_time.str[:10]<="2026-10-22"].to_parquet(run/"predictions.parquet",index=False)
    feed=root/"feed";old=feed/"snapshots"/"old";new=feed/"snapshots"/"new"
    import shutil
    shutil.copytree(original,old);shutil.copytree(original,new)
    ex.write(feed/"current.json",{"snapshot":"snapshots/old"})
    s,v=w.create(root,run,old,name="append",cash=100000,overrides={"n_hold":1,"hold_until_rank":1,"max_weight":1})
    loc,spec=w.revision(root,s,v)
    assert spec["data_feed"]==str(feed.resolve())
    w.evaluate(root,s,v,start="2026-10-09",end="2026-10-22")
    w.start(root,s,v,now="2026-10-23T10:00:00+08:00")
    def load(rc,cwd):
        limit="2026-10-23" if rc["data"]["dir"]==str(new.resolve()) else "2026-10-22"
        return {k:x.loc[:limit] for k,x in panel.items()},limit,None,None
    monkeypatch.setattr(w.pipeline,"load_data",load)
    assert w.tick(root,s,v,now="2026-10-23T18:00:00+08:00")["state"]=="WAITING_DATA"
    ex.write(feed/"current.json",{"snapshot":"snapshots/new"})
    assert w.tick(root,s,v,now="2026-10-23T18:00:00+08:00")["new_sessions"]==1
    first=w.inspect(root,s,v)["records"][0]
    assert first["decision"]["execution_date"]=="2026-10-26"


def test_cancelled_and_failed_evaluations_stay_visible(setup):
    root,s,v=create(setup)
    def cancel():raise Cancelled("停止")
    with pytest.raises(Cancelled):w.evaluate(root,s,v,start="2026-10-09",end="2026-10-22",check=cancel)
    with pytest.raises(ValueError):w.evaluate(root,s,v,start="1900-01-01",end="2026-10-22")
    state=w.inspect(root,s,v)
    assert len(state["evaluations"])==2 and all(r["status"]=="FAILED" for r in state["evaluations"])
    with pytest.raises(ValueError,match="请先完成"):w.start(root,s,v)


def test_new_implementation_blocks_operation_but_old_records_remain_viewable(setup,monkeypatch):
    root,s,v=evaluated(setup)
    monkeypatch.setattr(w,"implementation",lambda:"sha256:new-code")
    assert w.inspect(root,s,v)["health"]["state"]=="BLOCKED"
    assert w.inspect(root,s,v)["evaluations"][0]["status"]=="SUCCEEDED"
    with pytest.raises(ValueError,match="实现变化"):w.start(root,s,v)
    old,spec=w.revision(root,s,v,verify_implementation=False)
    _,v2=w.create(root,old,spec["data_feed"],sid=s,name="new",cash=100000)
    assert v2!=v and w.revision(root,s,v2)[1]["implementation_hash"]=="sha256:new-code"


def test_final_model_never_backfills_training_past_and_missing_history_blocks(setup):
    root,s,v=evaluated(setup)
    loc,_=w.revision(root,s,v)
    meta=ex.read(loc/"final_model/meta.json")
    with pytest.raises(ValueError,match="训练截止前"):
        w._live_scores(dict(meta,dir=loc/"final_model"),setup[3],None,None,[pd.Timestamp("2026-09-30")])
    with pytest.raises(ValueError,match="没有存档"):w.start(root,s,v,historical_start="2026-10-08")


@pytest.mark.parametrize("cash",[0,10000])
def test_initial_holdings_are_valued_and_sellable_state_is_not_discarded(setup,cash):
    root,run,feed,panel=setup
    holdings=[{"code":"000001.SZ","quantity":100,"sellable_quantity":0}]
    s,v=w.create(root,run,feed,name="持仓",cash=cash,holdings=holdings,
        overrides={"n_hold":1,"hold_until_rank":1,"max_weight":1})
    loc,spec=w.revision(root,s,v)
    arm=w.replay(loc,spec,panel,w.history(loc,panel),"fixture",start="2026-10-09",end="2026-10-12")
    assert arm["path"][0]["equity"]==pytest.approx(cash+100*panel["close"].at[pd.Timestamp("2026-10-09"),"000001.SZ"])
    assert arm["positions"][0]["state"]["positions"][0]["sellable_quantity"]==0


def test_version_publish_recovers_from_transient_windows_file_lock(setup,monkeypatch):
    from pathlib import Path
    rename=Path.rename;calls=[]
    def once(path,target):
        if path.name.startswith(".creating-"):
            calls.append(path)
            if len(calls)==1:raise PermissionError("temporary antivirus reader")
        return rename(path,target)
    monkeypatch.setattr(Path,"rename",once)
    root,s,v=create(setup)
    assert len(calls)==2 and w.revision(root,s,v)[1]["vid"]==v
