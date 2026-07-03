"""Tests for dedicated ROS template tools."""

from __future__ import annotations

import os

import pytest

from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.ros_template_tools import (
    RosEstimateTemplateCostTool,
    RosGetTemplateParameterConstraintsTool,
    RosPreviewTemplateTool,
    RosValidateTemplateTool,
)
from iac_code.types.permissions import ToolPermissionContext


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


@pytest.mark.asyncio
async def test_ros_template_tool_result_display_matches_aliyun_api(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = RosValidateTemplateTool()
    raw_output = '{\n  "RequestId": "REQ-42",\n  "Resources": ["long output"]\n}'

    async def fake_execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        self._last_action = tool_input["action"]
        self._last_result = {"RequestId": "REQ-42", "Resources": ["long output"]}
        return ToolResult.success(raw_output)

    monkeypatch.setattr(AliyunApi, "execute", fake_execute)

    result = await tool.execute(
        tool_input={"template_url": "templates/app.yml"},
        context=ToolContext(pipeline_mode=True),
    )

    assert tool.render_tool_result_message(result.content) == "Call succeeded (RequestId: REQ-42)"
    assert tool.render_tool_result_message(result.content, verbose=True) == raw_output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "tool_input", "expected_action", "expected_params"),
    [
        (
            RosValidateTemplateTool(),
            {"template_url": "templates/app.yml", "region_id": "cn-hangzhou"},
            "ValidateTemplate",
            {"TemplateURL": "templates/app.yml"},
        ),
        (
            RosGetTemplateParameterConstraintsTool(),
            {
                "template_url": "templates/app.yml",
                "region_id": "cn-hangzhou",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
            "GetTemplateParameterConstraints",
            {"TemplateURL": "templates/app.yml", "Parameters": {"ZoneId": "cn-hangzhou-k"}},
        ),
        (
            RosPreviewTemplateTool(),
            {
                "template_url": "templates/app.yml",
                "region_id": "cn-hangzhou",
                "stack_name": "preview-stack",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
            "PreviewStack",
            {
                "TemplateURL": "templates/app.yml",
                "StackName": "preview-stack",
                "Parameters": {"ZoneId": "cn-hangzhou-k"},
            },
        ),
        (
            RosEstimateTemplateCostTool(),
            {
                "template_url": "templates/app.yml",
                "region_id": "cn-hangzhou",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
            "GetTemplateEstimateCost",
            {"TemplateURL": "templates/app.yml", "Parameters": {"ZoneId": "cn-hangzhou-k"}},
        ),
    ],
)
async def test_ros_template_tools_delegate_to_aliyun_api_with_template_url(
    monkeypatch: pytest.MonkeyPatch,
    tool,
    tool_input: dict,
    expected_action: str,
    expected_params: dict,
) -> None:
    captured: dict = {}

    async def fake_execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        captured["tool_input"] = tool_input
        captured["pipeline_mode"] = context.pipeline_mode
        return ToolResult.success('{"ok": true}')

    monkeypatch.setattr(AliyunApi, "execute", fake_execute)

    context = ToolContext(pipeline_mode=True)
    result = await tool.execute(tool_input=tool_input, context=context)

    expected_params = dict(expected_params)
    expected_params["TemplateURL"] = os.path.realpath(os.path.join(context.cwd, expected_params["TemplateURL"]))
    assert not result.is_error
    assert captured["tool_input"] == {
        "product": "ros",
        "action": expected_action,
        "params": expected_params,
        "region_id": "cn-hangzhou",
    }
    assert captured["pipeline_mode"] is False


@pytest.mark.asyncio
async def test_ros_template_tool_delegates_without_region_id_for_aliyun_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        captured["tool_input"] = tool_input
        return ToolResult.success('{"ok": true}')

    monkeypatch.setattr(AliyunApi, "execute", fake_execute)

    result = await RosValidateTemplateTool().execute(
        tool_input={"template_url": "templates/app.yml"},
        context=ToolContext(pipeline_mode=True),
    )

    assert not result.is_error
    assert captured["tool_input"] == {
        "product": "ros",
        "action": "ValidateTemplate",
        "params": {"TemplateURL": os.path.realpath("templates/app.yml")},
    }


@pytest.mark.asyncio
async def test_ros_template_tools_resolve_local_template_url_from_tool_context_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict = {}

    async def fake_execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        captured["tool_input"] = tool_input
        return ToolResult.success('{"ok": true}')

    monkeypatch.setattr(AliyunApi, "execute", fake_execute)

    result = await RosValidateTemplateTool().execute(
        tool_input={"template_url": "templates/app.yml"},
        context=ToolContext(cwd=str(project), pipeline_mode=True),
    )

    assert not result.is_error
    assert captured["tool_input"]["params"] == {
        "TemplateURL": os.path.realpath(project / "templates" / "app.yml"),
    }


@pytest.mark.asyncio
async def test_ros_template_tools_preserve_remote_template_url_when_delegating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict = {}

    async def fake_execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        captured["tool_input"] = tool_input
        return ToolResult.success('{"ok": true}')

    monkeypatch.setattr(AliyunApi, "execute", fake_execute)

    result = await RosValidateTemplateTool().execute(
        tool_input={"template_url": "HTTPS://example.com/template.yml"},
        context=ToolContext(cwd=str(tmp_path), pipeline_mode=True),
    )

    assert not result.is_error
    assert captured["tool_input"]["params"] == {"TemplateURL": "HTTPS://example.com/template.yml"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "tool_input", "expected_action"),
    [
        (RosValidateTemplateTool(), {"template_url": "templates/app.yml"}, "ValidateTemplate"),
        (
            RosGetTemplateParameterConstraintsTool(),
            {"template_url": "templates/app.yml", "parameters": {"ZoneId": "cn-hangzhou-k"}},
            "GetTemplateParameterConstraints",
        ),
        (
            RosPreviewTemplateTool(),
            {
                "template_url": "templates/app.yml",
                "stack_name": "preview-stack",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
            "PreviewStack",
        ),
        (
            RosEstimateTemplateCostTool(),
            {"template_url": "templates/app.yml", "parameters": {"ZoneId": "cn-hangzhou-k"}},
            "GetTemplateEstimateCost",
        ),
    ],
)
async def test_ros_template_tools_delegate_permission_audit_to_aliyun_api(
    tool,
    tool_input: dict,
    expected_action: str,
) -> None:
    result = await tool.check_permissions(tool_input, _permission_context())

    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.is_read_only is True
    assert result.audit.operation["product"] == "ros"
    assert result.audit.operation["action"] == expected_action


@pytest.mark.asyncio
async def test_ros_template_tools_allow_local_template_url_under_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = await RosValidateTemplateTool().check_permissions(
        {"template_url": "templates/app.yml"},
        ToolPermissionContext(cwd=str(project)),
    )

    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.operation["action"] == "ValidateTemplate"


@pytest.mark.asyncio
async def test_ros_template_tools_ask_before_reading_local_template_url_outside_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside_template = tmp_path / "outside.yml"

    result = await RosValidateTemplateTool().check_permissions(
        {"template_url": str(outside_template)},
        ToolPermissionContext(cwd=str(project)),
    )

    assert result.behavior == "ask"
    assert result.reason is not None
    assert result.reason.type == "path_constraint"
    assert result.audit is not None
    assert result.audit.operation["product"] == "ros"
    assert result.audit.operation["action"] == "ValidateTemplate"
    assert result.audit.reason_type == "path_constraint"


@pytest.mark.asyncio
async def test_ros_template_tools_allow_local_template_url_under_additional_directory(tmp_path) -> None:
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    project.mkdir()
    shared.mkdir()

    result = await RosValidateTemplateTool().check_permissions(
        {"template_url": str(shared / "template.yml")},
        ToolPermissionContext(cwd=str(project), additional_directories=[str(shared)]),
    )

    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.operation["action"] == "ValidateTemplate"


@pytest.mark.asyncio
async def test_ros_template_tools_allow_local_template_url_under_trusted_read_directory(tmp_path) -> None:
    project = tmp_path / "project"
    trusted = tmp_path / "trusted"
    project.mkdir()
    trusted.mkdir()

    result = await RosValidateTemplateTool().check_permissions(
        {"template_url": str(trusted / "template.yml")},
        ToolPermissionContext(cwd=str(project), trusted_read_directories=[str(trusted)]),
    )

    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.operation["action"] == "ValidateTemplate"


@pytest.mark.asyncio
async def test_ros_template_tools_ask_before_reading_sensitive_local_template_url(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = await RosValidateTemplateTool().check_permissions(
        {"template_url": ".env"},
        ToolPermissionContext(cwd=str(project)),
    )

    assert result.behavior == "ask"
    assert result.reason is not None
    assert result.reason.type == "safety_check"


@pytest.mark.asyncio
async def test_ros_template_tools_honor_aliyun_api_action_deny_rule() -> None:
    result = await RosPreviewTemplateTool().check_permissions(
        {
            "template_url": "templates/app.yml",
            "stack_name": "preview-stack",
            "parameters": {"ZoneId": "cn-hangzhou-k"},
        },
        _permission_context(deny={"session": ["aliyun_api(ros:PreviewStack)"]}),
    )

    assert result.behavior == "deny"
    assert result.audit is not None
    assert result.audit.rule == "ros:PreviewStack"
