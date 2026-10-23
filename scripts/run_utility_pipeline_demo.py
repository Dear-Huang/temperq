"""Create explicit synthetic utility protocols/config and run the shared worker."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from qwb import experiments, runconfig, worker
from qwb.jobs import JobSpec, Store
from qwb.observations.bundle import PITBundle, session_time
from qwb.portfolio.recipes import CostRecipe, RiskRecipe
from qwb.training.utility_runner import verify
import yaml

from run_allocation_demo import synthetic_protocol


def create_config(source, data, output, *, epochs=2, seeds=(0,), validation_audit=False, outer_evaluation=False):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = Path(source).resolve()
    experiments.verify(source)
    bundle = PITBundle(data)
    if bundle.manifest["data_kind"] != "synthetic" or len(bundle.calendar) < 91:
        raise ValueError("utility pipeline demo requires a synthetic bundle with >=91 sessions")
    phases = {"train": bundle.calendar[80:83], "validation": bundle.calendar[83:86], "test": bundle.calendar[86:89]}
    protocols = {name: synthetic_protocol(bundle, [str(session_time(day, "15:30")) for day in dates],
        max_weight=.4, min_cash=.05, initial_holdings=True) for name, dates in phases.items()}
    for phase, protocol in protocols.items():
        experiments.write(root / f"{phase}.json", protocol)
    task = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples/cn_alloc_daily_v1.yaml").read_text(encoding="utf-8"))
    raw = {"spec_version": "e2e_utility_v1", "name": "utility-pipeline-demo", "task": task,
        "data": {"source": "pit_bundle", "dir": str(bundle.root.resolve())},
        "observations": {"lookback": 60, "min_history": 20}, "initialization": {"run": str(source), "fold": 0},
        "model": {"name": "decision_gru_v1", "freeze_encoder": False},
        "folds": [{"id": 0, **{phase: f"{phase}.json" for phase in phases},
                   "fit_cutoff": protocols["validation"]["sessions"][0]["decision_time"],
                   "selection_cutoff": protocols["test"]["sessions"][0]["decision_time"]}],
        "portfolio": {"risk": asdict(RiskRecipe(20, 10, .2)), "cost": asdict(CostRecipe(10, 10, "2020-01-01T00:00:00+08:00")),
            "optimizer": {"risk_penalty": 1., "stability_penalty": .02, "max_weight": .4,
                          "min_cash": .05, "max_turnover": 2., "tolerance": 1e-6},
            "production": {"policy": "torch_box_budget_turnover_dust_v1", "buffer": 1e-12, "dust_threshold": 1e-10}},
        "continuous": {"integer_lots": False, "settlement": True, "market_restrictions": True,
                       "minimum_commission": True, "fee_model": "declared_execution"},
        "utility": {"variance_penalty": 0.},
        "train": {"seeds": list(seeds), "epochs": epochs, "patience": 2, "learning_rate": .001,
                  "cpu_threads": 1, "max_seconds": 300., "max_episode_steps": 8, "detach_every": 2},
        "acceptance": {"state_comparison_version": "lifecycle_schedule_v1",
            "max_daily_entitlement_quantity_gap": 1e-8, "max_daily_entitlement_amount_gap_cny": 1e-8,
            "max_daily_entitlement_mismatch_count": 0, "max_daily_processed_event_mismatch_count": 0,
            "max_daily_equity_gap_cny": 10., "max_daily_equity_gap_bp": .1,
            "max_total_cost_gap_cny": 20., "max_total_cost_relative_gap": .1, "max_daily_cash_gap_cny": 10.,
            "max_daily_receivable_gap_cny": 1e-8, "max_quantity_gap": 1e-8, "max_locked_quantity_gap": 1e-8,
            "max_daily_turnover_gap": .001, "max_action_weight_gap": 1e-6, "max_unfilled_quantity_gap": 1e-8,
            "max_constraint_violation_count": 0, "zero_cost_relative_rule": "null_use_absolute"},
        "evaluation_asof": str(session_time(bundle.calendar[90], "15:30")), "output": "runs/utility-latest"}
    if validation_audit:
        raw["validation_audit"] = {"schema_version": "utility_validation_audit_v1"}
    if outer_evaluation:
        raw["outer_evaluation"] = {"schema_version": "utility_outer_evaluation_v1",
                                   "failure_policy": "explicit_failure_hold_v1", "max_seconds": 120.}
    config = root / "utility.yaml"
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    resolved, report = runconfig.resolve(raw, root)
    if resolved is None:
        raise ValueError(str(report))
    experiments.write(root / "plan.json", {"schema_version": "utility_pipeline_demo_plan_v1", "created_at": experiments.now(),
        "implementation_hash": experiments.code_identity(), "config_hash": resolved["config_hash"],
        "data_identity": bundle.identity, "evidence_status": "SYNTHETIC_ONLY",
        "scope": "software integration; engineering thresholds; not N04 or economic confirmation"})
    return config, resolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--seeds", type=int, nargs="+", default=[0])
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--validation-audit", action="store_true")
    parser.add_argument("--outer-evaluation", action="store_true", help="enable explicit outer diagnostic failure handling")
    args = parser.parse_args()
    config, resolved = create_config(args.source_run, args.bundle, args.output, epochs=args.epochs, seeds=args.seeds,
                                    validation_audit=args.validation_audit, outer_evaluation=args.outer_evaluation)
    if args.config_only:
        print(config)
        return 0
    root = config.parent
    store = Store(root / "runs/jobs")
    job = store.submit(JobSpec("train", {"action": "train", "resolved": resolved}))
    if worker.run_job(root, job, echo=True) != "succeeded":
        raise ValueError(str(store.status(job)))
    output = Path((store.root / job / "artifacts/output.txt").read_text(encoding="utf-8"))
    result = verify(output, inputs=True)
    experiments.write(root / "verification.json", result)
    print(f"{result['status']}; proxy comparison eligible {result['comparison_eligible']}; {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
