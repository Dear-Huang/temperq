"""Offline training -> frozen governance -> replay ledger acceptance; no market evidence."""
from __future__ import annotations

import argparse
from pathlib import Path

from qwb import daily_data, experiments as ex, pipeline, runconfig, strategy_workspace as ws

import numpy as np
import pandas as pd
import yaml


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument("output",type=Path)
    args=parser.parse_args();root=args.output.resolve()
    if root.exists():raise SystemExit("Choose a new directory; previous acceptance records are preserved.")
    root.mkdir(parents=True)
    codes=[f"{i:06d}.SZ" for i in range(1,13)]
    dates=pd.bdate_range("2019-01-01","2023-01-20")
    bars=pipeline.synth_bars(len(dates),len(codes))
    bars["date"]=np.repeat(dates,len(codes));bars["code"]=np.tile(codes,len(dates))
    bars["down_limit"]=bars["close"]*.8;bars["up_limit"]=bars["close"]*1.2;bars["is_st"]=0
    calendar=pd.DataFrame({"date":dates.strftime("%Y-%m-%d"),"is_open":1}).to_csv(index=False).encode()
    feed=root/"data/synthetic_daily";cutoff=pd.Timestamp("2022-12-30")
    initial=daily_data.update(feed,bars.loc[bars.date<=cutoff].to_csv(index=False).encode(),calendar,
                              source="synthetic_workflow_only")
    task=yaml.safe_load((Path(__file__).parents[1]/"examples/cn_rank_5d.yaml").read_text("utf-8"))
    raw={"name":"strategy_demo_ridge","task":task,
         "data":{"source":"snapshot","dir":initial["snapshot"]},"model":{"name":"ridge"},
         "train":{"universe":"all_ex_st","first_test_year":2021,"final_test_years":[2022],
                  "seeds":[0],"shuffle_null_draws":3,"portfolio":{"n_hold":2}},
         "output":"runs/strategy_demo_ridge"}
    cfg,report=runconfig.resolve(raw,root)
    if not report.ok:raise ValueError(str(report))
    pipeline.run(cfg,root)
    sid,vid=ws.create(root,root/raw["output"],feed,name="Synthetic acceptance only",cash=100000,
        preset="patient",overrides={"n_hold":2,"hold_until_rank":3,"max_weight":.5})
    evidence=ws.evaluate(root,sid,vid,start="2022-12-01",end="2022-12-30")
    ws.start(root,sid,vid,historical_start="2022-12-01")
    # Use a test clock, clearly restricted to historical software demonstration.
    ws.tick(root,sid,vid,now="2022-12-09T18:00:00+08:00")
    ws.tick(root,sid,vid,now="2022-12-30T18:00:00+08:00")
    records=ws.inspect(root,sid,vid)["records"]
    expected=ex.read(evidence/"selected.json")
    assert [r["path"] for r in records]==expected["path"]
    assert [f for r in records for f in r["fills"]]==expected["fills"]
    assert all(r["scope"]=="historical_replay" for r in records)
    assert ws.tick(root,sid,vid,now="2022-12-30T18:00:00+08:00")["new_sessions"]==0
    _,v2=ws.create(root,root/raw["output"],feed,name="second version",cash=100000,sid=sid,
        overrides={"n_hold":2,"hold_until_rank":3,"max_weight":.5})
    ws.evaluate(root,sid,v2,start="2022-12-01",end="2022-12-30")
    try:ws.start(root,sid,v2)
    except ValueError as exc:
        assert "合成数据" in str(exc)
    else:raise AssertionError("Synthetic feed accepted as genuine forward data")
    daily_data.update(feed,bars.loc[bars.date>cutoff].to_csv(index=False).encode(),calendar,
                      source="synthetic_workflow_only")
    loc,spec=ws.revision(root,sid,vid)
    meta,panel,_,st,members,_=ws.inputs(root,loc,spec)
    scores=ws._live_scores(meta,panel,st,members,pd.DatetimeIndex(["2023-01-02"]))
    assert scores["2023-01-02"]
    assert ws.inspect(root,sid,vid)["records"]==records
    ex.write(root/"ACCEPTANCE.json",{"status":"PASS","scope":"synthetic_workflow_only",
        "training_run":raw["output"],"sid":sid,"vid":vid,"sessions":len(records),
        "checks":["actual_ridge_training","frozen_policy","same_replay_and_ledger_path",
                  "same_fills","idempotent_retry","preserved_versions","synthetic_forward_blocked",
                  "append_only_feed","new_data_inference"],
        "limitation":"Synthetic prices, calendar and test clock validate software only; no investment evidence."})
    print(f"PASS: {root}; synthetic workflow only, {len(records)} historical sessions")


if __name__=="__main__":main()
