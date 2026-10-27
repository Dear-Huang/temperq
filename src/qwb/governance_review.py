"""Account-native, reference-price governance plans, independent of prediction training."""
from __future__ import annotations

from datetime import date
import threading
from typing import Literal
import warnings

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import experiments as ex, strategy
from .portfolio.reference import Fees
from .portfolio.contracts import timestamp

_SOLVER_LOCK = threading.Lock()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Forecast(Contract):
    code: str
    value: float


class Holding(Contract):
    code: str
    quantity: int = Field(ge=1, le=1_000_000_000_000, strict=True)
    sellable_quantity: int = Field(ge=0, le=1_000_000_000_000, strict=True)

    @model_validator(mode="after")
    def valid(self):
        if self.sellable_quantity > self.quantity:
            raise ValueError("可卖数量不能超过持仓数量")
        return self


class Quote(Contract):
    code: str
    price: float = Field(ge=.01, le=1_000_000)
    status: Literal["conditional", "normal", "buy_blocked", "sell_blocked", "suspended"] = "conditional"


class ReviewRules(Contract):
    max_holdings: int = Field(default=10, ge=1, le=100, strict=True)
    max_weight: float = Field(default=.2, gt=0, le=1)
    min_cash: float = Field(default=.02, ge=0, lt=1)
    turnover_cap: float = Field(default=.25, ge=0, le=2)
    deviation_tolerance: float = Field(default=.02, ge=0, le=.25)
    commission_bp: float = Field(default=3, ge=0, le=1000)
    minimum_commission: float = Field(default=5, ge=0, le=10000)
    slippage_bp: float = Field(default=0, ge=0, le=1000)
    preference: Literal["tracking", "fees"] = "fees"

    def fees(self):
        return Fees(self.commission_bp, self.minimum_commission, self.slippage_bp)


class ReviewInput(Contract):
    schema_version: Literal["governance_review_input_v1"] = "governance_review_input_v1"
    name: str = Field(min_length=1, max_length=80)
    observed_at: str
    signal_available_at: str
    execution_date: date
    signal_version: str = Field(min_length=1, max_length=100)
    signal_kind: Literal["score", "rank", "target_weight"] = "target_weight"
    cash: float = Field(ge=0, le=1e12)
    forecasts: list[Forecast] = Field(min_length=1, max_length=100)
    holdings: list[Holding] = Field(default_factory=list, max_length=100)
    quotes: list[Quote] = Field(min_length=1, max_length=100)
    rules: ReviewRules = Field(default_factory=ReviewRules)

    @model_validator(mode="after")
    def valid(self):
        observed, available = timestamp(self.observed_at), timestamp(self.signal_available_at)
        if available > observed:
            raise ValueError("信号可用时点不能晚于账户参考时点")
        if self.execution_date <= observed.tz_convert("Asia/Shanghai").date():
            raise ValueError("计划日期必须晚于账户参考日期；请填写拟执行的交易日")
        if self.execution_date.weekday() >= 5:
            raise ValueError("计划日期不能是周末；交易所节假日还需自行核对")
        for rows in (self.forecasts, self.holdings, self.quotes):
            codes = []
            for row in rows:
                row.code = strategy.stock_code(row.code)
                codes.append(row.code)
            if len(codes) != len(set(codes)):
                raise ValueError("同一张表存在重复股票代码")
        required = {r.code for r in self.forecasts + self.holdings}
        if required - {r.code for r in self.quotes}:
            raise ValueError("预测股票和全部现有持仓都需要参考价格")
        if len(required) > 100:
            raise ValueError("本轮最多处理 100 只预测与持仓股票")
        if self.signal_kind == "target_weight":
            if any(r.value < 0 or r.value > 1 for r in self.forecasts):
                raise ValueError("目标权重使用 0 到 1 的小数")
            if sum(r.value for r in self.forecasts) > 1 + 1e-9:
                raise ValueError("目标股票权重之和不能超过 100%")
        if self.signal_kind == "rank" and any(r.value <= 0 for r in self.forecasts):
            raise ValueError("排名必须为正数，数值越小越优先")
        equity = self.cash + sum(r.quantity * next(q.price for q in self.quotes if q.code == r.code)
                                 for r in self.holdings)
        if not 0 < equity <= 1e12:
            raise ValueError("现金加持仓参考市值须大于零且不超过本轮 1 万亿元规模上限")
        return self


