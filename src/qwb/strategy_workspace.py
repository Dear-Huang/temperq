"""One frozen policy snapshot for historical evaluation and opt-in paper operation.

Paper ticks rebuild the same deterministic governance path, verify its recorded
prefix, then transactionally append only unseen dates. This deliberately favors
an auditable small-account implementation over a second incremental executor.
"""
from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

from . import daily, daily_data, experiments as ex, governance as g, pipeline, strategy
from .policy import Policy, resolve

import numpy as np
import pandas as pd


@contextmanager
def connection(path):
    con=sqlite3.connect(path,timeout=30)
    try:
        with con:yield con
    finally:
        con.close()


def location(workdir, sid):
    if not ex.ID_RE.fullmatch(sid):
        raise ValueError("策略编号无效")
    return Path(workdir)/"runs/strategies"/sid


def implementation():
    return ex.object_digest({str(p.name): ex.digest(p) for p in (
        Path(__file__), Path(__file__).with_name("policy.py"), Path(g.__file__), Path(strategy.__file__), Path(daily.__file__),
        Path(__file__).parent/"portfolio/reference.py", Path(__file__).parent/"rules/cn_a.yaml")})


def versions(workdir, sid):
    return sorted(location(workdir,sid).glob("versions/*/spec.json"), key=lambda p:p.stat().st_mtime)


def revision(workdir, sid, vid, *, verify_implementation=True):
    if not ex.ID_RE.fullmatch(vid):
        raise ValueError("策略版本无效")
    root = location(workdir,sid)/"versions"/vid
    spec = ex.read(root/"spec.json")
    frozen = {k:v for k,v in spec.items() if k != "spec_hash"}
    if ex.object_digest(frozen) != spec["spec_hash"]:
        raise ValueError("冻结配置被修改")
    if strategy.model_identity(root/"final_model") != spec["signal"]["model_hash"]:
        raise ValueError("冻结模型被修改")
    if ex.digest(root/"predictions.parquet") != spec["signal"]["predictions_hash"]:
        raise ValueError("冻结历史预测被修改")
    if verify_implementation and spec["implementation_hash"] != implementation():
        raise ValueError("决策或执行实现变化，请建立新版本，不能继续旧成绩")
    Policy.model_validate(spec["resolved"]["policy"])
    return root,spec


def inputs(workdir, root, spec):
    snapshot = daily_data.current(spec["data_feed"])
    meta = ex.read(root/"final_model/meta.json")
    meta["dir"] = root/"final_model"
    panel, snapshot_id, st, members = pipeline.load_data(
        {"data":{"source":"snapshot","dir":str(snapshot)},"cfg":meta["cfg"]},Path(workdir))
    cal = pd.read_csv(snapshot/"calendar.csv",dtype=str)
    if set(cal.columns) != {"date","is_open"} or not cal.is_open.isin(["0","1"]).all():
        raise ValueError("需要有效的权威交易日历")
    parsed = pd.to_datetime(cal.date,errors="raise")
    if parsed.duplicated().any():
        raise ValueError("交易日历日期重复")
    calendar = pd.DatetimeIndex(parsed[cal.is_open.eq("1")]).sort_values()
    dates=panel["close"].index
    if not dates.equals(calendar[(calendar>=dates.min()) & (calendar<=dates.max())]):
        raise ValueError("行情缺少连续交易日，不能压缩时间")
    return meta,panel,snapshot_id,st,members,calendar


def history(root,panel):
    return g.signal_matrix(pd.read_parquet(root/"predictions.parquet"),panel)


def config(spec):
    policy = Policy.model_validate(spec["resolved"]["policy"])
    return policy.parameters,policy.mode


