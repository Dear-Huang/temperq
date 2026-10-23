"""Diagnose a frozen allocation with controlled continuous execution ablations."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from qwb import experiments
from qwb.bench import targets
from qwb.observations.bundle import PITBundle
from qwb.portfolio.continuous import ContinuousConfig, numbers, tensor
from qwb.portfolio.episode import ContinuousIntent, run_episode
from qwb.portfolio.lifecycle import LifecycleAccountState
from qwb.portfolio.contracts import AccountState, timestamp
from qwb.portfolio.prediction import environment
from qwb.portfolio.recipes import CostRecipe, RiskRecipe, estimate
from qwb.portfolio.reconciliation import cost_decomposition
from qwb.portfolio.reference import Fees
from qwb.training.utility import net_utility


SCENARIOS = {
    "fractional_no_fees": ContinuousConfig(settlement=False, market_restrictions=False, fee_model="none"),
    "fractional_proportional": ContinuousConfig(settlement=False, market_restrictions=False, minimum_commission=False),
    "fractional_minimum": ContinuousConfig(settlement=False, market_restrictions=False),
    "integer_minimum": ContinuousConfig(integer_lots=True, settlement=False, market_restrictions=False),
    "integer_settlement": ContinuousConfig(integer_lots=True, market_restrictions=False),
    "exact_constraints": ContinuousConfig(integer_lots=True),
    "causal_linear_fills": ContinuousConfig(minimum_commission=False, fee_model="causal_linear"),
    "hold_shares_control": ContinuousConfig(integer_lots=True),
}


def compute(source, output, *, data_dir=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    attempt_states = {}
    try:
        expected = targets.read(source / "checksums.json")
        inventory = experiments.inventory(source)
        inventory.pop("checksums.json", None)
        if inventory != expected:
            raise ValueError("allocation source inventory changed")
        source_result = targets.read(source / "result.json")
        if source_result["status"] != "COMPLETED":
            raise ValueError("continuous audit requires a completed frozen allocation")
        protocol = targets.read(source / "protocol.json")
        submission = targets.read(source / "submission.json")
        allocation = targets.read(source / "allocation_audit.json")
        original = targets.replay_submission(protocol, submission)
        if experiments.object_digest(original) != experiments.object_digest(targets.read(source / "account/replay.json")):
            raise ValueError("current independent account cannot reproduce the frozen source path")
        paths = targets.read(source / "inputs.json")
        bundle = PITBundle(data_dir or Path(paths["observation/manifest.json"]["path"]).parent)
        if bundle.identity != targets.read(source / "plan.json")["data_identity"]:
            raise ValueError("audit observation data identity mismatch")
        attempts = {row["decision_id"]: row for row in allocation["attempts"]}
        source_actions = {row["decision_id"]: row for row in submission["decisions"]}
        if len(attempts) != len(allocation["attempts"]) or set(attempts) != set(source_actions):
            raise ValueError("source decisions and attempts differ")
        for session, states in zip(protocol["sessions"], original["states"], strict=True):
            attempt = attempts[session["decision_id"]]
            if attempt["status"] != "target":
                continue
            cls = LifecycleAccountState if states["before"]["schema_version"] == "account_state_v2" else AccountState
            account = cls.from_dict(states["before"])
            _, _, _, recipe = estimate(bundle, account.observed_at, tuple(attempt["assets"]), equity=account.equity,
                fees=Fees(**protocol["fees"]), risk=RiskRecipe(**allocation["config"]["risk"]),
                cost=CostRecipe(**allocation["config"]["cost"]))
            if (experiments.object_digest(recipe) != experiments.object_digest(attempt["recipe"])
                    or account.state_id != attempt["state_id"]):
                raise ValueError("source causal recipe/state cannot be recomputed")
        plan = {"schema_version": "continuous_audit_plan_v1", "created_at": experiments.now(),
            "implementation_hash": experiments.code_identity(), "environment": environment(),
            "source": str(source), "source_inventory": expected, "source_implementation_hash": source_result["implementation_hash"],
            "data_identity": bundle.identity, "scenarios": {name: asdict(cfg) for name, cfg in SCENARIOS.items()},
            "exact_acceptance": {"money_absolute": 1e-8 * max(1, original["initial_equity"]),
                                 "quantity_absolute": 1e-9, "settlement_date_comparison": "per asset and UTC maturity"},
            "selection": "none; retain all scenarios and errors", "scope": "retrospective fixed-action software diagnosis; not closed-loop model comparison"}
        experiments.write(output / "plan.json", plan)
        attempt_states = {name: {"state": "queued"} for name in SCENARIOS}
        experiments.write(output / "attempts.json", attempt_states)
        decomposition = cost_decomposition(original, allocation)
        experiments.write(output / "cost_decomposition.json", decomposition)
        results = {}
        for name, config in SCENARIOS.items():
            attempt_states[name] = {"state": "started"}
            experiments.write(output / "attempts.json", attempt_states)
            def policy(context):
                row = source_actions[context.decision_id]
                rates = None
                if config.fee_model == "causal_linear":
                    attempt = attempts[context.decision_id]
                    index = {code: i for i, code in enumerate(attempt.get("assets", []))}
                    if attempt["status"] == "target":
                        rates = tuple(tensor([attempt["recipe"][direction][index[code]] for code in context.state.assets])
                                      for direction in ("buy_cost", "sell_cost"))
                    else:
                        rates = (tensor([0.] * len(context.state.assets)), tensor([0.] * len(context.state.assets)))
                if name == "hold_shares_control" or row["schema_version"] == "decision_failure_v1":
                    return ContinuousIntent(None, rates, hold_reason="fixed hold-shares control" if name == "hold_shares_control" else row["reason"])
                weights = {pair["asset_id"]: pair["target_weight"] for pair in row["weights"]}
                if set(weights) - set(context.state.assets):
                    # Zero-weight assets outside the current candidate/held union
                    # carry no action; nonzero missing assets are never dropped.
                    if any(value != 0 for code, value in weights.items() if code not in context.state.assets):
                        raise ValueError("fixed nonzero action asset is absent from continuous context")
                return ContinuousIntent(tensor([weights.get(code, 0) for code in context.state.assets]), rates)
            try:
                episode = run_episode(protocol, policy, config=config)
                loss, utility = net_utility(episode["equity"])
                audit = numbers(episode["audits"])
                results[name] = {"status": "COMPLETED", "equity": numbers(episode["equity"]),
                    "total_cost": sum(row["total_cost"] for row in audit), "utility_loss": numbers(loss),
                    "utility": numbers(utility), "final_equity": float(episode["final_state"].equity.detach())}
                if name == "exact_constraints":
                    differences = {key: 0.0 for key in ("equity", "cash", "quantity", "locked_quantity", "settlement_schedule", "cost", "receivable")}
                    for (_, state), row, exact_state, exact_audit in zip(episode["states"], audit, original["states"], original["audits"], strict=True):
                        exact = exact_state["after"]
                        positions = {p["asset_id"]: p for p in exact["positions"]}
                        differences["equity"] = max(differences["equity"], abs(row["equity_close"] - exact_audit["equity_close"]))
                        differences["cash"] = max(differences["cash"], abs(float(state.cash.detach()) - exact["cash"]))
                        differences["cost"] = max(differences["cost"], abs(row["total_cost"] - exact_audit["total_cost"]))
                        differences["receivable"] = max(differences["receivable"], abs(float(state.receivable.detach()) - exact_audit.get("receivable_equity", 0)))
                        for i, code in enumerate(state.assets):
                            position = positions.get(code, {"quantity": 0, "unsettled": []})
                            locked = sum(float(lot.quantity[i].detach()) for lot in state.lots)
                            differences["quantity"] = max(differences["quantity"], abs(float(state.quantity[i].detach()) - position["quantity"]))
                            differences["locked_quantity"] = max(differences["locked_quantity"], abs(locked - sum(lot["quantity"] for lot in position["unsettled"])))
                            schedule = {}
                            reference_schedule = {}
                            for lot in state.lots:
                                at = str(timestamp(lot.sellable_after))
                                schedule[at] = schedule.get(at, 0) + float(lot.quantity[i].detach())
                            for lot in position["unsettled"]:
                                at = str(timestamp(lot["sellable_after"]))
                                reference_schedule[at] = reference_schedule.get(at, 0) + lot["quantity"]
                            for at in set(schedule) | set(reference_schedule):
                                differences["settlement_schedule"] = max(differences["settlement_schedule"],
                                    abs(schedule.get(at, 0) - reference_schedule.get(at, 0)))
                    quantities = {"quantity", "locked_quantity", "settlement_schedule"}
                    if any(value > plan["exact_acceptance"]["quantity_absolute" if key in quantities else "money_absolute"]
                           for key, value in differences.items()):
                        raise ValueError(f"continuous exact-mode account differs from independent Bench: {differences}")
                    results[name]["independent_max_absolute_differences"] = differences
                experiments.write(output / f"{name}.json", {"config": asdict(config), "audits": audit, "result": results[name]})
            except (ValueError, KeyError) as exc:
                results[name] = {"status": "INVALID", "error": f"{type(exc).__name__}: {exc}"}
            attempt_states[name] = {"state": results[name]["status"],
                                    **({"error": results[name]["error"]} if "error" in results[name] else {})}
            experiments.write(output / "attempts.json", attempt_states)
        bundle.assert_unchanged()
        current = experiments.inventory(source)
        current.pop("checksums.json", None)
        if current != expected or experiments.code_identity() != plan["implementation_hash"]:
            raise ValueError("audit inputs/implementation changed during execution")
        result = {"schema_version": "continuous_audit_result_v1", "status": "COMPLETED" if all(row["status"] == "COMPLETED" for row in results.values()) else "DIAGNOSTIC_WITH_FAILURES",
            "implementation_hash": plan["implementation_hash"], "source_independent_replay": "MATCH",
            "source_causal_recipes": "RECOMPUTED", "scenarios": results, "cost_totals": decomposition["totals"],
            "evidence_status": original["evidence_status"], "economic_efficacy": "NOT_ESTABLISHED"}
        experiments.write(output / "result.json", result)
        lines = ["# Continuous Account Diagnostic", "", f"Evidence: **{original['evidence_status']}**. Economic efficacy: **NOT_ESTABLISHED**.",
            "", "Frozen actions and initial account; no model retraining or selection. Causal recipes were recomputed against the original decision states.",
            "", "| Scenario | Status | Final Equity CNY | Costs CNY |", "| --- | --- | ---: | ---: |"]
        for name, row in results.items():
            lines.append(f"| {name} | {row['status']} | {row.get('final_equity', float('nan')):.6f} | {row.get('total_cost', float('nan')):.6f} |")
        lines += ["", "## Same-Fill Cost Decomposition", "", "| Component | CNY |", "| --- | ---: |"]
        for key, value in decomposition["totals"].items():
            if key != "relative_actual_to_filled_gap":
                lines.append(f"| {key} | {value:.9f} |")
        lines += ["", "Components reprice identical actual fills. Scenario differences additionally change account state, funding and fills; they are not additive causal performance contributions.",
            "The hold-shares control is distinct from repeatedly targeting old decision weights. Fractional minimum-fee models can charge for tiny trades avoided by integer execution.",
            "Integer rounding has zero local derivatives. HiGHS production projections are not differentiated. This report does not establish closed-loop GRU utility training or market efficacy."]
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except BaseException as exc:
        for row in attempt_states.values():
            if row["state"] in {"queued", "started"}:
                row.update(state="not_run" if row["state"] == "queued" else "failed_run", error=f"{type(exc).__name__}: {exc}")
        if attempt_states:
            experiments.write(output / "attempts.json", attempt_states)
        experiments.write(output / "result.json", {"status": "INVALID_OR_INTERRUPTED", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        experiments.write(output / "checksums.json", experiments.inventory(output))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="frozen prediction allocation result directory")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    result = compute(args.source, args.output, data_dir=args.data_dir)
    print(f"{result['status']}: {args.output / 'result.json'}")
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
