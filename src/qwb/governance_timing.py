"""Opt-in timing research, separate from the frozen five-session account engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math

import pandas as pd

from . import experiments, governance as g, strategy
from .account_rehearsal import Action, RehearsalPlan
from .portfolio.contracts import identity, number, timestamp
from .portfolio.reference import Fees, execute_session


@dataclass(frozen=True)
class TimingPolicy:
    period_sessions: int = 5
    allow_between: bool = False
    max_extra_per_week: int = 1
    min_sessions_between_extra: int = 2
    extra_direction: str = "both"
    extra_turnover_cap: float = .5
    cost_multiplier: float = 1.5
    minimum_surplus_bp: float = 5.0
    evaluation_horizon: int = 5

    def __post_init__(self):
        for key in ("period_sessions", "max_extra_per_week", "min_sessions_between_extra", "evaluation_horizon"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if type(self.allow_between) is not bool:
            raise ValueError("allow_between must be an explicit boolean")
        if self.extra_direction not in {"both", "reduce_only", "add_only"}:
            raise ValueError("unknown extra trade direction")
        number(self.extra_turnover_cap, "extra turnover cap", upper=2)
        number(self.cost_multiplier, "cost multiplier", lower=1)
        number(self.minimum_surplus_bp, "minimum surplus bp")


@dataclass(frozen=True)
class SignalSnapshot:
    decision_at: str
    available_at: str
    data_asof: str
    version: str
    values: tuple[tuple[str, float], ...]
    score_kind: str = "ranking"
    horizon_sessions: int = 5
    calibration_id: str | None = None
    calibration_end_at: str | None = None

    def __post_init__(self):
        identity(self.version, "signal version")
        if not timestamp(self.data_asof) <= timestamp(self.available_at) <= timestamp(self.decision_at):
            raise ValueError("signal data/availability must not follow the decision")
        if self.score_kind not in {"ranking", "expected_return"}:
            raise ValueError("score kind must be ranking or expected_return (decimal return)")
        if type(self.horizon_sessions) is not int or self.horizon_sessions < 1:
            raise ValueError("signal horizon must be a positive integer")
        if (not isinstance(self.values, tuple) or not self.values
                or any(not isinstance(pair, tuple) or len(pair) != 2 for pair in self.values)):
            raise ValueError("signal values must be immutable asset/value pairs")
        if len({code for code, _ in self.values}) != len(self.values):
            raise ValueError("duplicate signal assets")
        for code, value in self.values:
            strategy.stock_code(code)
            number(value, "signal", lower=-float("inf"))
        if self.score_kind == "expected_return":
            identity(self.calibration_id, "calibration id")
            if self.calibration_end_at is None or timestamp(self.calibration_end_at) >= timestamp(self.decision_at):
                raise ValueError("return calibration must end before the decision")
            for _, value in self.values:
                number(value, "decimal expected return", lower=-1, upper=10)


@dataclass(frozen=True)
class TimingMemory:
    last_extra_session: int | None = None
    extra_week: str | None = None
    extra_count: int = 0


def read_snapshots(frame: pd.DataFrame, panel, contract: dict):
    """Import scores plus actual availability; static semantics live in a manifest."""
    required = {"code", "decision_time", "prediction", "available_at", "data_asof"}
    if not required <= set(frame):
        raise ValueError("signal CSV requires code,decision_time,prediction,available_at,data_asof")
    if not {"version", "score_kind", "horizon_sessions"} <= set(contract):
        raise ValueError("signal contract requires version,score_kind,horizon_sessions")
    matrix = g.signal_matrix(frame, panel)
    rows = frame.copy()
    rows["day"] = pd.to_datetime(rows.decision_time, utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    result = {}
    for day, group in rows.groupby("day"):
        for field in ("available_at", "data_asof"):
            if len({timestamp(value) for value in group[field]}) != 1:
                raise ValueError("each daily snapshot must have a single availability/data cutoff")
        result[day] = SignalSnapshot(
            str(day.tz_localize("Asia/Shanghai")+pd.Timedelta(hours=15, minutes=30)),
            str(group.available_at.iloc[0]), str(group.data_asof.iloc[0]),
            contract["version"], tuple((str(code), float(value)) for code, value in matrix.loc[day].dropna().items()),
            contract["score_kind"], contract["horizon_sessions"],
            contract.get("calibration_id"), contract.get("calibration_end_at"))
    return result


def assess(plan: RehearsalPlan, action_id: str, signal: SignalSnapshot | None,
           prices: dict[str, float], policy: TimingPolicy, *, session_index: int,
           memory=TimingMemory(), fees_by_asset=None):
    """Authorize intent using decision-known prices, never tomorrow's actual bars.

    The quote assumes flat prices and unrestricted liquidity; it is not a fill
    forecast. An authorized extra intent consumes its allowance even if the real
    order subsequently fails. Scheduled dates are anchored independently.
    """
    if type(session_index) is not int or session_index < 0:
        raise ValueError("session index must be a nonnegative integer")
    action = next((a for a in plan.actions if a.action_id == action_id), None)
    if action is None or action.target is None:
        raise ValueError("select a declared target action")
    scheduled = session_index % policy.period_sessions == 0
    report = {"authorized": False, "scheduled": scheduled, "action_id": action_id,
              "plan_id": plan.plan_id, "decision_at": plan.state.observed_at,
              "session_index": session_index, "quote": None,
              "signal_version": signal.version if signal else None,
              "actual_mandate_alert": (any(p.quantity*p.mark/plan.state.equity > plan.max_weight
                                           for p in plan.state.positions)
                                       or plan.state.cash/plan.state.equity < plan.min_cash),
              "limitations": ["calibration_is_user_declared_not_certified",
                              "flat_price_unrestricted_liquidity_quote",
                              "cash_return_assumed_zero", "not_an_optimal_waiting_policy",
                              "no_profit_or_loss_guarantee"]}

    def finish(reason, authorized=False):
        report.update(reason=reason, authorized=authorized)
        updated = memory
        if authorized and not scheduled:
            iso = timestamp(plan.state.observed_at).tz_convert("Asia/Shanghai").isocalendar()
            week = f"{iso.year}-{iso.week}"
            count = memory.extra_count if week == memory.extra_week else 0
            updated = TimingMemory(session_index, week, count + 1)
        return report, updated

    if signal is not None and timestamp(signal.decision_at) != timestamp(plan.state.observed_at):
        raise ValueError("signal must belong to the current decision; no implicit carry-forward")
    if not scheduled and not policy.allow_between:
        return finish("WAIT_PERIOD")
    if signal is None:
        return finish("BLOCKED_MISSING_SIGNAL" if scheduled else "WAIT_SIGNAL")
    if scheduled:
        return finish("SCHEDULED", True)
    iso = timestamp(plan.state.observed_at).tz_convert("Asia/Shanghai").isocalendar()
    week = f"{iso.year}-{iso.week}"
    if memory.extra_week == week and memory.extra_count >= policy.max_extra_per_week:
        return finish("EXTRA_LIMIT")
    if (memory.last_extra_session is not None
            and session_index - memory.last_extra_session < policy.min_sessions_between_extra):
        return finish("COOLDOWN")
    if signal.score_kind != "expected_return":
        return finish("UNCALIBRATED_SIGNAL")
    if signal.horizon_sessions != policy.evaluation_horizon:
        return finish("HORIZON_MISMATCH")
    relevant = {p.asset_id for p in plan.state.positions} | {c for c, w in action.target.weights if w > 0}
    values = dict(signal.values)
    if relevant - set(values):
        return finish("INCOMPLETE_RETURN_FORECAST")
    quote_bars = []
    for code in plan.assets:
        if code not in prices:
            raise ValueError("quote requires decision-known prices for every asset")
        price = number(prices[code], "decision price", lower=1e-12)
        quote_bars.append(strategy.session_bar(code, open_price=price, close_price=price,
                                               upper=price*100, lower=price/100))
    executor = execute_session
    extra = {}
    if fees_by_asset is not None:
        from .portfolio.struct_reference import execute_session as executor

        extra["fees_by_asset"] = fees_by_asset
    after, audit = executor(plan.state, action.target, tuple(quote_bars), open_at=plan.open_at,
                           close_at=plan.close_at, next_open_at=plan.next_open_at,
                           decision_assets=plan.assets, universe_id=plan.universe_id,
                           protocol_id=plan.protocol_id, fees=plan.fees,
                           max_weight=plan.max_weight, min_cash=plan.min_cash, **extra)
    old = {p.asset_id: p.quantity for p in plan.state.positions}
    new = {p.asset_id: p.quantity for p in after.positions}
    delta = {code: new.get(code, 0)-old.get(code, 0) for code in relevant}
    turnover = sum(f["notional"] for f in audit["fills"]) / plan.state.equity
    gain_bp = sum(delta[c]*prices[c]*values[c] for c in relevant)/plan.state.equity*10000
    cost_bp = audit["total_cost"]/plan.state.equity*10000
    report["quote"] = {"at": plan.state.observed_at, "prices": dict(prices),
                       "cost": audit["total_cost"], "cost_bp": cost_bp,
                       "gross_turnover": turnover, "estimated_gain_bp": gain_bp,
                       "required_gain_bp": policy.cost_multiplier*cost_bp+policy.minimum_surplus_bp,
                       "fills": audit["fills"]}
    if not any(delta.values()):
        return finish("NO_EXECUTABLE_CHANGE")
    if audit["constraint_violations"]:
        return finish("QUOTE_INFEASIBLE")
    if any(f["unfilled_quantity"] and f["reason"] != "LOT_ROUNDED" for f in audit["fills"]):
        return finish("QUOTE_INFEASIBLE")
    if ((policy.extra_direction == "reduce_only" and any(q > 0 for q in delta.values()))
            or (policy.extra_direction == "add_only" and any(q < 0 for q in delta.values()))):
        return finish("DIRECTION_NOT_AUTHORIZED")
    if turnover > policy.extra_turnover_cap + 1e-10:
        return finish("TURNOVER_BUDGET")
    if gain_bp <= policy.cost_multiplier*cost_bp + policy.minimum_surplus_bp:
        return finish("INSUFFICIENT_ESTIMATED_SURPLUS")
    return finish("EXTRA_AUTHORIZED", True)


def replay(panel, snapshots: dict[pd.Timestamp, SignalSnapshot], cfg: g.GovernanceConfig,
           timing: TimingPolicy, *, start, end, cash, holdings=(), snapshot_id="user-supplied",
           cost_model="commission_only", check=lambda: None):
    """Independent daily closed-loop replay for each timing policy (fixed risk only)."""
    if cost_model not in {"commission_only", "struct_v1"}:
        raise ValueError("unknown cost model")
    dates = panel["close"].index
    if (not dates.is_unique or not dates.is_monotonic_increasing or dates.tz is not None
            or not dates.equals(dates.normalize()) or pd.Timestamp(start) > pd.Timestamp(end)):
        raise ValueError("market dates must be unique sorted local sessions")
    if pd.Timestamp(start) < dates[0] or pd.Timestamp(end) > dates[-1]:
        raise ValueError("replay dates outside market coverage")
    days = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    if len(days) < 2 or len(dates[dates > days[-1]]) < 2:
        raise ValueError("at least two replay sessions and two following sessions required")
    stamp = lambda day, hour, minute=0: (day+pd.Timedelta(hours=hour, minutes=minute)).tz_localize("Asia/Shanghai")
    first_next = dates[dates > days[0]][0]
    state = strategy.account(cash, list(holdings), panel["close"].loc[days[0]].to_dict(),
                             observed_at=stamp(days[0], 15, 30), snapshot_id=snapshot_id,
                             locked_until=stamp(first_next, 9, 30))
    initial_equity = state.equity
    fees = Fees(cfg.commission_bp, cfg.minimum_commission, cfg.slippage_bp)
    spread = None
    if cost_model == "struct_v1":
        from . import struct_costs

        spread = struct_costs.estimates(panel)
    identity_hash = experiments.object_digest({"schema": "timed_governance_research_v1",
                                               "construction": asdict(cfg), "timing": asdict(timing),
                                               "cost_model": cost_model})
    pending = None
    memory = TimingMemory()
    paths, decisions, fills, positions, checks = [], [], [], [], []
    for index, day in enumerate(days):
        check()
        nxt = dates[dates > day][0]
        audit = None
        before = state.equity
        if index:
            for position in state.positions:
                factors = panel["adj_factor"][position.asset_id].loc[:day].tail(2)
                if len(factors) != 2 or not all(math.isfinite(x) and x > 0 for x in factors) or not math.isclose(*factors, rel_tol=1e-10):
                    raise ValueError("corporate action or missing adjustment factor; stop replay")
            assets = tuple(sorted(set(pending or {}) | {p.asset_id for p in state.positions}))
            if assets:
                target, _, universe = (strategy.target_for(state, pending, execution_at=stamp(day, 9, 30), recipe_hash=identity_hash)
                                       if pending is not None else (None, None, experiments.object_digest(assets)))
                bars = []
                for code in assets:
                    def price(field):
                        value = panel[field].at[day, code]
                        return float(value) if pd.notna(value) and math.isfinite(value) and value > 0 else None
                    if price("open") is None or price("close") is None:
                        raise ValueError("missing held/candidate market prices; stop replay")
                    bars.append(strategy.session_bar(code, open_price=price("open"), close_price=price("close"),
                                                     upper=price("up_limit"), lower=price("down_limit")))
                executor = execute_session
                extra = {}
                if spread is not None:
                    from .portfolio.struct_reference import execute_session as executor

                    extra["fees_by_asset"] = struct_costs.fees_for_day(spread, day, assets, fees)
                state, audit = executor(state, target, tuple(bars), open_at=stamp(day, 9, 30),
                                       close_at=stamp(day, 15), next_open_at=stamp(nxt, 9, 30),
                                       decision_assets=assets, universe_id=universe, protocol_id=identity_hash,
                                       fees=fees, max_weight=cfg.max_weight, min_cash=cfg.cash_floor, **extra)
                if audit["notices"]:
                    raise ValueError("missing market data; stop replay")
                fills.extend({"date": str(day.date()), **fill} for fill in audit["fills"])
            state = replace(state, observed_at=str(stamp(day, 15, 30)))
        pending = None
        signal = snapshots.get(day)
        if signal is None:
            if index % timing.period_sessions == 0:
                raise ValueError(f"scheduled session {day.date()} missing signal")
            result = {"decision_at": state.observed_at, "authorized": False, "reason": "WAIT_SIGNAL",
                      "scheduled": False, "session_index": index, "quote": None}
        else:
            if timestamp(signal.decision_at) != timestamp(state.observed_at):
                raise ValueError("snapshot date/time does not match decision")
            if set(dict(signal.values)) - set(panel["close"].columns):
                raise ValueError("signal contains assets without market data")
            basket, reasons = g.choose(pd.Series(dict(signal.values)), {p.asset_id for p in state.positions}, cfg)
            cap = max(0., min(cfg.exposure_cap, 1-cfg.cash_floor)-1e-12)
            weight = min(cap/cfg.n_hold, cfg.max_weight)
            drift = {p.asset_id: p.quantity*p.mark/state.equity for p in state.positions}
            weights = {code: min(cfg.max_weight, (1-cfg.adjustment_speed)*drift.get(code, 0)
                                + cfg.adjustment_speed*weight) for code in basket}
            total = sum(weights.values())
            if total > cap and total:
                weights = {code: value*cap/total for code, value in weights.items()}
            target, assets, universe = strategy.target_for(state, weights, execution_at=stamp(nxt, 9, 30), recipe_hash=identity_hash)
            settlement = dates[dates > nxt][0]
            plan = RehearsalPlan(state, (Action("hold", None), Action("adjust", target)), "hold", assets,
                                 universe, identity_hash, str(stamp(nxt, 9, 30)), str(stamp(nxt, 15)),
                                 str(stamp(settlement, 9, 30)), fees, cfg.max_weight, cfg.cash_floor)
            quote_fees = struct_costs.fees_for_day(spread, day, assets, fees) if spread is not None else None
            result, memory = assess(plan, "adjust", signal, panel["close"].loc[day, list(assets)].to_dict(), timing,
                                    session_index=index, memory=memory, fees_by_asset=quote_fees)
            if result["authorized"]:
                pending = weights
                decisions.append({"date": str(day.date()), "execution_date": str(nxt.date()),
                                  "weights": weights, "cash_target": 1-sum(weights.values()),
                                  "risk_reason": "FIXED_EXPOSURE", "evaluation_only_qlike": None,
                                  "timing_reason": result["reason"], "signal_version": signal.version,
                                  "signal_available_at": signal.available_at, "reasons": reasons})
        checks.append(result)
        violations = list(audit["constraint_violations"]) if audit else []
        exposure = 1-state.cash/state.equity
        if exposure > cfg.exposure_cap+1e-10:
            violations.append({"constraint": "exposure_cap", "actual": exposure, "limit": cfg.exposure_cap})
        paths.append({"date": str(day.date()), "equity": state.equity, "cash": state.cash,
                      "exposure": exposure, "net_return": state.equity/before-1,
                      "total_cost": audit["total_cost"] if audit else 0,
                      "gross_traded_fraction": sum(f["notional"] for f in audit["fills"])/before if audit else 0,
                      "actual_constraint_violations": len(violations), "timing_reason": result["reason"],
                      "decision_status": "REBALANCE" if pending is not None else "HOLD"})
        positions.append({"date": str(day.date()), "state": state.to_dict(), "constraint_violations": violations})
    return {"schema": "timed_governance_research_v1", "scope": "historical_replay", "diagnostic_only": True,
            "policy_hash": identity_hash, "timing": asdict(timing), "cost_model": cost_model,
            "path": paths, "decisions": decisions, "fills": fills, "positions": positions, "checks": checks,
            "metrics": g.summarize(paths, decisions, initial_equity, cfg),
            "limitations": ["fixed_risk_only", "user_declared_signal_provenance", "no_forward_evidence",
                            "cost_quote_not_realized_cost", "no_corporate_actions", "open_bar_proxy",
                            "calendar_completeness_must_be_checked_upstream", "no_statistical_superiority_claim",
                            "no_exposure_or_turnover_matched_controls_yet"]}
