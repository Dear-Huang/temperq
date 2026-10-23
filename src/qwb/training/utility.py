"""Utility on continuous, already net-of-cost equity; no additional fee penalty."""

from __future__ import annotations

import torch

from ..portfolio.contracts import number
from ..portfolio.continuous import _check


def net_utility(equity, *, variance_penalty=0.0):
    """Mean log growth minus population variance of simple daily net returns."""
    number(variance_penalty, "utility variance penalty")
    if not isinstance(equity, torch.Tensor) or equity.ndim != 1 or len(equity) < 2:
        raise ValueError("utility requires a chronological equity path with at least one return")
    _check(equity, (len(equity),), "utility equity")
    if bool((equity <= 0).any()):
        raise ValueError("utility equity must be strictly positive")
    return net_utility_returns(equity[1:] / equity[:-1] - 1, variance_penalty=variance_penalty)


def net_utility_returns(simple, *, variance_penalty=0.0):
    """Use step-local returns to preserve explicitly truncated state gradients."""
    number(variance_penalty, "utility variance penalty")
    if not isinstance(simple, torch.Tensor) or simple.ndim != 1 or not len(simple):
        raise ValueError("utility requires nonempty chronological returns")
    _check(simple, (len(simple),), "utility net returns")
    if bool((simple <= -1).any()):
        raise ValueError("utility return ratios must be strictly positive")
    ratios = 1 + simple
    growth = torch.log(ratios).mean()
    variance = simple.var(unbiased=False)
    loss = -growth + variance_penalty * variance
    if not bool(torch.isfinite(loss)):
        raise ValueError("nonfinite net utility")
    return loss, {"mean_log_growth": growth, "net_return_variance": variance,
                  "variance_convention": "population_ddof_0", "fees_already_deducted": True}
