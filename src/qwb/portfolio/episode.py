"""Chronological continuous episodes; policies only receive decision-known state."""

from __future__ import annotations

from dataclasses import dataclass, replace

import torch

from .. import experiments
from ..bench import targets
from .contracts import AccountState, timestamp
from .continuous import ContinuousConfig, ContinuousState, step
from .lifecycle import LifecycleAccountState, parse_actions
from .reference import ExecutionBar, Fees


@dataclass(frozen=True)
class ContinuousDecision:
    decision_id: str
    state: ContinuousState
    eligible_assets: tuple[str, ...]
    execution_not_before: str
    max_weight: float
    min_cash: float
    known_actions: tuple


@dataclass(frozen=True)
class ContinuousIntent:
    weights: torch.Tensor | None
    cost_rates: tuple[torch.Tensor, torch.Tensor] | None = None
    hold_reason: str | None = None

    def __post_init__(self):
        if self.weights is None:
            if not isinstance(self.hold_reason, str) or not self.hold_reason.strip():
                raise ValueError("keeping shares requires an explicit control/failure reason")
        elif self.hold_reason is not None:
            raise ValueError("a weight action cannot also declare holding shares")


def run_episode(protocol, policy, *, config=ContinuousConfig(), on_step=lambda *_: None, detach_every=None):
    """Return a chronological graph, with only explicitly requested state cuts.

    The caller owns training cuts, frozen input lineage and failure persistence.
    This local API does not certify arbitrary policy code's information access.
    """
    protocol_id = targets.validate_protocol(protocol)
    if detach_every is not None and (type(detach_every) is not int or detach_every < 1):
        raise ValueError("detach_every must be null or a positive step count")
    implementation = experiments.code_identity()
    cls = LifecycleAccountState if protocol["schema_version"] == "target_protocol_v2" else AccountState
    account = cls.from_dict(protocol["initial_state"])
    initial_assets = tuple(sorted(set(protocol["sessions"][0]["eligible_assets"]) | {p.asset_id for p in account.positions}))
    state = ContinuousState.from_account(account, initial_assets)
    initial_equity = state.equity
    actions = parse_actions(protocol["corporate_actions"]) if cls is LifecycleAccountState else ()
    fees = Fees(**protocol["fees"])
    limits = protocol["constraints"]
    audits, states, decisions = [], [], []
    cuts, returns = [], []
    for index, session in enumerate(protocol["sessions"]):
        if detach_every is not None and index and index % detach_every == 0:
            state = state.detach()
            cuts.append(index)
        assets = tuple(sorted(set(session["eligible_assets"]) |
                              {code for code, quantity in zip(state.assets, state.quantity, strict=True)
                               if float(quantity.detach()) > 0}))
        if not assets:
            raise ValueError("continuous episode needs a declared decision asset")
        state = replace(state.reindex(assets), observed_at=session["decision_time"])
        before = state
        context = ContinuousDecision(session["decision_id"], state, tuple(sorted(session["eligible_assets"])),
            session["open_at"], **limits, known_actions=tuple(action for action in actions
                if timestamp(action.available_at) <= timestamp(state.observed_at)))
        intent = policy(context)
        if not isinstance(intent, ContinuousIntent):
            raise ValueError("continuous policies must return a declared intent")
        if intent.weights is not None:
            from .continuous import _check

            _check(intent.weights, state.quantity.shape, "episode target weights", nonnegative=True)
            if (bool((intent.weights > limits["max_weight"]).any())
                    or float((1 - state.receivable / state.equity - intent.weights.sum()).detach()) < limits["min_cash"] - 1e-6):
                raise ValueError("continuous target violates declared position/cash limits")
        rows = {row["asset_id"]: row for row in session["bars"]}
        if set(assets) - set(rows):
            raise ValueError("held continuous assets require explicit execution rows")
        bars = tuple(ExecutionBar(**rows[code], buy_allowed=code in context.eligible_assets) for code in assets)
        state, audit = step(state, intent.weights, bars, open_at=session["open_at"], close_at=session["close_at"],
            next_open_at=session["next_open_at"], fees=fees, config=config, actions=actions, cost_rates=intent.cost_rates)
        audit.update(decision_id=context.decision_id, decision_time=before.observed_at,
                     hold_reason=intent.hold_reason, assets=assets, target_weights=intent.weights)
        violations = [{"constraint": "position_cap", "asset_id": code, "actual": state.weights[i],
                       "limit": limits["max_weight"]} for i, code in enumerate(assets)
                      if float(state.weights[i].detach()) > limits["max_weight"]]
        if float((state.cash / state.equity).detach()) < limits["min_cash"]:
            violations.append({"constraint": "minimum_cash", "actual": state.cash / state.equity,
                               "limit": limits["min_cash"]})
        audit["constraint_violations"] = violations
        audits.append(audit)
        states.append((before, state))
        # The denominator must be the same (possibly detached) state used today.
        returns.append(state.equity / before.equity - 1)
        decisions.append(intent)
        on_step(context, intent, state, audit)
    if experiments.object_digest(protocol) != protocol_id or experiments.code_identity() != implementation:
        raise ValueError("continuous episode inputs/implementation changed during execution")
    return {"schema_version": "continuous_episode_v1", "protocol_id": protocol_id,
            "equity": torch.stack([initial_equity, *[audit["equity_close"] for audit in audits]]),
            "net_returns": torch.stack(returns),
            "audits": audits, "states": states, "intents": decisions, "final_state": state,
            "detach_before_steps": cuts,
            "gradient_scope": "piecewise_autograd_no_STE; integer_floor_has_zero_derivative",
            "evidence_status": "SYNTHETIC_ONLY" if protocol["data_kind"] == "synthetic" else "DIAGNOSTIC_ONLY"}
