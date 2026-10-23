"""DPP allocation reference; solver outputs remain raw and independently checked."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class AllocationError(ValueError):
    """No fallback is implied by a failed or infeasible allocation."""


class AllocationRejection(AllocationError):
    """A valid request could not yield an admissible production allocation."""


@dataclass(frozen=True)
class AllocationConfig:
    risk_penalty: float = 1.0
    stability_penalty: float = 0.01
    max_weight: float = 1.0
    min_cash: float = 0.0
    max_turnover: float = 2.0
    tolerance: float = 1e-6

    def __post_init__(self):
        from .contracts import number
        number(self.risk_penalty, "risk penalty", lower=1e-12)
        number(self.stability_penalty, "stability penalty", lower=1e-12)
        number(self.max_weight, "position cap", upper=1)
        number(self.min_cash, "minimum cash", upper=1)
        number(self.max_turnover, "turnover cap", upper=2)
        number(self.tolerance, "feasibility tolerance", lower=1e-12, upper=1e-6)


class ConvexAllocator:
    """Float64 CPU reference. Assets and sector constraints are fixed per layer."""

    def __init__(self, n_assets: int, config=AllocationConfig(), *, sector_matrix=None, sector_caps=None):
        import cvxpy as cp
        from cvxpylayers.torch import CvxpyLayer

        if type(n_assets) is not int or n_assets < 1:
            raise ValueError("n_assets must be a positive integer")
        self.n, self.config = n_assets, config
        w = cp.Variable(n_assets)
        buy, sell = cp.Variable(n_assets, nonneg=True), cp.Variable(n_assets, nonneg=True)
        mu = cp.Parameter(n_assets)
        root = cp.Parameter((n_assets, n_assets))
        previous = cp.Parameter(n_assets)
        buy_cost, sell_cost = cp.Parameter(n_assets, nonneg=True), cp.Parameter(n_assets, nonneg=True)
        lower, upper = cp.Parameter(n_assets), cp.Parameter(n_assets)
        receivable = cp.Parameter(nonneg=True)
        objective = (mu @ w - config.risk_penalty * cp.sum_squares(root @ w)
                     - config.stability_penalty * cp.sum_squares(w - previous)
                     - buy_cost @ buy - sell_cost @ sell)
        constraints = [w - previous == buy - sell, w >= lower, w <= upper,
                       cp.sum(w) <= 1 - receivable - config.min_cash,
                       cp.sum(buy + sell) <= config.max_turnover]
        self.sector_matrix, self.sector_caps = None, None
        if (sector_matrix is None) != (sector_caps is None):
            raise ValueError("sector matrix and caps must be supplied together")
        if sector_matrix is not None:
            a, b = np.asarray(sector_matrix, float), np.asarray(sector_caps, float)
            if (a.ndim != 2 or a.shape[1] != n_assets or b.shape != (a.shape[0],)
                    or not np.isfinite(a).all() or not np.isfinite(b).all()
                    or not np.isin(a, [0, 1]).all() or (a.sum(axis=0) > 1).any()
                    or ((b < 0) | (b > 1)).any()):
                raise ValueError("invalid sector partition or caps")
            self.sector_matrix, self.sector_caps = a, b
            constraints.append(a @ w <= b)
        self.problem = cp.Problem(cp.Maximize(objective), constraints)
        if not self.problem.is_dpp():
            raise ValueError("allocator is not DPP compliant")
        self.parameters = [mu, root, previous, buy_cost, sell_cost, lower, upper, receivable]
        self.weight_variable = w
        self.layer = CvxpyLayer(self.problem, parameters=self.parameters, variables=[w],
                               solver="DIFFCP", solver_args={"solve_method": "SCS"})

    def reference_solve(self, args):
        """Separate CVXPY solve for numerical acceptance, with solver certificates."""
        args = list(args)
        if len(args) == 7:
            args.append(np.asarray(0.0))
        for parameter, value in zip(self.parameters, args, strict=True):
            parameter.value = np.asarray(value, dtype=float)
        self.problem.solve(solver="SCS", eps=1e-9, max_iters=100000)
        if self.problem.status != "optimal":
            raise AllocationError(f"reference solve status: {self.problem.status}")
        info = self.problem.solver_stats.extra_stats["info"]
        return self.weight_variable.value.copy(), {
            "primal_residual": float(info["res_pri"]),
            "dual_residual": float(info["res_dual"]), "gap": float(info["gap"]),
            "iterations": int(info["iter"]), "status": self.problem.status}

    def __call__(self, mu, root, previous, buy_cost, sell_cost, *, lower=None, upper=None,
                 receivable_weight=None):
        import torch

        lower = torch.zeros_like(mu) if lower is None else lower
        upper = torch.full_like(mu, self.config.max_weight) if upper is None else upper
        receivable_weight = torch.zeros((), dtype=torch.float64) if receivable_weight is None else receivable_weight
        args = [mu, root, previous, buy_cost, sell_cost, lower, upper, receivable_weight]
        for i, arg in enumerate(args):
            shape = () if i == 7 else ((self.n, self.n) if i == 1 else (self.n,))
            if (not isinstance(arg, torch.Tensor) or tuple(arg.shape) != shape
                    or arg.dtype != torch.float64 or arg.device.type != "cpu"
                    or not bool(torch.isfinite(arg).all())):
                raise AllocationError("allocator requires aligned, finite float64 CPU tensors")
        reserve = float(receivable_weight.detach())
        if (not 0 <= reserve <= 1 or bool((previous < 0).any())
                or float(previous.detach().sum()) > 1 - reserve + self.config.tolerance
                or bool((buy_cost < 0).any()) or bool((sell_cost < 0).any())
                or bool((lower < 0).any()) or bool((upper > self.config.max_weight).any())
                or bool((upper < lower).any())):
            raise AllocationError("invalid prior holdings, costs or known bounds")
        budget = 1 - reserve - self.config.min_cash
        if budget < 0 or float(lower.detach().sum()) > budget + self.config.tolerance:
            raise AllocationRejection("known locked holdings conflict with the cash constraint")
        try:
            (weights,) = self.layer(*args, solver_args={"eps": 1e-9, "max_iters": 100000})
        except Exception as exc:
            from ..jobs import Cancelled
            from diffcp.cone_program import SolverError

            if isinstance(exc, (Cancelled, OSError, MemoryError, torch.OutOfMemoryError)):
                raise
            if isinstance(exc, (SolverError, RuntimeError)):
                raise AllocationRejection(f"SCS allocation failed: {type(exc).__name__}: {exc}") from exc
            raise AllocationError(f"SCS allocation failed: {type(exc).__name__}: {exc}") from exc
        raw = weights.detach().numpy()
        lo, hi = lower.detach().numpy(), upper.detach().numpy()
        prior = previous.detach().numpy()
        violations = [np.max(lo - raw), np.max(raw - hi), raw.sum() - budget,
                      np.abs(raw - prior).sum() - self.config.max_turnover, 0.0]
        if self.sector_matrix is not None:
            violations.append(float(np.max(self.sector_matrix @ raw - self.sector_caps)))
        residual = float(max(violations))
        if not np.isfinite(raw).all() or residual > self.config.tolerance:
            raise AllocationRejection(f"allocator returned infeasible weights: residual={residual}")
        return weights, {"backend": "cvxpylayers_scs_reference", "is_dpp": True,
                         "constraint_residual": residual, "cash_weight": float(1 - reserve - raw.sum()),
                         "receivable_weight": reserve,
                         "turnover": float(np.abs(raw - prior).sum()),
                         "raw_weights": raw.tolist(), "postprocessed": False}
