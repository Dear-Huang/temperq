"""Frozen configuration, causal execution and user-facing four-step integration."""
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qwb import daily_data, experiments as ex, governance as g, policy, strategy_workspace as w, ui
from test_daily_strategy import fixture


@pytest.fixture
def setup(tmp_path,monkeypatch):
    run,panel=fixture(tmp_path,monkeypatch)
    feed=tmp_path/"data"/"daily";feed.mkdir()
    ex.write(feed/"snapshot.json",{"source":"fixture", "data_snapshot_id":"fixture", "start":"2026-07-01","end":"2026-10-30"})
    pd.DataFrame({"date":pd.bdate_range("2026-07-01","2026-11-06").strftime("%Y-%m-%d"),"is_open":1}).to_csv(feed/"calendar.csv",index=False)
    dates=panel["close"].index[panel["close"].index>=pd.Timestamp("2026-10-09")]
    pd.DataFrame([{"code":c,"decision_time":str(d.tz_localize("Asia/Shanghai")+pd.Timedelta(hours=15,minutes=30)),"prediction":float(3-i)}
                  for d in dates for i,c in enumerate(panel["close"].columns)]).to_parquet(run/"predictions.parquet",index=False)
    monkeypatch.setattr(ui,"hardware",lambda:{})
    return tmp_path,run,feed,panel


def create(setup,**kwargs):
    root,run,feed,panel=setup
    s,v=w.create(root,run,feed,name="纪律",cash=kwargs.pop("cash",100000),preset="patient",
                 overrides={"n_hold":1,"hold_until_rank":1,"max_weight":1.,**kwargs})
    return root,s,v


def evaluated(setup,**kwargs):
    root,s,v=create(setup,**kwargs)
    w.evaluate(root,s,v,start="2026-10-09",end="2026-10-22")
    return root,s,v


def test_policy_provenance_schema_and_invalid_values():
    r=policy.resolve("patient",{"minimum_commission":2})
    assert r["policy"]["parameters"]["adjustment_speed"]==.5
    assert r["inactive"]==["target_volatility"]
    assert r["provenance"]["minimum_commission"]=="user"
    assert policy.schema()["ui"]["target_volatility"]["applies_when"]=="mode != fixed"
    assert policy.resolve("risk")["inactive"]==[]
    for changes in ({"n_hold":True},{"adjustment_speed":0},{"minimum_commission":-1},{"unknown":1}, {"commission_bp":float("nan")}):
        with pytest.raises(ValueError):policy.resolve("direct",changes)


def test_partial_adjustment_is_causal_and_hard_exits_win(setup):
    root,s,v=create(setup)
    loc,spec=w.revision(root,s,v);panel=setup[3];signals=w.history(loc,panel)
    # First purchase from cash is one half of the direct target.
    a=w.replay(loc,spec,panel,signals,"fixture",start="2026-10-09",end="2026-10-22")
    assert sum(a["decisions"][0]["weights"].values())==pytest.approx(.5)
    assert a["decisions"][0]["execution_date"]=="2026-10-12"
    end=a["decisions"][1]["date"]
    altered={k:v.copy() for k,v in panel.items()}
    altered["close"].loc[altered["close"].index>pd.Timestamp(end)]*=20
    other=w.replay(loc,spec,altered,signals,"fixture",start="2026-10-09",end="2026-10-22")
    for old,new in zip(a["decisions"][:2],other["decisions"][:2]):
        assert old["weights"]==new["weights"]
    signals.loc[pd.Timestamp(end)]=[0.,3.,1.]
    changed=w.replay(loc,spec,panel,signals,"fixture",start="2026-10-09",end="2026-10-22")
    decision=changed["decisions"][1]
    assert panel["close"].columns[0] not in decision["weights"]
    assert any(r["reason"]=="SELL_OUT_OF_BAND" for r in decision["reasons"])
    cfg,_=w.config(spec)
    capped=w.replay(loc,spec,panel,signals,"fixture",start="2026-10-09",end="2026-10-22",
                   cfg=replace(cfg,max_weight=.1,exposure_cap=.15))
    assert all(max(r["weights"].values())<=.1 and sum(r["weights"].values())<=.15 for r in capped["decisions"])


