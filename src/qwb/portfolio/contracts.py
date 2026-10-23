"""Decision-time account state and target intent, independent of score tables."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from numbers import Real

import numpy as np
import pandas as pd

from ..experiments import object_digest

WEIGHT_SUM_TOL = 1e-6
STATE_SCHEMA = "account_state_v1"
TARGET_SCHEMA = "target_weights_v1"
TASK = "cn_alloc_daily_v1"


def timestamp(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("timestamps must be finite and timezone-aware")
    return stamp.tz_convert("UTC")


def number(value, name, *, lower=0.0, upper=float("inf")) -> float:
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not np.isfinite(value) or not lower <= value <= upper):
        raise ValueError(f"invalid {name}: finite value in [{lower}, {upper}] required")
    return float(value)


def identity(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True)
class SettlementLot:
    quantity: int
    sellable_after: str

    def __post_init__(self):
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("settlement quantity must be a positive integer")
        timestamp(self.sellable_after)


@dataclass(frozen=True)
class Position:
    asset_id: str
    quantity: int
    sellable_quantity: int
    mark: float
    mark_time: str
    unsettled: tuple[SettlementLot, ...] = ()

    def __post_init__(self):
        identity(self.asset_id, "asset_id")
        if (type(self.quantity) is not int or self.quantity <= 0
                or type(self.sellable_quantity) is not int or self.sellable_quantity < 0
                or not isinstance(self.unsettled, tuple)
                or not all(isinstance(lot, SettlementLot) for lot in self.unsettled)
                or self.sellable_quantity + sum(x.quantity for x in self.unsettled) != self.quantity):
            raise ValueError("position must reconcile integer sellable and unsettled quantities")
        number(self.mark, "mark", lower=np.finfo(float).tiny)
        timestamp(self.mark_time)


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    asset_id: str
    side: str
    remaining_quantity: int
    submitted_at: str
    expires_at: str

    def __post_init__(self):
        identity(self.order_id, "order_id")
        identity(self.asset_id, "asset_id")
        if self.side not in {"BUY", "SELL"} or type(self.remaining_quantity) is not int or self.remaining_quantity <= 0:
            raise ValueError("invalid pending order side/quantity")
        if timestamp(self.expires_at) <= timestamp(self.submitted_at):
            raise ValueError("order expiry must follow submission")


@dataclass(frozen=True)
class AccountState:
    observed_at: str
    data_snapshot_id: str
    cash: float
    positions: tuple[Position, ...] = ()
    pending_orders: tuple[PendingOrder, ...] = ()
    schema_version: str = STATE_SCHEMA

    def _validate_schema(self):
        if self.schema_version != STATE_SCHEMA:
            raise ValueError("unsupported account state schema")

    def __post_init__(self):
        self._validate_schema()
        now = timestamp(self.observed_at)
        identity(self.data_snapshot_id, "data_snapshot_id")
        number(self.cash, "cash")
        if not isinstance(self.positions, tuple) or not isinstance(self.pending_orders, tuple):
            raise ValueError("account collections must be immutable tuples")
        if not all(isinstance(p, Position) for p in self.positions):
            raise ValueError("invalid position objects")
        if len({p.asset_id for p in self.positions}) != len(self.positions):
            raise ValueError("duplicate positions")
        for p in self.positions:
            if timestamp(p.mark_time) > now:
                raise ValueError("future position mark")
            if any(timestamp(lot.sellable_after) <= now for lot in p.unsettled):
                raise ValueError("mature settlement lots must be settled before state creation")
        if not all(isinstance(o, PendingOrder) for o in self.pending_orders):
            raise ValueError("invalid pending order objects")
        if len({o.order_id for o in self.pending_orders}) != len(self.pending_orders):
            raise ValueError("duplicate pending order ids")
        for order in self.pending_orders:
            if not timestamp(order.submitted_at) <= now < timestamp(order.expires_at):
                raise ValueError("pending orders must already exist and remain live at observation")
        if self.equity <= 0 or not np.isfinite(self.equity):
            raise ValueError("account equity must be finite and positive")

    @property
    def equity(self):
        return self.cash + sum(p.quantity * p.mark for p in self.positions) + self.receivable_equity

    @property
    def receivable_equity(self):
        return 0.0

    def weights(self, assets):
        values = {p.asset_id: p.quantity * p.mark / self.equity for p in self.positions}
        if set(values) - set(assets):
            raise ValueError("allocation universe must retain every existing position")
        return np.array([values.get(code, 0.0) for code in assets])

    def to_dict(self):
        return asdict(self)

    @property
    def state_id(self):
        value = self.to_dict()
        value["positions"] = sorted(value["positions"], key=lambda p: p["asset_id"])
        value["pending_orders"] = sorted(value["pending_orders"], key=lambda p: p["order_id"])
        return object_digest(value)

    @classmethod
    def from_dict(cls, value):
        if set(value) != set(cls.__dataclass_fields__):
            raise ValueError("account state fields do not match schema")
        raw = dict(value)
        try:
            raw["positions"] = tuple(Position(**{**p, "unsettled": tuple(
                SettlementLot(**lot) for lot in p["unsettled"])}) for p in raw["positions"])
            raw["pending_orders"] = tuple(PendingOrder(**o) for o in raw["pending_orders"])
            return cls(**raw)
        except (TypeError, KeyError) as exc:
            raise ValueError(f"invalid account state fields: {exc}") from exc


@dataclass(frozen=True)
class TargetWeights:
    decision_id: str
    decision_time: str
    execution_not_before: str
    task: str
    model_version: str
    data_snapshot_id: str
    state_id: str
    universe_id: str
    protocol_id: str
    weights: tuple[tuple[str, float], ...]
    cash_weight: float
    schema_version: str = TARGET_SCHEMA

    def _validate_schema(self):
        if self.schema_version != TARGET_SCHEMA:
            raise ValueError("unsupported target schema")

    def _budget(self):
        return sum(v for _, v in self.weights) + self.cash_weight

    def validate_account(self, state):
        if state.schema_version != STATE_SCHEMA:
            raise ValueError("v1 targets require v1 account state")

    def __post_init__(self):
        self._validate_schema()
        for name in ("decision_id", "task", "model_version", "data_snapshot_id", "state_id", "universe_id", "protocol_id"):
            identity(getattr(self, name), name)
        if timestamp(self.execution_not_before) <= timestamp(self.decision_time):
            raise ValueError("execution must follow the decision")
        if not isinstance(self.weights, tuple):
            raise ValueError("target weights must be immutable tuples")
        names = []
        for pair in self.weights:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("weights need (asset_id, weight) pairs")
            code, weight = pair
            identity(code, "asset_id")
            number(weight, "target weight", upper=1.0)
            names.append(code)
        if len(set(names)) != len(names):
            raise ValueError("duplicate target assets")
        number(self.cash_weight, "cash weight", upper=1.0)
        if abs(self._budget() - 1) > WEIGHT_SUM_TOL:
            raise ValueError("stock and cash target weights must sum to one")

    def to_dict(self):
        value = asdict(self)
        value["weights"] = [{"asset_id": code, "target_weight": w} for code, w in self.weights]
        return value

    @classmethod
    def from_dict(cls, value):
        if set(value) != set(cls.__dataclass_fields__):
            raise ValueError("target fields do not match schema")
        raw = dict(value)
        if not isinstance(raw["weights"], list) or any(
                not isinstance(p, dict) or set(p) != {"asset_id", "target_weight"} for p in raw["weights"]):
            raise ValueError("invalid target weight rows")
        raw["weights"] = tuple((p["asset_id"], p["target_weight"]) for p in raw["weights"])
        return cls(**raw)


def validate_target(target: TargetWeights, state: AccountState, *, assets, universe_id,
                    protocol_id, execution_at, max_weight=1.0, min_cash=0.0,
                    task=TASK, sector_by_asset=None, sector_caps=None):
    """Validate intent against known state; never clip, drop, or renormalize."""
    target.validate_account(state)
    assets = tuple(assets)
    if not assets or len(set(assets)) != len(assets):
        raise ValueError("known assets must be nonempty and unique")
    number(max_weight, "max weight", upper=1.0)
    number(min_cash, "min cash", upper=1.0)
    if (target.state_id != state.state_id
            or timestamp(target.decision_time) != timestamp(state.observed_at)):
        raise ValueError("stale or mismatched account state")
    if target.data_snapshot_id != state.data_snapshot_id:
        raise ValueError("target snapshot mismatch")
    if target.task != task or target.universe_id != universe_id or target.protocol_id != protocol_id:
        raise ValueError("target task/universe/protocol mismatch")
    if timestamp(target.execution_not_before) != timestamp(execution_at):
        raise ValueError("target execution time mismatch")
    held = {p.asset_id for p in state.positions}
    if held - set(assets) or set(dict(target.weights)) - set(assets):
        raise ValueError("unknown target asset or dropped existing holding")
    if target.cash_weight < min_cash or any(w > max_weight for _, w in target.weights):
        raise ValueError("target violates cash or position constraint")
    if sector_caps:
        if not sector_by_asset or set(assets) - set(sector_by_asset):
            raise ValueError("complete decision-known sector membership required")
        for sector, cap in sector_caps.items():
            number(cap, "sector cap", upper=1.0)
            if sum(w for code, w in target.weights if sector_by_asset[code] == sector) > cap:
                raise ValueError("target violates sector constraint")
    return target
