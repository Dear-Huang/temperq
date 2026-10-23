"""Freeze solver outputs and compare production rules at unchanged checkpoints."""

from __future__ import annotations

import argparse
from pathlib import Path

from qwb import experiments
from qwb.bench import targets
from qwb.observations.bundle import PITBundle
from qwb.portfolio.continuous import ContinuousConfig, tensor
from qwb.portfolio.decision_evaluation import evaluate
from qwb.portfolio.lifecycle import LifecycleTargetWeights
from qwb.portfolio.production import project, tensor_produce, tensor_produce_dust
from qwb.training.decision import DecisionGRU
from qwb.training.utility_runner import policy_config, verify

import numpy as np
import torch
import yaml

POLICIES = ("strict_raw", "highs_l1_projection_v1", "torch_box_budget_turnover_v1", "torch_box_budget_turnover_dust_v1")


def frozen_maps(raw, inputs, assets, *, buffer, dust_threshold):
    """Run four forward maps on one frozen solve, without rerunning a solver."""
    raw, previous, lower, upper = [np.asarray(value, dtype=float) for value in
        (raw, inputs["previous_weights"], inputs["lower"], inputs["upper"])]
    if any(value.shape != (len(assets),) or not np.isfinite(value).all() for value in (raw, previous, lower, upper)):
        raise ValueError("frozen production inputs are not finite/aligned")
    budget, turnover, tolerance = (inputs[key] for key in ("budget", "max_turnover", "tolerance"))
    results = {}
    for name in POLICIES:
        try:
            if name == "strict_raw":
                output, audit = raw.copy(), {"policy": name, "postprocessed": False}
            elif name == "highs_l1_projection_v1":
                output, audit = project(raw.copy(), previous.copy(), lower.copy(), upper.copy(),
                    budget=budget, max_turnover=turnover, tolerance=tolerance)
            else:
                produce = tensor_produce_dust if name.endswith("dust_v1") else tensor_produce
                extra = {"dust_threshold": dust_threshold} if name.endswith("dust_v1") else {}
                value, audit = produce(tensor(raw.copy()), tensor(previous.copy()), tensor(lower.copy()), tensor(upper.copy()),
                    budget=tensor(budget), max_turnover=turnover, tolerance=tolerance, buffer=buffer, **extra)
                output = value.detach().numpy()
            # Structural target validation is separate from actual account binding.
            target = LifecycleTargetWeights("frozen", "2000-01-01T15:30:00+08:00", "2000-01-02T09:30:00+08:00",
                "cn_alloc_daily_v1", "frozen-map-diagnostic", "frozen", "frozen", "frozen", "frozen",
                tuple(zip(assets, output.tolist())), float(1 - inputs["receivable_weight"] - output.sum()),
                receivable_weight=inputs["receivable_weight"])
            residual = max(float((lower - output).max()), float((output - upper).max()),
                float(output.sum() - budget), float(np.abs(output - previous).sum() - turnover), 0.)
            adjustment = float(np.abs(output - raw).max())
            if (target.cash_weight < inputs["min_cash"] or (output > inputs["max_weight"]).any()
                    or residual > tolerance or adjustment > tolerance):
                raise ValueError("produced target exceeds declared constraints or repair budget")
            results[name] = {"status": "accepted", "weights": output.tolist(), "audit": audit,
                "max_absolute_adjustment": adjustment, "l1_adjustment": float(np.abs(output - raw).sum()),
                "constraint_residual": residual, "turnover": float(np.abs(output - previous).sum())}
        except ValueError as exc:
            results[name] = {"status": "rejected", "reason": f"{type(exc).__name__}: {exc}"}
    return {"raw_weights": raw.tolist(), "inputs": inputs, "assets": list(assets), "policies": results,
        "scope": "frozen_forward_and_structural_target_only; actual_account_binding_checked_in_closed_loop"}