def test_history_and_paper_use_same_path_and_retry_is_idempotent(setup):
    root,s,v=evaluated(setup)
    state=w.inspect(root,s,v);spec=state["spec"]
    w.start(root,s,v,historical_start="2026-10-09",now="2026-10-23T18:00:00+08:00")
    # Tick incrementally, including a Friday whose next trading day is Monday.
    assert w.tick(root,s,v,now="2026-10-09T18:00:00+08:00")["new_sessions"]==1
    assert w.tick(root,s,v,now="2026-10-22T18:00:00+08:00")["new_sessions"]==9
    result=w.inspect(root,s,v)
    before=result["records"]
    assert all(r["scope"]=="historical_replay" for r in before)
    assert all(r["spec_hash"]==spec["spec_hash"] for r in before)
    path=next((w.location(root,s)/"versions"/v/"evaluations").glob("*/selected.json"))
    expected=ex.read(path)
    assert [r["path"] for r in before]==expected["path"]
    assert [f for r in before for f in r["fills"]]==expected["fills"]
    assert all(not any(k.startswith("evaluation_") for k in r["decision"]) for r in before if r["decision"])
    assert w.tick(root,s,v,now="2026-10-22T18:00:00+08:00")["new_sessions"]==0
    assert w.inspect(root,s,v)["records"]==before
    with pytest.raises(ValueError,match="不能重置"):w.start(root,s,v)


def test_corporate_action_blocks_transaction_without_partial_append(setup):
    root,s,v=evaluated(setup)
    w.start(root,s,v,historical_start="2026-10-09")
    w.tick(root,s,v,now="2026-10-12T18:00:00+08:00")
    before=w.inspect(root,s,v)["records"]
    setup[3]["adj_factor"].loc["2026-10-13":,:]=2
    with pytest.raises(ValueError,match="公司行动"):w.tick(root,s,v,now="2026-10-22T18:00:00+08:00")
    state=w.inspect(root,s,v)
    assert state["records"]==before and state["health"]["state"]=="BLOCKED"


def test_forward_waits_does_not_use_future_and_labels_late_records(setup):
    root,s,v=evaluated(setup)
    w.start(root,s,v,now="2026-10-23T10:00:00+08:00")
    assert w.tick(root,s,v,now="2026-10-23T14:59:00+08:00")["new_sessions"]==0
    assert not w.inspect(root,s,v)["records"]
    w.tick(root,s,v,now="2026-10-23T18:00:00+08:00")
    w.tick(root,s,v,now="2026-10-26T18:00:00+08:00")
    records=w.inspect(root,s,v)["records"]
    assert len(records)==2 and all(r["scope"]=="forward_paper" for r in records)
    # A missed decision at t+5 is reconstructed, never relabeled as timely.
    w.tick(root,s,v,now="2026-11-02T18:00:00+08:00")
    records=w.inspect(root,s,v)["records"]
    assert records[-1]["scope"]=="late_reconstruction"
    assert all(r["day"]>="2026-10-23" for r in records)


def test_small_account_and_incomplete_calendar_do_not_silently_run(setup):
    root,s,v=create(setup,cash=10)
    with pytest.raises(ValueError,match="没有任何目标能买入"):w.preflight(root,s,v)
    root,s,v=evaluated(setup)
    w.start(root,s,v,historical_start="2026-10-09")
    for value in setup[3].values():value.drop(pd.Timestamp("2026-10-12"),inplace=True)
    with pytest.raises(ValueError,match="连续交易日"):w.tick(root,s,v,now="2026-10-22T18:00:00+08:00")
    assert not w.inspect(root,s,v)["records"]


def test_frozen_configuration_and_model_tampering_are_rejected(setup):
    root,s,v=create(setup)
    loc,spec=w.revision(root,s,v)
    spec["resolved"]["policy"]["parameters"]["adjustment_speed"]=1.
    ex.write(loc/"spec.json",spec)
    with pytest.raises(ValueError,match="冻结配置"):w.revision(root,s,v)
    root,s,v=create(setup);loc,spec=w.revision(root,s,v)
    (loc/"final_model/ridge.json").write_text('{"coef":[]}',encoding="utf-8")
    with pytest.raises(ValueError,match="冻结模型"):w.revision(root,s,v)


