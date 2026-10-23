"""Versioned raw-share corporate actions and nonspendable entitlements."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction

import numpy as np

from .contracts import (AccountState, Position, SettlementLot, TargetWeights,
                        WEIGHT_SUM_TOL, identity, number, timestamp, validate_target)
from .reference import execute_session


@dataclass(frozen=True)
class Entitlement:
    event_id: str
    asset_id: str
    kind: str
    quantity: int
    amount: float
    payable_at: str
    recognized: bool

    def __post_init__(self):
        identity(self.event_id, "entitlement event")
        identity(self.asset_id, "entitlement asset")
        if self.kind not in {"dividend", "terminal"}:
            raise ValueError("unknown entitlement kind")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("entitlement quantity must be a positive integer")
        number(self.amount, "entitlement amount")
        timestamp(self.payable_at)
        if type(self.recognized) is not bool or (self.kind == "terminal" and not self.recognized):
            raise ValueError("invalid entitlement recognition")


@dataclass(frozen=True)
class LifecycleAccountState(AccountState):
    schema_version: str = "account_state_v2"
    entitlements: tuple[Entitlement, ...] = ()
    processed_events: tuple[str, ...] = ()

    def _validate_schema(self):
        if self.schema_version != "account_state_v2":
            raise ValueError("unsupported lifecycle account schema")

    def __post_init__(self):
        if (not isinstance(self.entitlements, tuple)
                or not all(isinstance(e, Entitlement) for e in self.entitlements)
                or len({e.event_id for e in self.entitlements}) != len(self.entitlements)):
            raise ValueError("invalid or duplicate entitlements")
        if (not isinstance(self.processed_events, tuple)
                or len(set(self.processed_events)) != len(self.processed_events)):
            raise ValueError("invalid or duplicate processed events")
        for event in self.processed_events:
            identity(event, "processed event")
        if any(timestamp(e.payable_at) < timestamp(self.observed_at) for e in self.entitlements):
            raise ValueError("overdue entitlements must be settled")
        super().__post_init__()

    @property
    def receivable_equity(self):
        return sum(e.amount for e in self.entitlements if e.recognized)

    @property
    def state_id(self):
        from ..experiments import object_digest

        value = self.to_dict()
        value["positions"] = sorted(value["positions"], key=lambda p: p["asset_id"])
        value["pending_orders"] = sorted(value["pending_orders"], key=lambda p: p["order_id"])
        value["entitlements"] = sorted(value["entitlements"], key=lambda e: e["event_id"])
        value["processed_events"] = sorted(value["processed_events"])
        return object_digest(value)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("lifecycle account fields do not match schema")
        raw = dict(value)
        try:
            raw["positions"] = tuple(Position(**{**p, "unsettled": tuple(
                SettlementLot(**lot) for lot in p["unsettled"])}) for p in raw["positions"])
            from .contracts import PendingOrder

            raw["pending_orders"] = tuple(PendingOrder(**o) for o in raw["pending_orders"])
            raw["entitlements"] = tuple(Entitlement(**e) for e in raw["entitlements"])
            raw["processed_events"] = tuple(raw["processed_events"])
            return cls(**raw)
        except (TypeError, KeyError) as exc:
            raise ValueError(f"invalid lifecycle account: {exc}") from exc


@dataclass(frozen=True)
class LifecycleTargetWeights(TargetWeights):
    schema_version: str = "target_weights_v2"
    receivable_weight: float = 0.0

    def _validate_schema(self):
        if self.schema_version != "target_weights_v2":
            raise ValueError("unsupported lifecycle target schema")
        number(self.receivable_weight, "receivable weight", upper=1)

    def _budget(self):
        return super()._budget() + self.receivable_weight

    def validate_account(self, state):
        if (not isinstance(state, LifecycleAccountState)
                or abs(self.receivable_weight - state.receivable_equity / state.equity) > WEIGHT_SUM_TOL):
            raise ValueError("target receivable weight must match actual lifecycle state")


@dataclass(frozen=True)
class Dividend:
    event_id: str
    asset_id: str
    available_at: str
    record_at: str
    ex_at: str
    pay_at: str
    gross_per_share: float
    withholding_per_share: float
    ex_price_adjustment: float
    tax_treatment: str
    kind: str = "dividend"

    def __post_init__(self):
        _event_identity(self, "dividend")
        if not timestamp(self.available_at) <= timestamp(self.record_at) < timestamp(self.ex_at) <= timestamp(self.pay_at):
            raise ValueError("dividend availability/record/ex/payment order invalid")
        number(self.gross_per_share, "gross dividend", lower=np.finfo(float).tiny)
        number(self.withholding_per_share, "declared withholding", upper=self.gross_per_share)
        number(self.ex_price_adjustment, "ex price adjustment", lower=np.finfo(float).tiny)
        if (self.tax_treatment not in {"tax_exempt", "declared_withholding"}
                or (self.tax_treatment == "tax_exempt" and self.withholding_per_share != 0)):
            raise ValueError("explicit supported dividend tax treatment required")


@dataclass(frozen=True)
class Split:
    event_id: str
    asset_id: str
    available_at: str
    effective_at: str
    numerator: int
    denominator: int
    fractional_policy: str
    kind: str = "split"

    def __post_init__(self):
        _event_identity(self, "split")
        if timestamp(self.available_at) > timestamp(self.effective_at):
            raise ValueError("split must be available by effective time")
        if any(type(v) is not int or v <= 0 for v in (self.numerator, self.denominator)):
            raise ValueError("split ratio requires positive integers")
        if self.fractional_policy != "reject":
            raise ValueError("only explicit rejection of fractional shares is supported")


@dataclass(frozen=True)
class Terminal:
    event_id: str
    asset_id: str
    available_at: str
    effective_at: str
    settle_at: str
    amount_per_share: float
    valuation_basis: str
    kind: str = "terminal"

    def __post_init__(self):
        _event_identity(self, "terminal")
        if not timestamp(self.available_at) <= timestamp(self.effective_at) <= timestamp(self.settle_at):
            raise ValueError("terminal availability/valuation/settlement order invalid")
        number(self.amount_per_share, "terminal proceeds")
        identity(self.valuation_basis, "terminal valuation basis")


def _event_identity(event, kind):
    identity(event.event_id, "event id")
    if ":" in event.event_id:
        raise ValueError("event id cannot contain phase separator")
    identity(event.asset_id, "event asset")
    timestamp(event.available_at)
    if event.kind != kind:
        raise ValueError("event kind mismatch")


def parse_actions(rows):
    if not isinstance(rows, list):
        raise ValueError("corporate actions must be an explicit list")
    actions = []
    types = {"dividend": Dividend, "split": Split, "terminal": Terminal}
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in types:
            raise ValueError("unsupported corporate action")
        cls = types[row["kind"]]
        if set(row) != set(cls.__dataclass_fields__):
            raise ValueError("corporate action fields do not match schema")
        actions.append(cls(**row))
    if len({a.event_id for a in actions}) != len(actions):
        raise ValueError("duplicate corporate action id")
    for event in actions:
        if isinstance(event, Dividend):
            ratio = Fraction(1)
            for split in actions:
                if (isinstance(split, Split) and split.asset_id == event.asset_id
                        and timestamp(event.record_at) < timestamp(split.effective_at) <= timestamp(event.ex_at)):
                    ratio *= Fraction(split.numerator, split.denominator)
            adjustment = event.gross_per_share / float(ratio)
            if abs(event.ex_price_adjustment - adjustment) > 1e-10 * max(1, adjustment):
                raise ValueError("ex price adjustment conflicts with declared dividend/split share units")
    return tuple(actions)


def phases(actions):
    scheduled = []
    for event in actions:
        times = (("record", event.record_at), ("ex", event.ex_at), ("pay", event.pay_at)) if isinstance(event, Dividend) else (
            (("split", event.effective_at),) if isinstance(event, Split) else
            (("terminal", event.effective_at), ("settle", event.settle_at)))
        scheduled.extend((timestamp(at), event.event_id + ":" + phase, phase, event) for phase, at in times)
    order = {"split": 0, "ex": 1, "pay": 2, "terminal": 3, "settle": 4, "record": 5}
    return sorted(scheduled, key=lambda row: (row[0], order[row[2]], row[1]))


def validate_initial(state, actions):
    """Initial historical quantities are declared; phase/claim semantics are checked."""
    now = timestamp(state.observed_at)
    if any(timestamp(e.payable_at) <= now for e in state.entitlements):
        raise ValueError("initial mature entitlements must be settled")
    expected = {key for at, key, _, _ in phases(actions) if at <= now}
    if set(state.processed_events) != expected:
        raise ValueError("initial processed phases must match declared event history")
    by_id = {event.event_id: event for event in actions}
    for claim in state.entitlements:
        event = by_id.get(claim.event_id)
        if event is None or claim.asset_id != event.asset_id or claim.kind != event.kind:
            raise ValueError("initial entitlement must match a declared action")
        if isinstance(event, Dividend):
            amount = claim.quantity * (event.gross_per_share - event.withholding_per_share)
            payable, phase = event.pay_at, "record"
            recognized = timestamp(event.ex_at) <= now
        else:
            amount = claim.quantity * event.amount_per_share
            payable, phase, recognized = event.settle_at, "terminal", True
        if (event.event_id + ":" + phase not in expected
                or claim.recognized != recognized or timestamp(claim.payable_at) != timestamp(payable)
                or abs(claim.amount - amount) > 1e-10 * max(1, amount)):
            raise ValueError("initial entitlement amount or phase mismatch")
    for event in actions:
        if isinstance(event, Terminal) and timestamp(event.effective_at) <= now:
            if any(p.asset_id == event.asset_id for p in state.positions):
                raise ValueError("terminal asset cannot remain tradable in initial state")


def _settled_positions(positions, at):
    result = []
    for p in positions:
        locked = tuple(lot for lot in p.unsettled if timestamp(lot.sellable_after) > at)
        result.append(replace(p, sellable_quantity=p.quantity - sum(lot.quantity for lot in locked), unsettled=locked))
    return tuple(result)


def apply_phase(state, at, key, phase, event):
    """Apply one declared phase; payments never create additional profit."""
    at = timestamp(at)
    if (at, key, phase, event) not in phases((event,)):
        raise ValueError("corporate phase identity or timestamp mismatch")
    if at < timestamp(state.observed_at) or key in state.processed_events or timestamp(event.available_at) > at:
        raise ValueError("stale, repeated or unavailable corporate action")
    positions = {p.asset_id: p for p in _settled_positions(state.positions, at)}
    claims = {c.event_id: c for c in state.entitlements}
    position = positions.get(event.asset_id)
    quantity = position.quantity if position else 0
    cash, tax = state.cash, 0.0
    expected_pnl = 0.0
    entitled_quantity = claims[event.event_id].quantity if event.event_id in claims else 0
    if phase == "record":
        if event.event_id in claims:
            raise ValueError("duplicate dividend entitlement")
        if quantity:
            claims[event.event_id] = Entitlement(event.event_id, event.asset_id, "dividend", quantity,
                quantity * (event.gross_per_share - event.withholding_per_share), event.pay_at, False)
        entitled_quantity = quantity
    elif phase == "ex":
        if event.event_id + ":record" not in state.processed_events:
            raise ValueError("dividend requires recorded ownership")
        if event.event_id in claims:
            claim = claims[event.event_id]
            claims[event.event_id] = replace(claim, recognized=True)
            tax = claim.quantity * event.withholding_per_share
            expected_pnl += claim.amount
        expected_pnl -= quantity * event.ex_price_adjustment
        if position:
            positions[event.asset_id] = replace(position, mark=position.mark - event.ex_price_adjustment,
                                                mark_time=str(at))
    elif phase in {"pay", "settle"}:
        required = "ex" if phase == "pay" else "terminal"
        if event.event_id + ":" + required not in state.processed_events:
            raise ValueError("payment requires recognized entitlement phase")
        claim = claims.pop(event.event_id, None)
        if claim:
            if not claim.recognized or timestamp(claim.payable_at) != at:
                raise ValueError("payment does not match entitlement")
            cash += claim.amount
    elif phase == "split":
        if position:
            ratio = Fraction(event.numerator, event.denominator)
            quantities = [position.quantity, position.sellable_quantity, *[lot.quantity for lot in position.unsettled]]
            if any((v * ratio).denominator != 1 for v in quantities):
                raise ValueError("fractional split shares or settlement lots require an explicit supported policy")
            positions[event.asset_id] = replace(position, quantity=int(position.quantity * ratio),
                sellable_quantity=int(position.sellable_quantity * ratio), mark=position.mark / float(ratio),
                mark_time=str(at), unsettled=tuple(replace(lot, quantity=int(lot.quantity * ratio)) for lot in position.unsettled))
    elif phase == "terminal":
        if position:
            positions.pop(event.asset_id)
            claims[event.event_id] = Entitlement(event.event_id, event.asset_id, "terminal", quantity,
                quantity * event.amount_per_share, event.settle_at, True)
            expected_pnl = quantity * (event.amount_per_share - position.mark)
        entitled_quantity = quantity
    else:
        raise ValueError("unsupported corporate action phase")
    after = replace(state, observed_at=str(at), cash=cash, positions=tuple(positions.values()),
                    entitlements=tuple(claims.values()), processed_events=(*state.processed_events, key))
    audit = {"phase_id": key, "phase": phase, "at": str(at), "asset_id": event.asset_id,
             "state_before": state.state_id, "state_after": after.state_id,
             "equity_before": state.equity, "equity_after": after.equity,
             "cash_change": after.cash - state.cash, "receivable_change": after.receivable_equity - state.receivable_equity,
             "corporate_pnl": expected_pnl, "declared_withholding": tax,
             "entitled_quantity": entitled_quantity}
    security_change = sum(p.quantity * p.mark for p in after.positions) - sum(p.quantity * p.mark for p in state.positions)
    audit["security_value_change"] = security_change
    audit["accounting_residual"] = after.equity - state.equity - expected_pnl
    if abs(audit["accounting_residual"]) > 1e-8 * max(1, state.equity):
        raise ValueError("corporate action accounting invariant failed")
    return after, audit


def execute_lifecycle_session(state, target, bars, *, actions, **kwargs):
    decision_state = state
    assets = kwargs["decision_assets"]
    if target is not None:
        validate_target(target, state, assets=assets, universe_id=kwargs["universe_id"],
                        protocol_id=kwargs["protocol_id"], execution_at=kwargs["open_at"],
                        max_weight=kwargs.get("max_weight", 1), min_cash=kwargs.get("min_cash", 0))
    opened, closed = timestamp(kwargs["open_at"]), timestamp(kwargs["close_at"])
    scheduled = [row for row in phases(actions) if timestamp(state.observed_at) < row[0] <= closed]
    audits = []
    for at, key, phase, event in scheduled:
        if at < opened:
            state, audit = apply_phase(state, at, key, phase, event)
            audits.append(audit)
    if any(timestamp(e.payable_at) <= opened for e in state.entitlements):
        raise ValueError("opening account has unpaid mature entitlements")
    terminal_assets = {a.asset_id for a in actions if isinstance(a, Terminal) and timestamp(a.effective_at) < opened}
    if any(b.asset_id in terminal_assets and (b.open is not None or b.close is not None) for b in bars):
        raise ValueError("terminal assets require explicit missing execution prices")
    bars = tuple(replace(b, buy_allowed=False) if b.asset_id in terminal_assets else b for b in bars)
    state, trading = execute_session(state, target, bars, decision_state=decision_state, **kwargs)
    for at, key, phase, event in scheduled:
        if at >= opened:
            state, audit = apply_phase(state, at, key, phase, event)
            audits.append(audit)
    if any(timestamp(e.payable_at) <= closed for e in state.entitlements):
        raise ValueError("closing account has unpaid mature entitlements")
    pnl = sum(a["corporate_pnl"] for a in audits)
    residual = state.equity - decision_state.equity - trading["overnight_pnl"] - trading["intraday_pnl"] - pnl + trading["total_cost"]
    if abs(residual) > 1e-8 * max(1, decision_state.equity):
        raise ValueError("lifecycle PnL reconciliation failed")
    trading.update(schema_version="raw_share_lifecycle_v2", state_before=decision_state.state_id,
                   state_after=state.state_id, equity_before=decision_state.equity, equity_close=state.equity,
                   corporate_actions=audits, corporate_action_pnl=pnl,
                   declared_withholding=sum(a["declared_withholding"] for a in audits),
                   receivable_equity=state.receivable_equity,
                   actual_receivable_weight=state.receivable_equity / state.equity, pnl_residual=residual,
                   limitations=["open_bar_proxy", "no_existing_pending_orders", "no_capacity_certification",
                                "declared_withholding_only", "fractional_split_rejected"])
    return state, trading
