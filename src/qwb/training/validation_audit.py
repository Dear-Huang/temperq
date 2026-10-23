"""Inner-validation candidate diagnostics; never an alternative selection rule."""

from __future__ import annotations

import copy
import math
from dataclasses import asdict
from pathlib import Path
import tempfile
import time

import torch

from .. import experiments
from ..bench import targets
from ..portfolio.continuous import tensor
from ..portfolio.decision_evaluation import evaluate
from .decision import DecisionGRU
from .utility import net_utility_returns

VERSION = "utility_validation_audit_v1"
ACCOUNT_FILES = ("protocol.json", "policy.json", "submission.json", "continuous.json", "continuous-policy.json",
                 "fixed-continuous-action-exact.json", "differences.json", "result.json")


def rankings(candidates):
    epochs = [row["epoch"] for row in candidates]
    if len(set(epochs)) != len(epochs) or any(type(epoch) is not int or epoch < 1 for epoch in epochs):
        raise ValueError("validation audit requires unique positive epochs")
    success = [row for row in candidates if row["state"] == "succeeded"]
    losses = {row["epoch"]: (row["metrics"]["continuous"]["loss"], row["metrics"]["exact"]["loss"]) for row in success}
    if any(not math.isfinite(value) for pair in losses.values() for value in pair):
        raise ValueError("validation candidate losses must be finite")
    ordered = sorted(losses)
    concordant, discordant, tied_continuous, tied_exact, tied_both = 0, [], 0, 0, 0
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            dc = losses[first][0] - losses[second][0]
            de = losses[first][1] - losses[second][1]
            if dc == 0 and de == 0:
                tied_both += 1
            elif dc == 0:
                tied_continuous += 1
            elif de == 0:
                tied_exact += 1
            elif (dc > 0) == (de > 0):
                concordant += 1
            else:
                discordant.append([first, second])
    denominator = math.sqrt((concordant + len(discordant) + tied_continuous)
                            * (concordant + len(discordant) + tied_exact))
    reason = "fewer_than_two_successful_candidates" if len(success) < 2 else "one_or_both_paths_all_tied" if denominator == 0 else None
    return {"coverage_complete": len(success) == len(candidates), "completed_candidate_count": len(success),
            "failed_epochs": [row["epoch"] for row in candidates if row["state"] != "succeeded"],
            "continuous_order": sorted(ordered, key=lambda epoch: (losses[epoch][0], epoch)),
            "exact_order": sorted(ordered, key=lambda epoch: (losses[epoch][1], epoch)),
            "discordant_pairs": discordant, "concordant_pair_count": concordant,
            "tied_continuous_only": tied_continuous, "tied_exact_only": tied_exact, "tied_both": tied_both,
            "kendall_tau_b": (concordant - len(discordant)) / denominator if denominator else None,
            "undefined_reason": reason, "selection": "diagnostic_only; loss_ascending_epoch_ascending"}


def account_metrics(directory, variance_penalty):
    root = Path(directory)
    continuous = experiments.read(root / "continuous.json")
    exact = experiments.read(root / "exact/replay.json")
    result = {}
    for name, audits, returns, initial in (
            ("continuous", continuous["audits"], continuous["net_returns"], continuous["equity"][0]),
            ("exact", exact["audits"], [row["equity_close"] / row["equity_before"] - 1 for row in exact["audits"]], exact["initial_equity"])):
        loss, utility = net_utility_returns(tensor(returns), variance_penalty=variance_penalty)
        equities = tensor([initial, *[row["equity_close"] for row in audits]])
        result[name] = {"loss": float(loss), "mean_log_growth": float(utility["mean_log_growth"]),
            "net_return_variance": float(utility["net_return_variance"]), "variance_convention": "population_ddof_0",
            "net_return": float(equities[-1] / equities[0] - 1),
            "max_drawdown": float((equities / equities.cummax(0).values - 1).min()),
            "total_cost": sum(float(row["total_cost"]) for row in audits),
            "mean_daily_turnover": sum(sum(float(fill["notional"]) for fill in row["fills"]) / row["equity_open"] for row in audits) / len(audits),
            "constraint_violation_count": sum(len(row["constraint_violations"]) for row in audits),
            "fees_already_deducted": True}
    result["proxy_comparison_eligible"] = experiments.read(root / "differences.json")["closed_loop"]["comparison_eligible"]
    return result