def test_automation_is_opt_in_and_restart_deduplicated(setup,monkeypatch):
    root,s,v=evaluated(setup);w.start(root,s,v,historical_start="2026-10-09")
    calls=[];monkeypatch.setattr(w,"tick",lambda *a,**kw:calls.append(a))
    w.auto_check(root,now="2026-10-23T18:00:00+08:00");assert not calls
    w.automation(root,s,v,True)
    w.auto_check(root,now="2026-10-23T17:29:00+08:00");assert not calls
    w.auto_check(root,now="2026-10-23T18:00:00+08:00")
    w.auto_check(root,now="2026-10-23T19:00:00+08:00");assert len(calls)==1


def test_ui_four_steps_jobs_exports_and_new_version(setup,monkeypatch):
    root,run,feed,_=setup
    monkeypatch.setattr(ex,"frozen_source",lambda p:p)
    monkeypatch.setattr(ui,"datasets",lambda p:{"daily":{"path":feed}})
    app=ui.create_app(root,runner=lambda fn:fn());client=TestClient(app)
    assert client.get("/strategies/new").status_code==200
    response=client.get("/strategies/configure?source=model&dataset=daily")
    assert "高级设置" in response.text and "分步靠近目标" in response.text
    form={"token":app.state.token,"source":"model","dataset":"daily","name":"我的策略","cash":"100000",
          "preset":"patient","mode":"fixed","n_hold":"1","hold_until_rank":"1","max_weight":"1"}
    assert client.post("/strategies",data=dict(form,token="wrong")).status_code==403
    response=client.post("/strategies",data=form,follow_redirects=False);assert response.status_code==303,response.text
    base=response.headers["location"]
    report=client.get(base);assert "可运行性" in report.text and "经济增量" in report.text
    job=client.post(base+"/evaluate",data={"token":app.state.token,"start":"2026-10-09","end":"2026-10-22"},follow_redirects=False)
    status=app.state.store.status(job.headers["location"].split("/")[-1]);assert status["state"]=="succeeded",status
    assert "历史对照" in client.get(base).text
    assert client.post(base+"/start",data={"token":app.state.token,"historical_start":"2026-10-09"},follow_redirects=False).status_code==303
    job=client.post(base+"/tick",data={"token":app.state.token},follow_redirects=False)
    assert app.state.store.status(job.headers["location"].split("/")[-1])["state"]=="succeeded"
    assert "PARTIAL_ADJUSTMENT" not in client.get(base+"/decisions").text
    frozen=client.get(base+"/config.json").json()
    assert frozen["resolved"]["policy"]["parameters"]["adjustment_speed"]==.5
    assert "historical_replay" in client.get(base+"/records.csv").text
    sid,vid=base.split("/")[-2:]
    newer=client.post("/strategies",data=dict(form,sid=sid,parent=vid,adjustment_speed="0.8"),follow_redirects=False)
    assert newer.status_code==303 and newer.headers["location"]!=base
    assert client.get(base+"/config.json").json()==frozen
    assert len(w.versions(root,sid))==2


def test_partial_adjustment_has_actual_exposure_control_and_fixed_phase_boundary(setup):
    root,s,v=evaluated(setup)
    state=w.inspect(root,s,v);result=state["evaluations"][-1]
    assert set(result["metrics"])=={"selected","immediate","exposure_matched"}
    diagnostic=result["exposure_match"]
    assert diagnostic["scope"]=="post_hoc_explanation_not_deployable"
    assert diagnostic["reference_arm"]=="selected"
    assert abs(diagnostic["signed_match_error"])<=diagnostic["tolerance"]
    assert result["metrics"]["immediate"]["mean_exposure"]>result["metrics"]["selected"]["mean_exposure"]
    assert result["phases"]["startup_sessions"]==25
    assert result["phases"]["selected"]["post_startup"]["status"]=="NO_OBSERVATIONS"
    # The boundary comes from speed, not from realized returns or signal ranks.
    loc,spec=w.revision(root,s,v);cfg,_=w.config(spec)
    signal=w.history(loc,setup[3])
    arm=w.replay(loc,spec,setup[3],signal,"fixture",start="2026-10-09",end="2026-10-30")
    phases=w.phase_comparison({"selected":arm},replace(cfg,adjustment_speed=1.))
    assert phases["startup_sessions"]==5
    assert phases["selected"]["startup"]["sessions"]+phases["selected"]["post_startup"]["sessions"]==arm["metrics"]["sessions"]
    assert phases["selected"]["startup"]["total_cost"]+phases["selected"]["post_startup"]["total_cost"]==pytest.approx(arm["metrics"]["total_cost"])