def create(workdir, run_dir, feed, *, name, cash, preset="direct", overrides=None, mode=None,
           holdings=None, sid=None):
    if not name.strip() or len(name)>80 or isinstance(cash,bool) or not np.isfinite(cash) or cash<0:
        raise ValueError("请填写策略名称与非负现金")
    run_dir,feed=Path(run_dir).resolve(),Path(feed).resolve()
    if feed.parent.name=="snapshots" and (feed.parent.parent/"current.json").is_file():
        feed=feed.parent.parent
    meta=ex.read(run_dir/"final_model/meta.json")
    strategy.locked_recipe(meta)
    resolved=resolve(preset,overrides,mode)
    holdings=strategy.read_holdings(pd.DataFrame(holdings or [],columns=sorted(strategy.HOLDING_COLUMNS)).to_csv(index=False).encode())
    if cash==0 and not holdings:raise ValueError("现金与初始持仓不能同时为空")
    sid=sid or uuid.uuid4().hex
    family=location(workdir,sid)
    if family.exists() and not (family/"identity.json").exists():
        raise ValueError("策略目录不完整")
    family.mkdir(parents=True,exist_ok=True)
    if not (family/"identity.json").exists():
        ex.write(family/"identity.json",{"name":name,"created_at":ex.now()})
    vid=uuid.uuid4().hex
    root=family/"versions"/vid
    temporary=family/(".creating-"+vid)
    temporary.mkdir()
    try:
        shutil.copytree(run_dir/"final_model",temporary/"final_model")
        shutil.copy2(run_dir/"predictions.parquet",temporary/"predictions.parquet")
        spec={"schema":"strategy_workspace_v1","sid":sid,"vid":vid,"name":name,"created_at":ex.now(),
              "cash":float(cash),"holdings":holdings,"data_feed":str(feed),"resolved":resolved,
              "signal":{"source":str(run_dir),"model_hash":strategy.model_identity(temporary/"final_model"),
                        "predictions_hash":ex.digest(temporary/"predictions.parquet"),"task":meta["card"],
                        "final_model_data_end":meta["data_end"]},
              "protocol":strategy.protocol()[0],"implementation_hash":implementation()}
        spec["spec_hash"]=ex.object_digest(spec)
        ex.write(temporary/"spec.json",spec)
        # Validate the contract before publishing a version.
        _,p,_,_,_,_=inputs(workdir,temporary,spec)
        history(temporary,p)
        root.parent.mkdir(exist_ok=True)
        pipeline._retry(lambda:temporary.rename(root))
    finally:
        if temporary.exists():shutil.rmtree(temporary,ignore_errors=True)
    return sid,vid


def replay(root,spec,panel,signals,snapshot_id,*,start,end,cfg=None,mode=None,snapshot_ids=None,allow_single=False,
           calendar=None,check=lambda:None):
    chosen,control=config(spec)
    chosen=cfg or chosen
    samples=(g.risk_samples(g.RiskData(panel),signals,chosen.n_hold,check)
             if (mode or control)=="ml" else pd.DataFrame(columns=["date",*g.FEATURES,"variance","label_end"]))
    return g.replay(panel,signals,chosen,mode=mode or control,start=start,end=end,
        initial_cash=spec["cash"],initial_holdings=spec["holdings"],snapshot_id=snapshot_id,
        samples=samples,snapshot_ids=snapshot_ids,allow_single=allow_single,trading_calendar=calendar,check=check)


def evaluate(workdir,sid,vid,*,start,end,check=lambda:None,log=lambda x:None):
    root,spec=revision(workdir,sid,vid)
    out=root/"evaluations"/uuid.uuid4().hex
    out.mkdir(parents=True)
    result={"status":"RUNNING","spec_hash":spec["spec_hash"],"scope":"historical_replay",
            "created_at":ex.now(),"start":start,"end":end}
    ex.write(out/"result.json",result)
    try:
        check()
        _,panel,snapshot_id,_,_,calendar=inputs(workdir,root,spec)
        signals=history(root,panel)
        cfg,mode=config(spec)
        log("回放当前政策")
        selected=replay(root,spec,panel,signals,snapshot_id,start=start,end=end,calendar=calendar,check=check)
        check()
        log("回放相同信号、授权与风险方式的立即调整对照")
        immediate=replay(root,spec,panel,signals,snapshot_id,start=start,end=end,
                         cfg=replace(cfg,adjustment_speed=1.),calendar=calendar,check=check)
        for key,arm in (("selected",selected),("immediate",immediate)):
            ex.write(out/(key+".json"),arm)
        try:
            operability=preflight(workdir,sid,vid)
        except ValueError as exc:
            operability={"state":"BLOCKED","message":str(exc)}
        result.update(status="SUCCEEDED",snapshot_id=snapshot_id,
            operability=operability,
            metrics={"selected":selected["metrics"],"immediate":immediate["metrics"]},
            cost_difference=selected["metrics"]["total_cost"]-immediate["metrics"]["total_cost"],
            evidence="INSUFFICIENT",message="历史路径对照完成；没有据此证明收益增量。",
            limitations=g.LIMITATIONS+["此对照只改变调整速度；实际敞口、风险与换手可能不同。"])
    except BaseException as exc:
        result.update(status="FAILED",error=str(exc));ex.write(out/"result.json",result);raise
    ex.write(out/"result.json",result)
    return out


