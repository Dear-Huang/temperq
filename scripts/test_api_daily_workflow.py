"""Run one real-data workflow test; historical replay is not forward performance."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qwb import daily_data, experiments, paper, pipeline, runconfig, strategy

import pandas as pd
import yaml


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--source-feed", required=True, type=Path)
    parser.add_argument("--strategy-feed", required=True, type=Path)
    parser.add_argument("--name", default="api_test_40_lgbm")
    parser.add_argument("--replay-start", default="2026-10-12")
    args = parser.parse_args()
    if (args.strategy_feed / "current.json").exists() or (ROOT / "runs" / args.name).exists():
        raise ValueError("Use new feed and run names; existing test evidence is preserved")
    snapshot = daily_data.current(args.source_feed)
    source = pd.read_csv(snapshot / "source.csv", dtype={"code": str})
    calendar = (snapshot / "calendar.csv").read_bytes()
    baseline = source.loc[source.date.le("2025-12-31")]
    incoming = source.loc[source.date.gt("2025-12-31")]
    if baseline.empty or incoming.empty:
        raise ValueError("Need historical training data and later API data")
    first = daily_data.update(args.strategy_feed, baseline.to_csv(index=False).encode(), calendar,
                              source="configured_api_fixed_universe_test")
    task = yaml.safe_load((ROOT / "examples" / "cn_rank_5d.yaml").read_text("utf-8"))
    raw = {"name": args.name, "task": task,
           "data": {"source": "snapshot", "dir": first["snapshot"]},
           "model": {"name": "lightgbm", "num_boost_round": 100},
           "train": {"universe": "all_ex_st", "first_test_year": 2023,
                     "final_test_years": [2024, 2025], "seeds": [0, 1], "shuffle_null_draws": 3},
           "output": f"runs/{args.name}"}
    config, report = runconfig.resolve(raw, ROOT)
    if not report.ok:
        raise ValueError(str(report))
    output = ROOT / "artifacts" / args.name
    output.mkdir(parents=True, exist_ok=False)
    (output / "run.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), "utf-8")
    pipeline.run(config, ROOT)
    latest = daily_data.update(args.strategy_feed, incoming.to_csv(index=False).encode(), calendar,
                               source="configured_api_fixed_universe_test")
    run = ROOT / "runs" / args.name
    replay_name = args.name + "_replay"
    paper.initialize(ROOT, replay_name, run, 100000, [], replay_start=args.replay_start)
    end = experiments.read(Path(latest["snapshot"]) / "snapshot.json")["end"]
    result = paper.tick(ROOT, replay_name, now=end + "T18:00:00+08:00")
    records = paper.inspect(ROOT, replay_name)["sessions"]
    assert len(records) >= 7 and records[0]["next_weights"] and records[5]["next_weights"]
    assert all(r["scope"] == "historical_replay" for r in records)
    assert all(r["state"]["cash"] >= 0 for r in records)
    assert any((r["audit"] or {}).get("fills") for r in records)
    assert paper.tick(ROOT, replay_name, now=end + "T18:00:00+08:00")["new_sessions"] == 0
    assert paper.inspect(ROOT, replay_name)["sessions"] == records
    forward_name = args.name + "_forward"
    paper.initialize(ROOT, forward_name, run, 100000, [])
    forward = paper.tick(ROOT, forward_name)
    open_dates = pd.read_csv(snapshot / "calendar.csv", dtype=str)
    execution = open_dates.loc[open_dates.date.gt(end) & open_dates.is_open.eq("1"), "date"].min()
    advice = strategy.preview(run, ROOT, 100000, [], execution)
    assert advice["date"] == end and advice["rows"]
    assert advice["input_snapshot_id"] == latest["data_snapshot_id"]
    experiments.write(output / "advice.json", advice)
    pd.DataFrame(advice["rows"]).to_csv(output / "advice.csv", index=False, encoding="utf-8-sig")
    summary = {"status": "PASS", "scope": "real_data_workflow_test_not_profit_evidence",
               "input_end": end, "training_end": "2025-12-31", "codes": source.code.nunique(),
               "rows": len(source), "run": str(run), "replay": result,
               "replay_name": replay_name, "forward_name": forward_name, "forward": forward,
               "execution_day": execution, "advice_rows": len(advice["rows"]),
               "recipe_hash": advice["protocol_hash"], "model_hash": advice["model_hash"],
               "cost_scope": "commission_and_stamp_only_no_spread_or_impact"}
    experiments.write(output / "result.json", summary)
    print(summary, flush=True)


if __name__ == "__main__":
    main()
