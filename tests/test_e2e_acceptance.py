"""Failures must remain evidence, not become success through skipped checks."""

from pathlib import Path

import pytest

from qwb import experiments
from qwb.portfolio import acceptance


def test_acceptance_writes_failed_checks_and_does_not_overwrite(tmp_path, monkeypatch):
    def missing():
        raise ImportError("explicit missing numerical dependency")

    monkeypatch.setattr(acceptance, "solver_check", missing)
    out = tmp_path / "check"
    result = acceptance.run(out)
    assert result["status"] == "FAIL"
    assert result["checks"]["account_and_state"]["status"] == "PASS"
    assert result["checks"]["score_isolation"]["status"] == "PASS"
    assert "missing numerical dependency" in result["checks"]["solver_and_gradients"]["error"]
    checksums = experiments.read(out / "checksums.json")
    assert checksums["acceptance.json"] == experiments.digest(out / "acceptance.json")
    with pytest.raises(FileExistsError):
        acceptance.run(out)


def test_successful_local_check_does_not_clear_full_platform_gates(monkeypatch):
    monkeypatch.setattr(acceptance, "solver_check", lambda: {"test_fixture": True})
    result = acceptance.run()
    assert result["status"] == "PASS"
    assert "external_reproduction" in result["remaining_gates"]
    assert result["economic_evidence"] == "SYNTHETIC_ONLY"


def test_cli_returns_nonzero_for_failed_check(tmp_path, monkeypatch, capsys):
    from qwb.cli import main

    monkeypatch.setattr(acceptance, "solver_check", lambda: (_ for _ in ()).throw(ValueError("fixture failure")))
    assert main(["e2e", "check", "--output", str(tmp_path / "check")]) == 1
    assert "fixture failure" in capsys.readouterr().out
