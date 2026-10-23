"""Candidate diagnostics expose proxy ranking errors without selecting on them."""

import copy

import pytest

pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")
import torch

from qwb import experiments
from qwb.jobs import Cancelled
from qwb.portfolio.continuous import ContinuousConfig
from qwb.training import validation_audit as audit_module
from qwb.training.utility_fit import DecisionTrainingConfig, fit
from qwb.training.validation_audit import ValidationAudit, rankings, verify_audit
from test_decision_training import setup_training


def ranked(continuous, exact):
    return [{"epoch": i + 1, "state": "succeeded", "metrics": {"continuous": {"loss": c}, "exact": {"loss": e}}}
            for i, (c, e) in enumerate(zip(continuous, exact, strict=True))]


def test_rank_inversion_ties_and_partial_coverage_match_known_pairwise_results():
    result = rankings(ranked([3., 1., 2.], [1., 3., 2.]))
    assert result["continuous_order"] == [2, 3, 1] and result["exact_order"] == [1, 3, 2]
    assert result["kendall_tau_b"] == -1 and len(result["discordant_pairs"]) == 3
    assert result["coverage_complete"]
    from scipy.stats import kendalltau
    first, second = [1., 1., 2., 3.], [1., 2., 2., 0.]
    assert rankings(ranked(first, second))["kendall_tau_b"] == pytest.approx(kendalltau(first, second).statistic)
    partial = ranked([1.], [2.]) + [{"epoch": 2, "state": "failed"}]
    result = rankings(partial)
    assert not result["coverage_complete"] and result["failed_epochs"] == [2]
    assert result["kendall_tau_b"] is None and result["undefined_reason"] == "fewer_than_two_successful_candidates"
    assert rankings(ranked([1., 1.], [2., 3.]))["undefined_reason"] == "one_or_both_paths_all_tied"
    assert rankings(ranked([1., 1.], [2., 2.]))["tied_both"] == 1
    with pytest.raises(ValueError, match="unique"):
        rankings(partial + partial)
    with pytest.raises(ValueError, match="finite"):
        rankings(ranked([float("nan")], [1.]))


def test_actual_candidates_preserve_selected_parameters_rng_and_same_epoch_states(tmp_path, monkeypatch):
    model, bundle, train, validation, policy, context = setup_training(tmp_path, monkeypatch)
    control = copy.deepcopy(model)
    config = DecisionTrainingConfig(epochs=2, freeze_encoder=True, learning_rate=.0001)
    continuous = ContinuousConfig(minimum_commission=False)
    from test_utility_comparison import limits
    acceptance = limits()
    original_rng = torch.get_rng_state().clone()
    normal = fit(control, bundle, train, validation, policy_config=policy, context=context,
                 config=config, continuous=continuous)
    auditor = ValidationAudit(tmp_path / "candidates", model, bundle, validation, config=config, context=context,
                              policy=policy, continuous=continuous, acceptance=acceptance)
    audited = fit(model, bundle, train, validation, policy_config=policy, context=context,
                  config=config, continuous=continuous, candidate_audit=auditor)
    result = auditor.finish(audited)
    assert result["status"] == "COMPLETED" and result["selection_changed"] is False
    assert normal["history"] == audited["history"] and normal["best_epoch"] == audited["best_epoch"]
    assert torch.equal(original_rng, torch.get_rng_state())
    assert torch.equal(model.training_state["cpu_rng"], control.training_state["cpu_rng"])
    assert all(torch.equal(a, b) for a, b in zip(control.net.parameters(), model.net.parameters(), strict=True))
    for attempt in auditor.attempts:
        root = auditor.root / f"epoch-{attempt['epoch']:04d}"
        candidate = audit_module.DecisionGRU.load(root / "checkpoint")
        assert candidate.training_state["epoch"] == attempt["epoch"]
        assert all(float(value["step"]) == attempt["epoch"] for value in candidate.training_state["optimizer"]["state"].values())
        assert not candidate.net.training
        assert all(not p.requires_grad for p in candidate.net.encoder.parameters())
    options = dict(config=config, context=context, policy=policy, continuous=continuous,
                   acceptance=acceptance, fit_record=audited)
    assert verify_audit(auditor.root, model, bundle, validation, **options) == result
    result_path = auditor.root / "epoch-0001/result.json"
    forged = experiments.read(result_path)
    forged["metrics"]["exact"]["loss"] += .001
    experiments.write(result_path, forged)
    with pytest.raises(ValueError, match="result differs"):
        verify_audit(auditor.root, model, bundle, validation, **options)


def test_candidate_failure_is_retained_and_does_not_select_on_successful_subset(tmp_path, monkeypatch):
    model, bundle, train, validation, policy, context = setup_training(tmp_path, monkeypatch)
    from test_utility_comparison import limits
    config = DecisionTrainingConfig(epochs=1)
    continuous = ContinuousConfig(minimum_commission=False)
    auditor = ValidationAudit(tmp_path / "candidates", model, bundle, validation, config=config, context=context,
                              policy=policy, continuous=continuous, acceptance=limits())
    def failed(*_args, **_kwargs):
        raise ValueError("controlled diagnostic failure")
    monkeypatch.setattr(audit_module, "evaluate", failed)
    record = fit(model, bundle, train, validation, policy_config=policy, context=context,
                 config=config, continuous=continuous, candidate_audit=auditor)
    result = auditor.finish(record)
    assert record["best_epoch"] == 1 and result["status"] == "PARTIAL"
    assert result["ranking"]["completed_candidate_count"] == 0
    assert result["ranking"]["failed_epochs"] == [1]
    assert (auditor.root / "epoch-0001/checkpoint/weights.pt").is_file()
    assert "controlled diagnostic failure" in auditor.attempts[0]["error"]
    with pytest.raises(ValueError, match="incomplete"):
        verify_audit(auditor.root, model, bundle, validation, config=config, context=context,
                     policy=policy, continuous=continuous, acceptance=limits(), fit_record=record)


