from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "test_path",
    [
        "tests/skill_bridge/test_alicloud_ros_agent_bridge.py",
        "tests/skill_bridge/test_ros_remote_cli_diagnostics.py",
        "tests/services/test_permission_wait.py",
    ],
)
def test_windows_io_cases_run_once_outside_xdist(test_path: str) -> None:
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/test.yml"
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["test-windows"]["steps"]
    commands = [shlex.split(step["run"]) for step in steps if "run" in step]
    selected = [command for command in commands if test_path in command]
    assert len(selected) == 1
    assert "-n" not in selected[0]
    remaining = next(command for command in commands if "tests/" in command)
    assert "-n" in remaining
    assert "--ignore=" + test_path in remaining
