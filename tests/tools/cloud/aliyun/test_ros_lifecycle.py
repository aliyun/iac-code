"""Behavior tests for bounded ROS console action-group tools."""

from __future__ import annotations

from typing import Any

import pytest

from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.tools.cloud.aliyun.ros_lifecycle import (
    RosDiagnosticTool,
    RosResourceTypeRegistrationTool,
    RosStackGroupTool,
    RosTagTool,
    RosTemplateScratchTool,
    RosTemplateTool,
)
from iac_code.types.permissions import PermissionResult, ToolPermissionContext

EXPECTED_ACTIONS = {
    RosStackGroupTool: {
        "read": {
            "GetStackGroup",
            "ListStackGroups",
            "GetStackGroupOperation",
            "ListStackGroupOperations",
            "ListStackGroupOperationResults",
        },
        "write": {
            "CreateStackGroup",
            "UpdateStackGroup",
            "DeleteStackGroup",
            "DetectStackGroupDrift",
            "StopStackGroupOperation",
            "ImportStacksToStackGroup",
        },
    },
    RosTemplateTool: {
        "read": {"GetTemplate", "ListTemplates", "ListTemplateVersions"},
        "write": {"CreateTemplate", "UpdateTemplate", "DeleteTemplate", "SetTemplatePermission"},
    },
    RosTemplateScratchTool: {
        "read": {"GetTemplateScratch", "ListTemplateScratches"},
        "write": {
            "CreateTemplateScratch",
            "UpdateTemplateScratch",
            "DeleteTemplateScratch",
            "GenerateTemplateByScratch",
        },
    },
    RosDiagnosticTool: {
        "read": {"GetDiagnostic", "ListDiagnostics"},
        "write": {"CreateDiagnostic", "DeleteDiagnostic"},
    },
    RosResourceTypeRegistrationTool: {
        "read": {
            "GetResourceType",
            "GetResourceTypeTemplate",
            "ListResourceTypes",
            "ListResourceTypeRegistrations",
            "ListResourceTypeVersions",
        },
        "write": {"RegisterResourceType", "DeregisterResourceType", "SetResourceType"},
    },
    RosTagTool: {
        "read": {"ListTagKeys", "ListTagValues", "ListTagResources"},
        "write": {"TagResources", "UntagResources"},
    },
}


class _RecordingExecutor:
    def __init__(self) -> None:
        self.permission_calls: list[tuple[dict[str, Any], ToolPermissionContext]] = []
        self.execution_calls: list[tuple[dict[str, Any], ToolContext]] = []

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        self.permission_calls.append((tool_input, context))
        return PermissionResult(behavior="ask", message="delegated")

    async def execute(self, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        self.execution_calls.append((tool_input, context))
        return ToolResult.success('{"RequestId":"request-id"}')


@pytest.mark.parametrize("tool_class", EXPECTED_ACTIONS)
def test_action_group_exposes_complete_reviewed_read_and_write_api_sets(tool_class: type) -> None:
    expected = EXPECTED_ACTIONS[tool_class]
    tool = tool_class(delegated_executor=_RecordingExecutor())

    assert set(tool.read_actions) == expected["read"]
    assert set(tool.write_actions) == expected["write"]
    assert set(tool.actions) == expected["read"] | expected["write"]
    assert tool.operation_spec.public_tool_name == tool.name
    assert tool.operation_spec.product == "ros"
    assert tool.operation_spec.version == "2019-09-10"
    assert tool.operation_spec.actions == frozenset(tool.actions)
    assert getattr(tool.operation_spec, "write_actions", None) == frozenset(expected["write"])
    assert tool.input_schema["properties"]["action"]["enum"] == list(tool.actions)


@pytest.mark.parametrize("tool_class", EXPECTED_ACTIONS)
def test_action_group_classifies_each_api_for_permission_and_concurrency(tool_class: type) -> None:
    expected = EXPECTED_ACTIONS[tool_class]
    tool = tool_class(delegated_executor=_RecordingExecutor())

    for action in expected["read"]:
        tool_input = {"action": action, "params": {}}
        assert tool.is_read_only(tool_input) is True
        assert tool.is_concurrency_safe(tool_input) is True
        assert tool.is_destructive(tool_input) is False

    for action in expected["write"]:
        tool_input = {"action": action, "params": {}}
        assert tool.is_read_only(tool_input) is False
        assert tool.is_concurrency_safe(tool_input) is False
        assert tool.is_destructive(tool_input) is True


@pytest.mark.asyncio
async def test_action_group_forwards_permission_and_execution_to_shared_runtime() -> None:
    delegated = _RecordingExecutor()
    tool = RosTemplateTool(delegated_executor=delegated)
    tool_input = {
        "action": "UpdateTemplate",
        "params": {"TemplateId": "template-id"},
        "region_id": "cn-hangzhou",
    }
    permission_context = ToolPermissionContext(cwd="/tmp")
    execution_context = ToolContext(cwd="/tmp")

    permission = await tool.check_permissions(tool_input, permission_context)
    result = await tool.execute(tool_input=tool_input, context=execution_context)

    assert permission.behavior == "ask"
    assert result.is_error is False
    assert delegated.permission_calls == [(tool_input, permission_context)]
    assert delegated.execution_calls == [(tool_input, execution_context)]


@pytest.mark.asyncio
async def test_action_group_without_shared_runtime_fails_closed() -> None:
    tool = RosTemplateTool()
    tool_input = {"action": "GetTemplate", "params": {"TemplateId": "template-id"}}

    permission = await tool.check_permissions(tool_input, ToolPermissionContext(cwd="/tmp"))
    result = await tool.execute(tool_input=tool_input, context=ToolContext(cwd="/tmp"))

    expected = public_aliyun_error(
        "aliyun_delegated_executor_required",
        product="ros",
        version="2019-09-10",
        action="GetTemplate",
    )
    assert permission.behavior == "deny"
    assert permission.message == expected
    assert result.is_error is True
    assert result.content == expected


def test_action_group_permission_capabilities_are_operation_scoped() -> None:
    tool = RosDiagnosticTool(delegated_executor=_RecordingExecutor())
    tool_input = {"action": "CreateDiagnostic", "params": {}, "region_id": "cn-shanghai"}

    assert tool.requires_runtime_execution_class is True
    assert tool.uses_operation_scoped_permissions is True
    assert tool.supports_blanket_allow is False
    assert tool.permission_audit_operation(tool_input) == {
        "product": "ros",
        "action": "CreateDiagnostic",
        "region": "cn-shanghai",
    }
