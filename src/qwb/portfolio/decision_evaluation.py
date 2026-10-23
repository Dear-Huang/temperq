"""Own-state policy evaluation and frozen-action execution diagnostics."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path

import torch

from .. import experiments
from ..bench import targets
from ..training.decision import DecisionPolicy
from .continuous import numbers
from .contracts import TargetWeights, timestamp
from .episode import run_episode
from .lifecycle import LifecycleAccountState, LifecycleTargetWeights
from .comparison import AGGREGATE, LIFECYCLE, lifecycle_difference, validate_acceptance


def serialize(value):
    if is_dataclass(value):
        return {field.name: serialize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return numbers(value)


def _max_gap(first, second):
    return max((abs(first.get(key, 0) - second.get(key, 0)) for key in set(first) | set(second)), default=0.)


def compare(episode, exact, acceptance, *, continuous_policy, exact_actions):
    acceptance = validate_acceptance(acceptance)
    detailed = acceptance.get("state_comparison_version", AGGREGATE) == LIFECYCLE
    rows = []
    initial_equity = exact["initial_equity"]
    for audit, (prior, state), actual, exact_state, action, policy in zip(episode["audits"], episode["states"],
            exact["audits"], exact["states"], exact_actions, continuous_policy, strict=True):
        if not audit["decision_id"] == actual["decision_id"] == action["decision_id"] == policy["decision_id"]:
            raise ValueError("decision evaluation paths are not aligned")
        before, after = exact_state["before"], exact_state["after"]
        positions = {p["asset_id"]: p["quantity"] for p in after["positions"]}
        continuous_quantities = dict(zip(state.assets, numbers(state.quantity)))
        continuous_lots, exact_lots = {}, {}
        for lot in state.lots:
            for code, quantity in zip(state.assets, numbers(lot.quantity), strict=True):
                key = (code, str(timestamp(lot.sellable_after)))
                continuous_lots[key] = continuous_lots.get(key, 0) + quantity
        for position in after["positions"]:
            for lot in position["unsettled"]:
                key = (position["asset_id"], str(timestamp(lot["sellable_after"])))
                exact_lots[key] = exact_lots.get(key, 0) + lot["quantity"]
        actual_turnover = sum(f["notional"] for f in actual["fills"]) / actual["equity_open"]
        target = ({pair["asset_id"]: pair["target_weight"] for pair in action["weights"]}
                  if action["schema_version"] != "decision_failure_v1" else None)
        continuous_target = dict(zip(policy["assets"], policy["production"]["produced_weights"]))
        cost_gap = float(audit["total_cost"]) - actual["total_cost"]
        equity_gap = float(state.equity) - actual["equity_close"]
        approximate_unfilled, actual_unfilled = {}, {}
        for destination, fills in ((approximate_unfilled, audit["fills"]), (actual_unfilled, actual["fills"])):
            for fill in fills:
                key = (fill["asset_id"], fill["side"])
                destination[key] = destination.get(key, 0) + float(fill["unfilled_quantity"])
        row = {"decision_id": audit["decision_id"], "equity_gap_cny": equity_gap,
            "equity_gap_bp": equity_gap / initial_equity * 10000,
            "cash_gap_cny": float(state.cash) - after["cash"],
            "receivable_gap_cny": float(state.receivable) - actual.get("receivable_equity", 0),
            "quantity_gap": _max_gap(continuous_quantities, positions),
            "locked_quantity_gap": _max_gap(continuous_lots, exact_lots),
            "turnover_gap": float(audit["turnover_open_equity"]) - actual_turnover,
            "action_weight_gap": _max_gap(continuous_target, target) if target is not None else None,
            "unfilled_quantity_gap": _max_gap(approximate_unfilled, actual_unfilled),
            "cost_gap_cny": cost_gap, "cost_relative_gap": cost_gap / actual["total_cost"] if actual["total_cost"] else None,
            "constraint_violation_count": len(audit["constraint_violations"]) + len(actual["constraint_violations"]),
            "exact_state_before": actual["state_before"], "exact_equity_before": actual["equity_before"],
            "continuous_equity_before": float(episode["states"][len(rows)][0].equity),
            "exact_cash_before": before["cash"]}
        if detailed:
            details = {"before": lifecycle_difference(prior, before), "after": lifecycle_difference(state, after)}
            row["lifecycle_states"] = details
            for key in ("entitlement_quantity_gap", "entitlement_amount_gap_cny",
                        "entitlement_mismatch_count", "processed_event_mismatch_count"):
                row[key] = max(detail[key] for detail in details.values())
        rows.append(row)
    maxima = {key: max((abs(row[key]) for row in rows if row[key] is not None), default=None) for key in (
        "equity_gap_cny", "equity_gap_bp", "cash_gap_cny", "receivable_gap_cny", "quantity_gap",
        "locked_quantity_gap", "turnover_gap", "action_weight_gap", "unfilled_quantity_gap")}
    total_cost = sum(float(row["total_cost"]) for row in episode["audits"])
    total_gap = total_cost - exact["total_cost"]
    relative_gap = total_gap / exact["total_cost"] if exact["total_cost"] else None
    mappings = {"equity_gap_cny": "max_daily_equity_gap_cny", "equity_gap_bp": "max_daily_equity_gap_bp",
        "cash_gap_cny": "max_daily_cash_gap_cny", "receivable_gap_cny": "max_daily_receivable_gap_cny",
        "quantity_gap": "max_quantity_gap", "locked_quantity_gap": "max_locked_quantity_gap",
        "turnover_gap": "max_daily_turnover_gap", "action_weight_gap": "max_action_weight_gap",
        "unfilled_quantity_gap": "max_unfilled_quantity_gap"}
    if detailed:
        for key in ("entitlement_quantity_gap", "entitlement_amount_gap_cny",
                    "entitlement_mismatch_count", "processed_event_mismatch_count"):
            maxima[key] = max(row[key] for row in rows)
            mappings[key] = f"max_daily_{key}"
    breaches = [key for key, limit in mappings.items() if maxima[key] is not None and maxima[key] > acceptance[limit]]
    if any(action["schema_version"] == "decision_failure_v1" for action in exact_actions):
        breaches.append("exact_decision_failure")
    if abs(total_gap) > acceptance["max_total_cost_gap_cny"]:
        breaches.append("total_cost_gap_cny")
    if relative_gap is not None and abs(relative_gap) > acceptance["max_total_cost_relative_gap"]:
        breaches.append("total_cost_relative_gap")
    if sum(row["constraint_violation_count"] for row in rows) > acceptance["max_constraint_violation_count"]:
        breaches.append("constraint_violation_count")
    return {**({"state_comparison_version": LIFECYCLE} if detailed else {}),
        "daily": rows, "max_absolute_gaps": maxima, "continuous_total_cost": total_cost,
        "exact_total_cost": exact["total_cost"], "total_cost_gap_cny": total_gap, "total_cost_relative_gap": relative_gap,
        "threshold_breaches": breaches, "comparison_eligible": not breaches,
        "scope": "declared proxy agreement only; no economic confirmation"}


def evaluate(model, bundle, protocol, output, *, policy_config, continuous, acceptance, log=lambda *_: None,
             evaluation=None, feedback_cutoff=None):
    if evaluation is not None:
        from .evaluation_failures import evaluate as diagnostic

        return diagnostic(model, bundle, protocol, output, policy_config=policy_config, continuous=continuous,
                          acceptance=acceptance, log=log, evaluation=evaluation, feedback_cutoff=feedback_cutoff)
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    experiments.write(root / "protocol.json", protocol)
    with torch.no_grad():
        exact_policy = DecisionPolicy(model, bundle, **policy_config)
        def exact_action(context):
            log(f"utility exact decision {context.decision_id}")
            return exact_policy(context)
        exact = targets.replay(protocol, exact_action)
        experiments.write(root / "policy.json", {"decisions": exact_policy.audit})
        experiments.write(root / "submission.json", {"schema_version": "target_submission_v2"
            if protocol["schema_version"] == "target_protocol_v2" else "target_submission_v1",
            "protocol_id": exact["protocol_id"], "decisions": exact["decisions"]})
        score = targets.evaluate(root / "protocol.json", root / "submission.json", root / "exact")
        if score["status"] != "COMPLETED":
            raise ValueError(str(score))
        targets.verify(root / "exact", inputs=True)
        if experiments.object_digest(exact) != experiments.object_digest(experiments.read(root / "exact/replay.json")):
            raise ValueError("exact policy and independent submission replay differ")
        approximate_policy = DecisionPolicy(model, bundle, **policy_config)
        def approximate_action(context):
            log(f"utility continuous decision {context.decision_id}")
            return approximate_policy(context)
        episode = run_episode(protocol, approximate_action, config=continuous)
        experiments.write(root / "continuous.json", serialize(episode))
        experiments.write(root / "continuous-policy.json", {"decisions": approximate_policy.audit})
        frozen = {a["decision_id"]: dict(zip(a["assets"], numbers(intent.weights)))
                  for a, intent in zip(episode["audits"], episode["intents"], strict=True)}
        def fixed(context):
            log(f"utility fixed-action decision {context.decision_id}")
            values = frozen[context.decision_id]
            if any(weight != 0 for code, weight in values.items() if code not in context.assets):
                raise ValueError("continuous action has an asset absent from exact context")
            weights = tuple((code, values.get(code, 0)) for code in context.assets)
            reserve = context.state.receivable_equity / context.state.equity
            cls = LifecycleTargetWeights if isinstance(context.state, LifecycleAccountState) else TargetWeights
            extra = {"receivable_weight": reserve} if cls is LifecycleTargetWeights else {}
            return cls(context.decision_id, context.state.observed_at, context.execution_not_before,
                "cn_alloc_daily_v1", "frozen-continuous-action-diagnostic", context.state.data_snapshot_id,
                context.state.state_id, context.universe_id, context.protocol_id, weights,
                1 - reserve - sum(w for _, w in weights), **extra)
        fixed_result = targets.replay(protocol, fixed)
        experiments.write(root / "fixed-continuous-action-exact.json", fixed_result)
        comparisons = {"closed_loop": compare(episode, exact, acceptance,
                continuous_policy=approximate_policy.audit, exact_actions=exact["decisions"]),
            "fixed_actions": compare(episode, fixed_result, acceptance,
                continuous_policy=approximate_policy.audit, exact_actions=fixed_result["decisions"])}
        experiments.write(root / "differences.json", comparisons)
        result = {"exact_net_return": exact["net_return"], "exact_total_cost": exact["total_cost"],
            "continuous_net_return": float(episode["equity"][-1] / episode["equity"][0] - 1),
            "closed_loop_comparison_eligible": comparisons["closed_loop"]["comparison_eligible"],
            "threshold_breaches": comparisons["closed_loop"]["threshold_breaches"]}
        experiments.write(root / "result.json", result)
        return result, exact_policy.audit