def plan(bundle, protocol, *, template, config, context, policy, continuous, acceptance):
    from .utility_fit import validate_feedback

    feedback = validate_feedback(bundle, protocol, context["selection_cutoff"])
    return {"schema_version": VERSION, "implementation_hash": experiments.code_identity(),
            "protocol_id": targets.validate_protocol(protocol), "data_content_hash": bundle.identity["content_hash"],
            "source": template.source, "training_config": asdict(config), "context": context,
            "max_feedback_available": feedback, "policy": {**policy, "fees": asdict(policy["fees"])},
            "continuous": asdict(continuous), "acceptance": acceptance,
            "budget": "included in train.max_seconds; no separate uncharged search",
            "selection": "continuous_validation_loss_strict_improvement; epoch0_control_only",
            "scope": "inner_validation_only; diagnostics do not choose weights or early stopping"}


def audit_summary(attempts, selected_epoch):
    candidates = [row for row in attempts if row["state"] != "not_run"]
    ranking = rankings(candidates)
    return {"schema_version": VERSION, "status": "COMPLETED" if ranking["coverage_complete"] else "PARTIAL",
            "selected_epoch": selected_epoch, "selection_changed": False, "ranking": ranking,
            "attempts": attempts, "economic_efficacy": "NOT_ESTABLISHED"}


def same_training_state(first, second):
    if isinstance(first, torch.Tensor):
        return isinstance(second, torch.Tensor) and torch.equal(first, second)
    if isinstance(first, dict):
        return isinstance(second, dict) and first.keys() == second.keys() and all(same_training_state(first[key], second[key]) for key in first)
    if isinstance(first, (tuple, list)):
        return type(first) is type(second) and len(first) == len(second) and all(same_training_state(a, b) for a, b in zip(first, second, strict=True))
    return type(first) is type(second) and first == second


