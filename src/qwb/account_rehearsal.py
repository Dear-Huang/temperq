"""Offline, paired account-action outcomes. Not a live selector or profit claim."""

from __future__ import annotations

from dataclasses import dataclass

from .experiments import object_digest
from .portfolio.contracts import AccountState, TargetWeights, identity, number, timestamp, validate_target
from .portfolio.reference import ExecutionBar, Fees, execute_session


@dataclass(frozen=True)
class Action:
    action_id: str
    target: TargetWeights | None

    def __post_init__(self):
        identity(self.action_id, "action_id")
        if self.target is not None and not isinstance(self.target, TargetWeights):
            raise ValueError("action needs a target or explicit hold (None)")


@dataclass(frozen=True)
class RehearsalPlan:
    state: AccountState
    actions: tuple[Action, ...]
    baseline_id: str
    assets: tuple[str, ...]
    universe_id: str
    protocol_id: str
    open_at: str
    close_at: str
    next_open_at: str
    fees: Fees = Fees()
    max_weight: float = 1.0
    min_cash: float = 0.0

    def __post_init__(self):
        if not isinstance(self.state, AccountState) or self.state.schema_version != "account_state_v1":
            raise ValueError("rehearsal currently requires a v1 account state")
        if self.state.pending_orders:
            raise ValueError("rehearsal cannot consume pending orders")
        if (not isinstance(self.actions, tuple) or not self.actions
                or not all(isinstance(a, Action) for a in self.actions)
                or len({a.action_id for a in self.actions}) != len(self.actions)):
            raise ValueError("actions must be a nonempty immutable tuple with unique ids")
        if self.baseline_id not in {a.action_id for a in self.actions}:
            raise ValueError("baseline must be a declared action")
        if not any(a.target is None for a in self.actions):
            raise ValueError("include explicit hold, even when another policy is the baseline")
        if (not isinstance(self.assets, tuple) or not self.assets
                or len(set(self.assets)) != len(self.assets)):
            raise ValueError("assets must be a nonempty immutable tuple with unique ids")
        for asset in self.assets:
            identity(asset, "asset_id")
        if {p.asset_id for p in self.state.positions} - set(self.assets):
            raise ValueError("plan must retain existing holdings")
        identity(self.universe_id, "universe_id")
        identity(self.protocol_id, "protocol_id")
        number(self.max_weight, "max weight", upper=1.0)
        number(self.min_cash, "min cash", upper=1.0)
        if not isinstance(self.fees, Fees):
            raise ValueError("explicit Fees required")
        if not timestamp(self.state.observed_at) < timestamp(self.open_at) < timestamp(self.close_at) < timestamp(self.next_open_at):
            raise ValueError("session must follow the decision and precede settlement")
        for action in self.actions:
            if action.target is not None:
                validate_target(action.target, self.state, assets=self.assets,
                                universe_id=self.universe_id, protocol_id=self.protocol_id,
                                execution_at=self.open_at, max_weight=self.max_weight,
                                min_cash=self.min_cash)

    def to_dict(self):
        return {
            "schema_version": "account_rehearsal_plan_v1",
            "state": self.state.to_dict(),
            "actions": [{"action_id": a.action_id,
                         "target": a.target.to_dict() if a.target is not None else None}
                        for a in self.actions],
            "baseline_id": self.baseline_id, "assets": list(self.assets),
            "universe_id": self.universe_id, "protocol_id": self.protocol_id,
            "open_at": self.open_at, "close_at": self.close_at,
            "next_open_at": self.next_open_at,
            "fees": {"commission_bp": self.fees.commission_bp,
                     "minimum_commission": self.fees.minimum_commission,
                     "slippage_bp": self.fees.slippage_bp},
            "max_weight": self.max_weight, "min_cash": self.min_cash,
        }

    @property
    def plan_id(self):
        return object_digest(self.to_dict())


def replay_actions(plan: RehearsalPlan, bars: tuple[ExecutionBar, ...]):
    """Reveal one session after freezing intents; never choose a hindsight winner.

    Outcomes share the same initial state, not the evolving state of another
    action. These simulated labels are not causal observations or an unbiased
    estimate of a multi-session policy's value.
    """
    rows = []
    by_asset = {bar.asset_id: bar for bar in bars}
    for action in plan.actions:
        after, audit = execute_session(
            plan.state, action.target, bars, open_at=plan.open_at,
            close_at=plan.close_at, next_open_at=plan.next_open_at,
            decision_assets=plan.assets, universe_id=plan.universe_id,
            protocol_id=plan.protocol_id, fees=plan.fees,
            max_weight=plan.max_weight, min_cash=plan.min_cash)
        relevant = {p.asset_id for p in plan.state.positions}
        if action.target is not None:
            relevant.update(code for code, weight in action.target.weights if weight > 0)
        missing = sorted(code for code in relevant if by_asset[code].open is None
                         or by_asset[code].close is None
                         or (action.target is not None and
                             (by_asset[code].upper_limit is None or by_asset[code].lower_limit is None)))
        issues = []
        if missing or audit["notices"]:
            issues.append("incomplete_market_data")
        if audit["constraint_violations"]:
            issues.append("actual_constraint_violation")
        rows.append({"action_id": action.action_id, "state_after": after.to_dict(),
                     "net_pnl": after.equity - plan.state.equity,
                     "total_cost": audit["total_cost"],
                     "traded_notional": sum(fill["notional"] for fill in audit["fills"]),
                     "stock_weight_close": sum(audit["actual_weights"].values()),
                     "missing_assets": missing, "issues": issues, "audit": audit})
    baseline = next(row for row in rows if row["action_id"] == plan.baseline_id)
    for row in rows:
        eligible = not row["issues"] and not baseline["issues"]
        row["paired_label_eligible"] = eligible
        row["baseline_issues"] = list(baseline["issues"])
        row["simulated_delta_net_pnl"] = row["net_pnl"] - baseline["net_pnl"]
        row["training_label_bp"] = (row["simulated_delta_net_pnl"] / plan.state.equity * 10000
                                    if eligible else None)
    return {
        "schema_version": "account_rehearsal_outcomes_v1",
        "plan_id": plan.plan_id, "plan": plan.to_dict(),
        "market_digest": object_digest([vars(bar) for bar in bars]),
        "scope": "one_session_simulated_counterfactual", "diagnostic_only": True,
        "rows": rows,
        "limitations": ["no_live_selection", "no_statistical_evidence_from_single_session",
                        "not_causal_inference", "no_closed_loop_policy_evaluation",
                        "no_corporate_actions", "open_bar_proxy", "no_capacity_certification",
                        "exposure_differences_not_controlled", "no_struct_costs"],
    }
