"""Workflow files must parse; a YAML error makes GitHub skip every job and report one failure."""
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).parents[1] / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_parses_and_names_its_jobs(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data.get("jobs"), f"{path.name} 没有 jobs"
    for name, job in data["jobs"].items():
        assert job.get("steps"), f"{path.name}:{name} 没有 steps"
        for step in job["steps"]:
            assert isinstance(step.get("run", ""), str), f"{path.name}:{name} 有 run 被解析成了非字符串"
