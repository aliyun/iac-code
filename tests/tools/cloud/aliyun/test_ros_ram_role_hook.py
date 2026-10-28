"""ROS stack role rejection across the shared API hook and tool entry points."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.api_hooks import run_hooks
from iac_code.tools.cloud.aliyun.hooks.ros_ram_role import reject_ram_role_name
from iac_code.tools.cloud.aliyun.ros_stack import RosStack

_ROLE_ACTIONS = (
    "CreateStack",
    "UpdateStack",
    "ContinueCreateStack",
    "DeleteStack",
    "CreateChangeSet",
    "ListStackOperationRisks",
)


@pytest.mark.parametrize("action", _ROLE_ACTIONS)
@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_awareness_rejects_explicit_ros_stack_role(monkeypatch: pytest.MonkeyPatch, action: str, value: str) -> None:
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", value)
    params = {"RamRoleName": "test-role"}

    result = run_hooks("ros", action, params, read_only=True)

    assert result is not None and result.is_error
    assert "RamRoleName" in result.content
    assert params == {"RamRoleName": "test-role"}


@pytest.mark.parametrize("value", [None, "", "0", "false", "off"])
def test_disabled_awareness_allows_stack_role(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("IAC_CODE_ROS_AGENT_AWARENESS", raising=False)
    else:
        monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", value)

    assert reject_ram_role_name("ros", "CreateStack", {"RamRoleName": "test-role"}) is None


def test_awareness_only_rejects_top_level_role_on_documented_ros_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    assert reject_ram_role_name("ros", "CreateStack", {"TemplateBody": {"RamRoleName": "resource-role"}}) is None
    assert run_hooks("ros", "GetStack", {"RamRoleName": "test-role"}) is None
    assert run_hooks("ecs", "CreateInstance", {"RamRoleName": "instance-role"}) is None


@pytest.mark.asyncio
async def test_ros_stack_blocks_before_client_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    tool = RosStack()
    with patch.object(tool, "_get_client") as get_client:
        result = await tool.execute(
            tool_input={
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-1", "RamRoleName": "test-role"},
            },
            context=ToolContext(),
        )

    assert result.is_error and "RamRoleName" in result.content
    get_client.assert_not_called()


@pytest.mark.asyncio
async def test_ros_stack_pipeline_mode_blocks_before_client_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    tool = RosStack(allow_pipeline_deployment_actions=True)
    with patch.object(tool, "_get_client") as get_client:
        result = await tool.execute(
            tool_input={
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-1", "RamRoleName": "test-role"},
            },
            context=ToolContext(pipeline_mode=True),
        )

    assert result.is_error and "RamRoleName" in result.content
    get_client.assert_not_called()


@pytest.mark.asyncio
async def test_aliyun_api_blocks_before_cloud_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    result = await AliyunApi().execute(
        tool_input={
            "product": "ros",
            "action": "CreateStack",
            "region_id": "cn-hangzhou",
            "params": {"StackName": "demo", "RamRoleName": "test-role"},
        },
        context=ToolContext(),
    )

    assert result.is_error and "RamRoleName" in result.content
