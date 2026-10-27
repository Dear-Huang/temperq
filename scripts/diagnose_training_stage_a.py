"""Bounded synthetic gradient/action diagnostics of historical B2 checkpoints."""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import torch

from qwb import experiments
from qwb.bench import targets
from qwb.observations.bundle import PITBundle
from qwb.portfolio.continuous import ContinuousConfig
from qwb.portfolio.episode import run_episode
from qwb.portfolio.optimization import ConvexAllocator
from qwb.portfolio.reference import ExecutionBar, Fees
from qwb.training.decision import DecisionGRU, DecisionPolicy
from qwb.training.utility import net_utility_returns


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artifacts/utility-resource-audit-20261023-001/cases"


class ForecastProbe:
    def __init__(self, model, direction, shift):
        self.model, self.direction, self.shift = model, direction, shift

    def forecast(self, values, valid):
        predicted = self.model.forecast(values, valid)
        if predicted.shape != self.direction.shape:
            raise ValueError("diagnostic asset coverage changed")
        return predicted + self.shift * self.direction


class DiagnosticPolicy(DecisionPolicy):
    def __init__(self, *args, solver_eps=1e-9, **kwargs):
        super().__init__(*args, **kwargs)
        self.solver_eps = solver_eps

    def action(self, context):
        assets = tuple(context.state.assets if hasattr(context.state, "assets") else context.assets)
        if assets not in self.layers:
            allocator = ConvexAllocator(len(assets), self.optimizer)
            native = allocator.layer
            eps = self.solver_eps
            allocator.layer = lambda *a, **kw: native(*a, solver_args={"eps": eps, "max_iters": 100000})
            self.layers[assets] = allocator
        return super().action(context)


def objective(returns):
    return net_utility_returns(returns, variance_penalty=5)[0]


