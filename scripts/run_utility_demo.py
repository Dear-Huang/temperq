"""Fit a verified GRU on chronological synthetic episodes and audit exact replay.

This is a local N03 smoke, not the future e2e_utility_v1 CLI/worker interface.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, fields
from pathlib import Path

from qwb import experiments
import torch

from qwb.bench import targets
from qwb.observations.bundle import PITBundle, session_time
from qwb.portfolio.continuous import ContinuousConfig, ContinuousState, numbers
from qwb.portfolio.contracts import TargetWeights
from qwb.portfolio.episode import run_episode
from qwb.portfolio.lifecycle import LifecycleTargetWeights, LifecycleAccountState
from qwb.portfolio.prediction import environment
from qwb.portfolio.recipes import CostRecipe, RiskRecipe
from qwb.portfolio.reference import Fees
from qwb.training.decision import DecisionGRU, DecisionPolicy
from qwb.training.utility_fit import DecisionTrainingConfig, fit, validate_feedback
from run_allocation_demo import synthetic_protocol


def serialize(value):
    if isinstance(value, ContinuousState):
        return {field.name: serialize(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "__dataclass_fields__"):
        return {field.name: serialize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return numbers(value)


def compare(episode, exact, thresholds):
    differences = []
    for audit, (_, state), actual, exact_state in zip(episode["audits"], episode["states"],
            exact["audits"], exact["states"], strict=True):
        cost = float(audit["total_cost"])
        cost_gap = cost - actual["total_cost"]
        positions = {p["asset_id"]: p["quantity"] for p in exact_state["after"]["positions"]}
        approximate = dict(zip(state.assets, numbers(state.quantity)))
        differences.append({"decision_id": audit["decision_id"],
            "equity_gap_cny": float(state.equity) - actual["equity_close"],
            "cash_gap_cny": float(state.cash) - exact_state["after"]["cash"],
            "receivable_gap_cny": float(state.receivable) - actual.get("receivable_equity", 0),
            "max_quantity_gap": max(abs(approximate.get(code, 0) - positions.get(code, 0))
                                    for code in set(approximate) | set(positions)),
            "cost_gap_cny": cost_gap,
            "cost_relative_gap": cost_gap / actual["total_cost"] if actual["total_cost"] else None})
    max_equity = max(abs(row["equity_gap_cny"]) for row in differences)
    total_cost = sum(float(row["total_cost"]) for row in episode["audits"])
    gap = total_cost - exact["total_cost"]
    relative = gap / exact["total_cost"] if exact["total_cost"] else None
    breaches = []
    if max_equity > thresholds["max_daily_equity_gap_cny"]:
        breaches.append("daily_equity")
    if abs(gap) > thresholds["max_total_cost_gap_cny"]:
        breaches.append("total_cost")
    if relative is not None and abs(relative) > thresholds["max_total_cost_relative_gap"]:
        breaches.append("relative_cost")
    return {"daily": differences, "continuous_total_cost": total_cost, "exact_total_cost": exact["total_cost"],
        "total_cost_gap_cny": gap, "total_cost_relative_gap": relative, "threshold_breaches": breaches,
        "comparison_eligible": not breaches,
        "scope": "synthetic smoke thresholds only; no market or statistical inference"}


def compute(source, data, output, *, source_fold=0, seed=0, epochs=2,
            production_policy="torch_box_budget_turnover_v1", dust_threshold=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    attempt = {"state": "queued"}
    try:
        bundle = PITBundle(data)
        if bundle.manifest["data_kind"] != "synthetic":
            raise ValueError("utility demo only accepts explicitly synthetic data")
        source_record = experiments.verify(source)
        model = DecisionGRU.from_run(source, fold=source_fold, seed=seed, bundle=bundle)
        controls = DecisionGRU.from_run(source, fold=source_fold, seed=seed, bundle=bundle)
        # These periods are declared before any utility fitting or test scoring.
        dates = {"train": bundle.calendar[80:83], "validation": bundle.calendar[83:86], "test": bundle.calendar[86:89]}
        if any(len(period) != 3 for period in dates.values()):
            raise ValueError("utility smoke requires at least 90 sessions")
        protocols = {name: synthetic_protocol(bundle, [str(session_time(day, "15:30")) for day in period],
                         max_weight=.4, min_cash=.05, initial_holdings=True) for name, period in dates.items()}
        context = {"fit_cutoff": protocols["validation"]["sessions"][0]["decision_time"],
                   "selection_cutoff": protocols["test"]["sessions"][0]["decision_time"]}
        config = DecisionTrainingConfig(epochs=epochs, patience=2, seed=seed, detach_every=2, max_episode_steps=8)
        continuous = ContinuousConfig()
        fees = Fees(**protocols["train"]["fees"])
        policy = {"risk": asdict(RiskRecipe(20, 10, .2)),
                  "cost": asdict(CostRecipe(10, 10, "2020-01-01T00:00:00+08:00")),
                  "optimizer": {"max_weight": .4, "min_cash": .05, "stability_penalty": .02},
                  "fees": fees, "min_history": 20, "production_policy": production_policy, "dust_threshold": dust_threshold}
        thresholds = {"max_daily_equity_gap_cny": 10., "max_total_cost_gap_cny": 20.,
                      "max_total_cost_relative_gap": .1}
        plan = {"schema_version": "utility_smoke_plan_v1", "created_at": experiments.now(),
            "implementation_hash": experiments.code_identity(), "script_hash": experiments.digest(Path(__file__)),
            "environment": environment(), "source": str(source), "source_artifact_hash": source_record["artifact_hash"],
            "initialization": model.source, "data_identity": bundle.identity, "context": context,
            "train": asdict(config), "continuous": asdict(continuous), "policy": serialize(policy),
            "protocol_ids": {name: targets.validate_protocol(p) for name, p in protocols.items()},
            "diagnostic_thresholds": thresholds, "initial_book": "first three visible assets, <=9% each; independently reset per episode",
            "scope": "one fold/seed synthetic software smoke; not N04 comparison or economic confirmation"}
        experiments.write(output / "plan.json", plan)
        for name, protocol in protocols.items():
            experiments.write(output / f"{name}-protocol.json", protocol)
        attempt.update(state="running", started_at=experiments.now(), history=[])
        experiments.write(output / "attempt.json", attempt)
        def log(entry):
            if isinstance(entry, dict):
                attempt["history"].append(entry)
                experiments.write(output / "attempt.json", attempt)
        fit(model, bundle, protocols["train"], protocols["validation"], policy_config=policy,
            context=context, config=config, continuous=continuous, log=log)
        model.save(output / "model")
        restored = DecisionGRU.load(output / "model")
        if any(not torch.equal(a, b) for a, b in zip(model.net.parameters(), restored.net.parameters(), strict=True)):
            raise ValueError("selected decision checkpoint reload differs")
        test = protocols["test"]
        validate_feedback(bundle, test, session_time(bundle.calendar[90], "15:30"))
        evaluations = {}
        with torch.no_grad():
            for name, current in (("B1-float64-control", controls), ("B2-utility", restored)):
                root = output / name
                root.mkdir()
                exact_policy = DecisionPolicy(current, bundle, **policy)
                exact = targets.replay(test, exact_policy)
                experiments.write(root / "policy.json", {"decisions": exact_policy.audit})
                experiments.write(root / "submission.json", {"schema_version": "target_submission_v2",
                    "protocol_id": exact["protocol_id"], "decisions": exact["decisions"]})
                score = targets.evaluate(output / "test-protocol.json", root / "submission.json", root / "exact")
                if score["status"] != "COMPLETED":
                    raise ValueError(str(score))
                targets.verify(root / "exact", inputs=True)
                if experiments.object_digest(exact) != experiments.object_digest(experiments.read(root / "exact/replay.json")):
                    raise ValueError("exact closed-loop policy and independent submission replay differ")
                continuous_policy = DecisionPolicy(current, bundle, **policy)
                episode = run_episode(test, continuous_policy, config=continuous)
                experiments.write(root / "continuous.json", serialize(episode))
                experiments.write(root / "continuous-policy.json", {"decisions": continuous_policy.audit})
                frozen = {audit["decision_id"]: dict(zip(audit["assets"], numbers(intent.weights)))
                          for audit, intent in zip(episode["audits"], episode["intents"], strict=True)}
                def fixed(context):
                    values = frozen[context.decision_id]
                    if any(weight != 0 for code, weight in values.items() if code not in context.assets):
                        raise ValueError("continuous action has nonzero asset absent from exact context")
                    weights = tuple((code, values.get(code, 0)) for code in context.assets)
                    receivable = context.state.receivable_equity / context.state.equity
                    cls = LifecycleTargetWeights if isinstance(context.state, LifecycleAccountState) else TargetWeights
                    extra = {"receivable_weight": receivable} if cls is LifecycleTargetWeights else {}
                    return cls(context.decision_id, context.state.observed_at, context.execution_not_before,
                        "cn_alloc_daily_v1", "frozen-continuous-action-diagnostic", context.state.data_snapshot_id,
                        context.state.state_id, context.universe_id, context.protocol_id, weights,
                        1 - receivable - sum(w for _, w in weights), **extra)
                fixed_result = targets.replay(test, fixed)
                experiments.write(root / "fixed-continuous-action-exact.json", fixed_result)
                comparisons = {"closed_loop": compare(episode, exact, thresholds),
                               "fixed_actions": compare(episode, fixed_result, thresholds)}
                experiments.write(root / "differences.json", comparisons)
                evaluations[name] = {"exact_net_return": exact["net_return"], "exact_total_cost": exact["total_cost"],
                    "continuous_net_return": float(episode["equity"][-1] / episode["equity"][0] - 1),
                    "closed_loop_comparison_eligible": comparisons["closed_loop"]["comparison_eligible"],
                    "threshold_breaches": comparisons["closed_loop"]["threshold_breaches"]}
        bundle.assert_unchanged()
        if experiments.verify(source) != source_record or experiments.code_identity() != plan["implementation_hash"]:
            raise ValueError("utility smoke source or implementation changed")
        attempt.update(state="succeeded", finished_at=experiments.now())
        result = {"schema_version": "utility_smoke_result_v1", "status": "COMPLETED",
            "implementation_hash": plan["implementation_hash"], "best_epoch": restored.fit_record["best_epoch"],
            "parameter_change_norm": restored.fit_record["parameter_change_norm"], "evaluations": evaluations,
            "evidence_status": "SYNTHETIC_ONLY", "model_superiority": "NOT_ESTABLISHED",
            "limitations": ["one_fold_one_seed", "script_only_no_cli_worker", "whole_fold_rerun_only",
                            "fractional_minimum_fee_proxy", "no_economic_inference"]}
        lines = ["# GRU utility training smoke", "", "Evidence: SYNTHETIC_ONLY. Model superiority: NOT_ESTABLISHED.", "",
            f"Selected epoch: {result['best_epoch']}; parameter change norm: {result['parameter_change_norm']:.8g}.", "",
            "| Model | Exact net return | Exact cost CNY | Continuous net return | Proxy comparison eligible |",
            "| --- | ---: | ---: | ---: | --- |"]
        for name, row in evaluations.items():
            lines.append(f"| {name} | {row['exact_net_return']:.8g} | {row['exact_total_cost']:.8g} | "
                         f"{row['continuous_net_return']:.8g} | {row['closed_loop_comparison_eligible']} |")
        lines += ["", "Threshold breaches remain in differences.json. A completed software smoke does not imply the proxy is accurate.",
            "", "Three paths per model: own-state continuous policy, own-state exact policy with independent submission replay, "
            "and frozen continuous actions re-executed on exact states as a diagnostic.",
            "", "One fold and seed; separate initial accounts; no test selection; no CLI/worker integration or market conclusion."]
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        experiments.write(output / "result.json", result)
        return result
    except BaseException as exc:
        attempt.update(state="failed", finished_at=experiments.now(), error=f"{type(exc).__name__}: {exc}")
        experiments.write(output / "result.json", {"status": "INVALID", "error": attempt["error"]})
        raise
    finally:
        experiments.write(output / "attempt.json", attempt)
        experiments.write(output / "checksums.json", experiments.inventory(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-fold", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--production-policy", choices=["torch_box_budget_turnover_v1", "torch_box_budget_turnover_dust_v1"],
                        default="torch_box_budget_turnover_v1")
    parser.add_argument("--dust-threshold", type=float)
    args = parser.parse_args()
    result = compute(args.source_run, args.bundle, args.output, source_fold=args.source_fold, seed=args.seed, epochs=args.epochs,
                     production_policy=args.production_policy, dust_threshold=args.dust_threshold)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