def preflight(workdir,sid,vid):
    root,spec=revision(workdir,sid,vid)
    meta,panel,snap,st,members,calendar=inputs(workdir,root,spec)
    signals=history(root,panel)
    day=signals.index[-1]
    cfg,_=config(spec)
    if signals.loc[day].notna().sum()<cfg.n_hold:
        raise ValueError("信号股票数不足，请减少持仓数")
    arm=replay(root,spec,panel,signals,snap,start=day,end=day,allow_single=True,calendar=calendar)
    intent=arm["decisions"][0]["weights"]
    state=strategy.account(spec["cash"],spec["holdings"],panel["close"].loc[day].to_dict(),
        observed_at=(day+pd.Timedelta(hours=15,minutes=30)).tz_localize("Asia/Shanghai"),snapshot_id=snap,
        locked_until=(day+pd.Timedelta(days=2)).tz_localize("Asia/Shanghai"))
    can_buy=0
    for code,w in intent.items():
        price=panel["close"].at[day,code]
        lot=strategy.session_bar(code,open_price=price,close_price=price,upper=price*2,lower=price/2).minimum_buy
        budget=state.equity*w
        cost=max(cfg.minimum_commission,lot*price*cfg.commission_bp/10000)+lot*price*cfg.slippage_bp/10000
        can_buy+=bool(budget>=lot*price+cost and state.cash>=lot*price+cost)
    if sum(intent.values())>0 and not can_buy and not spec["holdings"]:
        raise ValueError("按参考价、最小申报量和费用，没有任何目标能买入；请调整资金或持仓设置")
    return {"state":"CHECKED","reference_date":str(day.date()),"buyable_targets":can_buy,
            "message":"已检查信号、日历、参数和参考价数量可行性；不保证实际开盘成交。",
            "unknown":["未独立证明上游训练无泄漏","价差与冲击未标定","公司行动尚不支持"]}


def inspect(workdir,sid,vid):
    root,spec=revision(workdir,sid,vid,verify_implementation=False)
    runtime=ex.read(root/"runtime.json") if (root/"runtime.json").exists() else None
    records=[]
    if (root/"ledger.sqlite3").exists():
        with connection(root/"ledger.sqlite3") as con:
            records=[json.loads(r[0]) for r in con.execute("SELECT payload FROM sessions ORDER BY day")]
    evidence=[ex.read(p) for p in sorted(root.glob("evaluations/*/result.json"),key=lambda p:p.stat().st_mtime)]
    health=ex.read(root/"health.json") if (root/"health.json").exists() else {"state":"NOT_STARTED","message":"尚未开启纸面跟踪"}
    if spec["implementation_hash"]!=implementation():
        health={"state":"BLOCKED","message":"执行实现已变化，旧成绩保留；请修改并建立新版本再运行"}
    return {"spec":spec,"runtime":runtime,"records":records,"evaluations":evidence,"health":health}


def start(workdir,sid,vid,*,historical_start=None,now=None):
    root,spec=revision(workdir,sid,vid)
    preflight(workdir,sid,vid)
    if not any(r.get("status")=="SUCCEEDED" for r in inspect(workdir,sid,vid)["evaluations"]):
        raise ValueError("请先完成本版本的历史检验，再开始纸面观察")
    stamp=pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Shanghai")
    if stamp.tzinfo is None:raise ValueError("运行时间需要时区")
    stamp=stamp.tz_convert("Asia/Shanghai")
    if historical_start:
        _,panel,_,_,_,_=inputs(workdir,root,spec)
        day=pd.Timestamp(historical_start)
        if day.tzinfo is not None or day!=day.normalize() or day>stamp.normalize().tz_localize(None):
            raise ValueError("历史演示的开始日必须是已经发生的交易日")
        if day not in history(root,panel).index:
            raise ValueError("历史演示首日没有存档当期预测")
    elif pd.Timestamp(spec["signal"]["final_model_data_end"])>stamp.normalize().tz_localize(None):
        raise ValueError("模型训练截止日晚于启用日")
    with ex.output_lock(root/"operation"):
        if (root/"runtime.json").exists():raise ValueError("本版本已有账户，不能重置；请继续运行或建立新版本")
        snapshot=ex.read(daily_data.current(spec["data_feed"])/"snapshot.json")
        if not historical_start and str(snapshot.get("source","")).startswith("synthetic"):
            raise ValueError("合成数据只能开启明确标识的历史演示")
        runtime={"scope":"historical_replay" if historical_start else "forward_paper",
            "start_date":str(pd.Timestamp(historical_start).date()) if historical_start else str(stamp.date()),
            "activated_at":str(stamp),"enabled":False,"spec_hash":spec["spec_hash"]}
        database=root/".initializing.sqlite3"
        try:
            database.unlink(missing_ok=True)
            with connection(database) as con:
                con.execute("CREATE TABLE sessions (day TEXT PRIMARY KEY,payload TEXT NOT NULL)")
                con.execute("CREATE TABLE signals (day TEXT PRIMARY KEY,payload TEXT NOT NULL)")
            database.replace(root/"ledger.sqlite3")
            ex.write(root/"runtime.json",runtime)
        except BaseException:
            database.unlink(missing_ok=True)
            if not (root/"runtime.json").exists():(root/"ledger.sqlite3").unlink(missing_ok=True)
            raise
        ex.write(root/"health.json",{"state":"WAITING_DATA","message":"等待启用后的完整收盘数据","checked_at":str(stamp)})
    return runtime


