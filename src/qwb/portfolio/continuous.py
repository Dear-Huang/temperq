"""Tensor account episodes, with explicit execution relaxations and path gradients.

This implementation shares contracts and event schedules with the independent
raw-share Bench, but does not call its state advancement or fee calculations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import torch

from .. import rules
from .contracts import AccountState, identity, timestamp
from .lifecycle import LifecycleAccountState, Dividend, Split, Terminal, phases
from .reference import ExecutionBar, Fees


def tensor(value):
    return torch.as_tensor(value, dtype=torch.float64, device="cpu")


def _check(value, shape, name, *, nonnegative=False):
    if (not isinstance(value, torch.Tensor) or tuple(value.shape) != shape
            or value.dtype != torch.float64 or value.device.type != "cpu"
            or not bool(torch.isfinite(value).all())
            or (nonnegative and bool((value < 0).any()))):
        raise ValueError(f"{name} requires finite aligned float64 CPU tensors" +
                         (" with nonnegative values" if nonnegative else ""))


def numbers(value):
    """Serialize diagnostics only; never use this detached view to advance state."""
    if isinstance(value, torch.Tensor):
        return value.detach().tolist()
    if isinstance(value, dict):
        return {key: numbers(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [numbers(item) for item in value]
    return value


@dataclass(frozen=True)
class ContinuousConfig:
    integer_lots: bool = False
    settlement: bool = True
    market_restrictions: bool = True
    minimum_commission: bool = True
    fee_model: str = "declared_execution"

    def __post_init__(self):
        if (any(type(getattr(self, key)) is not bool for key in
                ("integer_lots", "settlement", "market_restrictions", "minimum_commission"))
                or self.fee_model not in {"none", "declared_execution", "causal_linear"}
                or (self.fee_model == "causal_linear" and self.minimum_commission)):
            raise ValueError("invalid continuous execution configuration")


@dataclass(frozen=True)
class TensorLot:
    quantity: torch.Tensor
    sellable_after: str


@dataclass(frozen=True)
class TensorRight:
    event_id: str
    asset_id: str
    kind: str
    quantity: torch.Tensor
    amount: torch.Tensor
    payable_at: str
    recognized: bool


@dataclass(frozen=True)
class ContinuousState:
    assets: tuple[str, ...]
    observed_at: str
    cash: torch.Tensor
    quantity: torch.Tensor
    marks: torch.Tensor
    lots: tuple[TensorLot, ...] = ()
    rights: tuple[TensorRight, ...] = ()
    processed_events: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.assets, tuple) or not self.assets or tuple(sorted(set(self.assets))) != self.assets
                or not isinstance(self.lots, tuple) or not isinstance(self.rights, tuple)
                or not isinstance(self.processed_events, tuple)
                or len(set(self.processed_events)) != len(self.processed_events)):
            raise ValueError("continuous state requires sorted unique assets and immutable collections")
        for asset in self.assets:
            identity(asset, "continuous asset")
        timestamp(self.observed_at)
        _check(self.cash, (), "cash", nonnegative=True)
        _check(self.quantity, (len(self.assets),), "quantities", nonnegative=True)
        _check(self.marks, (len(self.assets),), "marks", nonnegative=True)
        if bool(((self.quantity > 0) & (self.marks <= 0)).any()):
            raise ValueError("held assets need positive marks")
        locked = torch.zeros_like(self.quantity)
        for lot in self.lots:
            _check(lot.quantity, self.quantity.shape, "locked quantities", nonnegative=True)
            if timestamp(lot.sellable_after) <= timestamp(self.observed_at):
                raise ValueError("mature tensor lots must be settled")
            locked = locked + lot.quantity
        if bool((locked - self.quantity > 1e-9).any()):
            raise ValueError("locked quantities exceed holdings")
        if len({right.event_id for right in self.rights}) != len(self.rights):
            raise ValueError("duplicate tensor rights")
        for right in self.rights:
            for key in ("event_id", "asset_id"):
                identity(getattr(right, key), key)
            _check(right.quantity, (), "right quantity", nonnegative=True)
            _check(right.amount, (), "right amount", nonnegative=True)
            if (right.kind not in {"dividend", "terminal"} or type(right.recognized) is not bool
                    or (right.kind == "terminal" and not right.recognized)
                    or timestamp(right.payable_at) < timestamp(self.observed_at)):
                raise ValueError("invalid tensor right phase")
        if not bool(torch.isfinite(self.equity)) or float(self.equity.detach()) <= 0:
            raise ValueError("continuous equity must remain finite and positive")

    @property
    def receivable(self):
        return sum((right.amount for right in self.rights if right.recognized), self.cash.new_zeros(()))

    @property
    def equity(self):
        return self.cash + self.quantity @ self.marks + self.receivable

    @property
    def weights(self):
        return self.quantity * self.marks / self.equity

    def detach(self):
        return replace(self, cash=self.cash.detach(), quantity=self.quantity.detach(), marks=self.marks.detach(),
            lots=tuple(replace(lot, quantity=lot.quantity.detach()) for lot in self.lots),
            rights=tuple(replace(right, quantity=right.quantity.detach(), amount=right.amount.detach()) for right in self.rights))

    def reindex(self, assets):
        assets = tuple(assets)
        index = {code: i for i, code in enumerate(self.assets)}
        if any(float(self.quantity[index[code]].detach()) != 0 for code in set(index) - set(assets)):
            raise ValueError("cannot remove a held continuous asset")
        def align(values):
            return torch.stack([values[index[code]] if code in index else values.new_zeros(()) for code in assets])
        return replace(self, assets=assets, quantity=align(self.quantity), marks=align(self.marks),
                       lots=tuple(replace(lot, quantity=align(lot.quantity)) for lot in self.lots))

    @classmethod
    def from_account(cls, account: AccountState, assets):
        assets = tuple(assets)
        if account.pending_orders or {p.asset_id for p in account.positions} - set(assets):
            raise ValueError("continuous initial account must retain holdings and have no pending orders")
        positions = {p.asset_id: p for p in account.positions}
        quantity = tensor([positions[code].quantity if code in positions else 0 for code in assets])
        marks = tensor([positions[code].mark if code in positions else 0 for code in assets])
        dates = sorted({lot.sellable_after for p in account.positions for lot in p.unsettled}, key=timestamp)
        lots = tuple(TensorLot(tensor([sum(lot.quantity for lot in positions[code].unsettled if lot.sellable_after == date)
                                      if code in positions else 0 for code in assets]), date) for date in dates)
        rights, processed = (), ()
        if isinstance(account, LifecycleAccountState):
            rights = tuple(TensorRight(right.event_id, right.asset_id, right.kind, tensor(right.quantity),
                                      tensor(right.amount), right.payable_at, right.recognized) for right in account.entitlements)
            processed = account.processed_events
        return cls(assets, account.observed_at, tensor(account.cash), quantity, marks, lots, rights, processed)


def _at(state, at):
    return replace(state, observed_at=str(at), lots=tuple(lot for lot in state.lots
                                                        if timestamp(lot.sellable_after) > timestamp(at)))


def _phase(state, at, key, phase, event, *, integer_lots):
    if (key in state.processed_events or timestamp(at) < timestamp(state.observed_at)
            or timestamp(event.available_at) > timestamp(at)):
        raise ValueError("stale, repeated or unavailable tensor event")
    before = state.equity
    state = _at(state, at)
    zero = state.cash.new_zeros(())
    index = state.assets.index(event.asset_id) if event.asset_id in state.assets else None
    quantity = state.quantity[index] if index is not None else zero
    rights = {right.event_id: right for right in state.rights}
    cash, quantities, marks, lots = state.cash, state.quantity, state.marks, state.lots
    expected, tax = zero, zero
    if phase == "record":
        if event.event_id in rights:
            raise ValueError("duplicate tensor dividend ownership")
        if float(quantity.detach()) > 0:
            rights[event.event_id] = TensorRight(event.event_id, event.asset_id, "dividend", quantity,
                quantity * (event.gross_per_share - event.withholding_per_share), event.pay_at, False)
    elif phase == "ex":
        if event.event_id + ":record" not in state.processed_events:
            raise ValueError("tensor dividend needs recorded ownership")
        if event.event_id in rights:
            right = rights[event.event_id]
            rights[event.event_id] = replace(right, recognized=True)
            expected, tax = right.amount, right.quantity * event.withholding_per_share
        expected = expected - quantity * event.ex_price_adjustment
        if index is not None and float(quantity.detach()) > 0:
            delta = torch.zeros_like(marks)
            delta[index] = event.ex_price_adjustment
            marks = marks - delta
    elif phase in {"pay", "settle"}:
        required = "ex" if phase == "pay" else "terminal"
        if event.event_id + ":" + required not in state.processed_events:
            raise ValueError("tensor payment needs recognition")
        right = rights.pop(event.event_id, None)
        if right is not None:
            if not right.recognized or timestamp(right.payable_at) != timestamp(at):
                raise ValueError("tensor payment phase mismatch")
            cash = cash + right.amount
    elif phase == "split":
        if index is not None:
            ratio = event.numerator / event.denominator
            if integer_lots:
                locked = sum((lot.quantity[index] for lot in lots), zero)
                values = [quantity, quantity - locked, *[lot.quantity[index] for lot in lots]]
                if any(float(value.detach()) != round(float(value.detach()))
                       or (round(float(value.detach())) * event.numerator) % event.denominator for value in values):
                    raise ValueError("fractional split rejected by integer tensor execution")
            scale = torch.ones_like(quantities)
            scale[index] = ratio
            quantities, marks = quantities * scale, marks / scale
            lots = tuple(replace(lot, quantity=lot.quantity * scale) for lot in lots)
    elif phase == "terminal":
        if index is not None and float(quantity.detach()) > 0:
            keep = torch.ones_like(quantities)
            keep[index] = 0
            quantities = quantities * keep
            lots = tuple(replace(lot, quantity=lot.quantity * keep) for lot in lots)
            rights[event.event_id] = TensorRight(event.event_id, event.asset_id, "terminal", quantity,
                quantity * event.amount_per_share, event.settle_at, True)
            expected = quantity * (event.amount_per_share - state.marks[index])
    else:
        raise ValueError("unsupported tensor event phase")
    after = replace(state, cash=cash, quantity=quantities, marks=marks, lots=lots, rights=tuple(rights.values()),
                    processed_events=(*state.processed_events, key))
    residual = after.equity - before - expected
    if float(residual.detach().abs()) > 1e-8 * max(1, float(before.detach())):
        raise ValueError("tensor corporate accounting failed")
    return after, {"phase_id": key, "corporate_pnl": expected, "declared_withholding": tax,
                   "equity_before": before, "equity_after": after.equity, "accounting_residual": residual}


def step(state, weights, bars, *, open_at, close_at, next_open_at, fees=Fees(),
         config=ContinuousConfig(), actions=(), cost_rates=None):
    """Advance one observed action using subsequently revealed execution data.

    Rounding has torch.floor's zero derivative. Hard gates, active min/max and
    minimum-fee discontinuities are explicit; no straight-through derivative or
    hidden weight repair is used. None means keep shares, not target old weights.
    """
    opened, closed, nxt = map(timestamp, (open_at, close_at, next_open_at))
    if not timestamp(state.observed_at) < opened < closed < nxt:
        raise ValueError("tensor session timestamps are not ordered")
    if not isinstance(config, ContinuousConfig) or not isinstance(fees, Fees):
        raise ValueError("explicit tensor execution config/fees required")
    if not isinstance(bars, tuple) or not all(isinstance(bar, ExecutionBar) for bar in bars):
        raise ValueError("tensor execution requires explicit bars")
    by_code = {bar.asset_id: bar for bar in bars}
    if len(by_code) != len(bars) or set(by_code) != set(state.assets):
        raise ValueError("tensor execution rows must match decision-known assets")
    if weights is not None:
        _check(weights, state.quantity.shape, "target weights", nonnegative=True)
        if float((weights.sum() + state.receivable / state.equity).detach()) > 1 + 1e-6:
            raise ValueError("tensor targets cannot spend receivables or exceed the budget")
    if config.fee_model == "causal_linear":
        if not isinstance(cost_rates, tuple) or len(cost_rates) != 2:
            raise ValueError("causal linear execution needs declared buy/sell cost vectors")
        for rate in cost_rates:
            _check(rate, state.quantity.shape, "causal cost rates", nonnegative=True)
    elif cost_rates is not None:
        raise ValueError("declared execution fees do not consume causal proxy rates")
    if not isinstance(actions, tuple) or not all(isinstance(event, (Dividend, Split, Terminal)) for event in actions):
        raise ValueError("unsupported tensor corporate action")
    if len({event.event_id for event in actions}) != len(actions):
        raise ValueError("duplicate tensor event id")
    scheduled = [row for row in phases(actions) if timestamp(state.observed_at) < row[0] <= closed]
    for at, _, phase, _ in scheduled:
        local = at.tz_convert("Asia/Shanghai")
        if ((phase == "record" and at != closed)
                or (phase != "record" and (local.strftime("%H:%M:%S") != "09:00:00"
                                          or local.date() != opened.tz_convert("Asia/Shanghai").date()))):
            raise ValueError("tensor events require 09:00 pre-open or close records")
    initial_equity = state.equity
    event_audits = []
    for at, key, phase, event in scheduled:
        if at < opened:
            state, audit = _phase(state, at, key, phase, event, integer_lots=config.integer_lots)
            event_audits.append(audit)
    terminal = {event.asset_id for event in actions if isinstance(event, Terminal) and timestamp(event.effective_at) < opened}
    if any(code in terminal and (bar.open is not None or bar.close is not None) for code, bar in by_code.items()):
        raise ValueError("terminal tensor assets require missing execution prices")
    if any(timestamp(right.payable_at) <= opened for right in state.rights):
        raise ValueError("tensor opening has unpaid mature rights")
    state = _at(state, opened)
    zero = state.cash.new_zeros(())
    opened_prices = tensor([by_code[code].open or 0 for code in state.assets])
    has_open = torch.tensor([by_code[code].open is not None for code in state.assets])
    open_marks = torch.where(has_open, opened_prices, state.marks)
    equity_open = state.cash + state.quantity @ open_marks + state.receivable
    if float(equity_open.detach()) <= 0:
        raise ValueError("tensor open equity must be positive")
    safe_prices = torch.where(has_open, opened_prices, torch.ones_like(opened_prices))
    desired = state.quantity if weights is None else torch.where(has_open, weights * equity_open / safe_prices, state.quantity)
    if config.integer_lots:
        if bool((state.quantity != state.quantity.round()).any()):
            raise ValueError("integer execution needs integer holdings")
        desired = torch.floor(desired + 1e-10)
    locked = sum((lot.quantity for lot in state.lots), torch.zeros_like(state.quantity)) if config.settlement else torch.zeros_like(state.quantity)
    sellable = state.quantity - locked
    quantity = list(state.quantity.unbind())
    cash = state.cash
    fills = []
    bought = [zero for _ in state.assets]
    day = opened.tz_convert("Asia/Shanghai").date()
    commission_rate, slip_rate = fees.commission_bp / 10000, fees.slippage_bp / 10000
    minimum = fees.minimum_commission if config.minimum_commission else 0.0
    sell_tax = rules.stamp_tax_sell_bp(day) / 10000

    def costs(notional, side, index):
        if config.fee_model == "none":
            return {"no_cost_control": notional * 0}
        if config.fee_model == "causal_linear":
            return {"proxy": notional * cost_rates[0 if side == "BUY" else 1][index]}
        positive = notional > 0
        return {"commission": torch.where(positive, torch.maximum(notional * commission_rate, tensor(minimum)), zero),
                "stamp_tax": notional * sell_tax if side == "SELL" else zero,
                "slippage": notional * slip_rate}

    for side in ("SELL", "BUY"):
        for index, code in enumerate(state.assets):
            bar = by_code[code]
            requested = torch.relu(quantity[index] - desired[index] if side == "SELL" else desired[index] - quantity[index])
            amount = requested
            blocked = (bar.open is None or (side == "BUY" and (not bar.buy_allowed or code in terminal)))
            reason = "MISSING_OPEN" if bar.open is None else "NOT_IN_DECISION_UNIVERSE" if blocked else None
            if config.market_restrictions:
                if bar.suspended:
                    reason, blocked = "SUSPENDED", True
                elif bar.lower_limit is None or bar.upper_limit is None:
                    reason, blocked = "MISSING_LIMITS", True
                elif bar.open is not None and ((side == "BUY" and bar.open >= bar.upper_limit - 1e-8)
                                              or (side == "SELL" and bar.open <= bar.lower_limit + 1e-8)):
                    reason, blocked = "LIMIT_UP" if side == "BUY" else "LIMIT_DOWN", True
            if blocked:
                amount = amount * 0
            if side == "SELL" and float(amount.detach()) > float(sellable[index].detach()):
                amount, reason = sellable[index], "T1_LOCKED"
            if config.market_restrictions and bar.max_quantity is not None and float(amount.detach()) > bar.max_quantity:
                amount, reason = amount.new_tensor(bar.max_quantity), "CAPACITY"
            affordable_value = None
            if side == "SELL" and config.integer_lots and float(amount.detach()) != float(quantity[index].detach()):
                rounded = torch.floor(amount / bar.sell_lot) * bar.sell_lot
                if float(rounded.detach()) != float(amount.detach()):
                    reason = reason or "LOT_ROUNDED"
                amount = rounded
            if side == "BUY" and bar.open is not None:
                if config.integer_lots:
                    rounded = torch.floor(amount / bar.buy_lot) * bar.buy_lot
                    if float(rounded.detach()) != float(amount.detach()):
                        reason = reason or "LOT_ROUNDED"
                    amount = rounded
                if config.fee_model == "none":
                    affordable_value = cash
                elif config.fee_model == "causal_linear":
                    affordable_value = cash / (1 + cost_rates[0][index])
                else:
                    affordable_value = torch.minimum(cash / (1 + commission_rate + slip_rate),
                                                      torch.relu(cash - minimum) / (1 + slip_rate))
                affordable = affordable_value / bar.open
                if config.integer_lots:
                    affordable = torch.floor(affordable / bar.buy_lot) * bar.buy_lot
                if float(amount.detach()) > float(affordable.detach()):
                    amount, reason = affordable, "INSUFFICIENT_CASH"
            value = amount * (bar.open or 0)
            parts = costs(value, side, index)
            total = sum(parts.values(), zero)
            if side == "SELL" and float((cash + value - total).detach()) < 0:
                amount, value, reason = amount * 0, value * 0, "INSUFFICIENT_CASH_FOR_FEES"
                parts, total = costs(value, side, index), zero
            if side == "SELL":
                cash = cash + value - total
                quantity[index] = quantity[index] - amount
            else:
                remaining = cash - value - total
                # Binding fractional funding has exactly zero remaining cash.
                cash = (torch.where((value == affordable_value) & (value > 0), zero, remaining)
                        if affordable_value is not None else remaining)
                quantity[index] = quantity[index] + amount
                bought[index] = amount
            if float(requested.detach()) > 0:
                fills.append({"asset_id": code, "side": side, "requested_quantity": requested,
                              "quantity": amount, "notional": value, **parts, "total_cost": total,
                              "unfilled_quantity": requested - amount, "reason": reason or "FILLED"})
    quantities = torch.stack(quantity)
    total_cost = sum((fill["total_cost"] for fill in fills), zero)
    post_open = cash + quantities @ open_marks + state.receivable
    self_financing = post_open - equity_open + total_cost
    if float(self_financing.detach().abs()) > 1e-8 * max(1, float(equity_open.detach())):
        raise ValueError("tensor self-financing failed")
    close_marks = tensor([by_code[code].close or 0 for code in state.assets])
    has_close = torch.tensor([by_code[code].close is not None for code in state.assets])
    close_marks = torch.where(has_close, close_marks, open_marks)
    overnight = state.quantity @ (open_marks - state.marks)
    intraday = quantities @ (close_marks - open_marks)
    lots = state.lots if config.settlement else ()
    if config.settlement and bool((torch.stack(bought) > 0).any()):
        lots = (*lots, TensorLot(torch.stack(bought), str(nxt)))
    after = replace(state, observed_at=str(closed), cash=cash, quantity=quantities, marks=close_marks, lots=lots)
    for at, key, phase, event in scheduled:
        if at >= opened:
            after, audit = _phase(after, at, key, phase, event, integer_lots=config.integer_lots)
            event_audits.append(audit)
    if any(timestamp(right.payable_at) <= closed for right in after.rights):
        raise ValueError("tensor close has unpaid mature rights")
    corporate_pnl = sum((audit["corporate_pnl"] for audit in event_audits), zero)
    residual = after.equity - initial_equity - overnight - intraday - corporate_pnl + total_cost
    if float(residual.detach().abs()) > 1e-8 * max(1, float(initial_equity.detach())):
        raise ValueError("tensor PnL reconciliation failed")
    notices = []
    for index, code in enumerate(state.assets):
        if (weights is not None and by_code[code].open is None
                and (float(state.quantity[index].detach()) > 0 or float(weights[index].detach()) > 0)):
            notices.append({"asset_id": code, "reason": "MISSING_OPEN_UNSIZED_TARGET"})
        if by_code[code].close is None and float(after.quantity[index].detach()) > 0:
            notices.append({"asset_id": code, "reason": "STALE_CLOSE"})
    return after, {"schema_version": "continuous_session_v1", "equity_before": initial_equity,
        "equity_open": equity_open, "equity_close": after.equity, "overnight_pnl": overnight,
        "intraday_pnl": intraday, "corporate_action_pnl": corporate_pnl, "corporate_actions": event_audits,
        "total_cost": total_cost, "fills": fills, "notices": notices, "pnl_residual": residual,
        "self_financing_residual": self_financing, "cash": after.cash, "receivable": after.receivable,
        "quantity": after.quantity, "net_return": after.equity / initial_equity - 1,
        "turnover_open_equity": sum((fill["notional"] for fill in fills), zero) / equity_open,
        "actual_weights": after.weights, "actual_cash_weight": after.cash / after.equity}
