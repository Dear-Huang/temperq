"""Disclosed feasible target production, separate from differentiable raw solves."""

from __future__ import annotations

import numpy as np

from .optimization import AllocationError, AllocationRejection


def project(raw, previous, lower, upper, *, budget, max_turnover, tolerance):
    """Nearest L1 feasible intent using HiGHS; never an investment fallback."""
    from scipy.optimize import linprog

    raw, previous, lower, upper = (np.asarray(x, dtype=float) for x in (raw, previous, lower, upper))
    n = len(raw)
    if (not n or any(x.shape != (n,) or not np.isfinite(x).all() for x in (raw, previous, lower, upper))
            or (lower < 0).any() or (upper < lower).any() or not np.isfinite([budget, max_turnover, tolerance]).all()
            or not 0 <= budget <= 1 or not 0 <= max_turnover <= 2 or not 0 < tolerance <= 1e-6):
        raise AllocationError("invalid target projection inputs")
    eye, zero = np.eye(n), np.zeros((n, n))
    matrix = np.vstack([np.block([eye, -eye, zero]), np.block([-eye, -eye, zero]),
                        np.block([eye, zero, -eye]), np.block([-eye, zero, -eye]),
                        np.r_[np.ones(n), np.zeros(2 * n)], np.r_[np.zeros(2 * n), np.ones(n)]])
    rhs = np.r_[raw, -raw, previous, -previous, budget, max_turnover]
    solved = linprog(np.r_[np.zeros(n), np.ones(n), np.zeros(n)], A_ub=matrix, b_ub=rhs,
        bounds=[*zip(lower, upper), *[(0, None)] * (2 * n)], method="highs",
        options={"primal_feasibility_tolerance": 1e-9, "dual_feasibility_tolerance": 1e-9})
    if not solved.success:
        raise AllocationError(f"target projection failed: {solved.status}: {solved.message}")
    output = solved.x[:n]
    adjustment = float(np.max(np.abs(output - raw)))
    residual = float(max(np.max(lower - output), np.max(output - upper),
                         output.sum() - budget, np.abs(output - previous).sum() - max_turnover, 0))
    if (not np.isfinite(output).all() or (output < 0).any()
            or adjustment > tolerance or residual > tolerance):
        raise AllocationError(f"target projection exceeds declared numerical limits: change={adjustment}, residual={residual}")
    return output, {"policy": "highs_l1_projection_v1", "backend": "scipy_highs", "status": solved.message,
                    "raw_weights": raw.tolist(), "produced_weights": output.tolist(),
                    "max_absolute_adjustment": adjustment, "l1_adjustment": float(np.abs(output - raw).sum()),
                    "constraint_residual": residual, "postprocessed": True,
                    "gradient_scope": "not_differentiated; raw CVXPYlayers gradients remain separate"}


