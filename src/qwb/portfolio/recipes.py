"""Causal, fixed risk and directional cost recipes in decimal daily-return units."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .. import experiments
from .contracts import number, timestamp


class RecipeError(ValueError):
    pass


class HistoryUnavailable(RecipeError):
    """Valid causal inputs have insufficient historical observations."""


@dataclass(frozen=True)
class RiskRecipe:
    lookback: int = 60
    min_observations: int = 20
    shrinkage: float = .1
    variance_floor: float = 1e-8

    def __post_init__(self):
        if (type(self.lookback) is not int or type(self.min_observations) is not int
                or not 2 <= self.min_observations <= self.lookback):
            raise ValueError("risk lookback/minimum must be integers with 2 <= minimum <= lookback")
        number(self.shrinkage, "covariance shrinkage", upper=1)
        number(self.variance_floor, "variance floor", lower=1e-12, upper=1)


@dataclass(frozen=True)
class CostRecipe:
    impact_bp_at_full_adv: float = 10.0
    sell_tax_bp: float = 10.0
    available_at: str = "2023-01-01T00:00:00+08:00"

    def __post_init__(self):
        number(self.impact_bp_at_full_adv, "impact basis points", upper=1000)
        number(self.sell_tax_bp, "declared sell tax basis points", upper=1000)
        timestamp(self.available_at)


def estimate(bundle, decision_time, assets, *, equity, fees, risk=RiskRecipe(), cost=CostRecipe()):
    """Latest visible revisions; missing assets fail instead of disappearing."""
    stamp = timestamp(decision_time)
    if timestamp(cost.available_at) > stamp:
        raise RecipeError("cost declaration is not yet available")
    number(equity, "actual account equity", lower=np.finfo(float).tiny)
    assets = tuple(assets)
    if not assets or len(set(assets)) != len(assets):
        raise RecipeError("risk assets must be explicit, unique and nonempty")
    day = stamp.tz_convert("Asia/Shanghai").tz_localize(None).normalize()
    if day not in bundle.calendar:
        raise RecipeError("risk decision must be in the explicit calendar")
    dates = bundle.calendar[bundle.calendar <= day][-risk.lookback:]
    visible = bundle.bars[(bundle.bars["available_time"] <= stamp)
                          & bundle.bars["date"].isin(dates) & bundle.bars["code"].isin(assets)]
    visible = visible.sort_values(["code", "date", "available_time", "revision"]).drop_duplicates(["code", "date"], keep="last")
    opened = visible.pivot(index="date", columns="code", values="open").reindex(index=dates, columns=assets)
    closed = visible.pivot(index="date", columns="code", values="close").reindex_like(opened)
    valid = np.isfinite(opened) & np.isfinite(closed) & (opened > 0) & (closed > 0)
    returns = (closed / opened - 1).where(valid).replace([np.inf, -np.inf], np.nan)
    common = returns.dropna(axis=0, how="any")
    if len(common) < risk.min_observations:
        raise HistoryUnavailable(f"insufficient common causal risk history: {len(common)} < {risk.min_observations}")
    covariance = np.atleast_2d(np.cov(common.to_numpy(float), rowvar=False, ddof=1))
    covariance = ((1 - risk.shrinkage) * covariance
                  + risk.shrinkage * np.diag(np.diag(covariance)))
    eigenvalues, vectors = np.linalg.eigh(covariance)
    floored = np.maximum(eigenvalues, risk.variance_floor)
    covariance = (vectors * floored) @ vectors.T
    root = np.linalg.cholesky(covariance).T
    amount = visible.pivot(index="date", columns="code", values="amount").reindex(index=dates, columns=assets)
    amount = amount.where(np.isfinite(amount) & (amount > 0))
    if (amount.count() < risk.min_observations).any():
        raise HistoryUnavailable("insufficient positive causal liquidity history for every asset")
    adv = amount.median().to_numpy(float)
    impact = cost.impact_bp_at_full_adv * equity / adv
    buy = (fees.commission_bp + fees.slippage_bp + impact) / 10000
    sell = buy + cost.sell_tax_bp / 10000
    if not all(np.isfinite(x).all() for x in (root, buy, sell)):
        raise RecipeError("nonfinite risk or cost estimate")
    columns = ["code", "date", "available_time", "revision", "open", "close", "amount"]
    consumed = visible[columns].astype(object).where(pd.notna(visible[columns]), None).to_dict("records")
    audit = {"recipe": "historical_intraday_shrinkage_linear_cost_v1", "decision_time": str(stamp),
             "assets": assets, "risk": asdict(risk), "cost": asdict(cost), "fees": asdict(fees),
             "return_units": "decimal_next_session_open_to_close", "amount_units": "CNY",
             "window_start": str(dates[0].date()), "window_end": str(dates[-1].date()),
             "risk_dates": [str(d.date()) for d in common.index], "n_common_dates": len(common),
             "liquidity_observations": amount.count().tolist(), "median_amount": adv.tolist(),
             "source_rows_hash": experiments.object_digest(consumed),
             "eigenvalues_before_floor": eigenvalues.tolist(), "eigenvalues_after_floor": floored.tolist(),
             "condition_number": float(floored.max() / floored.min()),
             "covariance": covariance.tolist(), "risk_root": root.tolist(),
             "buy_cost": buy.tolist(), "sell_cost": sell.tolist(), "actual_equity": float(equity),
             "missing_policy": "fail_if_insufficient_common_history_no_asset_drop",
             "cost_scope": "linear_full_equity_to_historical_median_amount_proxy_minimum_fees_excluded"}
    audit["estimate_id"] = experiments.object_digest(audit)
    return root, buy, sell, audit


def estimate_tensors(bundle, decision_time, assets, *, equity, fees, risk=RiskRecipe(), cost=CostRecipe()):
    """Freeze historical estimation, retaining the live capital-to-liquidity gradient."""
    import torch
    from .continuous import _check

    _check(equity, (), "recipe equity", nonnegative=True)
    if float(equity.detach()) <= 0:
        raise RecipeError("recipe equity must be positive")
    root, _, _, audit = estimate(bundle, decision_time, assets, equity=float(equity.detach()),
                                  fees=fees, risk=risk, cost=cost)
    historical_amount = torch.tensor(audit["median_amount"], dtype=torch.float64)
    buy = ((fees.commission_bp + fees.slippage_bp) / 10000
           + cost.impact_bp_at_full_adv / 10000 * equity / historical_amount)
    sell = buy + cost.sell_tax_bp / 10000
    return torch.tensor(root, dtype=torch.float64), buy, sell, audit