class ValidationAudit:
    def __init__(self, directory, model, bundle, protocol, *, config, context, policy, continuous, acceptance):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=False)
        self.template, self.bundle, self.protocol = copy.deepcopy(model), bundle, copy.deepcopy(protocol)
        self.config, self.context, self.policy = config, copy.deepcopy(context), copy.deepcopy(policy)
        self.continuous, self.acceptance = continuous, copy.deepcopy(acceptance)
        self.plan = plan(bundle, protocol, template=model, config=config, context=context,
                         policy=policy, continuous=continuous, acceptance=acceptance)
        self.attempts = [{"epoch": epoch, "state": "queued"} for epoch in range(1, config.epochs + 1)]
        experiments.write(self.root / "plan.json", self.plan)
        self.persist()

    def persist(self):
        experiments.write(self.root / "attempts.json", {"attempts": self.attempts})

    def __call__(self, snapshot, guard):
        epoch = snapshot["epoch"]
        attempt = self.attempts[epoch - 1]
        if attempt["epoch"] != epoch or attempt["state"] != "queued":
            raise ValueError("validation candidate epoch is duplicate or out of range")
        started = time.monotonic()
        attempt.update(state="running", validation_loss=snapshot["record"]["validation_loss"])
        self.persist()
        root = self.root / f"epoch-{epoch:04d}"
        root.mkdir()
        try:
            model = copy.deepcopy(self.template)
            model.net.load_state_dict(snapshot["weights"])
            model.net.eval()
            model.fit_record = {"schema_version": "decision_candidate_fit_v1", "epoch": epoch,
                "config": asdict(self.config), "context": self.context, "history": [snapshot["record"]],
                "implementation_hash": self.plan["implementation_hash"], "protocol_id": self.plan["protocol_id"],
                "scope": "completed_epoch_candidate_not_selected_model"}
            model.training_state = {key: snapshot[key] for key in ("epoch", "optimizer", "cpu_rng")}
            model.save(root / "checkpoint")
            attempt["checkpoint_inventory"] = experiments.inventory(root / "checkpoint")
            self.persist()
            restored = DecisionGRU.load(root / "checkpoint")
            with torch.no_grad():
                evaluate(restored, self.bundle, self.protocol, root / "accounts", policy_config=self.policy,
                         continuous=self.continuous, acceptance=self.acceptance, log=guard)
                metrics = account_metrics(root / "accounts", self.config.variance_penalty)
            if metrics["continuous"]["loss"] != attempt["validation_loss"]:
                raise ValueError("candidate continuous verification loss differs from training validation")
            guard()
            attempt.update(state="succeeded", metrics=metrics)
            experiments.write(root / "result.json", {"epoch": epoch, "metrics": metrics,
                "checkpoint_inventory": attempt["checkpoint_inventory"], "selection_changed": False})
        except (ValueError, KeyError, RuntimeError) as exc:
            attempt.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            experiments.write(root / "result.json", {"epoch": epoch, "status": "FAILED", "error": attempt["error"]})
            guard()
        except BaseException as exc:
            attempt.update(state="interrupted", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            attempt["elapsed_seconds"] = time.monotonic() - started
            self.persist()

    def finish(self, fit_record):
        for row in self.attempts:
            if row["state"] == "queued":
                row.update(state="not_run", reason="early_stopping" if fit_record else "training_failed_or_cancelled")
        self.persist()
        result = audit_summary(self.attempts, fit_record["best_epoch"] if fit_record else None)
        if fit_record is None:
            result["status"] = "INTERRUPTED_TRAINING"
        experiments.write(self.root / "summary.json", result)
        return result


def verify_audit(directory, model, bundle, protocol, *, config, context, policy, continuous, acceptance, fit_record):
    root = Path(directory)
    expected = plan(bundle, protocol, template=model, config=config, context=context,
                    policy=policy, continuous=continuous, acceptance=acceptance)
    if experiments.read(root / "plan.json") != expected:
        raise ValueError("validation candidate audit plan differs")
    attempts = experiments.read(root / "attempts.json")["attempts"]
    if [row["epoch"] for row in attempts] != list(range(1, config.epochs + 1)):
        raise ValueError("validation candidate epoch coverage differs")
    history = {row["epoch"]: row for row in fit_record["history"]}
    reached = [row for row in attempts if row["state"] != "not_run"]
    if [row["epoch"] for row in reached] != list(history):
        raise ValueError("validation candidate audit and training history differ")
    if any(row["state"] != "succeeded" for row in reached):
        raise ValueError("validation candidate audit incomplete; failed candidates remain diagnostic")
    for attempt in attempts[len(reached):]:
        if attempt != {"epoch": attempt["epoch"], "state": "not_run", "reason": "early_stopping"}:
            raise ValueError("invalid unrun validation candidate")
    for attempt in reached:
        epoch = attempt["epoch"]
        candidate = root / f"epoch-{epoch:04d}"
        restored = DecisionGRU.load(candidate / "checkpoint")
        expected_fit = {"schema_version": "decision_candidate_fit_v1", "epoch": epoch,
                "config": asdict(config), "context": context, "history": [history[epoch]],
                "implementation_hash": expected["implementation_hash"], "protocol_id": expected["protocol_id"],
                "scope": "completed_epoch_candidate_not_selected_model"}
        if (experiments.inventory(candidate / "checkpoint") != attempt["checkpoint_inventory"]
                or restored.fit_record != expected_fit or restored.training_state["epoch"] != epoch
                or restored.source != model.source):
            raise ValueError("validation candidate checkpoint lineage differs")
        if attempt["validation_loss"] != history[epoch]["validation_loss"]:
            raise ValueError("validation candidate loss/history differs")
        with tempfile.TemporaryDirectory(prefix="qwb-validation-candidate-") as temporary, torch.no_grad():
            evaluate(restored, bundle, protocol, Path(temporary) / "accounts", policy_config=policy,
                     continuous=continuous, acceptance=acceptance)
            metrics = account_metrics(Path(temporary) / "accounts", config.variance_penalty)
            if metrics != attempt["metrics"] or metrics["continuous"]["loss"] != attempt["validation_loss"]:
                raise ValueError("validation candidate arithmetic differs")
            for filename in ACCOUNT_FILES:
                if experiments.read(candidate / "accounts" / filename) != experiments.read(Path(temporary) / "accounts" / filename):
                    raise ValueError(f"validation candidate account arithmetic differs: {filename}")
        targets.verify(candidate / "accounts/exact", inputs=False)
        if experiments.read(candidate / "result.json") != {"epoch": epoch, "metrics": metrics,
                "checkpoint_inventory": attempt["checkpoint_inventory"], "selection_changed": False}:
            raise ValueError("validation candidate result differs")
    summary = audit_summary(attempts, fit_record["best_epoch"])
    if summary != experiments.read(root / "summary.json"):
        raise ValueError("validation candidate ranking summary differs")
    if summary["ranking"]["continuous_order"][0] != fit_record["best_epoch"]:
        raise ValueError("validation candidate diagnostic changed original selection")
    selected = DecisionGRU.load(root / f"epoch-{fit_record['best_epoch']:04d}" / "checkpoint")
    if any(not torch.equal(value, selected.net.state_dict()[name]) for name, value in model.net.state_dict().items()):
        raise ValueError("selected model differs from its validation candidate")
    if not same_training_state(model.training_state, selected.training_state):
        raise ValueError("selected optimizer/RNG differs from its validation candidate")
    return summary
