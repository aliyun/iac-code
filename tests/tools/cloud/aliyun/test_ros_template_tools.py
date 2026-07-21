"""Tests for dedicated ROS template tools."""

from __future__ import annotations

import pytest

from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.tools.cloud.aliyun.ros_template_tools import (
    RosEstimateTemplateCostTool,
    RosGetTemplateParameterConstraintsTool,
    RosPreviewTemplateTool,
    RosValidateTemplateTool,
    render_ros_template_tool_result_message,
)
from iac_code.types.permissions import PermissionResult, ToolPermissionContext


def _permission_context(*, deny: dict[str, list[str]] | None = None) -> ToolPermissionContext:
    return ToolPermissionContext(cwd="/tmp", deny_rules=deny or {})


def test_ros_template_tools_do_not_expose_raw_params_or_template_body() -> None:
    for tool in (
        RosValidateTemplateTool(),
        RosGetTemplateParameterConstraintsTool(),
        RosPreviewTemplateTool(),
        RosEstimateTemplateCostTool(),
    ):
        schema = tool.input_schema

        assert schema["additionalProperties"] is False
        assert "params" not in schema["properties"]
        assert "TemplateBody" not in schema["properties"]
        assert "TemplateURL" not in schema["properties"]
        assert "TemplateId" not in schema["properties"]
        assert "TemplateScratchId" not in schema["properties"]

        valid, error = tool.validate_input(
            {
                "template_url": "templates/app.yml",
                "region_id": "cn-hangzhou",
                **({"stack_name": "preview-stack"} if tool.name == "ros_preview_template" else {}),
                **({"parameters": {"ZoneId": "cn-hangzhou-k"}} if tool.name != "ros_validate_template" else {}),
            }
        )
        assert valid, error

        valid, error = tool.validate_input(
            {
                "template_url": "templates/app.yml",
                "region_id": "cn-hangzhou",
                **({"stack_name": "preview-stack"} if tool.name == "ros_preview_template" else {}),
                **({"parameters": {"ZoneId": "cn-hangzhou-k"}} if tool.name != "ros_validate_template" else {}),
                "TemplateBody": "ROSTemplateFormatVersion: '2015-09-01'",
            }
        )
        assert not valid
        assert "Additional properties are not allowed" in error


@pytest.mark.parametrize("template_source", ["TemplateId", "TemplateScratchId"])
def test_ros_template_tools_reject_raw_template_source_fields(template_source: str) -> None:
    for tool in (
        RosValidateTemplateTool(),
        RosGetTemplateParameterConstraintsTool(),
        RosPreviewTemplateTool(),
        RosEstimateTemplateCostTool(),
    ):
        valid, error = tool.validate_input(
            {
                "template_url": "templates/app.yml",
                **({"stack_name": "preview-stack"} if tool.name == "ros_preview_template" else {}),
                **({"parameters": {"ZoneId": "cn-hangzhou-k"}} if tool.name != "ros_validate_template" else {}),
                template_source: "tpl-123",
            }
        )

        assert not valid
        assert "Additional properties are not allowed" in error


def test_ros_template_tools_reject_flat_ros_parameter_keys() -> None:
    tool = RosEstimateTemplateCostTool()

    valid, error = tool.validate_input(
        {
            "template_url": "templates/app.yml",
            "region_id": "cn-hangzhou",
            "parameters": {
                "Parameters.1.ParameterKey": "ZoneId",
                "Parameters.1.ParameterValue": "cn-hangzhou-k",
            },
        }
    )

    assert not valid
    assert "should not be valid" in error or "does not match" in error


def test_ros_template_tools_allow_aliyun_default_region() -> None:
    for tool in (
        RosValidateTemplateTool(),
        RosGetTemplateParameterConstraintsTool(),
        RosPreviewTemplateTool(),
        RosEstimateTemplateCostTool(),
    ):
        assert "region_id" not in tool.input_schema["required"]
        valid, error = tool.validate_input(
            {
                "template_url": "templates/app.yml",
                **({"stack_name": "preview-stack"} if tool.name == "ros_preview_template" else {}),
                **({"parameters": {"ZoneId": "cn-hangzhou-k"}} if tool.name != "ros_validate_template" else {}),
            }
        )
        assert valid, error


def test_ros_template_tools_have_distinct_user_facing_names() -> None:
    assert RosValidateTemplateTool().user_facing_name() == "ROS Validate Template"
    assert RosGetTemplateParameterConstraintsTool().user_facing_name() == "ROS Template Parameters"
    assert RosPreviewTemplateTool().user_facing_name() == "ROS Preview Stack"
    assert RosEstimateTemplateCostTool().user_facing_name() == "ROS Estimate Cost"


class _FakeDelegatedExecutor:
    def __init__(
        self,
        *,
        permission_result: PermissionResult | None = None,
        tool_result: ToolResult | None = None,
    ) -> None:
        self.permission_calls = []
        self.execution_calls = []
        self.permission_result = permission_result or PermissionResult(behavior="allow", execution_class="concurrent")
        self.tool_result = tool_result or ToolResult.success('{"RequestId": "delegated"}')

    async def check_permissions(self, tool_input, context):
        self.permission_calls.append((tool_input, context))
        return self.permission_result

    async def execute(self, tool_input, context):
        self.execution_calls.append((tool_input, context))
        return self.tool_result


