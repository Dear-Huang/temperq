"""Prediction-to-target allocation with frozen recipes and an independent account."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import importlib.metadata
import platform
import shutil

import numpy as np
import pandas as pd
import yaml

from .. import experiments
from ..bench import targets
from ..observations.bundle import FILES, PITBundle, session_time
from .contracts import TASK, TargetWeights, timestamp, validate_target
from .lifecycle import LifecycleAccountState, LifecycleTargetWeights, Terminal
from .optimization import AllocationConfig, AllocationError, ConvexAllocator
from .recipes import CostRecipe, RecipeError, RiskRecipe, estimate
from .reference import Fees
from .production import project

SOURCE_FILES = ("run.json", "metrics.json", "predictions.parquet", "task.yaml", "config.resolved.yaml", "folds.json")


def configuration(raw):
    targets.fields(raw, {"schema_version", "risk", "cost", "optimizer", "output_policy", "diagnostics"}, "allocation config")
    if (raw["schema_version"] != "prediction_allocation_v1"
            or raw["output_policy"] not in {"strict_raw", "highs_l1_projection_v1"}):
        raise ValueError("unsupported allocation config or output policy")
    try:
        risk, cost, optimizer = RiskRecipe(**raw["risk"]), CostRecipe(**raw["cost"]), AllocationConfig(**raw["optimizer"])
    except TypeError as exc:
        raise ValueError(f"invalid recipe fields: {exc}") from exc
    limits = targets.fields(raw["diagnostics"], {"max_cost_gap_equity", "max_turnover_gap"}, "proxy thresholds")
    from .contracts import number

    for name, value in limits.items():
        number(value, name, upper=2)
    return {"schema_version": raw["schema_version"], "risk": asdict(risk), "cost": asdict(cost),
            "optimizer": asdict(optimizer), "output_policy": raw["output_policy"], "diagnostics": dict(limits)}


def environment():
    return {"python": platform.python_version(), "platform": platform.platform(),
            "packages": {name: importlib.metadata.version(name) for name in
                ("numpy", "pandas", "scipy", "torch", "cvxpy", "cvxpylayers", "diffcp", "scs")}}


class PredictionAllocator:
    """Trusted local policy: no execution rows enter risk, costs or known bounds."""

    def __init__(self, bundle, predictions, config, *, model_version, fees, on_attempt=lambda row: None):
        self.config = configuration(config)
        self.risk, self.cost = RiskRecipe(**self.config["risk"]), CostRecipe(**self.config["cost"])
        self.optimizer = AllocationConfig(**self.config["optimizer"])
        self.bundle, self.model_version, self.fees = bundle, model_version, fees
        self.on_attempt = on_attempt
        required = {"code", "decision_time", "prediction", "model_version", "task", "data_snapshot_id"}
        if not required <= set(predictions):
            raise ValueError("allocation requires versioned prediction columns")
        frame = predictions[list(sorted(required))].copy()
        if (not frame["code"].map(lambda code: isinstance(code, str) and bool(code.strip())).all()
                or frame.empty or frame["model_version"].nunique(dropna=False) != 1
                or frame["model_version"].isna().any()
                or set(frame["task"]) != {"cn_predict_intraday_v1"}
                or set(frame["data_snapshot_id"]) != {bundle.manifest["data_snapshot_id"]}):
            raise ValueError("prediction task/version/snapshot identity mismatch")
        frame["decision_time"] = pd.to_datetime([timestamp(t) for t in frame["decision_time"]], utc=True)
        frame["prediction"] = pd.to_numeric(frame["prediction"], errors="raise")
        if frame.duplicated(["code", "decision_time"]).any() or not np.isfinite(frame["prediction"].to_numpy(float)).all():
            raise ValueError("predictions must be finite and unique by asset and decision")
        self.predictions = frame.set_index(["decision_time", "code"])["prediction"]
        self.layers, self.attempts = {}, []

    def __call__(self, context):
        import torch

        state, assets = context.state, context.assets
        row = {"decision_id": context.decision_id, "decision_time": str(state.observed_at),
               "state_id": state.state_id, "assets": assets, "status": "started"}
        self.attempts.append(row)
        self.on_attempt(row)
        try:
            if (context.max_weight != self.optimizer.max_weight or context.min_cash != self.optimizer.min_cash):
                raise ValueError("optimizer cash/position constraints must equal the frozen account protocol")
            stamp = timestamp(state.observed_at)
            available = self.predictions.xs(stamp, level="decision_time") if stamp in self.predictions.index.get_level_values(0) else pd.Series(dtype=float)
            missing = set(context.eligible_assets) - set(available.index)
            if missing:
                raise RecipeError(f"missing eligible predictions: {sorted(missing)}")
            previous = state.weights(assets)
            lower, upper = np.zeros(len(assets)), np.full(len(assets), context.max_weight)
            positions = {p.asset_id: p for p in state.positions}
            known_terminal = {a.asset_id for a in context.known_actions if isinstance(a, Terminal)
                              and timestamp(a.effective_at) < timestamp(context.execution_not_before)}
            mu = []
            for i, code in enumerate(assets):
                p = positions.get(code)
                if p:
                    locked = sum(lot.quantity for lot in p.unsettled if timestamp(lot.sellable_after) > timestamp(context.execution_not_before))
                    lower[i] = locked * p.mark / state.equity
                if code not in context.eligible_assets:
                    upper[i] = min(upper[i], previous[i])
                if code in known_terminal:
                    lower[i], upper[i] = 0, 0
                mu.append(float(available[code]) if code in context.eligible_assets else 0.0)
            root, buy, sell, recipe = estimate(self.bundle, stamp, assets, equity=state.equity,
                fees=self.fees, risk=self.risk, cost=self.cost)
            reserve = state.receivable_equity / state.equity
            row.update(previous_weights=previous.tolist(), lower=lower.tolist(), upper=upper.tolist(),
                       prediction=mu, receivable_weight=reserve, recipe=recipe,
                       held_outside_prediction_pool=sorted(set(positions) - set(context.eligible_assets)))
            if assets not in self.layers:
                self.layers[assets] = ConvexAllocator(len(assets), self.optimizer)
            tensor = lambda value: torch.as_tensor(value, dtype=torch.float64)
            weights, audit = self.layers[assets](tensor(mu), tensor(root), tensor(previous), tensor(buy), tensor(sell),
                lower=tensor(lower), upper=tensor(upper), receivable_weight=tensor(reserve))
            raw = weights.detach().numpy()
            row["allocation"] = audit
            produced = raw
            if self.config["output_policy"] == "highs_l1_projection_v1":
                produced, row["production"] = project(raw, previous, lower, upper,
                    budget=1 - reserve - context.min_cash, max_turnover=self.optimizer.max_turnover,
                    tolerance=self.optimizer.tolerance)
            else:
                row["production"] = {"policy": "strict_raw", "postprocessed": False}
            cash_weight = float(1 - reserve - produced.sum())
            cls = LifecycleTargetWeights if isinstance(state, LifecycleAccountState) else TargetWeights
            extra = {"receivable_weight": reserve} if isinstance(state, LifecycleAccountState) else {}
            target = cls(context.decision_id, state.observed_at, context.execution_not_before, TASK,
                         self.model_version, state.data_snapshot_id, state.state_id, context.universe_id,
                         context.protocol_id, tuple(zip(assets, produced.tolist())), cash_weight, **extra)
            validate_target(target, state, assets=assets, universe_id=context.universe_id,
                            protocol_id=context.protocol_id, execution_at=context.execution_not_before,
                            max_weight=context.max_weight, min_cash=context.min_cash)
            row.update(status="target", intended_proxy_cost=float(state.equity * (
                buy @ np.maximum(produced - previous, 0) + sell @ np.maximum(previous - produced, 0))),
                produced_turnover=float(np.abs(produced - previous).sum()))
            self.on_attempt(row)
            return target
        except (RecipeError, AllocationError, ValueError) as exc:
            row.update(status="explicit_failure", error=f"{type(exc).__name__}: {exc}")
            self.on_attempt(row)
            return {"schema_version": "decision_failure_v1", "decision_id": context.decision_id,
                    "decision_time": state.observed_at, "task": TASK, "model_version": self.model_version,
                    "data_snapshot_id": state.data_snapshot_id, "state_id": state.state_id,
                    "universe_id": context.universe_id, "protocol_id": context.protocol_id, "reason": row["error"]}


def replay(protocol, bundle, predictions, config, *, on_attempt=lambda row: None):
    implementation = experiments.code_identity()
    config = configuration(config)
    protocol_id = targets.validate_protocol(protocol)
    if (protocol["data_kind"] != bundle.manifest["data_kind"]
            or protocol["initial_state"]["data_snapshot_id"] != bundle.manifest["data_snapshot_id"]):
        raise ValueError("account protocol and observation bundle identity mismatch")
    for s in protocol["sessions"]:
        day = timestamp(s["decision_time"]).tz_convert("Asia/Shanghai").tz_localize(None).normalize()
        if day not in bundle.calendar:
            raise ValueError("allocation decision missing from observation calendar")
        index = bundle.calendar.get_loc(day)
        if (index + 1 >= len(bundle.calendar)
                or timestamp(s["open_at"]) != timestamp(session_time(bundle.calendar[index + 1], "09:30"))
                or (index + 2 < len(bundle.calendar)
                    and timestamp(s["next_open_at"]) != timestamp(session_time(bundle.calendar[index + 2], "09:30")))):
            raise ValueError("allocation execution path must match the observation trading calendar")
        _, eligible = bundle.universe_at(s["decision_time"])
        if eligible != set(s["eligible_assets"]):
            raise ValueError("protocol and causal observation universe differ")
    if config["optimizer"]["max_weight"] != protocol["constraints"]["max_weight"] or config["optimizer"]["min_cash"] != protocol["constraints"]["min_cash"]:
        raise ValueError("allocation constraints differ from account protocol")
    version = experiments.object_digest({"prediction_version": sorted(set(predictions["model_version"])),
        "config": config, "protocol_id": protocol_id, "data": bundle.identity, "code": implementation})
    policy = PredictionAllocator(bundle, predictions, config, model_version=version, fees=Fees(**protocol["fees"]), on_attempt=on_attempt)
    account = targets.replay(protocol, policy)
    diagnostics = []
    for attempt, audit in zip(policy.attempts, account["audits"], strict=True):
        row = {"decision_id": audit["decision_id"], "status": attempt["status"],
               "actual_turnover": sum(f["notional"] for f in audit["fills"]) / audit["equity_open"],
               "actual_cost": audit["total_cost"]}
        if attempt["status"] == "target":
            index = {code: i for i, code in enumerate(attempt["assets"])}
            filled_proxy = sum(f["notional"] * attempt["recipe"]["buy_cost" if f["side"] == "BUY" else "sell_cost"][index[f["asset_id"]]] for f in audit["fills"])
            intended = attempt["intended_proxy_cost"]
            cost_gap = (audit["total_cost"] - intended) / audit["equity_before"]
            turnover_gap = row["actual_turnover"] - attempt["produced_turnover"]
            row.update(intended_proxy_cost=intended, filled_proxy_cost=filled_proxy,
                intent_to_fill_cost_gap=filled_proxy - intended,
                cost_model_gap=audit["total_cost"] - filled_proxy,
                cost_gap_equity=cost_gap, intended_turnover=attempt["produced_turnover"], turnover_gap=turnover_gap,
                target_actual_close_l1=(sum(abs(audit["target_weights"].get(code, 0) - audit["actual_weights"].get(code, 0))
                                           for code in attempt["assets"])
                                       + abs(audit["target_cash_weight"] - audit["actual_cash_weight"])
                                       + abs((audit.get("target_receivable_weight") or 0) - audit.get("actual_receivable_weight", 0))),
                threshold_exceeded=(abs(cost_gap) > config["diagnostics"]["max_cost_gap_equity"]
                                    or abs(turnover_gap) > config["diagnostics"]["max_turnover_gap"]))
        diagnostics.append(row)
    bundle.assert_unchanged()
    if experiments.code_identity() != implementation:
        raise ValueError("allocation implementation changed during replay")
    completed = [row for row in diagnostics if row["status"] == "target"]
    actual_cost = sum(row["actual_cost"] for row in diagnostics)
    filled_cost = sum(row["filled_proxy_cost"] for row in completed)
    totals = {"evaluated_targets": len(completed), "total_actual_cost": actual_cost,
              "total_intended_proxy_cost": sum(row["intended_proxy_cost"] for row in completed),
              "total_filled_proxy_cost": filled_cost,
              "total_cost_model_gap": sum(row["cost_model_gap"] for row in completed),
              "cost_model_relative_gap": (actual_cost / filled_cost - 1) if filled_cost > 0 else None,
              "total_actual_turnover": sum(row["actual_turnover"] for row in diagnostics),
              "total_intended_turnover": sum(row["intended_turnover"] for row in completed),
              "mean_target_actual_close_l1": float(np.mean([row["target_actual_close_l1"] for row in completed])) if completed else None,
              "threshold_scope": "daily absolute cost gap / prior equity and turnover gap; not relative cost accuracy"}
    return account, {"schema_version": "prediction_allocation_audit_v1", "model_version": version,
        "config": config, "attempts": policy.attempts, "diagnostics": diagnostics, "totals": totals,
        "failed_decisions": account["explicit_failures"],
        "threshold_exceedances": sum(r.get("threshold_exceeded", False) for r in diagnostics),
        "economic_efficacy": "NOT_ESTABLISHED"}


def source(root, bundle):
    root = Path(root)
    record = experiments.read(root / "run.json")
    if experiments.object_digest(record.get("artifacts")) != record.get("artifact_hash"):
        raise ValueError("prediction archive inventory identity mismatch")
    for name in SOURCE_FILES[1:]:
        if record.get("artifacts", {}).get(name) != experiments.digest(root / name):
            raise ValueError(f"frozen prediction source differs from its archive identity: {name}")
    summary = experiments.read(root / "metrics.json")
    card = yaml.safe_load((root / "task.yaml").read_text(encoding="utf-8"))
    expected_task = {"name": "cn_predict_intraday_v1", "market": "cn_a", "template": "daily", "output": "score"}
    if (not isinstance(card, dict) or any(card.get(k) != v for k, v in expected_task.items())
            or card.get("target") != {"type": "return", "horizon_days": 0}
            or card.get("timing") != {"signal_available": {"day": 0, "time": "15:00"},
                                      "decision": {"day": 0, "time": "15:30"},
                                      "execution": {"day": 1, "time": "09:30", "price": "open"}}
            or card.get("label") != {"start": {"day": 1, "time": "09:30", "price": "open"},
                                     "end": {"day": 1, "time": "15:00", "price": "close"}}):
        raise ValueError("prediction archive task does not define next-session intraday decimal returns")
    if any(summary.get(key) != record.get(key) for key in (
            "run_id", "experiment_id", "model_version", "prediction_version", "config_hash", "data_snapshot_id", "data_content_hash")):
        raise ValueError("prediction source summary lineage mismatch")
    if (record.get("state") != "succeeded" or record.get("origin") != "training"
            or summary.get("engine") != "e2e_prediction_v1"
            or summary.get("prediction_version") != record.get("prediction_version")
            or summary.get("data_content_hash") != bundle.identity["content_hash"]
            or summary.get("data_snapshot_id") != bundle.manifest["data_snapshot_id"]
            or summary.get("data_kind") != bundle.manifest["data_kind"]
            or card.get("name") != "cn_predict_intraday_v1"):
        raise ValueError("allocation needs a matching verified intraday prediction archive")
    frame = pd.read_parquet(root / "predictions.parquet")
    if set(frame["model_version"]) != {summary["prediction_version"]}:
        raise ValueError("prediction version differs from frozen training summary")
    return frame, record


def _plan(protocol, bundle, config, record):
    return {"schema_version": "prediction_allocation_plan_v1", "config": config,
            "implementation_hash": experiments.code_identity(), "environment": environment(),
            "data_identity": bundle.identity, "source_run_id": record["run_id"],
            "decisions": [{"decision_id": s["decision_id"], "status": "queued"} for s in protocol["sessions"]]}


def _result(record, account, audit, score):
    return {"schema_version": "prediction_allocation_result_v1", "status": "COMPLETED",
            "implementation_hash": experiments.code_identity(), "source_run_id": record["run_id"],
            "prediction_version": record["prediction_version"], "model_version": account["model_version"],
            "evidence_status": account["evidence_status"], "economic_efficacy": "NOT_ESTABLISHED",
            "failed_decisions": audit["failed_decisions"], "threshold_exceedances": audit["threshold_exceedances"],
            "proxy_diagnostics": audit["totals"], "account_summary": score["summary"]}


def evaluate(run, protocol_path, data_dir, config_path, output):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    implementation = experiments.code_identity()
    try:
        original = experiments.frozen_source(Path(run))
        experiments.verify(original)
        sources = {"protocol.json": Path(protocol_path).resolve(), "allocation.yaml": Path(config_path).resolve()}
        sources.update({"prediction_source/" + name: original / name for name in SOURCE_FILES})
        sources.update({"observation/" + name: Path(data_dir).resolve() / name for name in FILES})
        inputs = {name: {"path": str(path), "hash": experiments.digest(path)} for name, path in sources.items()}
        experiments.write(root / "inputs.json", inputs)
        for name, path in sources.items():
            if name.startswith("observation/"):
                continue
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            if experiments.digest(target) != inputs[name]["hash"]:
                raise ValueError("allocation input changed while freezing")
        bundle = PITBundle(data_dir)
        predictions, record = source(root / "prediction_source", bundle)
        protocol = targets.read(root / "protocol.json")
        config = configuration(yaml.safe_load((root / "allocation.yaml").read_text(encoding="utf-8")))
        experiments.write(root / "plan.json", _plan(protocol, bundle, config, record))
        attempts = {s["decision_id"]: {"decision_id": s["decision_id"], "status": "queued"} for s in protocol["sessions"]}
        experiments.write(root / "attempts.json", attempts)
        def save_attempt(row):
            attempts[row["decision_id"]] = row
            experiments.write(root / "attempts.json", attempts)
        account, audit = replay(protocol, bundle, predictions, config, on_attempt=save_attempt)
        experiments.write(root / "allocation_audit.json", audit)
        schema = "target_submission_v2" if protocol["schema_version"] == "target_protocol_v2" else "target_submission_v1"
        experiments.write(root / "submission.json", {"schema_version": schema,
                          "protocol_id": account["protocol_id"], "decisions": account["decisions"]})
        score = targets.evaluate(root / "protocol.json", root / "submission.json", root / "account")
        if score["status"] != "COMPLETED":
            raise ValueError(f"independent target account failed: {score.get('error')}")
        if experiments.object_digest(account) != experiments.object_digest(targets.read(root / "account/replay.json")):
            raise ValueError("policy path differs from independently submitted account")
        if any(experiments.digest(path) != inputs[name]["hash"] for name, path in sources.items()):
            raise ValueError("allocation source changed during execution")
        experiments.verify(original)
        if experiments.code_identity() != implementation:
            raise ValueError("allocation implementation changed during evaluation")
        result = _result(record, account, audit, score)
        (root / "report.md").write_text(
            "# Prediction allocation replay\n\n" + f"Evidence: **{account['evidence_status']}**. Economic efficacy: **NOT_ESTABLISHED**.\n\n"
            + f"Decisions: {len(audit['attempts'])}; explicit failures: {audit['failed_decisions']}; "
            + f"proxy threshold exceedances: {audit['threshold_exceedances']}.\n\n"
            + "| Cost Measure | Total CNY |\n| --- | ---: |\n"
            + f"| Intended trade proxy | {audit['totals']['total_intended_proxy_cost']:.6f} |\n"
            + f"| Filled trade proxy | {audit['totals']['total_filled_proxy_cost']:.6f} |\n"
            + f"| Actual trade cost | {audit['totals']['total_actual_cost']:.6f} |\n\n"
            + "Thresholds measure daily gaps relative to account equity, not relative accuracy of estimated costs. "
            + "Target-to-close weight differences include market drift.\n\n"
            + "Risk uses decision-visible intraday return revisions; costs use historical liquidity and declared rates. "
            + "Actual holdings determine turnover; the independent account applies integer lots, fees, settlement and unfilled orders. "
            + "See allocation_audit.json for every raw solver output and failure; account/report.md for actual equity. "
            + "Failure days keep holdings. No model superiority or market efficacy is established.\n", encoding="utf-8")
    except BaseException as exc:
        expected = isinstance(exc, (ValueError, TypeError, KeyError, OSError, ImportError))
        result = {"schema_version": "prediction_allocation_result_v1", "status": "INVALID" if expected else "INTERRUPTED_OR_FAILED",
                  "implementation_hash": experiments.code_identity(), "error": f"{type(exc).__name__}: {exc}"}
        if (root / "attempts.json").exists():
            attempts = targets.read(root / "attempts.json")
            for row in attempts.values():
                if row["status"] in {"queued", "started"}:
                    row.update(status="not_run" if row["status"] == "queued" else "failed_run", error=result["error"])
            experiments.write(root / "attempts.json", attempts)
        if not expected:
            experiments.write(root / "result.json", result)
            experiments.write(root / "checksums.json", experiments.inventory(root))
            raise
    experiments.write(root / "result.json", result)
    experiments.write(root / "checksums.json", experiments.inventory(root))
    return result


def verify(output, *, data_dir=None, inputs=False):
    root = Path(output).resolve()
    expected = targets.read(root / "checksums.json")
    actual = experiments.inventory(root)
    actual.pop("checksums.json", None)
    if expected != actual:
        raise ValueError("allocation artifacts changed")
    result, plan = targets.read(root / "result.json"), targets.read(root / "plan.json")
    if (result["status"] != "COMPLETED" or result["implementation_hash"] != experiments.code_identity()
            or plan["environment"] != environment()):
        raise ValueError("allocation verification requires its completed result and recorded implementation/environment")
    paths = targets.read(root / "inputs.json")
    if any(experiments.digest(root / name) != row["hash"] for name, row in paths.items() if not name.startswith("observation/")):
        raise ValueError("frozen allocation input differs from its original hash")
    bundle_root = Path(data_dir) if data_dir is not None else Path(paths["observation/manifest.json"]["path"]).parent
    bundle = PITBundle(bundle_root)
    if bundle.identity != plan["data_identity"]:
        raise ValueError("allocation observation content differs from the frozen plan")
    predictions, record = source(root / "prediction_source", bundle)
    config = configuration(yaml.safe_load((root / "allocation.yaml").read_text(encoding="utf-8")))
    protocol = targets.read(root / "protocol.json")
    if _plan(protocol, bundle, config, record) != plan:
        raise ValueError("allocation plan configuration mismatch")
    account, audit = replay(protocol, bundle, predictions, config)
    if (experiments.object_digest(audit) != experiments.object_digest(targets.read(root / "allocation_audit.json"))
            or experiments.object_digest(account) != experiments.object_digest(targets.read(root / "account/replay.json"))
            or result != _result(record, account, audit, targets.read(root / "account/scorecard.json"))
            or experiments.object_digest(targets.read(root / "attempts.json"))
               != experiments.object_digest({row["decision_id"]: row for row in audit["attempts"]})):
        raise ValueError("allocation policy or account recomputation mismatch")
    targets.verify(root / "account")
    if inputs:
        if any(experiments.digest(Path(row["path"])) != row["hash"] for row in paths.values()):
            raise ValueError("allocation original input changed")
        experiments.verify(Path(paths["prediction_source/run.json"]["path"]).parent)
    return {"status": "PASS", "scope": "causal_risk_cost_allocation_and_independent_account",
            "inputs_checked": inputs, "evidence_status": result["evidence_status"]}