def test_capital_defaults_use_equity_and_never_discard_explicit_override(setup):
    root,run,feed,panel=setup
    s,v=w.create(root,run,feed,name="大账户",cash=5_000_000)
    _,spec=w.revision(root,s,v)
    resolved=spec["resolved"]
    assert resolved["policy"]["parameters"]["n_hold"]==30
    assert resolved["policy"]["parameters"]["hold_until_rank"]==45
    assert resolved["policy"]["parameters"]["max_weight"]==pytest.approx(1/30)
    assert resolved["provenance"]["n_hold"]=="capital_tier"
    s,v=w.create(root,run,feed,name="自定",cash=5_000_000,overrides={"n_hold":10,"max_weight":.08})
    _,spec=w.revision(root,s,v)
    assert spec["resolved"]["policy"]["parameters"]["n_hold"]==10
    assert spec["resolved"]["provenance"]["n_hold"]=="user"
    holding={"code":panel["close"].columns[0],"quantity":100000,"sellable_quantity":100000}
    basis=w.capital_context(run,feed,100000,[holding])
    assert basis["equity"]==pytest.approx(100000+100000*panel["close"].iloc[-1,0])
    assert basis["holdings_reference_date"]==str(panel["close"].index[-1].date())


def test_defaults_preview_and_freeze_share_automatic_values(setup,monkeypatch):
    root,run,feed,_=setup
    monkeypatch.setattr(ex,"frozen_source",lambda p:p)
    monkeypatch.setattr(ui,"datasets",lambda p:{"daily":{"path":feed}})
    app=ui.create_app(root,runner=lambda fn:fn());client=TestClient(app)
    form={"token":app.state.token,"source":"model","dataset":"daily","name":"资金默认",
          "cash":"5000000","preset":"direct","mode":"fixed","n_hold":"10","explicit_overrides":"[]"}
    assert client.post('/strategies/defaults',data=dict(form,token='bad')).status_code==403
    preview=client.post('/strategies/defaults',data=form).json()
    response=client.post('/strategies',data=form,follow_redirects=False)
    frozen=client.get(response.headers['location']+'/config.json').json()
    assert frozen['resolved']['policy']==preview['policy']
    assert frozen['resolved']['policy']['parameters']['n_hold']==30
    form['explicit_overrides']='["n_hold"]'
    response=client.post('/strategies',data=form,follow_redirects=False)
    assert client.get(response.headers['location']+'/config.json').json()['resolved']['policy']['parameters']['n_hold']==10


def test_refresh_failure_does_not_tick_and_recovery_keeps_idempotence(setup,monkeypatch):
    from qwb import free_data
    root,s,v=evaluated(setup);w.start(root,s,v,historical_start='2026-10-09')
    events=[]
    def failed(*args,**kwargs):
        events.append('download');raise ValueError('当日行情尚未齐全')
    monkeypatch.setattr(free_data,'refresh_existing',failed)
    with pytest.raises(ValueError,match='尚未齐全'):
        w.refresh_and_tick(root,s,v,now='2026-10-22T18:00:00+08:00')
    assert not w.inspect(root,s,v)['records']
    assert w.inspect(root,s,v)['health']['state']=='BLOCKED'
    monkeypatch.setattr(free_data,'refresh_existing',lambda *a,**k:events.append('complete') or {'state':'UPDATED'})
    result=w.refresh_and_tick(root,s,v,now='2026-10-22T19:00:00+08:00')
    assert result['new_sessions']==10 and result['data_update']['state']=='UPDATED'
    before=w.inspect(root,s,v)['records']
    assert w.refresh_and_tick(root,s,v,now='2026-10-22T20:00:00+08:00')['new_sessions']==0
    assert w.inspect(root,s,v)['records']==before
