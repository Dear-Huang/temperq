"""Executable M0 checks with measured errors and explicit incomplete gates."""

from __future__ import annotations

from dataclasses import replace
import importlib.metadata
from pathlib import Path
import platform
import tempfile

import numpy as np
import pandas as pd

from .. import experiments
from .contracts import AccountState, TargetWeights, TASK, validate_target
from .reference import ExecutionBar, execute_session


def contract_check():
    state = AccountState("2024-01-02T15:30:00+08:00", "original_synthetic_fixture", 10000)
    target = TargetWeights("fixture-decision", state.observed_at, "2024-01-03T09:30:00+08:00",
                           TASK, "fixture-policy", state.data_snapshot_id, state.state_id,
                           "fixture-universe", "fixture-protocol", (("A", .5), ("B", .4)), .1)
    context = dict(assets=("A", "B"), universe_id="fixture-universe", protocol_id="fixture-protocol",
                   execution_at=target.execution_not_before)
    validate_target(target, state, **context)
    try:
        validate_target(target, replace(state, cash=10001), **context)
    except ValueError:
        pass
    else:
        raise AssertionError("stale state accepted")
    if TargetWeights.from_dict(target.to_dict()) != target or AccountState.from_dict(state.to_dict()) != state:
        raise AssertionError("contract roundtrip changed data")
    return state, target


def account_check():
    state, target = contract_check()
    bars = (ExecutionBar("A", 10, 11, 20, 5), ExecutionBar("B", 20, 19, 40, 10))
    result, audit = execute_session(state, target, bars,
        open_at=target.execution_not_before, close_at="2024-01-03T15:30:00+08:00",
        next_open_at="2024-01-04T09:30:00+08:00", universe_id=target.universe_id,
        protocol_id=target.protocol_id, decision_assets=("A", "B"))
    if result.cash != 990 or result.equity != 10290 or audit["total_cost"] != 10:
        raise AssertionError("hand-computed account mismatch")
    if any(p.sellable_quantity != 0 for p in result.positions):
        raise AssertionError("same-day purchases became sellable")
    return {"scope": "original_synthetic_account_fixture", "expected_equity": 10290,
            "actual_equity": result.equity, "cash": result.cash,
            "self_financing_residual": audit["self_financing_residual"],
            "pnl_residual": audit["pnl_residual"], "cost": audit["total_cost"],
            "state_before": state.to_dict(), "target": target.to_dict(), "state_after": result.to_dict(),
            "economic_evidence": "SYNTHETIC_ONLY"}


def score_isolation_check():
    from ..bench.contracts import align_predictions
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "disguised.parquet"
        pd.DataFrame({"code": ["A"], "decision_time": ["2024-01-02T15:30:00+08:00"],
                      "task": [TASK], "prediction": [.5], "model_version": ["fixture"],
                      "data_snapshot_id": ["fixture"], "target_weight": [.5]}).to_parquet(path)
        try:
            align_predictions(path, pd.DatetimeIndex(["2024-01-02"]), pd.Index(["A"]), {})
        except ValueError as exc:
            if "separate portfolio" not in str(exc):
                raise
        else:
            raise AssertionError("weight table accepted as prediction scores")
    return {"mixed_submission_rejected": True}


def solver_check():
    import torch
    from .optimization import ConvexAllocator

    layer = ConvexAllocator(2)
    args = [torch.tensor(x, dtype=torch.float64) for x in
            ([.03, .02], [[.2, 0], [0, .2]], [.2, .2], [.0002, .0002], [.0004, .0004])]
    args[0].requires_grad_(True)
    probe = torch.tensor([.7, -.3], dtype=torch.float64)
    w, audit = layer(*args)
    (w @ probe).backward()
    grad = args[0].grad.detach().numpy()
    expected = np.array([.338, .238])
    solution_error = float(np.max(np.abs(w.detach().numpy() - expected)))
    ref, certificate = layer.reference_solve([a.detach().numpy() for a in args] + [np.zeros(2), np.ones(2)])
    reference_error = float(np.max(np.abs(w.detach().numpy() - ref)))
    scans = []
    for step in (1e-3, 1e-4, 1e-5):
        finite = []
        for i in range(2):
            hi, lo = args[0].detach().clone(), args[0].detach().clone()
            hi[i] += step
            lo[i] -= step
            plus, _ = layer(hi, *args[1:])
            minus, _ = layer(lo, *args[1:])
            finite.append(float((plus @ probe - minus @ probe) / (2 * step)))
        absolute = np.abs(grad - finite)
        relative = absolute / np.maximum(np.maximum(np.abs(grad), np.abs(finite)), 1e-12)
        ok = np.isfinite(grad).all() and bool(np.all((relative <= 1e-3) | (absolute <= 1e-5)))
        scans.append({"step": step, "autograd": grad.tolist(), "finite_difference": finite,
                      "absolute_error": absolute.tolist(), "relative_error": relative.tolist(), "pass": ok})
    if (solution_error > 1e-6 or reference_error > 1e-6 or not all(x["pass"] for x in scans)
            or max(certificate["primal_residual"], certificate["dual_residual"], abs(certificate["gap"])) > 1e-6):
        raise AssertionError(f"numerical acceptance failed: {scans}, {certificate}")
    return {**audit, "closed_form_solution_error": solution_error,
            "independent_cvxpy_solution_error": reference_error,
            "certificate": certificate, "gradient_scans": scans,
            "gradient_scope": "smooth_interior_only_boundary_tests_are_separate"}


def run(output=None):
    out = Path(output).resolve() if output else None
    if out:
        out.mkdir(parents=True, exist_ok=False)
    result = {"schema_version": 1, "created_at": experiments.now(),
              "platform": platform.platform(), "python": platform.python_version(),
              "code_hash": experiments.code_identity(), "packages": {}, "checks": {},
              "scope": "M0_local_reference_not_full_platform_acceptance",
              "remaining_gates": ["clean_environment_windows_and_linux", "corporate_action_accounting",
                                  "full_target_bench", "sequence_training", "utility_training",
                                  "frozen_experiments", "external_reproduction"],
              "economic_evidence": "SYNTHETIC_ONLY"}
    for package in ("torch", "cvxpy", "cvxpylayers", "scs", "diffcp", "numpy", "pandas"):
        try:
            result["packages"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][package] = None
    for name, check in (("account_and_state", account_check), ("score_isolation", score_isolation_check),
                         ("solver_and_gradients", solver_check)):
        try:
            result["checks"][name] = {"status": "PASS", "evidence": check()}
        except Exception as exc:
            result["checks"][name] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    result["status"] = "PASS" if all(x["status"] == "PASS" for x in result["checks"].values()) else "FAIL"
    if out:
        experiments.write(out / "acceptance.json", result)
        experiments.write(out / "checksums.json", {"acceptance.json": experiments.digest(out / "acceptance.json")})
    return result