def _live_scores(meta,panel,st,members,days):
    features=daily.selected_features(panel,meta["cfg"])
    if daily.feature_names(features)!=meta["features"]:raise ValueError("新数据特征与冻结模型不一致")
    universe=daily.universe(panel,meta["cfg"],st,members)
    result={}
    for day in days:
        if day<pd.Timestamp(meta["data_end"]):raise ValueError("禁止最终模型回填训练截止前的信号")
        idx=[code for code in panel["close"].columns if universe.at[day,code]]
        for code in idx:strategy.stock_code(code)
        x=np.column_stack([features[k].loc[day,idx].to_numpy() for k in meta["features"]])
        scores=daily.predict_rows(meta["dir"],meta,x)
        if not len(scores) or not np.isfinite(scores).all():raise ValueError("新信号为空或无效")
        result[str(day.date())]=dict(zip(idx,map(float,scores)))
    return result


def tick(workdir,sid,vid,*,now=None,check=lambda:None):
    root,spec=revision(workdir,sid,vid)
    stamp=pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Shanghai")
    if stamp.tzinfo is None:raise ValueError("运行时间需要时区")
    stamp=stamp.tz_convert("Asia/Shanghai")
    with ex.output_lock(root/"operation"):
        try:
            check()
            runtime=ex.read(root/"runtime.json")
            if runtime["spec_hash"]!=spec["spec_hash"]:raise ValueError("运行实例的策略版本不一致")
            meta,panel,snap,st,members,calendar=inputs(workdir,root,spec)
            latest_day=stamp.normalize().tz_localize(None)
            if (stamp.hour,stamp.minute)<(15,30):latest_day-=pd.Timedelta(days=1)
            calendar_table=pd.read_csv(daily_data.current(spec["data_feed"])/"calendar.csv",dtype=str)
            if pd.Timestamp(calendar_table.date.max())<latest_day:
                raise ValueError("交易日历未覆盖本次检查日期，请更新日历后重试")
            eligible=calendar[(calendar>=pd.Timestamp(runtime["start_date"])) & (calendar<=latest_day)]
            if len(eligible) and eligible[0]<=panel["close"].index.max() and eligible[0] not in panel["close"].index:
                raise ValueError("启用后首个交易日行情缺失，不能从后面的日期重启账户")
            days=panel["close"].index
            days=days[(days>=pd.Timestamp(runtime["start_date"])) & (days<=latest_day)]
            with connection(root/"ledger.sqlite3") as con:
                con.execute("BEGIN IMMEDIATE")
                records=[json.loads(r[0]) for r in con.execute("SELECT payload FROM sessions ORDER BY day")]
                old_scores={r[0]:json.loads(r[1]) for r in con.execute("SELECT day,payload FROM signals")}
                unseen=days[~days.strftime("%Y-%m-%d").isin([r["day"] for r in records])]
                if not len(unseen):
                    state="WAITING_DATA" if len(eligible) and (not records or records[-1]["day"]<str(eligible[-1].date())) else "UP_TO_DATE"
                    status={"state":state,"message":"等待新行情" if state=="WAITING_DATA" else "当前可用交易日已记录，未重复记账",
                            "new_sessions":0,"checked_at":str(stamp)}
                    ex.write(root/"health.json",status);return status
                if not len(calendar[calendar>days[-1]]):raise ValueError("日历未覆盖下个开市日")
                signals=history(root,panel).reindex(columns=panel["close"].columns)
                if runtime["scope"]=="historical_replay":
                    for day in unseen:
                        if day not in signals.index:raise ValueError("历史演示缺少当期存档预测，不能用最终模型倒填")
                    new_scores={str(day.date()):signals.loc[day].dropna().to_dict() for day in unseen}
                else:
                    new_scores=_live_scores(meta,panel,st,members,unseen)
                all_scores=dict(old_scores,**new_scores)
                for day,values in all_scores.items():
                    signals.loc[pd.Timestamp(day)]=pd.Series(values)
                signals=signals.sort_index()
                ids={r["day"]:r["input_snapshot_id"] for r in records}
                ids.update({str(d.date()):snap for d in unseen})
                arm=replay(root,spec,panel,signals,snap,start=days[0],end=days[-1],snapshot_ids=ids,allow_single=True,calendar=calendar,check=check)
                decisions={r["date"]:{k:v for k,v in r.items() if not k.startswith("evaluation_")} for r in arm["decisions"]}
                paths={r["date"]:r for r in arm["path"]}
                positions={r["date"]:r for r in arm["positions"]}
                computed=[]
                for day in days:
                    key=str(day.date())
                    computed.append({"day":key,"path":paths[key],"state":positions[key]["state"],
                        "constraint_violations":positions[key]["constraint_violations"],
                        "decision":decisions.get(key),"fills":[f for f in arm["fills"] if f["date"]==key],
                        "input_snapshot_id":ids[key],"spec_hash":spec["spec_hash"]})
                for old,new in zip(records,computed):
                    if any(ex.object_digest(old[k])!=ex.object_digest(new[k]) for k in new):
                        raise ValueError("冻结历史与重算不一致，停止追加；检查历史修订或信号变更")
                for record in computed[len(records):]:
                    check()
                    day=pd.Timestamp(record["day"])
                    timely=(not records or records[-1].get("decision") is None
                            or records[-1].get("decision_recorded_before_execution",False))
                    scope="historical_replay" if runtime["scope"]=="historical_replay" else (
                        "forward_paper" if day==stamp.normalize().tz_localize(None) and timely else "late_reconstruction")
                    nxt=calendar[calendar>day][0].tz_localize("Asia/Shanghai")+pd.Timedelta(hours=9,minutes=30)
                    record.update(scope=scope,recorded_at=str(stamp),decision_recorded_before_execution=bool(stamp<nxt))
                    con.execute("INSERT INTO sessions VALUES (?,?)",(record["day"],json.dumps(record,ensure_ascii=False)))
                    records.append(record)
                for day,value in new_scores.items():con.execute("INSERT INTO signals VALUES (?,?)",(day,json.dumps(value)))
                check()
            status={"state":"UP_TO_DATE" if len(eligible) and days[-1]==eligible[-1] else "WAITING_DATA",
                    "message":f"已记账至 {days[-1]:%Y-%m-%d}；缺少新数据时保持等待",
                    "new_sessions":len(unseen),"checked_at":str(stamp)}
            ex.write(root/"health.json",status);return status
        except Exception as exc:
            ex.write(root/"health.json",{"state":"BLOCKED","message":str(exc),"checked_at":str(stamp)})
            raise


def automation(workdir,sid,vid,enabled):
    root,_=revision(workdir,sid,vid)
    with ex.output_lock(root/"automation"):
        runtime=ex.read(root/"runtime.json");runtime["enabled"]=bool(enabled)
        ex.write(root/"runtime.json",runtime)


def auto_check(workdir,*,now=None):
    stamp=pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Shanghai")
    stamp=stamp.tz_convert("Asia/Shanghai")
    if (stamp.hour,stamp.minute)<(17,30):return
    for path in (Path(workdir)/"runs/strategies").glob("*/versions/*/runtime.json"):
        try:
            with ex.output_lock(path.parent/"automation"):
                value=ex.read(path)
                if not value["enabled"] or value.get("attempted_day")==str(stamp.date()):continue
                value["attempted_day"]=str(stamp.date());ex.write(path,value)
            tick(workdir,path.parents[2].name,path.parent.name,now=stamp)
        except Exception as exc:
            try:
                ex.write(path.parent/"health.json",{"state":"BLOCKED","message":str(exc),"checked_at":str(stamp)})
            except OSError:pass
