"""Reproduce the CSV -> B0 -> appended data -> paper workflow with fake prices."""
from __future__ import annotations

import argparse
from pathlib import Path

from qwb import daily_data, experiments, paper, pipeline, runconfig

import numpy as np
import pandas as pd
import yaml


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists():
        raise SystemExit("Use a new output directory; existing demonstrations are preserved.")
    root.mkdir(parents=True)
    codes = [f"{i:06d}.SZ" for i in range(1, 13)]
    dates = pd.bdate_range("2019-01-01", "2023-01-20")
    bars = pipeline.synth_bars(len(dates), len(codes))
    bars["date"] = np.repeat(dates, len(codes))
    bars["code"] = np.tile(codes, len(dates))
    bars["down_limit"] = bars["close"] * .8
    bars["up_limit"] = bars["close"] * 1.2
    bars["is_st"] = 0
    cutoff = pd.Timestamp("2022-12-30")
    calendar = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "is_open": 1}).to_csv(index=False).encode()
    feed = root / "data" / "synthetic_daily"
    initial = daily_data.update(feed, bars.loc[bars.date <= cutoff].to_csv(index=False).encode(), calendar,
                                source="synthetic_workflow_only")
    task = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples" / "cn_rank_5d.yaml").read_text("utf-8"))
    raw = {"name": "daily_demo_ridge", "task": task,
           "data": {"source": "snapshot", "dir": initial["snapshot"]},
           "model": {"name": "ridge"},
           "train": {"universe": "all_ex_st", "first_test_year": 2021,
                     "final_test_years": [2022], "seeds": [0], "shuffle_null_draws": 3,
                     "portfolio": {"n_hold": 2}},
           "output": "runs/daily_demo_ridge"}
    config, report = runconfig.resolve(raw, root)
    if not report.ok:
        raise ValueError(str(report))
    (root / "run.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), "utf-8")
    pipeline.run(config, root)
    run = root / "runs" / "daily_demo_ridge"
    paper.initialize(root, "synthetic_replay", run, 100000, [], replay_start="2023-01-02")
    daily_data.update(feed, bars.loc[bars.date > cutoff].to_csv(index=False).encode(), calendar,
                      source="synthetic_workflow_only")
    result = paper.tick(root, "synthetic_replay", now="2023-01-12T18:00:00+08:00")
    history = paper.inspect(root, "synthetic_replay")["sessions"]
    assert result["new_sessions"] == 9
    assert history[0]["next_weights"] and history[5]["next_weights"]
    assert any((row["audit"] or {}).get("fills") for row in history)
    assert paper.tick(root, "synthetic_replay", now="2023-01-12T18:00:00+08:00")["new_sessions"] == 0
    assert paper.inspect(root, "synthetic_replay")["sessions"] == history
    experiments.write(root / "DEMO.json", {"scope": "synthetic_workflow_only", "result": result,
                                          "run": str(run), "account": "synthetic_replay"})
    print(f"PASS: {root}; synthetic history is not investment evidence")


if __name__ == "__main__":
    main()
