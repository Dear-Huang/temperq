"""Reconcile frozen utility checkpoints through execution and feedback bridges."""

from __future__ import annotations

import argparse
import math
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from qwb import experiments
from qwb.bench import targets
from qwb.observations.bundle import PITBundle
from qwb.portfolio.continuous import ContinuousConfig, numbers, tensor
from qwb.portfolio.decision_evaluation import compare, evaluate, serialize
from qwb.portfolio.episode import ContinuousIntent, run_episode
from qwb.portfolio.prediction import environment
from qwb.portfolio.reconciliation import cost_decomposition
from qwb.training.decision import DecisionGRU
from qwb.training.utility_runner import policy_config, verify

import torch
import yaml


BRIDGE_ORDER = ("fractional_minimum", "fractional_proportional", "integer_proportional",
                "integer_minimum", "independent_fixed_actions", "independent_own_state")
OBSERVATION_TOLERANCES = {"money_cny": 1e-7, "quantity": 1e-8}


def scenarios(config):
    if config != ContinuousConfig():
        raise ValueError("execution audit v1 requires fractional declared fees and all execution switches enabled")
    integer = replace(config, integer_lots=True)
    return {"fractional_minimum": config,
            "fractional_proportional": replace(config, minimum_commission=False),
            "integer_proportional": replace(integer, minimum_commission=False),
            "integer_minimum": integer,
            "integer_without_settlement": replace(integer, settlement=False),
            "integer_without_market_restrictions": replace(integer, market_restrictions=False),
            "hold_shares_control": integer}


def unique_rows(rows, key):
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"duplicate diagnostic {key}")
    return result


def fixed_policy(policies, *, hold=False):
    by_id = unique_rows(policies, "decision_id")

    def policy(context):
        row = by_id[context.decision_id]
        if hold:
            return ContinuousIntent(None, hold_reason="predeclared hold-shares control")
        weights = dict(zip(row["assets"], row["production"]["produced_weights"], strict=True))
        if any(value != 0 for code, value in weights.items() if code not in context.state.assets):
            raise ValueError("frozen nonzero target is absent from diagnostic account")
        return ContinuousIntent(tensor([weights.get(code, 0) for code in context.state.assets]))

    return policy


def same_fill_costs(account, policies):
    attempts = []
    for row in policies:
        previous = row["production_inputs"]["previous_weights"]
        produced = row["production"]["produced_weights"]
        recipe = row["recipe"]
        intended = recipe["actual_equity"] * math.fsum(
            buy * max(w - h, 0) + sell * max(h - w, 0)
            for w, h, buy, sell in zip(produced, previous, recipe["buy_cost"], recipe["sell_cost"], strict=True))
        attempts.append({"decision_id": row["decision_id"], "status": "target", "assets": row["assets"],
                         "recipe": recipe, "intended_proxy_cost": intended})
    result = cost_decomposition(account, {"attempts": attempts}, schema_version="same_fill_cost_decomposition_v2")
    for row in result["daily"]:
        row["actual_to_filled_gap"] = row["actual_cost"] - row["filled_proxy_cost"]
        row["actual_to_intended_gap"] = row["actual_cost"] - row["intended_proxy_cost"]
        row["bridge_residual"] = (row["actual_to_intended_gap"] - row["intent_to_fill_gap"]
                                  - row["actual_to_filled_gap"])
        for key in ("actual_to_filled_gap", "actual_to_intended_gap", "bridge_residual"):
            row["bp_of_initial_equity"][key] = row[key] / result["initial_equity_cny"] * 10000
    result["recipe_scope"] = "frozen original decision-state recipe; no counterfactual refit"
    return result


def fill_differences(approximate, exact):
    if len(approximate) != len(exact):
        raise ValueError("diagnostic session coverage differs")
    rows = []
    fields = ("requested_quantity", "quantity", "unfilled_quantity", "notional", "commission", "stamp_tax",
              "slippage", "total_cost")
    for first, second in zip(approximate, exact, strict=True):
        if first["decision_id"] != second["decision_id"]:
            raise ValueError("diagnostic sessions are not aligned")
        indexed = []
        for audit in (first, second):
            mapping = {(f["asset_id"], f["side"]): f for f in audit["fills"]}
            if len(mapping) != len(audit["fills"]):
                raise ValueError("duplicate asset/side fill; explicit order matching required")
            indexed.append(mapping)
        for asset, side in sorted(set(indexed[0]) | set(indexed[1])):
            left, right = (mapping.get((asset, side)) for mapping in indexed)
            row = {"decision_id": first["decision_id"], "asset_id": asset, "side": side,
                   "continuous": left, "exact": right, "gaps": {}}
            for field in fields:
                row["gaps"][field] = float((left or {}).get(field, 0)) - float((right or {}).get(field, 0))
            row["record_mismatch"] = (left is None or right is None
                                      or left.get("reason") != right.get("reason"))
            rows.append(row)
    return rows