def tensor_produce(raw, previous, lower, upper, *, budget, max_turnover, tolerance, buffer=1e-12):
    """Declared numerical map, with autograd through its actual piecewise forward.

    Box and budget corrections precede a convex interpolation toward a feasible
    anchor if needed for turnover. This is not a nearest-point projection. A map
    needing a material correction, or without a usable anchor, fails explicitly.
    """
    import torch
    from .continuous import _check
    from .contracts import number

    if not isinstance(raw, torch.Tensor) or raw.ndim != 1 or not len(raw):
        raise AllocationError("tensor production requires a nonempty weight vector")
    for value, name in ((raw, "raw weights"), (previous, "previous weights"),
                        (lower, "lower bounds"), (upper, "upper bounds")):
        _check(value, tuple(raw.shape), name)
    _check(budget, (), "weight budget")
    number(max_turnover, "turnover cap", upper=2)
    number(tolerance, "production tolerance", lower=1e-12, upper=1e-6)
    number(buffer, "production buffer", lower=1e-15, upper=min(tolerance, 1e-9))
    if (bool((previous < 0).any()) or bool((lower < 0).any()) or bool((upper < lower).any())
            or bool((upper > 1).any()) or not 0 <= float(budget.detach()) <= 1
            or float(lower.sum().detach()) > float(budget.detach())):
        raise AllocationError("infeasible bounds for tensor production")
    safe_budget = budget - torch.minimum(budget.new_tensor(buffer), (budget - lower.sum()) / 2)

    def box_budget(value):
        boxed = torch.minimum(torch.maximum(value, lower), upper)
        slack = boxed - lower
        total = slack.sum()
        denominator = torch.where(total > 0, total, torch.ones_like(total))
        scale = torch.minimum(torch.ones_like(total), (safe_budget - lower.sum()) / denominator)
        return lower + slack * scale

    produced = box_budget(raw)
    turnover = (produced - previous).abs().sum()
    safe_turnover = max(0., max_turnover - min(buffer, max_turnover / 2))
    corrected_turnover = float(turnover.detach()) > safe_turnover
    if corrected_turnover:
        anchor = box_budget(previous)
        anchor_turnover = (anchor - previous).abs().sum()
        if float(anchor_turnover.detach()) > safe_turnover:
            raise AllocationRejection("tensor production has no feasible turnover anchor")
        fraction = (turnover - safe_turnover) / (turnover - anchor_turnover)
        produced = (1 - fraction) * produced + fraction * anchor
    adjustment = float((produced - raw).detach().abs().max())
    residual = max(float((lower - produced).detach().max()), float((produced - upper).detach().max()),
                   float((produced.sum() - budget).detach()),
                   float(((produced - previous).abs().sum() - max_turnover).detach()), 0.)
    if (not bool(torch.isfinite(produced).all()) or bool((produced < 0).any())
            or adjustment > tolerance or residual > tolerance):
        raise AllocationRejection(f"tensor production exceeds numerical limits: change={adjustment}, residual={residual}")
    return produced, {"policy": "torch_box_budget_turnover_v1", "raw_weights": raw.detach().tolist(),
        "produced_weights": produced.detach().tolist(), "max_absolute_adjustment": adjustment,
        "l1_adjustment": float((produced - raw).detach().abs().sum()),
        "constraint_residual": residual, "buffer": buffer, "turnover_correction": corrected_turnover,
        "postprocessed": True, "gradient_scope": "piecewise_autograd_through_actual_mapping_no_STE"}


def tensor_produce_dust(raw, previous, lower, upper, *, budget, max_turnover, tolerance,
                        buffer=1e-12, dust_threshold=1e-10):
    """Explicit target dead zone after the v1 map; never remove account positions.

    This declares numerical-scale targets economically zero. It cannot infer
    whether a tiny positive solver value is noise or a deliberate prediction.
    Positive locked lower bounds are preserved; new turnover violations fail.
    """
    import torch
    from .contracts import number

    number(dust_threshold, "target dust threshold", lower=1e-15, upper=min(tolerance, 1e-8))
    base, audit = tensor_produce(raw, previous, lower, upper, budget=budget,
        max_turnover=max_turnover, tolerance=tolerance, buffer=buffer)
    suppressed = (lower == 0) & (base > 0) & (base <= dust_threshold)
    produced = torch.where(suppressed, torch.zeros_like(base), base)
    turnover = float((produced - previous).detach().abs().sum())
    adjustment = float((produced - raw).detach().abs().max())
    residual = max(float((lower - produced).detach().max()), float((produced - upper).detach().max()),
        float((produced.sum() - budget).detach()), turnover - max_turnover, 0.)
    if adjustment > tolerance or residual > tolerance or turnover > max_turnover:
        raise AllocationRejection(f"target dust map violates numerical/turnover limits: change={adjustment}, residual={residual}")
    return produced, {"policy": "torch_box_budget_turnover_dust_v1", "base_mapping": audit,
        "raw_weights": raw.detach().tolist(), "produced_weights": produced.detach().tolist(),
        "dust_threshold": dust_threshold, "suppressed_indices": suppressed.nonzero().flatten().tolist(),
        "suppressed_weight": float((base - produced).detach().sum()),
        "max_absolute_adjustment": adjustment, "l1_adjustment": float((produced - raw).detach().abs().sum()),
        "constraint_residual": residual, "buffer": buffer, "postprocessed": True,
        "gradient_scope": "actual_piecewise_map; zero_derivative_for_suppressed_targets; no_STE"}