def compute(source, output, *, fold=0, seed=0, dust_threshold=1e-10):
    source, output = Path(source).resolve(), Path(output).resolve()
    if experiments.read(source / "metrics.json").get("outer_evaluation_status", "COMPLETED") != "COMPLETED":
        raise ValueError("production ablation requires a complete outer evaluation without decision failures")
    verify(source)
    source_record = experiments.verify(source)
    rc = yaml.safe_load((source / "config.resolved.yaml").read_text(encoding="utf-8"))
    bundle = PITBundle(rc["data"]["dir"], lookback=rc["observations"]["lookback"])
    if bundle.manifest["data_kind"] != "synthetic":
        raise ValueError("production ablation demo is restricted to synthetic data")
    record = next(row for row in experiments.read(source / "folds.json")["folds"] if row["fold"] == fold)
    trial = next(row for row in record["seeds"] if row["seed"] == seed)
    protocol = experiments.read(source / f"protocol-fold-{fold:04d}-test.json")
    models = {"B1-float64-control": source / trial["control_checkpoint"], "B2-utility": source / trial["checkpoint"]}
    checkpoints = {name: experiments.inventory(path) for name, path in models.items()}
    implementation, script_hash = experiments.code_identity(), experiments.digest(Path(__file__))
    output.mkdir(parents=True, exist_ok=False)
    plan = {"schema_version": "utility_production_ablation_v1", "created_at": experiments.now(),
        "source": str(source), "source_artifact_hash": source_record["artifact_hash"],
        "implementation_hash": implementation, "script_hash": script_hash,
        "fold": fold, "seed": seed, "data_identity": bundle.identity,
        "protocol_id": targets.validate_protocol(protocol), "checkpoint_inventories": checkpoints,
        "buffer": rc["portfolio"]["production"]["buffer"], "dust_threshold": dust_threshold,
        "acceptance": rc["acceptance"], "frozen_policies": POLICIES, "closed_loop_policies": POLICIES[2:],
        "scope": "fixed checkpoints; no training or model selection; synthetic diagnosis; not N04"}
    experiments.write(output / "plan.json", plan)
    attempts = [{"model": model, "policy": name, "state": "queued"} for model in models for name in POLICIES[2:]]
    experiments.write(output / "attempts.json", attempts)
    frozen, summaries = [], {}
    threads = torch.get_num_threads()
    try:
        torch.set_num_threads(rc["train"]["cpu_threads"])
        for attempt in attempts:
            name, model_name = attempt["policy"], attempt["model"]
            directory = output / model_name / name
            attempt.update(state="running")
            experiments.write(output / "attempts.json", attempts)
            try:
                model = (DecisionGRU.from_prediction(models[model_name]) if model_name == "B1-float64-control"
                         else DecisionGRU.load(models[model_name]))
                policy = dict(policy_config(rc, protocol), production_policy=name,
                    dust_threshold=dust_threshold if name.endswith("dust_v1") else None)
                result, _ = evaluate(model, bundle, protocol, directory, policy_config=policy,
                    continuous=ContinuousConfig(**rc["continuous"]), acceptance=rc["acceptance"])
                differences = experiments.read(directory / "differences.json")
                summaries.setdefault(model_name, {})[name] = {**result,
                    "continuous_total_cost": differences["closed_loop"]["continuous_total_cost"],
                    "fixed_action_exact_cost": differences["fixed_actions"]["exact_total_cost"],
                    "closed_loop_gaps": differences["closed_loop"]["max_absolute_gaps"],
                    "checkpoint_identity": experiments.object_digest(checkpoints[model_name])}
                if name == POLICIES[2]:
                    for path, filename in (("exact", "policy.json"), ("continuous", "continuous-policy.json")):
                        for decision in experiments.read(directory / filename)["decisions"]:
                            frozen.append({"model": model_name, "path": path, "decision_id": decision["decision_id"],
                                **frozen_maps(decision["allocation"]["raw_weights"], decision["production_inputs"],
                                    decision["assets"], buffer=plan["buffer"], dust_threshold=dust_threshold)})
                attempt.update(state="succeeded")
            except (ValueError, OSError, KeyError) as exc:
                attempt.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            experiments.write(output / "attempts.json", attempts)
        bundle.assert_unchanged()
        if (experiments.verify(source) != source_record or experiments.code_identity() != implementation
                or experiments.digest(Path(__file__)) != script_hash
                or {name: experiments.inventory(path) for name, path in models.items()} != checkpoints):
            raise ValueError("production ablation input or implementation changed")
        experiments.write(output / "frozen-map-comparison.json", {"rows": frozen, "counts": {
            name: {status: sum(row["policies"][name]["status"] == status for row in frozen)
                   for status in ("accepted", "rejected")} for name in POLICIES}})
        result = {"status": "COMPLETED" if all(row["state"] == "succeeded" for row in attempts) else "FAILED",
            "evidence_status": "SYNTHETIC_ONLY", "economic_efficacy": "NOT_ESTABLISHED",
            "model_superiority": "NOT_ESTABLISHED", "trained": False, "evaluations": summaries,
            "frozen_solve_count": len(frozen), "scope": plan["scope"]}
        experiments.write(output / "result.json", result)
        lines = ["# Fixed-checkpoint production ablation", "", result["scope"], "",
            "| Model | Production rule | Exact return | Exact cost CNY | Continuous cost CNY | Proxy eligible |",
            "| --- | --- | ---: | ---: | ---: | --- |"]
        for model, rules in summaries.items():
            for rule, row in rules.items():
                lines.append(f"| {model} | {rule} | {row['exact_net_return']:.8g} | {row['exact_total_cost']:.8g} | "
                             f"{row['continuous_total_cost']:.8g} | {row['closed_loop_comparison_eligible']} |")
        lines += ["", "All checkpoints and parameters are fixed before evaluation. Rule differences are not learning gains.",
                  "Failed attempts remain in attempts.json; failed/partial results cannot establish a winner.",
                  "Frozen maps use identical raw solver outputs; their structural checks do not certify account binding."]
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        experiments.write(output / "checksums.json", experiments.inventory(output))
        return result
    finally:
        torch.set_num_threads(threads)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--utility-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fold", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--dust-threshold", type=float, default=1e-10)
    args = parser.parse_args()
    result = compute(args.utility_run, args.output, fold=args.fold, seed=args.seed, dust_threshold=args.dust_threshold)
    print(f"{result['status']}; {result['frozen_solve_count']} frozen solves; {args.output}")
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
