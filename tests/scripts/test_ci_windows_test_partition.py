from __future__ import annotations

import shlex
from fnmatch import fnmatch
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


def _remaining_tests(job: str) -> list[str]:
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/test.yml"
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][job]["steps"]
    return next(shlex.split(step["run"]) for step in steps if "run" in step and "pytest tests/ " in step["run"])


def _ignored(command: list[str], test_path: str) -> bool:
    return any(
        (argument.startswith("--ignore=") and test_path == argument.removeprefix("--ignore="))
        or (argument.startswith("--ignore-glob=") and fnmatch(test_path, argument.removeprefix("--ignore-glob=")))
        for argument in command
    )


@pytest.mark.parametrize(
    "test_path",
    [
        "tests/web/test_resource_link_workflow.py",
        "tests/web/test_resource_link_workflow_credentials.py",
        "tests/web/test_resource_link_execution_gate.py",
        "tests/web/test_resource_link_agent_client.py",
        "tests/web/test_resource_link_cleanup_identity.py",
        "tests/web/test_resource_link_cleanup_recovery.py",
        "tests/web/test_resource_link_mse_cleanup_inspection.py",
        "tests/web/test_resource_link_native_identity_recovery.py",
    ],
)
def test_internal_acceptance_tool_keeps_linux_coverage_without_windows(test_path: str) -> None:
    assert _ignored(_remaining_tests("test-windows"), test_path)
    assert not _ignored(_remaining_tests("test"), test_path)
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/test.yml"
    windows_steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["test-windows"]["steps"]
    for step in windows_steps:
        command = shlex.split(step.get("run", ""))
        assert not any(argument == test_path or argument.startswith(test_path + "::") for argument in command)


@pytest.mark.parametrize(
    "test_path",
    [
        "tests/web/test_resource_links_frontend.py",
        "tests/web/test_resource_link_popover.py",
        "tests/web/test_resource_references.py",
        "tests/web/test_resource_previews.py",
        "tests/services/test_execution_policy.py",
        "tests/agent/test_agent_loop_new.py",
        "tests/tools/cloud/aliyun/test_aliyun_api.py",
        "tests/tools/cloud/aliyun/test_oss_v4_adapter.py",
    ],
)
def test_windows_keeps_product_resource_links_and_policy_regressions(test_path: str) -> None:
    assert not _ignored(_remaining_tests("test-windows"), test_path)