def context(case: ReviewInput):
    codes = sorted({r.code for r in case.forecasts + case.holdings})
    quotes = {q.code: q for q in case.quotes}
    holdings = {h.code: h for h in case.holdings}
    price = np.array([quotes[c].price for c in codes])
    quantity = np.array([holdings[c].quantity if c in holdings else 0 for c in codes])
    sellable = np.array([holdings[c].sellable_quantity if c in holdings else 0 for c in codes])
    equity = float(case.cash + price @ quantity)
    if case.signal_kind == "target_weight":
        weights = {r.code: r.value for r in case.forecasts}
    else:
        ordered = sorted(case.forecasts, key=lambda r: (r.value if case.signal_kind == "rank" else -r.value, r.code))
        selected = ordered[:case.rules.max_holdings]
        weights = {r.code: min(case.rules.max_weight, (1 - case.rules.min_cash) / len(selected)) for r in selected}
    target = np.array([weights.get(c, 0.) for c in codes])
    bars = [strategy.session_bar(c, open_price=quotes[c].price, close_price=quotes[c].price,
                                 upper=None, lower=None) for c in codes]
    return codes, quotes, price, quantity, sellable, equity, target, bars


def project(case: ReviewInput, proposed, *, candidate_id, label, solver=None):
    """Price a proposed quantity change; never create orders, fills, or account PnL."""
    codes, quotes, price, current, sellable, equity, target, bars = context(case)
    proposed = np.asarray(proposed, dtype=np.int64)
    delta = proposed - current
    fees = case.rules.fees()
    cost, commission_premium, rows = 0., 0., []
    violations = []
    for i, code in enumerate(codes):
        amount = float(abs(delta[i]) * price[i])
        side = "BUY" if delta[i] > 0 else "SELL" if delta[i] < 0 else "HOLD"
        parts = fees.costs(amount, side, case.execution_date)
        line_cost = sum(parts.values())
        cost += line_cost
        commission_premium += parts["commission"] - amount * fees.commission_bp / 10000
        bar = bars[i]
        if proposed[i] < 0 or delta[i] < -sellable[i]:
            violations.append({"rule": "sellable_quantity", "code": code})
        if delta[i] > 0 and (delta[i] % bar.buy_lot or delta[i] < bar.minimum_buy):
            violations.append({"rule": "buy_lot", "code": code})
        if delta[i] < 0 and proposed[i] != 0 and ((-delta[i]) % bar.sell_lot or -delta[i] < bar.minimum_sell):
            violations.append({"rule": "sell_lot", "code": code})
        if solver and solver.get('target_only'):
            desired = target[i] * equity / price[i] - current[i]
            limit = (np.floor((desired + 1e-8) / bar.buy_lot) * bar.buy_lot if desired >= 0 else
                     current[i] if target[i] == 0 else np.floor((-desired + 1e-8) / bar.sell_lot) * bar.sell_lot)
            if desired * delta[i] < -1e-8 or abs(delta[i]) > limit + 1e-8:
                violations.append({'rule': 'original_target_direction', 'code': code})
        blocked = quotes[code].status == "suspended" or (side == "BUY" and quotes[code].status == "buy_blocked") or (side == "SELL" and quotes[code].status == "sell_blocked")
        if amount and blocked:
            violations.append({"rule": "trade_blocked", "code": code})
        rows.append({"code": code, "price": float(price[i]), "current_quantity": int(current[i]),
                     "sellable_quantity": int(sellable[i]), "proposed_quantity": int(proposed[i]),
                     "delta": int(delta[i]), "side": side, "notional": amount, "estimated_cost": line_cost,
                     **parts, "target_weight": float(target[i]), "quote_status": quotes[code].status})
    cash = float(case.cash - delta @ price - cost)
    after_equity = equity - cost
    weights = proposed * price / after_equity if after_equity > 0 else np.full(len(codes), np.inf)
    turnover = float(np.abs(delta) @ price / equity)
    r = case.rules
    if cash < -1e-6 or cash < r.min_cash * after_equity - 1e-6:
        violations.append({"rule": "min_cash"})
    for i in np.flatnonzero(weights > r.max_weight + 1e-8):
        violations.append({"rule": "max_weight", "code": codes[i]})
    if np.count_nonzero(proposed) > r.max_holdings:
        violations.append({"rule": "max_holdings"})
    if turnover > r.turnover_cap + 1e-8:
        violations.append({"rule": "turnover_cap"})
    for i, row in enumerate(rows):
        row["projected_weight"] = float(weights[i])
    deviation = float(np.abs(weights - target).sum() + abs(cash / after_equity - (1 - target.sum()))) if after_equity > 0 else None
    conditional = any(quotes[c].status == "conditional" and delta[i] for i, c in enumerate(codes))
    result = {"id": candidate_id, "label": label, "feasible": not violations, "violations": violations,
            "estimated_cost": cost, "minimum_commission_premium": commission_premium,
            "cash_after": cash, "equity_after": after_equity, "cash_weight": cash / after_equity,
            "turnover": turnover, "holdings_count": int(np.count_nonzero(proposed)),
            "target_deviation": deviation, "order_count": int(np.count_nonzero(delta)), "rows": rows,
            "execution_status": "conditional" if conditional else "reference_price_only", "solver": solver}
    result['target_repair'] = target_repair(case, result)
    return result


