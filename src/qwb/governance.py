"""Isolated historical governance experiment; never writes to forward paper accounts.

Signals are supplied, not refit here. A fixed log-variance Ridge head uses only
labels that matured before each fit. All allocation arms share the raw-share
executor. Corporate actions require a later lifecycle-data adapter.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from . import daily, daily_data, experiments, ledger, strategy
from .jobs import Cancelled
from .portfolio.contracts import AccountState
from .portfolio.reference import Fees, execute_session

SCHEMA = "governance_replay_v1"
FEATURES = ["log_var5", "log_var20", "log_var60", "log_down20"]
HORIZON = 5
MIN_TRAIN = 126
REFIT_SESSIONS = 21
RIDGE_ALPHA = 10.0
VAR_FLOOR = 1e-8
LIMITATIONS = ["历史回放，不是前向成绩；不连接券商或自动下单。",
    "信号是否真正样本外仍取决于上游训练记录；导入不等于通过前视审计。",
    "原始股数开盘代理；公司行动不支持，持仓复权因子变化会停止回放。",
    "扣佣金、印花税和用户指定的滑点压力，未标定真实价差与冲击。",
    "日线平方收益是风险代理，不是高频 realized variance；短样本不能证明盈利。",
    "仓位限制约束目标；跳空、涨跌停和停牌可能导致实际持仓超限。",
    "平均仓位匹配使用整段历史，属于事后解释对照，不是可部署策略或 ML 增益证明。"]
EXPOSURE_MATCH_TOLERANCE = .002  # 0.2 percentage points of actual mean exposure
EXPOSURE_MATCH_STEPS = 12


@dataclass(frozen=True)
class GovernanceConfig:
    n_hold: int = field(default=10, metadata={"minimum":1,"maximum":500})
    hold_until_rank: int = field(default=15, metadata={"minimum":1})
    max_weight: float = field(default=.1, metadata={"exclusiveMinimum":0,"maximum":1})
    exposure_cap: float = field(default=1., metadata={"minimum":0,"maximum":1})
    cash_floor: float = field(default=0., metadata={"minimum":0,"maximum":1})
    target_volatility: float = field(default=.15, metadata={"exclusiveMinimum":0,"maximum":2})
    slippage_bp: float = field(default=0., metadata={"minimum":0,"maximum":1000})
    adjustment_speed: float = field(default=1., metadata={"exclusiveMinimum":0,"maximum":1})
    commission_bp: float = field(default=3., metadata={"minimum":0,"maximum":1000})
    minimum_commission: float = field(default=5., metadata={"minimum":0})

    def __post_init__(self):
        if type(self.n_hold) is not int or not 1 <= self.n_hold <= 500:
            raise ValueError("持仓数需为 1–500 的整数")
        if type(self.hold_until_rank) is not int or self.hold_until_rank < self.n_hold:
            raise ValueError("继续持有的排名阈值不能小于持仓数")
        for key in ("max_weight", "exposure_cap", "cash_floor", "target_volatility", "slippage_bp",
                    "adjustment_speed", "commission_bp", "minimum_commission"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not np.isfinite(value):
                raise ValueError(f"{key} 必须是有限数字")
        if not (0 < self.max_weight <= 1 and 0 <= self.exposure_cap <= 1
                and 0 <= self.cash_floor <= 1 and 0 < self.target_volatility <= 2
                and 0 <= self.slippage_bp <= 1000 and 0 < self.adjustment_speed <= 1
                and 0 <= self.commission_bp <= 1000 and self.minimum_commission >= 0):
            raise ValueError("仓位、现金、风险或费用参数越界")


def signal_matrix(frame: pd.DataFrame, panel: dict) -> pd.DataFrame:
    """Require explicit 15:30 decision timestamps; never infer intraday availability."""
    required = {"code", "decision_time", "prediction"}
    if not required <= set(frame):
        raise ValueError("预测表需包含 code,decision_time,prediction；时间必须含时区")
    rows = frame.copy()
    rows["code"] = rows.code.map(strategy.stock_code)
    stamps = []
    for value in rows.decision_time:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("决策时间必须含时区")
        local = stamp.tz_convert("Asia/Shanghai")
        if (local.hour, local.minute, local.second, local.microsecond, local.nanosecond) != (15, 30, 0, 0, 0):
            raise ValueError("首版只接受收盘后 15:30 的预测；不能回填更晚生成的信号")
        stamps.append(local.normalize().tz_localize(None))
    rows["date"] = stamps
    rows["prediction"] = pd.to_numeric(rows.prediction, errors="raise")
    if rows.empty or not np.isfinite(rows.prediction.to_numpy()).all():
        raise ValueError("预测必须非空且为有限数字")
    if rows.duplicated(["date", "code"]).any():
        raise ValueError("同一交易日同一股票不能有多份分数；请先固定一个信号版本")
    if set(rows.code) - set(panel["close"].columns) or set(rows.date) - set(panel["close"].index):
        raise ValueError("预测日期或证券不在行情快照中")
    return rows.pivot(index="date", columns="code", values="prediction").sort_index().sort_index(axis=1)


def choose(scores: pd.Series, held: set[str], cfg: GovernanceConfig):
    ordered = scores.dropna().sort_index().sort_values(ascending=False, kind="stable")
    if not len(ordered):
        raise ValueError("调仓日缺少有效信号")
    ranks = {str(code): i + 1 for i, code in enumerate(ordered.index)}
    retained = [code for code in ordered.index if code in held and ranks[code] <= cfg.hold_until_rank]
    # If actual holdings exceed N (e.g. unfilled sells), keep the best-ranked N.
    selected = retained[:cfg.n_hold]
    selected += [code for code in ordered.index[:cfg.n_hold] if code not in selected][:cfg.n_hold-len(selected)]
    reasons = [{"code": code, "reason": "HOLD_IN_BAND" if code in retained else "BUY_TOP_N",
                "rank": ranks[code]} for code in selected]
    reasons += [{"code": code, "reason": "SELL_OUT_OF_BAND", "rank": ranks.get(code)}
                for code in sorted(held - set(selected))]
    return list(selected), reasons


class RiskData:
    def __init__(self, panel):
        self.dates = panel["close"].index
        self.returns = (panel["close"] * panel["adj_factor"]).pct_change(fill_method=None)
        self.open_returns = (panel["open"] * panel["adj_factor"]).pct_change(fill_method=None)

    def features(self, day, basket):
        history = self.returns.loc[:day, basket].tail(60)
        if len(history) != 60 or not np.isfinite(history.to_numpy()).all():
            return None
        r = history.mean(axis=1).to_numpy()
        variances = [252 * np.mean(r[-n:] ** 2) for n in (5, 20, 60)]
        variances.append(252 * np.mean(np.minimum(r[-20:], 0) ** 2))
        ewma = float(pd.Series(r ** 2).ewm(alpha=.06, adjust=False).mean().iloc[-1] * 252)
        return np.log(np.maximum(variances, VAR_FLOOR)), max(ewma, VAR_FLOOR)

    def label(self, day, basket):
        t = self.dates.get_loc(day)
        # Decision at t close -> t+1 open through t+6 open: five returns.
        if t + HORIZON + 1 >= len(self.dates):
            return None
        future = self.open_returns.loc[:, basket].iloc[t+2:t+HORIZON+2]
        if not np.isfinite(future.to_numpy()).all():
            return None
        value = max(float(252 * np.mean(future.mean(axis=1).to_numpy() ** 2)), VAR_FLOOR)
        return value, self.dates[t + HORIZON + 1]


def risk_samples(risk: RiskData, signals: pd.DataFrame, n_hold: int, check=lambda: None):
    rows = []
    cfg = GovernanceConfig(n_hold=n_hold, hold_until_rank=n_hold)
    for day, scores in signals.iterrows():
        check()
        basket, _ = choose(scores, set(), cfg)
        features, label = risk.features(day, basket), risk.label(day, basket)
        if features is not None and label is not None:
            rows.append({"date": day, **dict(zip(FEATURES, features[0])),
                         "variance": label[0], "label_end": label[1]})
    return pd.DataFrame(rows, columns=["date", *FEATURES, "variance", "label_end"])


class RiskHead:
    """Fixed, standardized Ridge on log annualized mean-square returns; CPU only."""
    def __init__(self, samples):
        self.samples, self.model, self.fitted_index = samples, None, None
        self.checkpoints = []

    def predict(self, day, session_index, features, ewma):
        if self.fitted_index is None or session_index - self.fitted_index >= REFIT_SESSIONS:
            available = self.samples.loc[(self.samples.date < day) & (self.samples.label_end < day)]
            if len(available) >= MIN_TRAIN:
                x = available[FEATURES].to_numpy(float)
                y = np.log(available.variance.to_numpy(float))
                mean, scale = x.mean(axis=0), np.maximum(x.std(axis=0), 1e-6)
                z = (x - mean) / scale
                coef = np.linalg.solve(z.T @ z + RIDGE_ALPHA * np.eye(len(FEATURES)), z.T @ (y-y.mean()))
                self.model = {"fit_day": str(day.date()), "rows": len(x),
                    "max_label_end": str(available.label_end.max().date()),
                    "mean": mean.tolist(), "scale": scale.tolist(), "coef": coef.tolist(),
                    "intercept": float(y.mean()), "log_bounds": [float(y.min()), float(y.max())]}
                self.fitted_index = session_index
                self.checkpoints.append(dict(self.model))
        if self.model is None:
            return ewma, "ML_WARMUP_EWMA"
        m = self.model
        z = (features - np.array(m["mean"])) / np.array(m["scale"])
        value = np.clip(m["intercept"] + z @ m["coef"], *m["log_bounds"])
        return max(float(np.exp(value)), VAR_FLOOR), "ML_RISK"


def replay(panel, signals, cfg, *, mode, start, end, initial_cash, snapshot_id, samples,
           check=lambda: None, log=lambda message: None, constant_exposure=None,
           initial_holdings=None, snapshot_ids=None, allow_single=False, trading_calendar=None,
           cost_model="commission_only"):
    if mode not in {"fixed", "ewma", "ml"}:
        raise ValueError("unknown risk control")
    if cost_model not in {"commission_only","struct_v1"}:raise ValueError("未知成本口径")
    if constant_exposure is not None and (mode != "fixed" or isinstance(constant_exposure, bool)
            or not np.isfinite(constant_exposure) or not 0 <= constant_exposure <= cfg.exposure_cap):
        raise ValueError("固定仓位对照必须在原授权内，只适用于固定仓位政策")
    dates = panel["close"].index
    if pd.Timestamp(start) < dates[0] or pd.Timestamp(end) > dates[-1] or pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError("回放起止日期必须在行情覆盖范围内且顺序正确")
    selected_days = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    if len(selected_days) < (1 if allow_single else 2) or selected_days[0] not in signals.index:
        raise ValueError("回放至少需要两个交易日，首日必须有信号")
    if not np.isfinite(initial_cash) or initial_cash < 0 or (initial_cash==0 and not initial_holdings):
        raise ValueError("初始现金需非负，现金与持仓不能同时为空")
    identity = experiments.object_digest({"schema": SCHEMA, "config": asdict(cfg), "mode": mode,
                                         "constant_exposure": constant_exposure,"cost_model":cost_model})
    fees = Fees(cfg.commission_bp, cfg.minimum_commission, cfg.slippage_bp)
    cost_estimates=None
    if cost_model=="struct_v1":
        from . import struct_costs
        cost_estimates=struct_costs.estimates(panel)
    risk, head = RiskData(panel), RiskHead(samples)
    snapshot_ids = snapshot_ids or {}
    first = selected_days[0]
    calendar = dates if trading_calendar is None else trading_calendar
    first_next = calendar[calendar > first]
    first_next = first_next[0] if len(first_next) else first + pd.Timedelta(days=1)
    state = strategy.account(initial_cash, initial_holdings or [], panel["close"].loc[first].to_dict(),
        observed_at=first.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15, minutes=30),
        snapshot_id=snapshot_ids.get(str(first.date()), snapshot_id),
        locked_until=(first_next + pd.Timedelta(hours=9, minutes=30)).tz_localize("Asia/Shanghai"))
    initial_equity = state.equity
    pending, decisions, paths, fills, positions = None, [], [], [], []
    for i, day in enumerate(selected_days):
        check()
        t = dates.get_loc(day)
        at = lambda h, m=0: (day + pd.Timedelta(hours=h, minutes=m)).tz_localize("Asia/Shanghai")
        next_days = calendar[calendar > day]
        nxt_day = next_days[0] if len(next_days) else day + pd.Timedelta(days=1)
        nxt = (nxt_day + pd.Timedelta(hours=9, minutes=30)).tz_localize("Asia/Shanghai")
        before, audit = state.equity, None
        if i:
            assets = tuple(sorted(set(pending or {}) | {p.asset_id for p in state.positions}))
            # Raw shares cannot cross unmodelled dividends/splits. New buyers at
            # the ex-date open do not own the preceding entitlement.
            for pos in state.positions:
                factors = panel["adj_factor"][pos.asset_id].iloc[t-1:t+1].to_numpy()
                if not np.isfinite(factors).all() or not np.isclose(*factors, rtol=1e-10, atol=0):
                    raise ValueError(f"{day:%Y-%m-%d} {pos.asset_id}：公司行动或复权因子缺失，停止回放；请使用有事件覆盖的数据")
            if assets:
                target, _, universe_id = strategy.target_for(state, pending, execution_at=at(9, 30), recipe_hash=identity) if pending is not None else (None, None, experiments.object_digest(assets))
                bars = []
                for code in assets:
                    def price(field):
                        v = panel[field].at[day, code] if field in panel else np.nan
                        return float(v) if np.isfinite(v) and v > 0 else None
                    if price("open") is None or price("close") is None:
                        raise ValueError(f"{day:%Y-%m-%d} {code} 缺少行情/显式停牌状态，停止回放")
                    bars.append(strategy.session_bar(code, open_price=price("open"), close_price=price("close"),
                        upper=price("up_limit"), lower=price("down_limit"), suspended=price("open") is None))
                executor=execute_session
                cost_args={}
                if cost_estimates is not None:
                    from .portfolio.struct_reference import execute_session as executor
                    cost_args={"fees_by_asset":struct_costs.fees_for_day(cost_estimates,day,assets,fees)}
                state, audit = executor(state, target, tuple(bars), open_at=at(9, 30), close_at=at(15),
                    next_open_at=nxt, decision_assets=assets, universe_id=universe_id, protocol_id=identity,
                    fees=fees, max_weight=cfg.max_weight, min_cash=cfg.cash_floor,**cost_args)
                if audit["notices"]:
                    raise ValueError(f"{day:%Y-%m-%d} 行情或估值缺失，停止回放：{audit['notices']}")
                fills += [{"date": str(day.date()), **f} for f in audit["fills"]]
            state = replace(state, observed_at=str(at(15, 30)),
                            data_snapshot_id=snapshot_ids.get(str(day.date()), snapshot_id))
        pending = None
        # All arms anchor their five-session cycle at exactly the same start.
        if i % 5 == 0:
            if day not in signals.index:
                raise ValueError(f"{day:%Y-%m-%d} 调仓日缺少信号，不能跳过")
            basket, reasons = choose(signals.loc[day], {p.asset_id for p in state.positions}, cfg)
            variance, risk_reason = None, "FIXED_EXPOSURE"
            if mode != "fixed":
                f = risk.features(day, basket)
                if f is None:
                    raise ValueError(f"{day:%Y-%m-%d} 风险估计需要所选股票连续 60 日有效历史")
                variance, risk_reason = (f[1], "EWMA_RISK") if mode == "ewma" else head.predict(day, t, f[0], f[1])
            # Reserve a tiny amount for strict binary floating-point cash checks.
            cap = max(0., min(cfg.exposure_cap, 1-cfg.cash_floor)-1e-12)
            requested_exposure = (cfg.exposure_cap if constant_exposure is None else constant_exposure) if variance is None else cfg.target_volatility / np.sqrt(variance)
            exposure = min(cap, requested_exposure)
            weight = min(exposure / cfg.n_hold, cfg.max_weight)
            pending = {code: weight for code in basket}
            immediate_weights = dict(pending)
            drift_weights = {p.asset_id: p.quantity*p.mark/state.equity for p in state.positions}
            if cfg.adjustment_speed < 1:
                # Selection exits and hard limits take precedence over smoothing.
                pending = {code: min(cfg.max_weight, (1-cfg.adjustment_speed)*drift_weights.get(code, 0.)
                                    + cfg.adjustment_speed*w) for code, w in pending.items()}
                total = sum(pending.values())
                if total > cap and total:
                    pending = {code: w*cap/total for code, w in pending.items()}
                reasons.append({"reason": "PARTIAL_ADJUSTMENT", "speed": cfg.adjustment_speed})
            if requested_exposure > cap+1e-10:
                reasons.append({"reason": "REDUCED_MANDATE"})
            if weight < exposure / cfg.n_hold:
                reasons.append({"reason": "CAPPED_MAX_WEIGHT"})
            # Forecast quality is paired on a common decision-known basket;
            # allocation quality is measured on each arm's actual account.
            reference, _ = choose(signals.loc[day], set(), cfg)
            label = risk.label(day, reference)
            if label is not None and label[1] > selected_days[-1]:
                label = None
            score_variance = None
            if mode != "fixed":
                common = risk.features(day, reference)
                if common is not None:
                    score_variance = common[1] if mode == "ewma" else head.predict(day, t, common[0], common[1])[0]
            qlike = None if score_variance is None or label is None else float(label[0]/score_variance-np.log(label[0]/score_variance)-1)
            decisions.append({"date": str(day.date()), "execution_date": str(nxt_day.date()),
                "weights": pending, "cash_target": 1-sum(pending.values()), "risk_reason": risk_reason,
                "immediate_weights": immediate_weights, "drift_weights": drift_weights,
                "adjustment_speed": cfg.adjustment_speed,
                "unconstrained_exposure": float(requested_exposure), "authorized_exposure": float(exposure),
                "uncapped_weight": float(exposure/cfg.n_hold), "target_exposure": float(sum(pending.values())),
                "forecast_variance": variance, "risk_fit_day": head.model["fit_day"] if mode == "ml" and head.model else None,
                "evaluation_basket": reference, "evaluation_forecast_variance": score_variance,
                "evaluation_target_variance": label[0] if label else None,
                "evaluation_only_qlike": qlike, "evaluation_label_end": str(label[1].date()) if label else None,
                "reasons": reasons})
        total_cost = audit["total_cost"] if audit else 0.0
        traded = sum(f["notional"] for f in audit["fills"]) if audit else 0.0
        violations = list(audit["constraint_violations"]) if audit else []
        actual_exposure=1-state.cash/state.equity
        if actual_exposure>cfg.exposure_cap+1e-10:
            violations.append({"constraint":"exposure_cap","actual":actual_exposure,"limit":cfg.exposure_cap})
        paths.append({"date": str(day.date()), "equity": state.equity, "cash": state.cash,
            "exposure": 1-state.cash/state.equity, "net_return": state.equity/before-1,
            "total_cost": total_cost, "gross_traded_fraction": traded/before,
            "struct_spread":sum(f.get("struct_spread",0.) for f in audit["fills"]) if audit else 0.,
            "actual_constraint_violations": len(violations),
            "decision_status": "REBALANCE" if pending is not None else "NOT_REBALANCE_DAY"})
        positions.append({"date": str(day.date()), "state": state.to_dict(), "constraint_violations": violations})
        if i % 50 == 0:
            log(f"{mode} {i+1}/{len(selected_days)} {day:%Y-%m-%d}")
    return {"path": paths, "decisions": decisions, "fills": fills, "positions": positions,
            "risk_models": head.checkpoints, "metrics": summarize(paths, decisions, initial_equity, cfg)}


def exposure_matched_control(panel, signals, cfg, reference, fixed, *, start, end,
        initial_cash, snapshot_id, samples, check=lambda: None, log=lambda message: None,
        initial_holdings=None, trading_calendar=None, reference_arm="ml",cost_model="commission_only"):
    """Post-hoc explanation only: calibrate constant intent to actual ML exposure.

    Integer execution and policy feedback make the mapping discontinuous and not
    necessarily monotone. Retain the best tested point and disclose the residual;
    never select using returns or claim exact matching when it is infeasible.
    """
    target = reference["metrics"]["mean_exposure"]
    trials = [{"constant_target": cfg.exposure_cap,
               "actual_mean": fixed["metrics"]["mean_exposure"]}]
    best, constant = fixed, cfg.exposure_cap
    gap = abs(trials[0]["actual_mean"]-target)
    lo, hi = 0., min(cfg.exposure_cap, 1-cfg.cash_floor)
    if gap > EXPOSURE_MATCH_TOLERANCE:
        for step in range(EXPOSURE_MATCH_STEPS):
            check()
            candidate = 0. if step == 0 else (lo+hi)/2
            arm = replay(panel, signals, cfg, mode="fixed", start=start, end=end,
                initial_cash=initial_cash, snapshot_id=snapshot_id, samples=samples,
                constant_exposure=candidate, check=check, initial_holdings=initial_holdings,
                trading_calendar=trading_calendar,cost_model=cost_model)
            actual = arm["metrics"]["mean_exposure"]
            trials.append({"constant_target": candidate, "actual_mean": actual})
            current = abs(actual-target)
            if current < gap:
                best, constant, gap = arm, candidate, current
            log(f"事后仓位匹配 {step+1}/{EXPOSURE_MATCH_STEPS}：实际差 {current:.4%}")
            if gap <= EXPOSURE_MATCH_TOLERANCE:
                break
            if actual < target:
                lo = candidate
            else:
                hi = candidate
    # Do not mutate the original fixed arm if it already meets the tolerance.
    result = dict(best)
    result["diagnostic"] = {"scope": "post_hoc_explanation_not_deployable", "reference_arm": reference_arm,
        "matched_quantity": "mean_actual_close_exposure_excluding_initial_state",
        "reference_mean_exposure": target, "constant_target_exposure": float(constant),
        "actual_mean_exposure": best["metrics"]["mean_exposure"],
        "signed_match_error": best["metrics"]["mean_exposure"]-target,
        "tolerance": EXPOSURE_MATCH_TOLERANCE,
        "status": "MATCHED_WITHIN_TOLERANCE" if gap <= EXPOSURE_MATCH_TOLERANCE else "NOT_MATCHED",
        "selection_criterion": "absolute_exposure_error_only_not_return",
        "trials": trials, "limitation": "使用整个回放区间的参考组仓位校准；不能区分所有风格/择时效应或证明 alpha。"}
    return result


def summarize(path, decisions, cash, cfg):
    equity = np.array([row["equity"] for row in path])
    returns = np.array([row["net_return"] for row in path[1:]])
    peak = np.maximum.accumulate(np.r_[cash, equity])[1:]
    qlike = [d["evaluation_only_qlike"] for d in decisions if d["evaluation_only_qlike"] is not None]
    annual_risk = float(np.sqrt(np.mean(returns**2)*252)) if len(returns) else 0.0
    return {"sessions": len(returns), "total_return": float(equity[-1]/cash-1),
        "max_drawdown": float(np.min(equity/peak-1)), "annualized_rms_return": annual_risk,
        "risk_target_error": abs(annual_risk-cfg.target_volatility),
        "mean_exposure": float(np.mean([r["exposure"] for r in path[1:]])) if len(path)>1 else path[0]["exposure"],
        "total_cost": sum(r["total_cost"] for r in path),
        "struct_spread":sum(r.get("struct_spread",0.) for r in path),
        "gross_traded_fraction": sum(r["gross_traded_fraction"] for r in path),
        "actual_constraint_violations": sum(r["actual_constraint_violations"] for r in path),
        "risk_qlike": float(np.mean(qlike)) if qlike else None, "risk_labels": len(qlike),
        "ml_warmup_decisions": sum(d["risk_reason"] == "ML_WARMUP_EWMA" for d in decisions)}


def compare(panel, signals, cfg, *, start, end, initial_cash=100000, snapshot_id="unverified",
            check=lambda: None, log=lambda message: None):
    samples = risk_samples(RiskData(panel), signals, cfg.n_hold, check)
    arms = {}
    baseline = replace(cfg, hold_until_rank=cfg.n_hold, max_weight=1., adjustment_speed=1.)
    for name, mode, rules in [("rules_baseline", "fixed", baseline), ("fixed", "fixed", cfg),
                              ("ewma", "ewma", cfg), ("ml", "ml", cfg)]:
        log(f"运行 {name}")
        arms[name] = replay(panel, signals, rules, mode=mode, start=start, end=end,
            initial_cash=initial_cash, snapshot_id=snapshot_id, samples=samples, check=check, log=log)
    arms["ml_matched_fixed"] = exposure_matched_control(panel, signals, cfg, arms["ml"], arms["fixed"],
        start=start, end=end, initial_cash=initial_cash, snapshot_id=snapshot_id, samples=samples, check=check, log=log)
    return arms, samples


def run(predictions, data_dir, output, cfg, *, start, end, initial_cash=100000,
        check=lambda: None, log=lambda message: None):
    """One explicit attempt; failures remain visible and no existing result is replaced."""
    output, predictions = Path(output), Path(predictions)
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    record = {"schema": SCHEMA, "scope": "historical_replay", "status": "RUNNING",
              "created_at": experiments.now(), "config": asdict(cfg), "start": str(start), "end": str(end),
              "initial_cash": initial_cash, "limitations": LIMITATIONS}
    experiments.write(output / "result.json", record)
    try:
        snapshot = daily_data.current(data_dir)
        source = pd.read_parquet(predictions) if predictions.suffix.lower() == ".parquet" else pd.read_csv(predictions, dtype={"code": str})
        panel = daily.load_panel(snapshot)
        dates = panel["close"].index
        calendar = pd.read_csv(snapshot / "calendar.csv", dtype=str)
        open_days = pd.DatetimeIndex(pd.to_datetime(calendar.loc[calendar.is_open.eq("1"), "date"]))
        expected = open_days[(open_days >= dates.min()) & (open_days <= dates.max())]
        if not dates.equals(expected):
            raise ValueError("行情与权威交易日历不连续，不能压缩缺失交易日")
        signals = signal_matrix(source, panel)
        snapshot_id = experiments.read(snapshot / "snapshot.json")["data_snapshot_id"]
        if "data_snapshot_id" in source and set(source.data_snapshot_id) != {snapshot_id}:
            raise ValueError("预测的数据快照与行情不一致；请在同一冻结快照上导出预测")
        record.update(signal_hash=experiments.digest(predictions), snapshot_id=snapshot_id,
            environment={"numpy": np.__version__, "pandas": pd.__version__},
            execution_policy={"protocol": strategy.protocol()[0],
                              "fees": asdict(Fees(cfg.commission_bp, cfg.minimum_commission, cfg.slippage_bp)),
                              "initial_account": "cash_only", "cadence_sessions": 5,
                              "cadence_anchor": "first_observed_session_within_requested_interval",
                              "cadence_anchor_resets_when_replay_start_changes": True},
            exposure_match_spec={"scope": "post_hoc_explanation_not_deployable",
                "reference_arm": "ml", "tolerance": EXPOSURE_MATCH_TOLERANCE, "max_steps": EXPOSURE_MATCH_STEPS},
            data_files={p.relative_to(snapshot).as_posix(): experiments.digest(p)
                        for p in sorted(snapshot.rglob("*")) if p.is_file() and p.suffix in {".parquet", ".csv", ".json"}},
            implementation_hash=experiments.object_digest({"governance": experiments.digest(Path(__file__)),
                "executor": experiments.digest(Path(__file__).parent / "portfolio/reference.py"),
                "contracts": experiments.digest(Path(__file__).parent / "portfolio/contracts.py"),
                "market_rules": experiments.digest(Path(__file__).parent / "rules/cn_a.yaml"),
                "strategy": experiments.digest(Path(strategy.__file__))}),
            risk_spec={"features": FEATURES, "horizon": HORIZON, "min_train": MIN_TRAIN,
                       "ridge_alpha": RIDGE_ALPHA, "refit_sessions": REFIT_SESSIONS,
                       "training_basket": "decision_known_top_n_equal_weight",
                       "label": "annualized_mean_square_adjusted_open_returns"})
        record["experiment_hash"] = experiments.object_digest({key: record[key] for key in
            ("schema", "config", "start", "end", "initial_cash", "signal_hash", "snapshot_id", "data_files", "implementation_hash", "risk_spec", "environment", "execution_policy", "exposure_match_spec")})
        experiments.write(output / "result.json", record)
        arms, samples = compare(panel, signals, cfg, start=start, end=end, initial_cash=initial_cash,
                                snapshot_id=snapshot_id, check=check, log=log)
        samples.to_parquet(output / "risk_samples.parquet", index=False)
        record["metrics"] = {}
        record["diagnostics"] = {}
        for name, arm in arms.items():
            root = output / name
            root.mkdir()
            pd.DataFrame(arm["path"]).to_csv(root / "path.csv", index=False)
            for kind in ("decisions", "fills", "positions", "risk_models"):
                (root / f"{kind}.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, default=str)+"\n" for row in arm[kind]), encoding="utf-8")
            record["metrics"][name] = arm["metrics"]
            if "diagnostic" in arm:
                record["diagnostics"][name] = arm["diagnostic"]
        record.update(status="SUCCEEDED", elapsed_seconds=round(time.perf_counter()-started, 3),
            conclusion="历史工程对照完成；不据此宣布 ML 优于规则或产生 alpha。")
    except BaseException as exc:
        record.update(status="CANCELLED" if isinstance(exc, Cancelled) else "FAILED", error=str(exc) or type(exc).__name__,
                      elapsed_seconds=round(time.perf_counter()-started, 3))
        experiments.write(output / "result.json", record)
        ledger.append(output.parent / "attempts.jsonl", {"output": output.name, "config": asdict(cfg),
            "status": record["status"], "error": record["error"]})
        raise
    experiments.write(output / "result.json", record)
    ledger.append(output.parent / "attempts.jsonl", {"output": output.name, "config": asdict(cfg),
        "signal_hash": record["signal_hash"], "snapshot_id": record["snapshot_id"], "status": record["status"]})
    return record
