"""Analytical interior solutions, solver certificates, gradients and hard bounds."""

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")

from qwb.portfolio.optimization import AllocationConfig, AllocationError, ConvexAllocator


def inputs():
    values = [[.03, .02], [[.2, 0], [0, .2]], [.2, .2], [.0002, .0002], [.0004, .0004]]
    return [torch.tensor(x, dtype=torch.float64) for x in values]


def test_solution_matches_closed_form_and_independent_cvxpy_certificate():
    model = ConvexAllocator(2)
    args = inputs()
    w, audit = model(*args)
    expected = (args[0].numpy() + .02 * args[2].numpy() - args[3].numpy()) / .1
    np.testing.assert_allclose(w.detach().numpy(), expected, atol=1e-7)
    ref, certificate = model.reference_solve([a.numpy() for a in args] + [np.zeros(2), np.ones(2)])
    np.testing.assert_allclose(w.detach().numpy(), ref, atol=1e-7)
    assert certificate["primal_residual"] < 1e-6
    assert certificate["dual_residual"] < 1e-6
    assert abs(certificate["gap"]) < 1e-6
    assert audit["constraint_residual"] <= 1e-6 and not audit["postprocessed"]


def test_interior_autograd_matches_finite_difference_step_sweep():
    model = ConvexAllocator(2)
    args = inputs()
    args[0].requires_grad_(True)
    probe = torch.tensor([.7, -.3], dtype=torch.float64)
    w, _ = model(*args)
    (w @ probe).backward()
    analytic = args[0].grad.detach().numpy()
    np.testing.assert_allclose(analytic, [7, -3], atol=1e-4)
    for step in [1e-3, 1e-4, 1e-5]:
        finite = []
        for i in range(2):
            hi, lo = args[0].detach().clone(), args[0].detach().clone()
            hi[i] += step
            lo[i] -= step
            plus, _ = model(hi, *args[1:])
            minus, _ = model(lo, *args[1:])
            finite.append(float((plus @ probe - minus @ probe) / (2 * step)))
        np.testing.assert_allclose(analytic, finite, rtol=1e-3, atol=1e-5)


def test_boundary_sector_limits_and_infeasibility_are_not_silent_fallbacks():
    model = ConvexAllocator(2, AllocationConfig(max_weight=.4),
                            sector_matrix=[[1, 1]], sector_caps=[.5])
    args = inputs()
    args[0] = torch.tensor([1., .02], dtype=torch.float64)
    w, audit = model(*args)
    assert float(w[0]) == pytest.approx(.4, abs=1e-6)
    assert float(w.sum()) <= .5 + 1e-6
    low = torch.tensor([.4, .4], dtype=torch.float64)
    with pytest.raises(AllocationError, match="failed"):
        model(*args, lower=low)


def test_invalid_precision_nan_and_locked_cash_conflict_rejected():
    args = inputs()
    model = ConvexAllocator(2, AllocationConfig(min_cash=.3))
    with pytest.raises(AllocationError, match="float64"):
        model(args[0].float(), *args[1:])
    with pytest.raises(AllocationError, match="cash"):
        model(*args, lower=torch.tensor([.4, .4], dtype=torch.float64))
    args[0][0] = float("nan")
    with pytest.raises(AllocationError, match="finite"):
        model(*args)


def test_receivable_budget_is_not_cash_and_keeps_dpp_gradient():
    model = ConvexAllocator(2, AllocationConfig(min_cash=.1))
    args = inputs()
    args[0] = torch.tensor([1., 1.], dtype=torch.float64)
    reserve = torch.tensor(.25, dtype=torch.float64, requires_grad=True)
    weights, audit = model(*args, receivable_weight=reserve)
    assert float(weights.sum().detach()) == pytest.approx(.65, abs=1e-6)
    assert audit["cash_weight"] == pytest.approx(.1, abs=1e-6)
    assert audit["receivable_weight"] == .25
    weights.sum().backward()
    assert float(reserve.grad) == pytest.approx(-1, abs=1e-4)
    reference, _ = model.reference_solve([a.numpy() for a in args] + [np.zeros(2), np.ones(2), np.array(.25)])
    np.testing.assert_allclose(weights.detach().numpy(), reference, atol=1e-6)
    args[2] = torch.zeros(2, dtype=torch.float64)
    with pytest.raises(AllocationError, match="cash"):
        model(*args, receivable_weight=torch.tensor(.95, dtype=torch.float64))