def target_repair(case: ReviewInput, candidate):
    """Physical stock-gap bound at this candidate's equity, not lost-return attribution."""
    codes, quotes, price, old, sellable, equity, target, _ = context(case)
    after = candidate['equity_after']
    rows, requested, completed, retained_value = [], 0., 0., 0.
    for i, code in enumerate(codes):
        status = quotes[code].status
        lower = old[i] if status in {'sell_blocked', 'suspended'} else old[i] - sellable[i]
        upper = old[i] if status in {'buy_blocked', 'suspended'} else None
        reasons = []
        if status in {'suspended', 'buy_blocked', 'sell_blocked'}:
            reasons.append({'suspended': '停牌', 'buy_blocked': '禁止买入', 'sell_blocked': '禁止卖出'}[status])
        if old[i] > sellable[i]:
            reasons.append(f'不可卖{int(old[i]-sellable[i])}股（含冻结等，非自动认定T+1）')
        target_value = target[i] * after
        gap = max(0., lower * price[i] - target_value)
        if upper is not None:
            gap += max(0., target_value - upper * price[i])
        retained_value += lower * price[i]
        desired = target[i] * equity / price[i] - old[i]
        actual = candidate['rows'][i]['delta']
        requested += abs(desired) * price[i]
        if desired * actual > 0:
            completed += min(abs(desired), abs(actual)) * price[i]
        if reasons:
            rows.append({'code': code, 'reasons': reasons, 'minimum_quantity': int(lower),
                'maximum_quantity': int(upper) if upper is not None else None,
                'target_value': float(target_value), 'retained_value': float(lower * price[i]),
                'blocked_stock_gap_value': float(gap)})
    blocked = sum(r['blocked_stock_gap_value'] for r in rows)
    return {'rows': rows, 'blocked_stock_gap_value': blocked,
        'blocked_stock_gap_weight': blocked / after if after > 0 else None,
        'other_deviation_weight': max(0., candidate['target_deviation'] - blocked / after) if after > 0 else None,
        'retained_value': float(retained_value), 'requested_trade_value': float(requested),
        'completed_toward_target_value': float(completed),
        'target_trade_completion': completed / requested if requested > 1e-8 else None,
        'meaning': '受阻部分仅为按本方案费用后权益、数量上下界计算的股票偏离下界；不包含受阻引起的现金偏离，不能翻倍或当作全部因果归因。其余偏离含现金、整手、费用、预算等，不保证可消除。朝原目标完成额用初始权益固定目标计量，不是收益或实际成交。'}


