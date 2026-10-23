"""Versioned outer diagnostics; recorded failures are facts, not model forecasts."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import tempfile
import time

from .. import experiments
from ..bench import targets
from .contracts import number, timestamp
from .optimization import AllocationRejection
from .recipes import HistoryUnavailable

SCHEMA = "utility_outer_evaluation_v1"
KINDS = {"observation_history": ("observations", "ValueError"),
         "nonfinite_forecast": ("forecast", "ValueError"),
         "recipe_history": ("risk_cost_recipe", "HistoryUnavailable"),
         "allocation_rejected": ("allocation", "AllocationRejection"),
         "production_rejected": ("production", "AllocationRejection")}


def configuration(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "failure_policy", "max_seconds"}
            or value["schema_version"] != SCHEMA or value["failure_policy"] != "explicit_failure_hold_v1"):
        raise ValueError("outer_evaluation requires an explicit supported version, failure policy and time budget")
    number(value["max_seconds"], "outer evaluation time budget", lower=.001)
    return copy.deepcopy(value)


def _detail(value):
    kind = value.get("failure_kind") if isinstance(value, dict) else None
    if (kind not in KINDS or value.get("schema_version") != "decision_policy_error_v1"
            or (value.get("stage"), value.get("exception_type")) != KINDS[kind]
            or not isinstance(value.get("reason"), str) or not value["reason"].strip()):
        raise ValueError("unsupported or unclassified outer decision failure")
    return copy.deepcopy(value)


def _recoverable(exc):
    if not isinstance(exc, (ValueError, AllocationRejection, HistoryUnavailable)):
        return False
    try:
        _detail(getattr(exc, "qwb_decision_failure", None))
    except ValueError:
        return False
    return True


def _binding(context):
    from .decision_evaluation import serialize

    return experiments.object_digest(serialize(context))


def _model_identity(model):
    tensors = {name: {"shape": list(value.shape), "dtype": str(value.dtype),
                      "sha256": hashlib.sha256(value.detach().cpu().contiguous().numpy().tobytes()).hexdigest()}
               for name, value in model.net.state_dict().items()}
    return experiments.object_digest({"tensors": tensors, "label_mean": model.label_mean,
                                      "label_scale": model.label_scale})


def _failure(context, policy, detail):
    if (detail["decision_id"] != context.decision_id
            or detail["decision_time"] != str(context.state.observed_at)
            or detail.get("assets") != list(context.assets)
            or detail.get("data_snapshot_id") != context.state.data_snapshot_id):
        raise ValueError("outer failure context identity differs")
    return {"schema_version": "decision_failure_v1", "decision_id": context.decision_id,
            "decision_time": str(context.state.observed_at), "task": "cn_alloc_daily_v1",
            "model_version": policy.model_version, "data_snapshot_id": context.state.data_snapshot_id,
            "state_id": context.state.state_id, "universe_id": context.universe_id,
            "protocol_id": context.protocol_id, "reason": f"{detail['exception_type']}: {detail['reason']}"}


def _incomplete(protocol, exact, completed, failed):
    completed = {row["decision_id"] for row in completed}
    rows = [{"decision_id": session["decision_id"], "exact_status": audit["decision_status"],
             "continuous_status": "completed" if session["decision_id"] in completed else
                                  "failed" if session["decision_id"] == failed else "not_run",
             "equity_gap_cny": None, "cash_gap_cny": None, "action_weight_gap": None}
            for session, audit in zip(protocol["sessions"], exact["audits"], strict=True)]
    return {"status": "INCOMPLETE", "daily": rows, "comparison_eligible": False,
            "threshold_breaches": ["continuous_evaluation_incomplete"],
            "scope": "no full continuous account; missing differences are null, not zero"}


def evaluate(model, bundle, protocol, output, *, policy_config, continuous, acceptance, evaluation,
             feedback_cutoff, log=lambda *_: None, recorded=None):
    import torch

    from ..training.decision import DecisionPolicy
    from .decision_evaluation import compare, serialize
    from .episode import run_episode
    from .contracts import TargetWeights
    from .lifecycle import LifecycleAccountState, LifecycleTargetWeights
    from ..training.utility_fit import validate_feedback

    evaluation = configuration(evaluation)
    started = time.monotonic()

    def guard(message):
        log(message)
        if time.monotonic() - started > evaluation["max_seconds"]:
            raise TimeoutError("predeclared outer evaluation time budget exceeded")

    bundle.assert_unchanged()
    validate_feedback(bundle, protocol, feedback_cutoff)
    for session in protocol["sessions"]:
        if bundle.universe_at(session["decision_time"])[1] != set(session["eligible_assets"]):
            raise ValueError("outer evaluation causal universe differs")
    plan = {"schema_version": SCHEMA, "evaluation": evaluation, "implementation_hash": experiments.code_identity(),
            "model_content_hash": _model_identity(model),
            "data_content_hash": bundle.identity["content_hash"], "protocol_id": targets.validate_protocol(protocol),
            "feedback_cutoff": str(timestamp(feedback_cutoff)),
            "policy": serialize(policy_config), "continuous": serialize(continuous), "acceptance": acceptance}
    if recorded is not None and experiments.object_digest(plan) != experiments.object_digest(
            experiments.read(Path(recorded) / "evaluation-plan.json")):
        raise ValueError("outer evaluation plan or model identity differs")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    experiments.write(root / "evaluation-plan.json", plan)
    experiments.write(root / "protocol.json", protocol)
    declared = [session["decision_id"] for session in protocol["sessions"]]
    prior = None
    if recorded is not None:
        prior = experiments.read(Path(recorded) / "decision-attempts.json")["decisions"]
        if [row.get("decision_id") for row in prior] != declared:
            raise ValueError("outer recorded decision coverage differs")
    attempts, progress = [], []
    with torch.no_grad():
        exact_policy = DecisionPolicy(model, bundle, **policy_config)

        def exact_action(context):
            guard(f"utility exact decision {context.decision_id}")
            row = {"decision_id": context.decision_id, "context_hash": _binding(context)}
            previous = prior[len(attempts)] if prior is not None else None
            if previous is not None and previous.get("context_hash") != row["context_hash"]:
                raise ValueError("outer recorded decision context differs")
            try:
                if previous is not None and previous.get("outcome") == "explicit_failure":
                    detail = _detail(previous.get("failure"))
                    action = _failure(context, exact_policy, detail)
                    row.update(outcome="explicit_failure", failure=detail)
                else:
                    if previous is not None and previous.get("outcome") != "target":
                        raise ValueError("outer recorded decision outcome differs")
                    action = exact_policy(context)
                    row["outcome"] = "target"
            except ValueError as exc:
                if previous is not None or not _recoverable(exc):
                    raise
                detail = _detail(exc.qwb_decision_failure)
                action = _failure(context, exact_policy, detail)
                row.update(outcome="explicit_failure", failure=detail)
            attempts.append(row)
            experiments.write(root / "decision-attempts.json", {"decisions": attempts})
            return action

        exact = targets.replay(protocol, exact_action)
        experiments.write(root / "policy.json", {"decisions": exact_policy.audit})
        experiments.write(root / "submission.json", {"schema_version": "target_submission_v2"
            if protocol["schema_version"] == "target_protocol_v2" else "target_submission_v1",
            "protocol_id": exact["protocol_id"], "decisions": exact["decisions"]})
        guard("utility independent exact replay")
        score = targets.evaluate(root / "protocol.json", root / "submission.json", root / "exact")
        if score["status"] != "COMPLETED":
            raise ValueError(str(score))
        targets.verify(root / "exact", inputs=True)
        if experiments.object_digest(exact) != experiments.object_digest(experiments.read(root / "exact/replay.json")):
            raise ValueError("outer exact policy and independent replay differ")
        approximate = DecisionPolicy(model, bundle, **policy_config)
        continuous_failure = None
        previous_failure = None
        if recorded is not None and (Path(recorded) / "continuous-failure.json").exists():
            previous_failure = experiments.read(Path(recorded) / "continuous-failure.json")
        last_context = None

        def approximate_action(context):
            nonlocal last_context
            guard(f"utility continuous decision {context.decision_id}")
            last_context = context
            if previous_failure is not None and previous_failure["decision_id"] == context.decision_id:
                if previous_failure["context_hash"] != _binding(context):
                    raise ValueError("recorded continuous failure context differs")
                detail = _detail(previous_failure["failure"])
                exc = ValueError(detail["reason"])
                exc.qwb_decision_failure = detail
                raise exc
            return approximate(context)

        def on_step(context, intent, state, audit):
            progress.append(serialize({"decision_id": context.decision_id, "before": context.state,
                                       "intent": intent, "after": state, "audit": audit}))
            experiments.write(root / "continuous-progress.json", {"steps": progress})

        experiments.write(root / "continuous-progress.json", {"steps": progress})
        try:
            episode = run_episode(protocol, approximate_action, config=continuous, on_step=on_step)
        except ValueError as exc:
            if not _recoverable(exc):
                raise
            detail = _detail(exc.qwb_decision_failure)
            if (last_context is None or detail["decision_id"] != last_context.decision_id
                    or detail["decision_time"] != str(last_context.state.observed_at)
                    or detail.get("assets") != list(last_context.state.assets)
                    or detail.get("data_snapshot_id") != bundle.manifest["data_snapshot_id"]):
                raise ValueError("continuous failure decision differs") from exc
            continuous_failure = {"decision_id": last_context.decision_id,
                                  "context_hash": _binding(last_context), "failure": detail}
            experiments.write(root / "continuous-failure.json", continuous_failure)
        experiments.write(root / "continuous-policy.json", {"decisions": approximate.audit})
        guard("utility outer comparison")
        if continuous_failure is None:
            experiments.write(root / "continuous.json", serialize(episode))
            frozen = {a["decision_id"]: dict(zip(a["assets"], intent.weights.tolist(), strict=True))
                      for a, intent in zip(episode["audits"], episode["intents"], strict=True)}

            def fixed(context):
                guard(f"utility fixed-action decision {context.decision_id}")
                values = frozen[context.decision_id]
                if any(weight != 0 for code, weight in values.items() if code not in context.assets):
                    raise ValueError("continuous action has an asset absent from exact context")
                weights = tuple((code, values.get(code, 0)) for code in context.assets)
                reserve = context.state.receivable_equity / context.state.equity
                cls = LifecycleTargetWeights if isinstance(context.state, LifecycleAccountState) else TargetWeights
                extra = {"receivable_weight": reserve} if cls is LifecycleTargetWeights else {}
                return cls(context.decision_id, context.state.observed_at, context.execution_not_before,
                    "cn_alloc_daily_v1", "frozen-continuous-action-diagnostic", context.state.data_snapshot_id,
                    context.state.state_id, context.universe_id, context.protocol_id, weights,
                    1 - reserve - sum(weight for _, weight in weights), **extra)

            fixed_result = targets.replay(protocol, fixed)
            experiments.write(root / "fixed-continuous-action-exact.json", fixed_result)
            comparisons = {"closed_loop": compare(episode, exact, acceptance,
                    continuous_policy=approximate.audit, exact_actions=exact["decisions"]),
                "fixed_actions": compare(episode, fixed_result, acceptance,
                    continuous_policy=approximate.audit, exact_actions=fixed_result["decisions"])}
            continuous_return = float(episode["equity"][-1] / episode["equity"][0] - 1)
        else:
            comparisons = {key: _incomplete(protocol, exact, progress, continuous_failure["decision_id"])
                           for key in ("closed_loop", "fixed_actions")}
            continuous_return = None
        guard("utility outer finalization")
        bundle.assert_unchanged()
        if (experiments.code_identity() != plan["implementation_hash"]
                or targets.validate_protocol(protocol) != plan["protocol_id"]
                or _model_identity(model) != plan["model_content_hash"]):
            raise ValueError("outer evaluation implementation, protocol or model changed")
        experiments.write(root / "differences.json", comparisons)
        coverage = {"declared": len(declared), "exact_targets": len(declared) - exact["explicit_failures"],
                    "exact_failures": exact["explicit_failures"], "continuous_completed": len(progress),
                    "continuous_failed": int(continuous_failure is not None),
                    "continuous_not_run": len(declared) - len(progress) - int(continuous_failure is not None)}
        result = {"exact_net_return": exact["net_return"], "exact_total_cost": exact["total_cost"],
            "continuous_net_return": continuous_return,
            "closed_loop_comparison_eligible": comparisons["closed_loop"]["comparison_eligible"],
            "threshold_breaches": comparisons["closed_loop"]["threshold_breaches"],
            "evaluation_status": "DIAGNOSTIC_WITH_FAILURES" if exact["explicit_failures"] or continuous_failure else "COMPLETED",
            "coverage": coverage, "evaluation_version": SCHEMA}
        experiments.write(root / "result.json", result)
        return result, exact_policy.audit


def verify(model, bundle, protocol, directory, **kwargs):
    """Replay recorded failure facts; successful actions still require the model."""
    root = Path(directory)
    with tempfile.TemporaryDirectory(prefix="qwb-outer-verify-") as temporary:
        regenerated = Path(temporary) / "accounts"
        result, audit = evaluate(model, bundle, protocol, regenerated, recorded=root, **kwargs)
        expected = {path.name for path in regenerated.glob("*.json")}
        if expected != {path.name for path in root.glob("*.json")}:
            raise ValueError("outer evaluation artifact coverage differs")
        for name in expected:
            if experiments.read(regenerated / name) != experiments.read(root / name):
                raise ValueError(f"outer evaluation arithmetic mismatch: {name}")
        targets.verify(root / "exact", inputs=False)
        return result, audit
