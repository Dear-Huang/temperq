"""Small raw-share execution reference for the daily allocation contract.

Open-only IOC instructions, explicit lot rules and T+1 settlement. This is a
deterministic accounting reference, not an intraday or corporate-action engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math

from .. import rules
from .contracts import (AccountState, Position, SettlementLot, TargetWeights,
                        identity, number, timestamp, validate_target)


@dataclass(frozen=True)
class ExecutionBar:
    asset_id: str
    open: float | None
    close: float | None
    upper_limit: float | None
    lower_limit: float | None
    suspended: bool = False
    buy_lot: int = 100
    max_quantity: int | None = None
    buy_allowed: bool = True
    sell_lot: int = 1
    minimum_buy: int = 1
    minimum_sell: int = 1

    def __post_init__(self):
        identity(self.asset_id, "asset_id")
        if any(type(lot) is not int or lot <= 0 for lot in
               (self.buy_lot, self.sell_lot, self.minimum_buy, self.minimum_sell)):
            raise ValueError("buy/sell lots must be positive integers")
        if self.max_quantity is not None and (type(self.max_quantity) is not int or self.max_quantity < 0):
            raise ValueError("execution capacity must be a nonnegative integer")
        if type(self.suspended) is not bool or type(self.buy_allowed) is not bool:
            raise ValueError("execution suspension status must be explicit")
        for name in ("open", "close", "upper_limit", "lower_limit"):
            if getattr(self, name) is not None:
                number(getattr(self, name), name, lower=1e-12)
        if self.upper_limit is not None and self.lower_limit is not None and self.upper_limit <= self.lower_limit:
            raise ValueError("invalid execution price limits")


@dataclass(frozen=True)
class Fees:
    commission_bp: float = 3.0
    minimum_commission: float = 5.0
    slippage_bp: float = 0.0

    def __post_init__(self):
        number(self.commission_bp, "commission bp", upper=1000)
        number(self.minimum_commission, "minimum commission")
        number(self.slippage_bp, "slippage bp", upper=1000)

    def costs(self, notional, side, day):
        if notional == 0:
            return {"commission": 0.0, "stamp_tax": 0.0, "slippage": 0.0}
        return {"commission": max(self.minimum_commission, notional * self.commission_bp / 10000),
                "stamp_tax": notional * rules.stamp_tax_sell_bp(day) / 10000 if side == "SELL" else 0.0,
                "slippage": notional * self.slippage_bp / 10000}


def execute_session(state: AccountState, target: TargetWeights | None, bars: tuple[ExecutionBar, ...],
                    *, open_at, close_at, next_open_at, decision_assets, universe_id, protocol_id,
                    fees=Fees(), max_weight=1.0, min_cash=0.0, decision_state=None):
    """Reveal execution data only after validating the decision-known target.

    Target sizing uses actual open equity. Sells precede buys, with deterministic
    asset-order cash allocation. Unfilled open IOC quantities expire; their reasons
    remain in the audit. Existing pending orders require a different protocol.
    """
    op, cl, nxt = map(timestamp, (open_at, close_at, next_open_at))
    if not timestamp(state.observed_at) < op < cl < nxt:
        raise ValueError("session must follow the observation and precede next settlement")
    if state.pending_orders:
        raise ValueError("open_ioc_v1 cannot consume existing pending orders")
    if len({b.asset_id for b in bars}) != len(bars):
        raise ValueError("duplicate execution bars")
    by_code = {b.asset_id: b for b in bars}
    assets = tuple(sorted(decision_assets))
    if not assets or len(set(assets)) != len(assets):
        raise ValueError("decision-known asset order must be nonempty and unique")
    if {p.asset_id for p in state.positions} - set(assets):
        raise ValueError("execution data must retain existing holdings")
    if {p.asset_id for p in state.positions} - set(by_code):
        raise ValueError("execution data must retain existing holdings")
    if set(by_code) != set(assets):
        raise ValueError("execution rows must match decision-known assets; missing prices need explicit rows")
    if decision_state is not None:
        if (state.schema_version != "account_state_v2"
                or decision_state.data_snapshot_id != state.data_snapshot_id
                or timestamp(decision_state.observed_at) > timestamp(state.observed_at)):
            raise ValueError("invalid lifecycle decision state")
    if target is not None:
        validate_target(target, decision_state if decision_state is not None else state,
                        assets=assets, universe_id=universe_id,
                        protocol_id=protocol_id, execution_at=open_at,
                        max_weight=max_weight, min_cash=min_cash)
    positions = {p.asset_id: p for p in state.positions}
    qty = {code: positions[code].quantity if code in positions else 0 for code in assets}
    locked = {code: [lot for lot in positions[code].unsettled if timestamp(lot.sellable_after) > op]
              if code in positions else [] for code in assets}
    sellable = {code: qty[code] - sum(lot.quantity for lot in locked[code]) for code in assets}
    marks = {code: (by_code[code].open if by_code[code].open is not None else
                    positions[code].mark if code in positions else 0.0) for code in assets}
    cash = float(state.cash)
    equity_open = cash + state.receivable_equity + sum(qty[code] * marks[code] for code in assets)
    weights = dict(target.weights) if target else {}
    desired = {}
    for code in assets:
        bar = by_code[code]
        if target is None or bar.open is None:
            desired[code] = qty[code]
        else:
            desired[code] = int(math.floor(weights.get(code, 0) * equity_open / bar.open + 1e-10))
    initial_qty = dict(qty)
    fills, notices = [], []
    for side in ("SELL", "BUY"):
        for code in assets:
            bar = by_code[code]
            signed = desired[code] - qty[code]
            if not signed or (signed > 0) != (side == "BUY"):
                continue
            requested = abs(signed)
            reason = None
            if bar.open is None:
                reason = "MISSING_OPEN"
            elif side == "BUY" and not bar.buy_allowed:
                reason = "NOT_IN_DECISION_UNIVERSE"
            elif bar.suspended:
                reason = "SUSPENDED"
            elif bar.upper_limit is None or bar.lower_limit is None:
                reason = "MISSING_LIMITS"
            elif side == "BUY" and bar.open >= bar.upper_limit - 1e-8:
                reason = "LIMIT_UP"
            elif side == "SELL" and bar.open <= bar.lower_limit + 1e-8:
                reason = "LIMIT_DOWN"
            amount = requested
            if reason:
                amount = 0
            if side == "SELL" and amount > sellable[code]:
                amount = sellable[code]
                reason = "T1_LOCKED"
            if bar.max_quantity is not None and amount > bar.max_quantity:
                amount = bar.max_quantity
                reason = "CAPACITY"
            if side == "SELL" and amount != qty[code] and amount % bar.sell_lot:
                amount = amount // bar.sell_lot * bar.sell_lot
                reason = reason or "LOT_ROUNDED"
            if side == "SELL" and amount < bar.minimum_sell and amount != qty[code]:
                amount, reason = 0, reason or "BELOW_MINIMUM_SELL"
            if side == "BUY":
                lots = amount // bar.buy_lot
                if amount % bar.buy_lot:
                    reason = reason or "LOT_ROUNDED"
                lo, hi = 0, lots
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    value = mid * bar.buy_lot * bar.open
                    required = value + sum(fees.costs(value, side, op.tz_convert("Asia/Shanghai").date()).values())
                    if required <= cash:
                        lo = mid
                    else:
                        hi = mid - 1
                if lo < lots:
                    reason = "INSUFFICIENT_CASH"
                amount = lo * bar.buy_lot
                if amount < bar.minimum_buy:
                    amount, reason = 0, reason or "BELOW_MINIMUM_BUY"
            value = amount * bar.open if amount else 0.0
            cost = fees.costs(value, side, op.tz_convert("Asia/Shanghai").date())
            total_cost = sum(cost.values())
            if side == "SELL" and value - total_cost + cash < 0:
                amount, value = 0, 0.0
                cost = fees.costs(0, side, op.date())
                total_cost, reason = 0.0, "INSUFFICIENT_CASH_FOR_FEES"
            if side == "BUY":
                cash -= value + total_cost
                qty[code] += amount
                if amount:
                    locked[code].append(SettlementLot(amount, str(nxt)))
            else:
                cash += value - total_cost
                qty[code] -= amount
                sellable[code] -= amount
            fills.append({"asset_id": code, "side": side, "requested_quantity": requested,
                          "quantity": amount, "open_price": bar.open, "notional": value,
                          **cost, "total_cost": total_cost, "reason": reason or "FILLED",
                          "unfilled_quantity": requested - amount, "lifetime": "OPEN_IOC"})
    # Missing prices produce explicit diagnostic notices, even if no order was sized.
    for code in assets:
        bar = by_code[code]
        if target and bar.open is None and (qty[code] or weights.get(code, 0)):
            notices.append({"asset_id": code, "reason": "MISSING_OPEN_UNSIZED_TARGET"})
    cost_sum = sum(f["total_cost"] for f in fills)
    post_open = cash + state.receivable_equity + sum(qty[code] * marks[code] for code in assets)
    residual = post_open - equity_open + cost_sum
    if cash < 0 or abs(residual) > 1e-8 * max(1.0, equity_open):
        raise ValueError("reference self-financing invariant failed")
    result = []
    for code in assets:
        if qty[code] == 0:
            continue
        bar = by_code[code]
        if bar.close is None:
            notices.append({"asset_id": code, "reason": "STALE_CLOSE"})
        mark = bar.close if bar.close is not None else marks[code]
        mark_time = close_at if bar.close is not None else (open_at if bar.open is not None else positions[code].mark_time)
        result.append(Position(code, qty[code], sellable[code], mark, str(mark_time), tuple(locked[code])))
    after = replace(state, observed_at=str(cl), cash=cash, positions=tuple(result), pending_orders=())
    overnight = sum(initial_qty[c] * (marks[c] - positions[c].mark) for c in positions)
    intraday = sum(qty[c] * ((by_code[c].close if by_code[c].close is not None else marks[c]) - marks[c]) for c in assets)
    pnl_residual = after.equity - state.equity - overnight - intraday + cost_sum
    if abs(pnl_residual) > 1e-8 * max(1.0, state.equity):
        raise ValueError("reference PnL reconciliation failed")
    actual_weights = {p.asset_id: p.quantity * p.mark / after.equity for p in after.positions}
    violations = [{"constraint": "position_cap", "asset_id": code, "actual": weight,
                   "limit": max_weight} for code, weight in actual_weights.items() if weight > max_weight]
    actual_cash = after.cash / after.equity
    if actual_cash < min_cash:
        violations.append({"constraint": "minimum_cash", "actual": actual_cash, "limit": min_cash})
    return after, {"schema_version": "raw_share_open_ioc_v1", "state_before": state.state_id,
                   "state_after": after.state_id, "equity_before": state.equity,
                   "equity_open": equity_open, "equity_close": after.equity,
                   "overnight_pnl": overnight, "intraday_pnl": intraday,
                   "total_cost": cost_sum, "self_financing_residual": residual,
                   "pnl_residual": pnl_residual, "fills": fills, "notices": notices,
                   "actual_weights": actual_weights, "actual_cash_weight": actual_cash,
                   "constraint_violations": violations,
                   "fees": asdict(fees), "diagnostic_only": True,
                   "limitations": ["no_corporate_actions", "open_bar_proxy", "explicit_lot_rules_required",
                                   "no_existing_pending_orders", "no_capacity_certification"]}