def solve(case: ReviewInput, *, objective="tracking", deviation_bound=None, target_only=False):
    """Exact linear fees and integer lots; HiGHS proposes, project() checks independently."""
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    codes, quotes, price, old, sellable, equity, target, bars = context(case)
    n = len(codes)
    # Per asset: buy/sell lot counts, active flags, full liquidation, commission
    # regime flags, commissions, final-holding flag, absolute target deviation.
    width = 11
    count = n * width + 1
    lower, upper, integral = np.zeros(count), np.full(count, np.inf), np.zeros(count)
    rows, lo, hi = [], [], []

    def expr(*pairs):
        out = np.zeros(count)
        for index, value in pairs:
            out[index] += value
        return out

    def add(row, a=-np.inf, b=np.inf):
        rows.append(row); lo.append(a); hi.append(b)

    net_value, trading, all_cost = np.zeros(count), np.zeros(count), np.zeros(count)
    quantities, deviations = [], []
    r, fees = case.rules, case.rules.fees()
    for i, code in enumerate(codes):
        k = i * width
        buy, sell, yb, ys, full, rb, rs, cb, cs, held, dev = range(k, k + width)
        bar, p = bars[i], price[i]
        step_b, step_s = bar.buy_lot, bar.sell_lot
        remainder = int(old[i] % step_s)
        max_b = int(equity / (p * step_b))
        max_s = int(old[i] // step_s)
        upper[[buy, sell]] = [max_b, max_s]
        upper[[yb, ys, full, rb, rs, held]] = 1
        integral[[buy, sell, yb, ys, full, rb, rs, held]] = 1
        if quotes[code].status in {"buy_blocked", "suspended"} or target[i] <= 0:
            upper[buy] = upper[yb] = 0
        if quotes[code].status in {"sell_blocked", "suspended"}:
            upper[sell] = upper[ys] = upper[full] = 0
        q = expr((buy, step_b), (sell, -step_s), (full, -remainder))
        quantities.append(q)
        if target_only:
            # No overshoot beyond the lot-reachable original target. This
            # comparison does not reallocate blocked weight to other names.
            desired = target[i] * equity / p - old[i]
            if desired >= 0:
                add(q, a=0, b=float(np.floor((desired + 1e-8) / step_b) * step_b))
            else:
                reduction = old[i] if target[i] == 0 else np.floor((-desired + 1e-8) / step_s) * step_s
                add(q, a=-float(reduction), b=0)
        add(expr((buy, 1), (yb, -max_b)), b=0)
        add(expr((buy, step_b), (yb, -bar.minimum_buy)), a=0)
        add(expr((sell, 1), (ys, -max_s)), b=0)
        add(expr((full, 1), (ys, -1)), b=0)
        add(expr((sell, 1), (full, -max_s)), a=0)
        add(expr((sell, step_s), (full, remainder)), b=float(sellable[i]))
        add(expr((sell, step_s), (full, remainder), (ys, -1)), a=0)
        add(expr((sell, step_s), (full, remainder + bar.minimum_sell), (ys, -bar.minimum_sell)), a=0)
        add(expr((yb, 1), (ys, 1)), b=1)
        add(q, a=-float(old[i]))
        max_q = old[i] + max_b * step_b
        add(q - expr((held, max_q)), b=-float(old[i]))
        add(q - expr((held, 1)), a=-float(old[i]))
        buy_notional = expr((buy, p * step_b))
        sell_notional = expr((sell, p * step_s), (full, p * remainder))
        net_value += buy_notional - sell_notional
        trading += buy_notional + sell_notional
        all_cost += expr((cb, 1), (cs, 1)) + (buy_notional + sell_notional) * fees.slippage_bp / 10000
        tax_rate = fees.costs(10000., "SELL", case.execution_date)["stamp_tax"] / 10000
        all_cost += sell_notional * tax_rate
        for notional, active, regime, commission in ((buy_notional, yb, rb, cb), (sell_notional, ys, rs, cs)):
            prop = notional * fees.commission_bp / 10000
            big_m = max(1., (equity + p * old[i]) * fees.commission_bp / 10000 + fees.minimum_commission)
            upper[commission] = big_m
            add(expr((commission, 1)) - prop, a=0)
            add(expr((commission, 1), (active, -fees.minimum_commission)), a=0)
            add(expr((commission, 1), (regime, -big_m)) - prop, b=0)
            add(expr((commission, 1), (active, -fees.minimum_commission), (regime, big_m)), b=big_m)
        deviations.append(dev)
    # Cash, post-fee position caps and target deviations all use the same equity.
    add(net_value + (1-r.min_cash) * all_cost, b=case.cash - r.min_cash * equity)
    add(all_cost, b=equity - min(.01, equity / 2))
    add(trading, b=r.turnover_cap * equity)
    add(expr(*[(i * width + 9, 1) for i in range(n)]), b=r.max_holdings)
    for i, q in enumerate(quantities):
        value = q * price[i]
        add(value + r.max_weight * all_cost, b=r.max_weight * equity - price[i] * old[i])
        difference = value + target[i] * all_cost
        base = price[i] * old[i] - target[i] * equity
        dev = deviations[i]
        add(difference - expr((dev, 1)), b=-base)
        add(-difference - expr((dev, 1)), b=base)
    cash_difference = -net_value - target.sum() * all_cost
    cash_base = case.cash - (1-target.sum()) * equity
    add(cash_difference - expr((count-1, 1)), b=-cash_base)
    add(-cash_difference - expr((count-1, 1)), b=cash_base)
    dev_total = expr(*[(i, 1) for i in [*deviations, count-1]])
    if deviation_bound is not None:
        # Bound is a fraction of post-fee equity, exactly matching the display.
        add(dev_total + deviation_bound * all_cost, b=deviation_bound * equity)
    # Keep fee objectives in currency units so small fees remain numerically
    # significant even for large accounts. Scaling does not change the model.
    objective_vector = (dev_total + all_cost * 1e-5) / equity if objective == "tracking" else all_cost + trading * 1e-9
    matrix = coo_matrix(np.asarray(rows)).tocsc()
    # HiGHS' native scheduler must not fan out across short-lived web threads.
    # SciPy forwards this supported HiGHS option, although it warns about it.
    with _SOLVER_LOCK, warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Unrecognized options detected: .*threads.*", category=RuntimeWarning)
        result = milp(objective_vector, integrality=integral, bounds=Bounds(lower, upper),
                      constraints=LinearConstraint(matrix, lo, hi),
                      options={"time_limit": 5., "mip_rel_gap": 1e-7, "threads": 1})
    info = {"backend": "scipy_highs", "status": int(result.status), "message": str(result.message),
            "time_limit_seconds": 5., "relative_gap_tolerance": 1e-7,
            "gap": float(result.mip_gap) if getattr(result, "mip_gap", None) is not None and np.isfinite(result.mip_gap) else None,
            "objective": objective, "optimal_within_tolerance": result.status == 0,
            "objective_unit": "initial_equity_fraction" if objective == 'tracking' else 'CNY',
            'target_only': target_only,
            "deviation_bound": deviation_bound,
            "tracking_objective": "absolute_stock_and_cash_value_gaps_to_post_fee_target_plus_1e-5_cost"}
    if result.x is None:
        return None, info
    integers = np.flatnonzero(integral)
    if np.max(np.abs(result.x[integers] - np.rint(result.x[integers]))) > 1e-5:
        return None, {**info, "rejected": "non_integer_solution"}
    x = result.x.copy()
    x[integers] = np.rint(x[integers])
    proposed = np.rint(old + np.array([q @ x for q in quantities])).astype(np.int64)
    independently_priced = project(case, proposed, candidate_id="verification", label="verification", solver=info)
    if not independently_priced['feasible']:
        return None, {**info, 'rejected': 'independent_constraint_check', 'violations': independently_priced['violations']}
    if deviation_bound is not None and independently_priced['target_deviation'] > deviation_bound + 1e-7:
        return None, {**info, 'rejected': 'independent_deviation_check'}
    modeled_cost = float(all_cost @ x)
    if abs(modeled_cost - independently_priced["estimated_cost"]) > 1e-5:
        return None, {**info, "rejected": "modeled_commission_does_not_reconcile"}
    return proposed, info


def constraint_actions(case: ReviewInput):
    codes, quotes, price, old, sellable, equity, target, _ = context(case)
    rows = []
    retained = np.array([old[i] if quotes[c].status in {"sell_blocked", "suspended"} else old[i]-sellable[i]
                         for i, c in enumerate(codes)])
    for i in np.flatnonzero(retained * price / equity > case.rules.max_weight + 1e-9):
        rows.append({"rule": "max_weight", "code": codes[i], "retained_quantity": int(retained[i]),
                     "minimum_weight": float(retained[i] * price[i] / equity), "limit": case.rules.max_weight,
                     "action": "参考价格下保留仓位已超过上限；先核对可卖数量与禁卖状态，条件解除后再计算，当前不生成超限买入计划"})
    if np.count_nonzero(retained) > case.rules.max_holdings:
        rows.append({"rule": "max_holdings", "minimum_holdings": int(np.count_nonzero(retained)),
                     "limit": case.rules.max_holdings,
                     "action": "必须保留的股票数超过持仓上限；先处理锁仓或交易受阻，不能通过买入另一只股票解决"})
    # Even before fees and lot rounding, reducing existing excess requires this
    # much gross turnover. Fees can only tighten the post-fee weight cap.
    required_sales = float(np.maximum(old * price - case.rules.max_weight * equity, 0).sum())
    if required_sales / equity > case.rules.turnover_cap + 1e-9:
        rows.append({"rule": "turnover_cap", "minimum_turnover_lower_bound": required_sales / equity,
                     "limit": case.rules.turnover_cap,
                     "action": "仅降低已有超限仓位所需的换手下界已超过本次预算；需用户重新确认规则优先级，未自动放松预算或仓位上限"})
    return rows


def tolerance_comparison(case, candidates, attempts):
    """Sample only the approved preference range; hard constraints stay fixed."""
    baseline = next((c for c in candidates if c['id'] == 'tracking'), None)
    comparison = {'version': 1, 'status': 'unavailable', 'baseline_id': 'tracking',
                  'approved_tolerance': case.rules.deviation_tolerance,
                  'scope': 'reference_price_preference_scenarios_not_return_estimates',
                  'cost_unit': 'CNY', 'deviation_unit': 'L1_stock_and_cash_weight', 'rows': []}
    if baseline is None:
        return comparison
    comparison['status'] = 'sampled'
    bands = sorted({0., case.rules.deviation_tolerance / 2, case.rules.deviation_tolerance})
    for index, tolerance in enumerate(bands):
        bound = baseline['target_deviation'] + tolerance
        candidate = next((c for c in candidates if c['id'] == 'fees'), None) if tolerance == bands[-1] else None
        if tolerance == bands[-1]:
            info = next(a for a in attempts if a['objective'] == 'fees')
        else:
            proposed, info = solve(case, objective='fees', deviation_bound=bound)
            attempts.append(info)
            if proposed is not None:
                candidate = project(case, proposed, candidate_id=f'tolerance_{index}',
                                    label=f'额外偏离不超过 {tolerance * 100:.2f} 个百分点', solver=info)
                if candidate['feasible'] and candidate['target_deviation'] <= bound + 1e-7:
                    candidates.append(candidate)
                else:
                    candidate = None
        row = {'additional_tolerance': tolerance, 'deviation_bound': bound,
               'candidate_id': candidate['id'] if candidate else None, 'solver': info,
               'status': 'verified' if candidate else 'infeasible' if info['status'] == 2 else 'unresolved'}
        if candidate:
            row.update(estimated_cost=candidate['estimated_cost'], target_deviation=candidate['target_deviation'],
                       cost_saved_vs_tracking=baseline['estimated_cost'] - candidate['estimated_cost'],
                       deviation_added_vs_tracking=candidate['target_deviation'] - baseline['target_deviation'],
                       turnover=candidate['turnover'], cash_after=candidate['cash_after'])
        comparison['rows'].append(row)
    return comparison


def review(case: ReviewInput):
    codes, quotes, price, old, sellable, equity, target, _ = context(case)
    hold = project(case, old, candidate_id="hold", label="保持现有持仓")
    candidates, attempts = [hold], []
    proposed, info = solve(case)
    attempts.append(info)
    if proposed is not None:
        candidate = project(case, proposed, candidate_id="tracking", label="优先贴近原目标", solver=info)
        if candidate["feasible"]:
            candidates.append(candidate)
            bound = candidate["target_deviation"] + case.rules.deviation_tolerance
            cheaper, cheap_info = solve(case, objective="fees", deviation_bound=bound)
            attempts.append(cheap_info)
            if cheaper is not None:
                saving = project(case, cheaper, candidate_id="fees", label="允许偏离内减少费用", solver=cheap_info)
                if saving["feasible"] and saving["target_deviation"] <= bound + 1e-7:
                    candidates.append(saving)
    has_blocks = any(old[i] > sellable[i] or quotes[c].status in {'suspended', 'buy_blocked', 'sell_blocked'}
                     for i, c in enumerate(codes))
    if has_blocks:
        subset, subset_info = solve(case, target_only=True)
        attempts.append(subset_info)
        if subset is not None:
            partial = project(case, subset, candidate_id='target_only', label='只做朝向原目标的调整', solver=subset_info)
            if partial['feasible']:
                candidates.append(partial)
    comparison = tolerance_comparison(case, candidates, attempts)
    feasible = [c for c in candidates if c["feasible"]]
    recommendation = None
    if feasible:
        best_tracking = min(c["target_deviation"] for c in feasible)
        acceptable = [c for c in feasible if c["target_deviation"] <= best_tracking + case.rules.deviation_tolerance + 1e-8]
        if case.rules.preference == "fees":
            recommendation = min(acceptable, key=lambda c: (round(c["estimated_cost"], 6), c["target_deviation"], c["id"] != "hold"))
        else:
            recommendation = min(feasible, key=lambda c: (c["target_deviation"], c["estimated_cost"]))
    tracking = next((c for c in candidates if c["id"] == "tracking"), None)
    chosen = recommendation or hold
    limits = []
    if int(np.count_nonzero(target)) > case.rules.max_holdings:
        limits.append("原目标股票数超过持仓上限，计划只在硬约束内贴近原目标")
    if np.any(target > case.rules.max_weight + 1e-9):
        limits.append("原目标存在超过单票上限的权重，原目标保持存档，不作为可直接实施方案")
    locked = [{"code": c, "quantity": int(old[i]-sellable[i])} for i, c in enumerate(codes) if old[i] > sellable[i]]
    findings = [
        {"id": "allocation", "title": "仓位与资金", "status": "plan" if recommendation else "blocked",
         "action": "按推荐股数形成待确认计划" if recommendation else "当前硬约束无法同时满足；保留持仓并先处理锁仓、禁卖或资金限制",
         "current": hold["target_deviation"], "proposed": chosen["target_deviation"], "unit": "target_deviation"},
        {"id": "cost", "title": "费用与微调", "status": "plan" if tracking else "limited",
         "action": "在允许的目标偏离内比较预计费用；保留收益结论待验证",
         "current": tracking["estimated_cost"] if tracking else hold["estimated_cost"],
         "proposed": chosen["estimated_cost"], "unit": "cny"},
        {"id": "turnover", "title": "换手预算", "status": "plan" if recommendation else "blocked",
         "action": "按买卖金额合计 / 当前权益检查单次预算，交易所个股换手率不作替代",
         "current": case.rules.turnover_cap, "proposed": chosen["turnover"], "unit": "gross_turnover"},
        {"id": "execution", "title": "可卖数量与受阻交易", "status": "conditional" if locked or chosen["execution_status"] == "conditional" else "plan",
         "action": "锁仓和已知禁买禁卖已纳入；未知成交条件须在拟执行日复核", "locked": locked},
        {"id": "timing", "title": "节奏与信号年龄", "status": "needs_history",
         "action": "当前先交付账户计划；补充历史信号与行情后，才能比较分批节奏和信号年龄"},
    ]
    result = {"schema_version": "governance_review_v1", "input_digest": ex.object_digest(case.model_dump(mode="json")),
              "name": case.name, "equity": equity, "target_weights": dict(zip(codes, target.tolist())),
              "recommended_id": recommendation["id"] if recommendation else None,
              "recommendation_reason": "在硬约束内，按用户的目标偏好及偏离容忍比较" if recommendation else "没有通过独立约束检查的候选，未确认任何规则变更",
              "candidates": candidates, "findings": findings, "solver_attempts": attempts, "input_notices": limits,
              "constraint_actions": constraint_actions(case),
              "tolerance_comparison": comparison,
              'repair_comparison': {'has_blocks': has_blocks,
                  'authorization': '仅原始目标与原有持仓；不扩股票池、不改硬约束。朝原目标方案不把受阻权重转给其他股票；贴近目标方案可在原目标股票内配置可调资金。',
                  'target_only_status': next((a['status'] for a in attempts if a.get('target_only')), None)},
              "scope": "reference_price_plan_not_orders_or_fills", "turnover_definition": "sum_abs_trade_notional / initial_equity",
              "deviation_definition": "L1_stock_and_cash_weights_against_original_target",
              "limitations": ["no_future_return_claim", "no_actual_fills", "no_corporate_action_projection",
                              "no_pending_orders_supported", "execution_calendar_user_declared", "slippage_is_user_assumption",
                              "snapshot_does_not_identify_optimal_rebalance_timing"]}
    result["result_digest"] = ex.object_digest(result)
    return result
