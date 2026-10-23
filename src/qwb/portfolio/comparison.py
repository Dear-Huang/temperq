"""Versioned utility-account thresholds and entitlement schedule diagnostics."""

from __future__ import annotations

import copy

from .contracts import number, timestamp

AGGREGATE = "aggregate_v1"
LIFECYCLE = "lifecycle_schedule_v1"
BASE_LIMITS = {"max_daily_equity_gap_cny", "max_daily_equity_gap_bp", "max_total_cost_gap_cny",
    "max_total_cost_relative_gap", "max_daily_cash_gap_cny", "max_daily_receivable_gap_cny",
    "max_quantity_gap", "max_locked_quantity_gap", "max_daily_turnover_gap",
    "max_action_weight_gap", "max_unfilled_quantity_gap", "max_constraint_violation_count"}
STATE_LIMITS = {"max_daily_entitlement_quantity_gap", "max_daily_entitlement_amount_gap_cny",
    "max_daily_entitlement_mismatch_count", "max_daily_processed_event_mismatch_count"}


def validate_acceptance(value):
    if not isinstance(value, dict):
        raise ValueError("acceptance must be a mapping")
    value = copy.deepcopy(value)
    version = value.get("state_comparison_version", AGGREGATE)
    if version not in {AGGREGATE, LIFECYCLE}:
        raise ValueError("unsupported acceptance state comparison version")
    limits = BASE_LIMITS | (STATE_LIMITS if version == LIFECYCLE else set())
    expected = limits | {"zero_cost_relative_rule"}
    if "state_comparison_version" in value:
        expected.add("state_comparison_version")
    if set(value) != expected:
        raise ValueError("acceptance must explicitly declare every versioned utility difference limit")
    for key in limits:
        number(value[key], f"acceptance.{key}")
        if key.endswith("_count") and type(value[key]) is not int:
            raise ValueError(f"acceptance.{key} must be a nonnegative integer")
    if value["zero_cost_relative_rule"] != "null_use_absolute":
        raise ValueError("invalid zero-cost relative denominator rule")
    return value


def lifecycle_difference(state, exact):
    """Compare outstanding rights, including unrecognized/zero-amount claims."""
    approximate = {right.event_id: {"asset_id": right.asset_id, "kind": right.kind,
        "payable_at": str(timestamp(right.payable_at)), "recognized": right.recognized,
        "quantity": float(right.quantity.detach()), "amount": float(right.amount.detach())}
        for right in state.rights}
    reference = {right["event_id"]: {**{key: right[key] for key in
        ("asset_id", "kind", "recognized", "quantity", "amount")},
        "payable_at": str(timestamp(right["payable_at"]))} for right in exact.get("entitlements", ())}
    quantity_gap, amount_gap, mismatches = 0., 0., []
    for event in sorted(set(approximate) | set(reference)):
        first, second = approximate.get(event), reference.get(event)
        quantity_gap = max(quantity_gap, abs((first or {}).get("quantity", 0) - (second or {}).get("quantity", 0)))
        amount_gap = max(amount_gap, abs((first or {}).get("amount", 0) - (second or {}).get("amount", 0)))
        if (first is None or second is None or any(first[key] != second[key]
                for key in ("asset_id", "kind", "payable_at", "recognized"))):
            mismatches.append({"event_id": event, "continuous": first, "exact": second})
    approximate_events, reference_events = set(state.processed_events), set(exact.get("processed_events", ()))
    return {"entitlement_quantity_gap": quantity_gap, "entitlement_amount_gap_cny": amount_gap,
        "entitlement_mismatch_count": len(mismatches),
        "processed_event_mismatch_count": len(approximate_events ^ reference_events),
        "entitlement_mismatches": mismatches,
        "processed_events_continuous_only": sorted(approximate_events - reference_events),
        "processed_events_exact_only": sorted(reference_events - approximate_events)}