def run_case(assets, output):
    started_cpu, started_wall = time.process_time(), time.monotonic()
    torch.set_num_threads(1)
    case = SOURCE / f"a{assets:04d}-t0003-kfull-r01"
    task = experiments.read(case / "task.json")
    inputs = experiments.read(case / "fit-inputs.json")
    protocol = experiments.read(case / "train.json")
    if protocol["data_kind"] != "synthetic" or any("2026" in s["decision_time"] for s in protocol["sessions"]):
        raise ValueError("registered synthetic pre-2026 source required")
    source_protocol_hash = experiments.object_digest(protocol)
    # Old artifacts predate explicit minimum-order fields; preserve lot sizes.
    defaults = {name: ExecutionBar.__dataclass_fields__[name].default for name in ("minimum_buy", "minimum_sell")}
    for session in protocol["sessions"]:
        for bar in session["bars"]:
            for name, default in defaults.items():
                bar.setdefault(name, default)
    experiments.write(output / f"a{assets}-derived-protocol.json", protocol)
    model = DecisionGRU.load(case / "checkpoint")
    bundle = PITBundle(task["source"]["bundle"], lookback=60)
    config = copy.deepcopy(inputs["policy"])
    config["fees"] = Fees(**config["fees"])
    source_paths = [case / "train.json", case / "fit-inputs.json", case / "task.json", case / "checkpoint/weights.pt", case / "checkpoint/model.json"]
    identities = {p.relative_to(ROOT).as_posix(): experiments.digest(p) for p in source_paths}
    rng = np.random.default_rng(20261026)
    direction = rng.normal(size=assets)
    direction -= direction.mean()
    direction /= np.linalg.norm(direction)
    direction = torch.tensor(direction, dtype=torch.float64)

    def calculate(probe_direction, shift, eps, *, gradients=False):
        wrapped = ForecastProbe(model, probe_direction, shift)
        policy = DiagnosticPolicy(wrapped, bundle, **config, solver_eps=eps)
        episode = run_episode(protocol, policy, config=ContinuousConfig())
        loss = objective(episode["net_returns"])
        first = policy.audit[0]
        raw = np.array(first["allocation"]["raw_weights"])
        lower = np.array(first["production_inputs"]["lower"])
        row = {"loss": float(loss.detach()), "net_return": float((episode["equity"][-1] / episode["equity"][0] - 1).detach()),
            "cost": float(sum(float(a["total_cost"]) for a in episode["audits"])),
            "first_raw": raw.tolist(), "first_produced": first["production"]["produced_weights"],
            "first_lower_active": int((raw <= lower + 1e-7).sum()),
            "first_budget_used": float(raw.sum() / first["production_inputs"]["budget"]),
            "max_constraint_residual": max(p["allocation"]["constraint_residual"] for p in policy.audit)}
        if gradients:
            params = list(model.net.parameters())
            grads = torch.autograd.grad(loss, [shift, *params], allow_unused=True)
            row.update(directional_autograd=float(grads[0]), parameter_gradient_l2=float(sum(g.square().sum() for g in grads[1:] if g is not None).sqrt()))
        with torch.no_grad():
            actual_policy = DiagnosticPolicy(wrapped, bundle, **config, solver_eps=eps)
            exact = targets.replay(protocol, actual_policy)
            equities = np.array([exact["initial_equity"], *[a["equity_close"] for a in exact["audits"]]])
            returns = torch.tensor(equities[1:] / equities[:-1] - 1, dtype=torch.float64)
            row.update(exact_loss=float(objective(returns)), exact_cost=exact["total_cost"], exact_net_return=exact["net_return"],
                first_exact_produced=actual_policy.audit[0]["production"]["produced_weights"])
        return row

    results = []
    for eps in (1e-9, 1e-10):
        zero = torch.tensor(0., dtype=torch.float64, requires_grad=True)
        base = calculate(direction, zero, eps, gradients=True)
        rows, differences = [], []
        with torch.no_grad():
            for delta in (1e-5, 1e-4, 1e-3):
                pair = []
                for sign in (-1, 1):
                    row = calculate(direction, sign * delta, eps)
                    row.update(kind="zero_mean_l2_direction", shift=sign * delta,
                        first_raw_l1_delta=float(np.abs(np.array(row["first_raw"]) - base["first_raw"]).sum()),
                        first_produced_l1_delta=float(np.abs(np.array(row["first_produced"]) - base["first_produced"]).sum()))
                    pair.append(row)
                    rows.append(row)
                fd = (pair[1]["loss"] - pair[0]["loss"]) / (2 * delta)
                ad = base["directional_autograd"]
                differences.append({"epsilon": delta, "autograd": ad, "finite_difference": fd,
                    "absolute_error": abs(fd - ad), "relative_error_with_1e_minus_10_floor": abs(fd - ad) / max(abs(fd), abs(ad), 1e-10),
                    "interpretation": "numerically_uninformative" if max(abs(fd), abs(ad)) < 1e-8 else "inspect_active_set_and_step_dependence"})
            for delta in (-1e-3, 1e-3):
                row = calculate(torch.ones(assets, dtype=torch.float64), delta, eps)
                row.update(kind="common_shift", shift=delta,
                    first_raw_l1_delta=float(np.abs(np.array(row["first_raw"]) - base["first_raw"]).sum()),
                    first_produced_l1_delta=float(np.abs(np.array(row["first_produced"]) - base["first_produced"]).sum()))
                rows.append(row)
        results.append({"solver_eps": eps, "baseline": base, "directional_derivatives": differences, "perturbations": rows})
        experiments.write(output / f"a{assets}-partial.json", {"results": results})
    if identities != {p.relative_to(ROOT).as_posix(): experiments.digest(p) for p in source_paths}:
        raise ValueError("source artifacts changed during diagnosis")
    return {"assets": assets, "scope": "SYNTHETIC_ONLY_FIXED_CHECKPOINT_NO_RETRAINING", "source_hashes": identities,
        "source_protocol_hash": source_protocol_hash, "derived_protocol_hash": experiments.object_digest(protocol), "explicit_minimum_field_defaults": defaults,
        "current_core": experiments.code_identity(), "historical_checkpoint_core": model.fit_record["implementation_hash"],
        "source_data_hash": task["source"]["data_content_hash"], "results": results,
        "cpu_seconds": time.process_time() - started_cpu, "wall_seconds": time.monotonic() - started_wall,
        "limitations": ["Does not qualify a real-market encoder, a trained factor-context model or STRUCT costs.", "Tolerance changes apply only to local diagnostic allocator instances; fees and constraints unchanged.", "Forecast probes are sensitivity tests, not learned improvements.", "Original B2 qualification remains false; no stage B or promotion."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child-assets", type=int, choices=[64, 128])
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    out = args.output.resolve()
    if args.child_assets:
        try:
            result = run_case(args.child_assets, out)
            experiments.write(out / f"a{args.child_assets}-result.json", result)
            print(json.dumps({"assets": args.child_assets, "cpu_seconds": result["cpu_seconds"], "wall_seconds": result["wall_seconds"], "gradient_norms": [r["baseline"]["parameter_gradient_l2"] for r in result["results"]]}))
            return 0
        except Exception as e:
            experiments.write(out / f"a{args.child_assets}-failure.json", {"status": "FAILED", "type": type(e).__name__, "message": str(e)})
            raise
    for required in ("registration.json", "power.json", "holdout-audit.json", "literature.json"):
        if not (out / required).is_file():
            raise ValueError("missing prerequisite " + required)
    plan = {"script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "prerequisite_hashes": {p: experiments.digest(out / p) for p in ("registration.json", "power.json", "holdout-audit.json", "literature.json")}, "assets": [64, 128], "per_child_wall_seconds": 900, "threads": 1}
    out = out / f"diagnostic-attempt-{args.attempt:03d}"
    out.mkdir(exist_ok=False)
    experiments.write(out / "diagnostic-plan.json", plan)
    from run_utility_resource_audit import execute_process
    attempts = []
    for assets in plan["assets"]:
        with (out / f"a{assets}.log").open("w", encoding="utf-8") as log:
            attempt = execute_process([sys.executable, str(Path(__file__).resolve()), "--output", str(out), "--child-assets", str(assets)], log, wall_seconds=900)
        attempts.append({"assets": assets, **attempt})
        experiments.write(out / "diagnostic-attempts.json", {"attempts": attempts})
        print(json.dumps(attempts[-1]), flush=True)
    return 0 if all(a["exit_code"] == 0 for a in attempts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