@pytest.mark.asyncio
async def test_ros_template_tool_uses_only_injected_delegated_executor() -> None:
    delegated = _FakeDelegatedExecutor()
    tool_input = {"template_url": "templates/app.yml", "region_id": "cn-hangzhou"}
    tool = RosValidateTemplateTool(delegated_executor=delegated)
    permission_context = ToolPermissionContext(cwd="/tmp")
    execution_context = ToolContext(cwd="/tmp", pipeline_mode=True)

    permission = await tool.check_permissions(tool_input, permission_context)
    result = await tool.execute(tool_input=tool_input, context=execution_context)

    assert permission.behavior == "allow"
    assert result.is_error is False
    assert delegated.permission_calls == [(tool_input, permission_context)]
    assert delegated.execution_calls == [(tool_input, execution_context)]


@pytest.mark.asyncio
async def test_no_arg_ros_template_tool_is_discovery_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden_legacy_execute(*args, **kwargs):
        raise AssertionError("no-arg discovery tool must not construct AliyunApi")

    monkeypatch.setattr(AliyunApi, "execute", forbidden_legacy_execute)
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.public_errors._",
        lambda message: "translated:" + message,
    )
    tool = RosValidateTemplateTool()
    result = await tool.execute(
        tool_input={"template_url": "templates/app.yml"},
        context=ToolContext(),
    )
    permission = await tool.check_permissions(
        {"template_url": "templates/app.yml"},
        ToolPermissionContext(cwd="/tmp"),
    )

    expected = public_aliyun_error(
        "aliyun_delegated_executor_required",
        product="ROS",
        action="ValidateTemplate",
    )
    assert result == ToolResult.error(expected)
    assert permission.behavior == "deny"
    assert permission.message == expected
    assert result.content.startswith("translated:")
    assert "aliyun_delegated_executor_required" not in result.content


def test_ros_template_renderer_cannot_reach_legacy_execution_credentials_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("renderer must remain pure-local")

    async def forbidden_async(*_args, **_kwargs):
        forbidden()

    monkeypatch.setattr(AliyunApi, "execute", forbidden_async)
    monkeypatch.setattr(AliyunApi, "call_action", forbidden_async)
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.aliyun_api.CloudCredentials", forbidden)
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", forbidden)

    rendered = render_ros_template_tool_result_message(
        "ros_validate_template",
        '{"RequestId":"REQ-42"}',
    )

    assert rendered == "Call succeeded (RequestId: REQ-42)"


@pytest.mark.asyncio
async def test_ros_template_tool_result_display_matches_aliyun_api() -> None:
    raw_output = '{\n  "RequestId": "REQ-42",\n  "Resources": ["long output"]\n}'
    delegated = _FakeDelegatedExecutor(tool_result=ToolResult.success(raw_output))
    tool = RosValidateTemplateTool(delegated_executor=delegated)

    result = await tool.execute(
        tool_input={"template_url": "templates/app.yml"},
        context=ToolContext(pipeline_mode=True),
    )

    assert tool.render_tool_result_message(result.content) == "Call succeeded (RequestId: REQ-42)"
    assert tool.render_tool_result_message(result.content, verbose=True) == raw_output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_type", "tool_input"),
    [
        (
            RosValidateTemplateTool,
            {"template_url": "templates/app.yml", "region_id": "cn-hangzhou"},
        ),
        (
            RosGetTemplateParameterConstraintsTool,
            {
                "template_url": "templates/app.yml",
                "region_id": "cn-hangzhou",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
        ),
        (
            RosPreviewTemplateTool,
            {
                "template_url": "templates/app.yml",
                "region_id": "cn-hangzhou",
                "stack_name": "preview-stack",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
        ),
        (
            RosEstimateTemplateCostTool,
            {
                "template_url": "templates/app.yml",
                "region_id": "cn-hangzhou",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
        ),
    ],
)
async def test_ros_template_tools_delegate_the_exact_outer_input(
    tool_type,
    tool_input: dict,
) -> None:
    delegated = _FakeDelegatedExecutor()
    tool = tool_type(delegated_executor=delegated)
    context = ToolContext(pipeline_mode=True)
    result = await tool.execute(tool_input=tool_input, context=context)

    assert not result.is_error
    assert delegated.execution_calls == [(tool_input, context)]


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_type", "tool_input"),
    [
        (RosValidateTemplateTool, {"template_url": "templates/app.yml"}),
        (
            RosGetTemplateParameterConstraintsTool,
            {"template_url": "templates/app.yml", "parameters": {"ZoneId": "cn-hangzhou-k"}},
        ),
        (
            RosPreviewTemplateTool,
            {
                "template_url": "templates/app.yml",
                "stack_name": "preview-stack",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
        ),
        (
            RosEstimateTemplateCostTool,
            {"template_url": "templates/app.yml", "parameters": {"ZoneId": "cn-hangzhou-k"}},
        ),
    ],
)
async def test_ros_template_tools_delegate_permissions_without_local_preprocessing(
    tool_type,
    tool_input: dict,
) -> None:
    delegated = _FakeDelegatedExecutor()
    tool = tool_type(delegated_executor=delegated)
    context = _permission_context()
    result = await tool.check_permissions(tool_input, context)

    assert result.behavior == "allow"
    assert delegated.permission_calls == [(tool_input, context)]


@pytest.mark.asyncio
async def test_ros_template_tool_preserves_delegated_permission_result() -> None:
    expected = PermissionResult(behavior="ask", message="delegated_ask", execution_class="serial")
    delegated = _FakeDelegatedExecutor(permission_result=expected)
    tool = RosPreviewTemplateTool(delegated_executor=delegated)
    tool_input = {
        "template_url": "templates/app.yml",
        "stack_name": "preview-stack",
        "parameters": {"ZoneId": "cn-hangzhou-k"},
    }

    result = await tool.check_permissions(tool_input, _permission_context())

    assert result is expected
