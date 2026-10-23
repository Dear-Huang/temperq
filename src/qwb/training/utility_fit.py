"""Chronological decision training, with historical selection and whole-fold reruns."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import time
import sys

import torch

from .. import experiments
from ..bench import targets
from ..portfolio.contracts import number, timestamp
from ..portfolio.continuous import ContinuousConfig, numbers
from ..portfolio.episode import run_episode
from ..portfolio.reference import Fees
from .decision import DecisionPolicy
from .utility import net_utility_returns


def process_peak_memory():
    """Process high-water RSS, including imports and earlier trials, in bytes."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                *[(name, ctypes.c_size_t) for name in ("peak_rss", "rss", "peak_paged", "paged",
                    "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError(ctypes.get_last_error(), "cannot measure process memory")
        peak = counters.peak_rss
    else:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    return {"peak_rss_bytes": peak, "scope": "process_high_water_including_imports_and_prior_trials"}


@dataclass(frozen=True)
class DecisionTrainingConfig:
    epochs: int = 10
    patience: int = 3
    learning_rate: float = .001
    variance_penalty: float = 0.0
    freeze_encoder: bool = False
    detach_every: int | None = None
    max_episode_steps: int = 256
    max_seconds: float = 300.0
    cpu_threads: int = 1
    seed: int = 0

    def __post_init__(self):
        for name in ("epochs", "patience", "max_episode_steps", "cpu_threads"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.detach_every is not None and (type(self.detach_every) is not int or self.detach_every < 1):
            raise ValueError("detach_every must be null or a positive integer")
        if type(self.freeze_encoder) is not bool or type(self.seed) is not int or self.seed < 0:
            raise ValueError("invalid encoder freeze setting or seed")
        number(self.learning_rate, "utility learning rate", lower=1e-8, upper=1)
        number(self.variance_penalty, "utility variance penalty")
        number(self.max_seconds, "utility time budget", lower=.001)


def validate_feedback(bundle, protocol, cutoff):
    """Bind execution prices to revisions actually available at the fit cutoff."""
    targets.validate_protocol(protocol)
    cutoff = timestamp(cutoff)
    if protocol["initial_state"]["data_snapshot_id"] != bundle.manifest["data_snapshot_id"]:
        raise ValueError("utility protocol data snapshot differs from observations")
    if protocol["data_kind"] != bundle.manifest["data_kind"]:
        raise ValueError("utility protocol data kind differs from observations")
    visible = bundle.bars[bundle.bars["available_time"] <= cutoff].drop_duplicates(["code", "date"], keep="last")
    lookup = visible.set_index(["code", "date"])
    latest = None
    for session in protocol["sessions"]:
        if timestamp(session["close_at"]) > cutoff:
            raise ValueError("utility execution feedback exceeds its cutoff")
        day = timestamp(session["close_at"]).tz_convert("Asia/Shanghai").tz_localize(None).normalize()
        decision_day = timestamp(session["decision_time"]).tz_convert("Asia/Shanghai").tz_localize(None).normalize()
        index = bundle.calendar.get_loc(decision_day)
        if index + 1 >= len(bundle.calendar) or bundle.calendar[index + 1] != day:
            raise ValueError("utility execution must use the next bundle session")
        for bar in session["bars"]:
            key = (bar["asset_id"], day)
            if key not in lookup.index:
                raise ValueError("utility feedback lacks a visible source bar")
            source = lookup.loc[key]
            for field in ("open", "close"):
                actual, expected = bar[field], source[field]
                # Missing execution quotes are an explicit account condition.
                if actual is not None and actual != expected:
                    raise ValueError("utility feedback price differs from its visible revision")
            available = max(timestamp(source["available_time"]), timestamp(session["close_at"]))
            latest = available if latest is None else max(latest, available)
    for event in protocol["corporate_actions"]:
        if timestamp(event["available_at"]) > cutoff:
            raise ValueError("utility feedback includes an event unavailable at cutoff")
    return str(latest)


def fit(model, bundle, train_protocol, validation_protocol, *, policy_config, context,
        config=DecisionTrainingConfig(), continuous=ContinuousConfig(), log=lambda *_: None, candidate_audit=None):
    """One episode and Adam step per epoch; no outer-test data enters this API.

    The caller persists each log entry and failed/cancelled trial. Returned fit
    metadata and training state describe the selected epoch, not the last epoch.
    """
    model.fit_record, model.training_state = None, None
    required = {"fit_cutoff", "selection_cutoff"}
    if not isinstance(context, dict) or set(context) != required:
        raise ValueError("utility context requires fit_cutoff and selection_cutoff")
    fit_cutoff, selection_cutoff = (timestamp(context[key]) for key in ("fit_cutoff", "selection_cutoff"))
    first_train = timestamp(train_protocol["sessions"][0]["decision_time"])
    first_validation = timestamp(validation_protocol["sessions"][0]["decision_time"])
    source_cutoff = timestamp(model.source["fit"]["context"]["selection_cutoff"])
    if not source_cutoff <= first_train <= fit_cutoff <= first_validation <= selection_cutoff:
        raise ValueError("utility initialization and episode cutoffs overlap or are reversed")
    max_train = validate_feedback(bundle, train_protocol, fit_cutoff)
    max_validation = validate_feedback(bundle, validation_protocol, selection_cutoff)
    for protocol in (train_protocol, validation_protocol):
        if len(protocol["sessions"]) > config.max_episode_steps:
            raise ValueError("predeclared utility episode step budget exceeded")
        if protocol["fees"] != asdict(policy_config["fees"]):
            raise ValueError("policy fees differ from utility account protocol")
    if policy_config.get("fee_model", "declared_execution") != continuous.fee_model:
        raise ValueError("policy and continuous account fee models differ")
    bundle.assert_unchanged()
    identity = experiments.code_identity()
    protocol_hashes = [experiments.object_digest(p) for p in (train_protocol, validation_protocol)]
    started = time.monotonic()
    old_threads = torch.get_num_threads()
    history, best, stale, selected = [], float("inf"), 0, None
    trainable = []
    for name, parameter in model.net.named_parameters():
        parameter.requires_grad_(not (config.freeze_encoder and name.startswith("encoder.")))
        if parameter.requires_grad:
            trainable.append((name, parameter))
    if not trainable:
        raise ValueError("utility model has no trainable parameters")
    initial = copy.deepcopy(model.net.state_dict())
    optimizer = torch.optim.Adam([p for _, p in trainable], lr=config.learning_rate,
        betas=(.9, .999), eps=1e-8, weight_decay=0, amsgrad=False)

    def guard(*_):
        log("utility decision boundary")
        if time.monotonic() - started > config.max_seconds:
            raise ValueError("predeclared utility training time budget exceeded")

    def episode(protocol, training):
        guard()
        policy = DecisionPolicy(model, bundle, **policy_config)
        result = run_episode(protocol, policy, config=continuous, on_step=guard,
            detach_every=config.detach_every if training else None)
        loss, audit = net_utility_returns(result["net_returns"], variance_penalty=config.variance_penalty)
        return loss, {**numbers(audit), "total_cost": sum(float(a["total_cost"].detach()) for a in result["audits"]),
                      "net_return": float((result["equity"][-1] / result["equity"][0] - 1).detach()),
                      "detach_before_steps": result["detach_before_steps"]}

    current_epoch, phase = 0, "baseline_validation"
    try:
        torch.set_num_threads(config.cpu_threads)
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(config.seed)
            model.net.eval()
            with torch.no_grad():
                baseline_loss, baseline = episode(validation_protocol, False)
            for epoch in range(1, config.epochs + 1):
                current_epoch, phase = epoch, "training_episode"
                guard()
                model.net.train()
                optimizer.zero_grad(set_to_none=True)
                train_loss, train_audit = episode(train_protocol, True)
                if not train_loss.requires_grad:
                    raise ValueError("utility loss is disconnected from all model parameters")
                phase = "backward"
                train_loss.backward()
                phase = "gradient_validation"
                gradients = [p.grad for _, p in trainable if p.grad is not None]
                if not gradients or any(not bool(torch.isfinite(g).all()) for g in gradients):
                    raise ValueError("missing or nonfinite utility gradients")
                gradient_norm = float(torch.sqrt(sum((g.square().sum() for g in gradients))))
                number(gradient_norm, "utility gradient norm")
                zero_count = sum(int((g == 0).sum()) for g in gradients)
                gradient_count = sum(g.numel() for g in gradients)
                before = {name: p.detach().clone() for name, p in trainable}
                phase = "optimizer_update"
                optimizer.step()
                if any(not bool(torch.isfinite(p).all()) for p in model.net.parameters()):
                    raise ValueError("nonfinite utility parameter update")
                update_norm = float(torch.sqrt(sum(((p.detach() - before[name]).square().sum() for name, p in trainable))))
                model.net.eval()
                phase = "validation_episode"
                with torch.no_grad():
                    validation_loss, validation_audit = episode(validation_protocol, False)
                value = float(validation_loss)
                record = {"epoch": epoch, "train_loss": float(train_loss.detach()), "validation_loss": value,
                    "gradient_norm": gradient_norm, "zero_gradient_fraction": zero_count / gradient_count,
                    "missing_gradient_parameters": [name for name, p in trainable if p.grad is None],
                    "parameter_update_norm": update_norm, "train": train_audit, "validation": validation_audit}
                history.append(record)
                if value < best:
                    best, stale = value, 0
                    selected = {"epoch": epoch, "weights": copy.deepcopy(model.net.state_dict()),
                                "optimizer": copy.deepcopy(optimizer.state_dict()), "cpu_rng": torch.get_rng_state().clone()}
                else:
                    stale += 1
                log(copy.deepcopy(record))
                if candidate_audit is not None:
                    phase = "candidate_audit"
                    snapshot = {"epoch": epoch, "weights": copy.deepcopy(model.net.state_dict()),
                                "optimizer": copy.deepcopy(optimizer.state_dict()), "cpu_rng": torch.get_rng_state().clone(),
                                "record": copy.deepcopy(record)}
                    with torch.random.fork_rng(devices=[]), torch.no_grad():
                        candidate_audit(snapshot, guard)
                guard()
                if stale >= config.patience:
                    break
            phase = "finalization"
            bundle.assert_unchanged()
            if identity != experiments.code_identity() or protocol_hashes != [
                    experiments.object_digest(p) for p in (train_protocol, validation_protocol)]:
                raise ValueError("utility inputs or implementation changed during training")
            model.net.load_state_dict(selected["weights"])
            model.training_state = {key: value for key, value in selected.items() if key != "weights"}
            model.fit_record = {"schema_version": "decision_fit_v1", "config": asdict(config), "context": context,
                "best_epoch": selected["epoch"], "validation_loss": best, "baseline_validation_loss": float(baseline_loss),
                "baseline_validation": baseline, "history": history, "trainable_parameters": [name for name, _ in trainable],
                "trainable_count": sum(p.numel() for _, p in trainable), "protocol_ids": protocol_hashes,
                "max_train_feedback_available": max_train, "max_validation_feedback_available": max_validation,
                "continuous": asdict(continuous), "production": {
                    "policy": policy_config.get("production_policy", "torch_box_budget_turnover_v1"),
                    "buffer": policy_config.get("production_buffer", 1e-12),
                    "dust_threshold": policy_config.get("dust_threshold")},
                "optimizer": {"name": "Adam", "defaults": optimizer.defaults},
                "data_content_hash": bundle.identity["content_hash"], "implementation_hash": identity,
                "parameter_change_norm": float(torch.sqrt(sum((p.detach() - initial[name]).square().sum()
                    for name, p in model.net.named_parameters()))),
                "elapsed_seconds": time.monotonic() - started, "resume": "whole_fold_only_no_mid_episode_resume",
                "memory": process_peak_memory(),
                "refit_on_validation": False, "economic_efficacy": "NOT_ESTABLISHED"}
            return model.fit_record
    except BaseException as exc:
        exc.qwb_training_failure = {"schema_version": "utility_training_failure_v1", "epoch": current_epoch,
            "phase": phase, "completed_epochs": len(history), "exception_type": type(exc).__name__, "reason": str(exc)}
        if hasattr(exc, "qwb_decision_failure"):
            exc.qwb_training_failure["decision"] = exc.qwb_decision_failure
        raise
    finally:
        torch.set_num_threads(old_threads)
        model.net.eval()
