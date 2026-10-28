"""R7: thresholds are tested only through a frozen, pre-registered trial."""
import json

import pytest

from qwb import audit_trials

PLAN = {"id": "trial-buffer-001", "rule": "T-BUFFER-RANK", "default": 2, "candidates": [2, 3, 4],
        "development": {"start": "2023-10-27", "end": "2025-10-27"},
        "validation": {"start": "2025-10-28", "end": "2026-10-27"},
        "primary_metric": "费用后净收益", "drawdown_tolerance": .01, "pooling": "中位数差与改善策略占比",
        "pass": {"min_improved_share": .6, "max_drawdown_worse_share": .2}}


def rows(segment, values, status="ok"):
    """values: {strategy: {candidate: (metric, drawdown)}}"""
    return [{"strategy": s, "candidate": c, "segment": segment, "status": status, "metric": m, "drawdown": dd}
            for s, by in values.items() for c, (m, dd) in by.items()]


def test_register_validates_and_freezes(tmp_path):
    path = tmp_path / "trial.json"
    with pytest.raises(ValueError, match="经验阈值"):
        audit_trials.register(path, {**PLAN, "rule": "F-TICK-STOCK"})
    with pytest.raises(ValueError, match="默认值"):
        audit_trials.register(path, {**PLAN, "candidates": [3, 4]})
    with pytest.raises(ValueError, match="早于验证段"):
        audit_trials.register(path, {**PLAN, "validation": {"start": "2025-01-01", "end": "2026-10-27"}})
    frozen = audit_trials.register(path, PLAN)
    assert frozen["max_validation_views"] == 1 and frozen["sha256"]
    with pytest.raises(ValueError, match="已存在"):
        audit_trials.register(path, PLAN)


def test_edited_plan_cannot_run(tmp_path):
    path = tmp_path / "trial.json"
    audit_trials.register(path, PLAN)
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["candidates"] = [2, 3, 4, 5]
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="改动"):
        audit_trials.evaluate(path, [])


def test_candidate_chosen_on_development_and_validation_seen_once(tmp_path):
    path = tmp_path / "trial.json"
    audit_trials.register(path, PLAN)
    dev = rows("development", {"s38": {2: (1., -.3), 3: (1.2, -.3), 4: (1.1, -.3)},
                               "s74": {2: (.5, -.1), 3: (.6, -.1), 4: (.55, -.1)}})
    # Candidate 3 wins development but loses validation on both strategies.
    val = rows("validation", {"s38": {2: (.3, -.2), 3: (.2, -.2), 4: (.35, -.2)},
                              "s74": {2: (.1, -.1), 3: (.05, -.15), 4: (.12, -.1)}})
    report = audit_trials.evaluate(path, dev + val)
    assert report["chosen_on_development"] == 3 and report["candidates_tested"] == 2
    assert not report["passed"] and "保持经验阈值" in report["conclusion"]
    # Candidate 4 looks better on validation, but it was not chosen in advance: it does not pass.
    assert next(r for r in report["validation"] if r["candidate"] == 4)["median_diff"] > 0
    with pytest.raises(ValueError, match="只看一次"):
        audit_trials.evaluate(path, dev + val)


def test_failed_runs_are_kept_and_count_as_not_improved(tmp_path):
    path = tmp_path / "trial.json"
    audit_trials.register(path, PLAN)
    dev = rows("development", {"a": {2: (1., -.1), 3: (2., -.1)}, "b": {2: (1., -.1), 3: (2., -.1)}})
    val = (rows("validation", {"a": {2: (1., -.1), 3: (2., -.1)}})
           + rows("validation", {"b": {2: (1., -.1), 3: (0., -.1)}}, status="failed"))
    report = audit_trials.evaluate(path, dev + val)
    assert report["failed_strategies"] == ["b"]
    chosen = next(r for r in report["validation"] if r["candidate"] == 3)
    assert chosen["improved_share"] == .5 and not report["passed"]


def test_unregistered_candidate_is_refused(tmp_path):
    path = tmp_path / "trial.json"
    audit_trials.register(path, PLAN)
    with pytest.raises(ValueError, match="未登记"):
        audit_trials.evaluate(path, rows("development", {"a": {5: (1., -.1)}}))
