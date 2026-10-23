"""Versioned raw-share executor with per-asset STRUCT fee estimates.

Derived from reference.execute_session. The legacy executor stays unchanged so
existing frozen paper accounts retain their recorded implementation identity.
Sizing, lot rules, settlement, and self-financing checks have the same semantics.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import math
from .contracts import AccountState, Position, SettlementLot, TargetWeights, identity, timestamp, validate_target
from .reference import ExecutionBar, Fees


def execute_session(state: AccountState, target: TargetWeights | None, bars: tuple[ExecutionBar, ...],
                    *, open_at, close_at, next_open_at, decision_assets, universe_id, protocol_id,
                    fees=Fees(), max_weight=1.0, min_cash=0.0, decision_state=None, fees_by_asset=None):
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
            asset_fees = fees_by_asset[code] if fees_by_asset is not None else fees
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
                    required = value + sum(asset_fees.costs(value, side, op.tz_convert("Asia/Shanghai").date()).values())
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
            cost = asset_fees.costs(value, side, op.tz_convert("Asia/Shanghai").date())
            total_cost = sum(cost.values())
            if side == "SELL" and value - total_cost + cash < 0:
                amount, value = 0, 0.0
                cost = asset_fees.costs(0, side, op.date())
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
                          **cost, **(asset_fees.metadata() if hasattr(asset_fees, "metadata") else {}), "total_cost": total_cost, "reason": reason or "FILLED",
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
    return after, {"schema_version": "raw_share_open_ioc_struct_v1", "state_before": state.state_id,
                   "state_after": after.state_id, "equity_before": state.equity,
                   "equity_open": equity_open, "equity_close": after.equity,
                   "overnight_pnl": overnight, "intraday_pnl": intraday,
                   "total_cost": cost_sum, "self_financing_residual": residual,
                   "pnl_residual": pnl_residual, "fills": fills, "notices": notices,
                   "actual_weights": actual_weights, "actual_cash_weight": actual_cash,
                   "constraint_violations": violations,
                   "fees": asdict(fees), "fees_by_asset":{k:asdict(v) for k,v in (fees_by_asset or {}).items()}, "diagnostic_only": True,
                   "limitations": ["no_corporate_actions", "open_bar_proxy", "explicit_lot_rules_required",
                                   "no_existing_pending_orders", "no_capacity_certification"]}
