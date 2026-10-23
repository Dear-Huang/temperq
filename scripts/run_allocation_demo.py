"""Freeze and replay B0/B1 allocations from an existing synthetic sequence demo."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from qwb import experiments
import pandas as pd
import yaml
from qwb.bench import targets
from qwb.observations.bundle import PITBundle, session_time
from qwb.portfolio.lifecycle import LifecycleAccountState
from qwb.portfolio.contracts import Position
from qwb.portfolio.prediction import configuration, evaluate, verify
from qwb.portfolio.reference import ExecutionBar, Fees


def synthetic_protocol(bundle, decision_times, *, max_weight, min_cash, initial_holdings=False):
    if bundle.manifest["data_kind"] != "synthetic":
        raise ValueError("fixture construction is only allowed for explicitly synthetic data")
    decisions = sorted({pd.Timestamp(t) for t in decision_times})
    sessions = []
    latest = bundle.bars.drop_duplicates(["code", "date"], keep="last").set_index(["date", "code"])
    for i, decision in enumerate(decisions):
        day = decision.tz_convert("Asia/Shanghai").tz_localize(None).normalize()
        index = bundle.calendar.get_loc(day)
        if index + 1 >= len(bundle.calendar):
            raise ValueError("synthetic execution fixture requires an execution session")
        execution = bundle.calendar[index + 1]
        next_day = bundle.calendar[index + 2] if index + 2 < len(bundle.calendar) else execution + pd.offsets.BDay(1)
        _, eligible = bundle.universe_at(decision)
        bars = []
        for code in sorted(eligible):
            bar, prior = latest.loc[(execution, code)], latest.loc[(day, code)]
            row = asdict(ExecutionBar(code, float(bar["open"]), float(bar["close"]),
                                     float(prior["close"]) * 1.1, float(prior["close"]) * .9, sell_lot=100))
            row.pop("buy_allowed")
            bars.append(row)
        sessions.append({"decision_id": f"d{i:04d}", "decision_time": str(decision),
                         "open_at": str(session_time(execution, "09:30")), "close_at": str(session_time(execution, "15:00")),
                         "next_open_at": str(session_time(next_day, "09:30")), "eligible_assets": sorted(eligible), "bars": bars})
    positions = []
    if initial_holdings:
        # This declared starting book exercises drift/execution without changing
        # model forecasts or selecting assets using future returns.
        visible = bundle.bars[bundle.bars["available_time"] <= decisions[0]].sort_values(
            ["code", "date", "available_time", "revision"]).drop_duplicates("code", keep="last").set_index("code")
        for code in sessions[0]["eligible_assets"][:3]:
            mark = float(visible.loc[code, "close"])
            quantity = int(1000000 * min(max_weight * .9, .09) / mark / 100) * 100
            if quantity:
                positions.append(Position(code, quantity, quantity, mark, str(decisions[0])))
    cash = 1000000.0 - sum(p.quantity * p.mark for p in positions)
    return {"schema_version": "target_protocol_v2", "task": "cn_alloc_daily_v1",
            "initial_state": LifecycleAccountState(str(decisions[0]), bundle.manifest["data_snapshot_id"], cash, tuple(positions)).to_dict(),
            "fees": asdict(Fees(slippage_bp=2)), "constraints": {"max_weight": max_weight, "min_cash": min_cash},
            "sessions": sessions, "corporate_actions": [], "data_kind": "synthetic",
            "data_license": bundle.manifest["license"], "execution_model": "raw_share_lifecycle_v2"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequence-demo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-holdings", action="store_true", help="declare a starting book in the first three assets to exercise state drift")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "examples/allocation_intraday_v1.yaml")
    args = parser.parse_args(argv)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    previous = targets.read(args.sequence_demo / "comparison.json")
    runs = {name: Path(previous["runs"][name]["output"]) for name in ("B0-ridge", "B1-gru")}
    for run in runs.values():
        experiments.verify(run)
    frames = {name: pd.read_parquet(run / "predictions.parquet") for name, run in runs.items()}
    indices = [frame.set_index(["decision_time", "code"]).sort_index().index for frame in frames.values()]
    if not indices[0].equals(indices[1]):
        raise ValueError("B0/B1 prediction coverage differs")
    bundle = PITBundle(args.sequence_demo / "fixture")
    config = configuration(yaml.safe_load(args.config.read_text(encoding="utf-8")))
    protocol = synthetic_protocol(bundle, frames["B0-ridge"]["decision_time"],
                                  max_weight=config["optimizer"]["max_weight"], min_cash=config["optimizer"]["min_cash"],
                                  initial_holdings=args.initial_holdings)
    targets.validate_protocol(protocol)
    experiments.write(root / "protocol.json", protocol)
    (root / "allocation.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    experiments.write(root / "plan.json", {"schema_version": "allocation_demo_plan_v1", "frozen_at": experiments.now(),
        "config": config, "protocol_id": experiments.object_digest(protocol),
        "runs": {name: str(path) for name, path in runs.items()}, "selection": "none; retain both including failures",
        "initial_holdings": "first three decision-eligible assets, <=9% each from visible marks" if args.initial_holdings else "cash only",
        "evidence_status": "SYNTHETIC_ONLY", "economic_efficacy": "NOT_ESTABLISHED"})
    results = {}
    for name, run in runs.items():
        results[name] = evaluate(run, root / "protocol.json", bundle.root, root / "allocation.yaml", root / name)
        if results[name]["status"] != "COMPLETED":
            raise ValueError(str(results[name]))
        verify(root / name, inputs=True)
    experiments.write(root / "comparison.json", {"schema_version": "allocation_demo_result_v1",
        "plan_hash": experiments.digest(root / "plan.json"), "results": results,
        "evidence_status": "SYNTHETIC_ONLY", "model_superiority": "NOT_ESTABLISHED",
        "scope": "same frozen account and recipes; no economic inference or model selection"})
    print(root / "comparison.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