def test_cancellation_during_exact_validation_preserves_candidate_and_unrun_epochs(tmp_path, monkeypatch):
    model, bundle, train, validation, policy, context = setup_training(tmp_path, monkeypatch)
    from test_utility_comparison import limits
    config = DecisionTrainingConfig(epochs=3)
    continuous = ContinuousConfig(minimum_commission=False)
    auditor = ValidationAudit(tmp_path / "candidates", model, bundle, validation, config=config, context=context,
                              policy=policy, continuous=continuous, acceptance=limits())
    reached = []
    def log(entry):
        if isinstance(entry, dict):
            reached.append(entry)
        elif reached:
            raise Cancelled("cancel candidate validation")
    with pytest.raises(Cancelled):
        fit(model, bundle, train, validation, policy_config=policy, context=context, config=config,
            continuous=continuous, candidate_audit=auditor, log=log)
    result = auditor.finish(model.fit_record)
    assert result["status"] == "INTERRUPTED_TRAINING"
    assert [row["state"] for row in auditor.attempts] == ["interrupted", "not_run", "not_run"]
    assert (auditor.root / "epoch-0001/checkpoint/weights.pt").exists()
    assert model.fit_record is None


def test_validation_audit_rejects_feedback_after_selection_cutoff(tmp_path, monkeypatch):
    model, bundle, _, validation, policy, context = setup_training(tmp_path, monkeypatch)
    from test_utility_comparison import limits
    context["selection_cutoff"] = validation["sessions"][0]["decision_time"]
    with pytest.raises(ValueError, match="cutoff"):
        ValidationAudit(tmp_path / "candidates", model, bundle, validation, config=DecisionTrainingConfig(),
                        context=context, policy=policy, continuous=ContinuousConfig(), acceptance=limits())


def test_exact_validation_is_charged_to_original_training_time_budget(tmp_path, monkeypatch):
    model, bundle, train, validation, policy, context = setup_training(tmp_path, monkeypatch)
    from test_utility_comparison import limits
    config = DecisionTrainingConfig(epochs=2, max_seconds=300)
    continuous = ContinuousConfig(minimum_commission=False)
    auditor = ValidationAudit(tmp_path / "candidates", model, bundle, validation, config=config, context=context,
                              policy=policy, continuous=continuous, acceptance=limits())
    clock = [0.]
    monkeypatch.setattr("qwb.training.utility_fit.time.monotonic", lambda: clock[0])
    def over_budget(*_args, **kwargs):
        clock[0] = 301.
        kwargs["log"]("exact validation decision")
    monkeypatch.setattr(audit_module, "evaluate", over_budget)
    with pytest.raises(ValueError, match="time budget"):
        fit(model, bundle, train, validation, policy_config=policy, context=context, config=config,
            continuous=continuous, candidate_audit=auditor)
    result = auditor.finish(model.fit_record)
    assert result["status"] == "INTERRUPTED_TRAINING"
    assert auditor.attempts[0]["state"] == "failed" and "time budget" in auditor.attempts[0]["error"]
    assert auditor.attempts[1]["state"] == "not_run"


def test_early_stopping_retains_unrun_candidates_and_missing_epoch_is_rejected(tmp_path, monkeypatch):
    model, bundle, train, validation, policy, context = setup_training(tmp_path, monkeypatch)
    from test_utility_comparison import limits

    # A genuine zero-gradient control makes consecutive validation paths tie.
    def constant_forecast(self, values, valid):
        return self.net(values, valid) * 0 + .002
    monkeypatch.setattr(audit_module.DecisionGRU, "forecast", constant_forecast)
    config = DecisionTrainingConfig(epochs=4, patience=1)
    continuous = ContinuousConfig(minimum_commission=False)
    auditor = ValidationAudit(tmp_path / "candidates", model, bundle, validation, config=config, context=context,
                              policy=policy, continuous=continuous, acceptance=limits())
    record = fit(model, bundle, train, validation, policy_config=policy, context=context, config=config,
                 continuous=continuous, candidate_audit=auditor)
    result = auditor.finish(record)
    assert len(record["history"]) == 2 and record["best_epoch"] == 1
    assert [row["state"] for row in auditor.attempts] == ["succeeded", "succeeded", "not_run", "not_run"]
    assert result["ranking"]["kendall_tau_b"] is None
    options = dict(config=config, context=context, policy=policy, continuous=continuous,
                   acceptance=limits(), fit_record=record)
    assert verify_audit(auditor.root, model, bundle, validation, **options)["status"] == "COMPLETED"
    attempts = experiments.read(auditor.root / "attempts.json")
    attempts["attempts"].pop(1)
    experiments.write(auditor.root / "attempts.json", attempts)
    with pytest.raises(ValueError, match="epoch coverage differs"):
        verify_audit(auditor.root, model, bundle, validation, **options)