def first_divergence(differences, fills, *, money, quantity):
    by_id = {}
    for row in fills:
        by_id.setdefault(row["decision_id"], []).append(row)
    for row in differences["daily"]:
        triggers = [key for key in ("equity_gap_cny", "cash_gap_cny", "receivable_gap_cny", "cost_gap_cny")
                    if abs(row[key]) > money]
        triggers += [key for key in ("quantity_gap", "locked_quantity_gap", "unfilled_quantity_gap")
                     if abs(row[key]) > quantity]
        for phase, detail in row.get("lifecycle_states", {}).items():
            if (detail["entitlement_quantity_gap"] > quantity or detail["entitlement_amount_gap_cny"] > money
                    or detail["entitlement_mismatch_count"] or detail["processed_event_mismatch_count"]):
                triggers.append(f"{phase}_lifecycle")
        changed = [fill for fill in by_id.get(row["decision_id"], ()) if fill["record_mismatch"] or any(
            abs(value) > (quantity if "quantity" in key else money) for key, value in fill["gaps"].items())]
        if triggers or changed:
            return {"decision_id": row["decision_id"], "state_or_cost_triggers": triggers, "fills": changed,
                    "scope": "first observed mismatch, including order metadata; not a unique causal attribution"}
    return None


def summary(audits):
    unique_rows(audits, "decision_id")
    daily = []
    for row in audits:
        values = {"decision_id": row["decision_id"], **{key: float(row[key]) for key in
                  ("equity_before", "equity_close", "total_cost", "overnight_pnl", "intraday_pnl")},
                  "corporate_action_pnl": float(row.get("corporate_action_pnl", 0))}
        values["pnl_residual"] = (values["equity_close"] - values["equity_before"] - values["overnight_pnl"]
                                  - values["intraday_pnl"] - values["corporate_action_pnl"] + values["total_cost"])
        if abs(values["pnl_residual"]) > 1e-7:
            raise ValueError("diagnostic daily PnL does not reconcile")
        daily.append(values)
    if not daily or any(not math.isfinite(value) for row in daily for key, value in row.items() if key != "decision_id"):
        raise ValueError("diagnostic metrics require finite nonempty sessions")
    return {"daily": daily, "initial_equity": daily[0]["equity_before"],
            "total_cost": math.fsum(row["total_cost"] for row in daily),
            "final_equity": daily[-1]["equity_close"]}


def bridge(results, order=BRIDGE_ORDER):
    missing = [name for name in order if name not in results]
    if missing:
        return {"status": "INCOMPLETE", "missing_scenarios": missing}
    coverage = [[row["decision_id"] for row in results[name]["daily"]] for name in order]
    if any(ids != coverage[0] for ids in coverage):
        raise ValueError("bridge requires identical ordered decision coverage")
    if any(results[name]["initial_equity"] != results[order[0]]["initial_equity"] for name in order):
        raise ValueError("bridge requires identical initial equity")
    edges = []
    for start, end in zip(order, order[1:]):
        first, second = results[start], results[end]
        edges.append({"from": start, "to": end,
                      "cost_delta_cny": second["total_cost"] - first["total_cost"],
                      "final_equity_delta_cny": second["final_equity"] - first["final_equity"],
                      "daily": [{"decision_id": left["decision_id"],
                          "cost_delta_cny": right["total_cost"] - left["total_cost"],
                          "equity_delta_cny": right["equity_close"] - left["equity_close"],
                          "prior_equity_delta_cny": right["equity_before"] - left["equity_before"],
                          **{f"{key}_delta_cny": right[key] - left[key] for key in
                             ("overnight_pnl", "intraday_pnl", "corporate_action_pnl")}}
                          for left, right in zip(first["daily"], second["daily"], strict=True)]})
        for row in edges[-1]["daily"]:
            row["pnl_residual_cny"] = (row["equity_delta_cny"] - row["prior_equity_delta_cny"]
                - row["overnight_pnl_delta_cny"] - row["intraday_pnl_delta_cny"]
                - row["corporate_action_pnl_delta_cny"] + row["cost_delta_cny"])
            if abs(row["pnl_residual_cny"]) > 1e-7:
                raise ValueError("counterfactual PnL bridge does not reconcile")
    total = {"cost_delta_cny": results[order[-1]]["total_cost"] - results[order[0]]["total_cost"],
             "final_equity_delta_cny": results[order[-1]]["final_equity"] - results[order[0]]["final_equity"]}
    residual = {key: total[key] - math.fsum(row[key] for row in edges) for key in total}
    if any(abs(value) > 1e-7 for value in residual.values()):
        raise ValueError("execution bridge does not reconcile")
    return {"status": "COMPLETED", "order": list(order), "edges": edges, "endpoint_deltas": total,
            "residuals": residual, "sign": "next minus previous; endpoint exact own-state minus continuous",
            "scope": "ordered counterfactual differences; interactions and path dependence prevent unique causal attribution"}


