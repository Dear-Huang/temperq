"""Actual isolated fits, complete failure coverage and resource-summary checks."""

import copy
import os
from pathlib import Path
import sys

from qwb import experiments
import pytest

pytest.importorskip("torch")
pytest.importorskip("cvxpylayers")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_utility_resource_audit import cases, compute, configuration, execute_process, run_child, verify


@pytest.mark.parametrize("change", [
    {"assets": [2, 2]}, {"assets": [1]}, {"assets": [True]}, {"steps": []},
    {"detach": [0]}, {"detach": [None, None]}, {"repeats": 0}, {"epochs": True},
    {"max_seconds": float("nan")}, {"wall_seconds": 120},
])
def test_resource_grid_rejects_ambiguous_or_unbounded_inputs(change):
    with pytest.raises(ValueError):
        configuration(**change)


def test_actual_child_wall_timeout_kills_and_reaps_process(tmp_path):
    task = tmp_path / "task.json"
    experiments.write(task, {})
    result = run_child(task, wall_seconds=.01)
    assert result["state"] == "timed_out" and result["exit_code"] is None
    assert "killed and reaped" in result["error"] and "child" not in result


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv process-tree regression")
def test_windows_timeout_terminates_nested_interpreter_not_only_venv_launcher(tmp_path):
    import ctypes
    from ctypes import wintypes

    marker = tmp_path / "grandchild.pid"
    nested = f"import os,time; from pathlib import Path; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    command = [sys.executable, "-c", f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{nested!r}]); time.sleep(60)"]
    with (tmp_path / "nested.log").open("wb") as log:
        result = execute_process(command, log, wall_seconds=3)
    assert result["state"] == "timed_out" and marker.exists()
    assert result["termination"]["method"] == "windows_taskkill_tree"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(marker.read_text()))
    if handle:
        try:
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259
        finally:
            kernel.CloseHandle(handle)


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    root = tmp_path_factory.mktemp("isolated-resources") / "audit"
    result = compute(root, configuration(assets=[3], steps=[2], detach=[None, 1], repeats=2))
    assert result["status"] == "COMPLETED", experiments.read(root / "attempts.json")
    return root, result


def test_actual_pretraining_isolated_fit_repetitions_and_checkpoint_provenance(measured):
    root, result = measured
    assert result["success_count"] == result["total_declared"] == 4
    assert verify(root)["status"] == "PASS"
    plan = experiments.read(root / "plan.json")
    assert len(cases(plan["config"])) == 4
    assert plan["sources"]["3"]["child"]["source_run_id"]
    attempts = experiments.read(root / "attempts.json")
    for name, row in attempts.items():
        assert row["exit_code"] == 0
        assert row["child"]["checkpoint_reload_equal"]
        assert row["child"]["pid"] != os.getpid()
        assert row["child"]["torch_num_threads"] == 1
        assert row["child"]["memory"]["peak_rss_bytes"] > 0
        assert row["child"]["fit"]["trainable_count"] > 0
        assert row["child"]["fit"]["implementation_hash"] == plan["implementation_hash"]
        assert (root / "cases" / name / "checkpoint/weights.pt").exists()
    assert all(group["repeated_parameters_equal"] is True for group in result["groups"].values())
    assert all(group["success_fraction_of_all_declared"] == 1 for group in result["groups"].values())


def test_resource_summary_tampering_rehashed_inventory_still_rejected(measured):
    root, original = measured
    changed = copy.deepcopy(original)
    next(iter(changed["groups"].values()))["conditional_success_ranges"]["peak_rss_bytes"]["median"] += 1024
    experiments.write(root / "result.json", changed)
    inventory = experiments.inventory(root)
    inventory.pop("checksums.json")
    experiments.write(root / "checksums.json", inventory)
    try:
        with pytest.raises(ValueError, match="summary arithmetic differs"):
            verify(root)
    finally:
        experiments.write(root / "result.json", original)
        inventory = experiments.inventory(root)
        inventory.pop("checksums.json")
        experiments.write(root / "checksums.json", inventory)


def test_resource_missing_declared_case_rehashed_inventory_still_rejected(measured):
    root, _ = measured
    original = experiments.read(root / "attempts.json")
    changed = copy.deepcopy(original)
    changed.pop(next(iter(changed)))
    experiments.write(root / "attempts.json", changed)
    inventory = experiments.inventory(root)
    inventory.pop("checksums.json")
    experiments.write(root / "checksums.json", inventory)
    try:
        with pytest.raises(ValueError, match="coverage differs"):
            verify(root)
    finally:
        experiments.write(root / "attempts.json", original)
        inventory = experiments.inventory(root)
        inventory.pop("checksums.json")
        experiments.write(root / "checksums.json", inventory)


def test_failed_actual_pretraining_preserves_every_dependent_unrun_cell(tmp_path):
    root = tmp_path / "failed-pretraining"
    result = compute(root, configuration(assets=[3], steps=[2], detach=[None, 1],
                                         repeats=2, pretrain_max_samples=1))
    assert result["status"] == "DIAGNOSTIC_WITH_FAILURES"
    assert result["total_declared"] == 4 and result["success_count"] == 0
    assert experiments.read(root / "preparation.json")["3"]["state"] == "failed"
    assert {row["state"] for row in experiments.read(root / "attempts.json").values()} == {"not_run"}
    assert all(group["conditional_success_ranges"]["peak_rss_bytes"] is None
               and group["success_fraction_of_all_declared"] == 0 for group in result["groups"].values())
    assert verify(root)["status"] == "PASS"


def test_actual_fit_budget_failure_retains_context_and_stays_in_denominator(tmp_path):
    root = tmp_path / "failed-fit"
    result = compute(root, configuration(assets=[3], steps=[2], detach=[None], repeats=1, max_seconds=.001))
    assert result["status"] == "DIAGNOSTIC_WITH_FAILURES" and result["success_count"] == 0
    row, = experiments.read(root / "attempts.json").values()
    assert row["state"] == "failed" and "training time budget exceeded" in row["error"]
    assert row["child"]["failure_context"]["schema_version"] == "utility_training_failure_v1"
    assert row["child"]["memory"]["peak_rss_bytes"] > 0
    assert verify(root)["status"] == "PASS"