def paired_effects(results):
    names = BRIDGE_ORDER[:4]
    if any(name not in results for name in names):
        return {"status": "INCOMPLETE", "missing_scenarios": [name for name in names if name not in results]}
    effects = {}
    for key in ("total_cost", "final_equity"):
        fractional = results[names[0]][key] - results[names[1]][key]
        integer = results[names[3]][key] - results[names[2]][key]
        effects[key] = {"minimum_fee_effect_fractional": fractional, "minimum_fee_effect_integer": integer,
                        "integer_x_minimum_interaction": integer - fractional}
    return {"status": "COMPLETED", "effects": effects,
            "scope": "2x2 frozen-target path effects including funding/fill feedback; not same-fill fee effects"}


def diagnose(protocol, episode, policies, exact_fixed, exact_own, acceptance, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    configs = scenarios(ContinuousConfig())
    expected_ids = [row["decision_id"] for row in protocol["sessions"]]
    if list(unique_rows(policies, "decision_id")) != expected_ids:
        raise ValueError("frozen policy coverage differs from protocol")
    attempts = {name: {"state": "queued"} for name in configs}
    results, failures = {}, {}
    experiments.write(output / "attempts.json", attempts)
    try:
        for name, config in configs.items():
            attempts[name]["state"] = "running"
            experiments.write(output / "attempts.json", attempts)
            try:
                with torch.no_grad():
                    current = run_episode(protocol, fixed_policy(policies, hold=name == "hold_shares_control"), config=config)
                if name == "fractional_minimum" and serialize(current) != serialize(episode):
                    raise ValueError("frozen target replay does not reproduce original continuous episode")
                audit = numbers(current["audits"])
                row = summary(audit)
                payload = {"config": asdict(config), "episode": serialize(current), "summary": row}
                if name != "hold_shares_control":
                    comparison = compare(current, exact_fixed, acceptance,
                                         continuous_policy=policies, exact_actions=exact_fixed["decisions"])
                    fills = fill_differences(audit, exact_fixed["audits"])
                    payload.update(differences=comparison, fills=fills,
                        first_divergence=first_divergence(comparison, fills,
                            money=OBSERVATION_TOLERANCES["money_cny"], quantity=OBSERVATION_TOLERANCES["quantity"]),
                        same_fill_costs=same_fill_costs({"audits": audit}, policies))
                    row["comparison_eligible"] = comparison["comparison_eligible"]
                    row["first_divergence"] = payload["first_divergence"]
                experiments.write(output / f"{name}.json", payload)
                results[name] = row
                attempts[name]["state"] = "succeeded"
            except (ValueError, KeyError, RuntimeError) as exc:
                failures[name] = f"{type(exc).__name__}: {exc}"
                attempts[name].update(state="failed", error=failures[name])
                experiments.write(output / f"{name}.json", {"status": "FAILED", "config": asdict(config), "error": failures[name]})
            experiments.write(output / "attempts.json", attempts)
        results["independent_fixed_actions"] = summary(exact_fixed["audits"])
        results["independent_own_state"] = summary(exact_own["audits"])
        result = {"status": "COMPLETED" if not failures else "DIAGNOSTIC_WITH_FAILURES", "scenarios": results,
                  "failures": failures, "bridge": bridge(results), "paired_effects": paired_effects(results),
                  "scope": "fixed checkpoints and targets; no training or model selection"}
        experiments.write(output / "result.json", result)
        return result
    finally:
        for attempt in attempts.values():
            if attempt["state"] in {"queued", "running"}:
                attempt["state"] = "not_run" if attempt["state"] == "queued" else "interrupted"
        experiments.write(output / "attempts.json", attempts)


def compute(source, output, *, fold=0, seed=0):
    source, output = Path(source).resolve(), Path(output).resolve()
    if experiments.read(source / "metrics.json").get("outer_evaluation_status", "COMPLETED") != "COMPLETED":
        raise ValueError("execution audit requires a complete outer evaluation without decision failures")
    verify(source, inputs=True)
    source_record = experiments.verify(source)
    rc = yaml.safe_load((source / "config.resolved.yaml").read_text(encoding="utf-8"))
    configs = scenarios(ContinuousConfig(**rc["continuous"]))
    bundle = PITBundle(rc["data"]["dir"], lookback=rc["observations"]["lookback"])
    record = next(row for row in experiments.read(source / "folds.json")["folds"] if row["fold"] == fold)
    trial = next(row for row in record["seeds"] if row["seed"] == seed)
    protocol = experiments.read(source / f"protocol-fold-{fold:04d}-test.json")
    models = {"B1-float64-control": source / trial["control_checkpoint"], "B2-utility": source / trial["checkpoint"]}
    checkpoints = {name: experiments.inventory(path) for name, path in models.items()}
    output.mkdir(parents=True, exist_ok=False)
    implementation, script_hash = experiments.code_identity(), experiments.digest(Path(__file__))
    plan = {"schema_version": "utility_execution_audit_plan_v1", "created_at": experiments.now(),
            "implementation_hash": implementation, "script_hash": script_hash, "environment": environment(),
            "source": str(source), "source_artifact_hash": source_record["artifact_hash"],
            "checkpoint_inventories": checkpoints, "data_identity": bundle.identity,
            "protocol_id": targets.validate_protocol(protocol), "fold": fold, "seed": seed,
            "policy_config": policy_config(rc, protocol), "acceptance": rc["acceptance"],
            "scenarios": {name: asdict(config) for name, config in configs.items()},
            "bridge_order": list(BRIDGE_ORDER), "first_divergence_tolerances": OBSERVATION_TOLERANCES,
            "cost_decomposition_version": "same_fill_cost_decomposition_v2",
            "tolerance_scope": "observation only; source qualification thresholds unchanged",
            "scope": "retrospective development diagnostic; no training/selection; not N04"}
    experiments.write(output / "plan.json", plan)
    attempts = {name: {"state": "queued"} for name in models}
    experiments.write(output / "attempts.json", attempts)
    results, threads = {}, torch.get_num_threads()
    try:
        torch.set_num_threads(rc["train"]["cpu_threads"])
        for name, checkpoint in models.items():
            attempts[name]["state"] = "running"
            experiments.write(output / "attempts.json", attempts)
            try:
                model = DecisionGRU.from_prediction(checkpoint) if name == "B1-float64-control" else DecisionGRU.load(checkpoint)
                root = output / name
                root.mkdir()
                evaluate(model, bundle, protocol, root / "evaluation", policy_config=plan["policy_config"],
                         continuous=ContinuousConfig(**rc["continuous"]), acceptance=rc["acceptance"])
                policies = experiments.read(root / "evaluation/continuous-policy.json")["decisions"]
                with torch.no_grad():
                    episode = run_episode(protocol, fixed_policy(policies), config=ContinuousConfig(**rc["continuous"]))
                if serialize(episode) != experiments.read(root / "evaluation/continuous.json"):
                    raise ValueError("source continuous evaluation cannot be replayed from frozen targets")
                fixed = experiments.read(root / "evaluation/fixed-continuous-action-exact.json")
                own = experiments.read(root / "evaluation/exact/replay.json")
                results[name] = diagnose(protocol, episode, policies, fixed, own, rc["acceptance"], root / "mechanisms")
                experiments.write(root / "fixed-action-costs.json", same_fill_costs(fixed, policies))
                own_policies = experiments.read(root / "evaluation/policy.json")["decisions"]
                experiments.write(root / "own-state-costs.json", same_fill_costs(own, own_policies))
                attempts[name]["state"] = "succeeded" if results[name]["status"] == "COMPLETED" else "failed"
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                attempts[name].update(state="failed", error=f"{type(exc).__name__}: {exc}")
            experiments.write(output / "attempts.json", attempts)
        bundle.assert_unchanged()
        if (experiments.verify(source) != source_record or implementation != experiments.code_identity()
                or script_hash != experiments.digest(Path(__file__))
                or checkpoints != {name: experiments.inventory(path) for name, path in models.items()}):
            raise ValueError("execution audit inputs or implementation changed")
        result = {"schema_version": "utility_execution_audit_result_v1",
                  "status": "COMPLETED" if all(a["state"] == "succeeded" for a in attempts.values()) else "DIAGNOSTIC_WITH_FAILURES",
                  "models": results, "trained": False, "model_selected": False,
                  "evidence_status": "SYNTHETIC_ONLY" if protocol["data_kind"] == "synthetic" else "DIAGNOSTIC_ONLY",
                  "economic_efficacy": "NOT_ESTABLISHED", "source_qualification_changed": False}
        experiments.write(output / "result.json", result)
        lines = ["# Frozen-checkpoint execution diagnosis", "", plan["scope"], "",
                 "| Model | Scenario | Final equity CNY | Cost CNY |", "| --- | --- | ---: | ---: |"]
        for name, result_row in results.items():
            for scenario, row in result_row["scenarios"].items():
                lines.append(f"| {name} | {scenario} | {row['final_equity']:.9f} | {row['total_cost']:.9f} |")
        lines += ["", "Bridge deltas are next minus previous. The final edge isolates own-state policy feedback.",
                  "Mechanism effects depend on order; the 2x2 interaction is reported separately. All failed scenarios remain in attempts.json.",
                  "Hold-shares is a control, not the meaning of target weights. Prices, corporate actions and cash affordability remain active.",
                  "Per-fill cost components are in CNY, bp of fixed initial equity and bp of each fill notional; zero notional/proxy denominators give null.",
                  "No training, selection, qualification change, superiority claim or economic confirmation."]
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return result
    except BaseException as exc:
        experiments.write(output / "result.json", {"status": "INVALID_OR_INTERRUPTED", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        torch.set_num_threads(threads)
        for attempt in attempts.values():
            if attempt["state"] in {"queued", "running"}:
                attempt["state"] = "not_run" if attempt["state"] == "queued" else "interrupted"
        experiments.write(output / "attempts.json", attempts)
        inventory = experiments.inventory(output)
        inventory.pop("checksums.json", None)
        experiments.write(output / "checksums.json", inventory)


def compare_recomputed(root, fresh):
    root, fresh = Path(root), Path(fresh)
    inventory = experiments.inventory(root)
    inventory.pop("checksums.json", None)
    if inventory != experiments.read(root / "checksums.json"):
        raise ValueError("execution audit inventory changed")
    expected = experiments.inventory(fresh)
    expected.pop("checksums.json", None)
    if set(inventory) != set(expected):
        raise ValueError("execution audit recomputed file coverage differs")
    for name in inventory:
        if name == "plan.json":
            original, recomputed = experiments.read(root / name), experiments.read(fresh / name)
            original.pop("created_at")
            recomputed.pop("created_at")
            if original != recomputed:
                raise ValueError("execution audit recomputed plan differs")
        elif name.endswith("/evaluation/exact/inputs.json") or name.endswith("/checksums.json"):
            continue
        elif inventory[name] != expected[name]:
            raise ValueError(f"execution audit arithmetic differs: {name}")


def verify_audit(directory):
    root = Path(directory).resolve()
    plan = experiments.read(root / "plan.json")
    if (plan["schema_version"] != "utility_execution_audit_plan_v1"
            or plan["script_hash"] != experiments.digest(Path(__file__))
            or plan["implementation_hash"] != experiments.code_identity()):
        raise ValueError("execution audit verification requires its recorded implementation and script")
    for name in plan["checkpoint_inventories"]:
        targets.verify(root / name / "evaluation/exact", inputs=True)
    with tempfile.TemporaryDirectory(prefix="qwb-execution-verify-") as temporary:
        fresh = Path(temporary) / "audit"
        compute(plan["source"], fresh, fold=plan["fold"], seed=plan["seed"])
        compare_recomputed(root, fresh)
    return {"status": "PASS", "scope": "full execution diagnostic and policy arithmetic; not training reproduction",
            "source_qualification_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--utility-run", type=Path)
    action.add_argument("--verify", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fold", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    if args.verify is not None:
        if args.output is not None:
            parser.error("--output cannot be used with --verify")
        result = verify_audit(args.verify)
        print(f"{result['status']}: {result['scope']}")
        return 0
    if args.output is None:
        parser.error("--utility-run requires --output")
    result = compute(args.utility_run, args.output, fold=args.fold, seed=args.seed)
    print(f"{result['status']}: {args.output / 'result.json'}")
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
