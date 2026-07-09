"""Dedicated ROS template tools with narrow input schemas."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.template_source import (
    check_local_template_url_read_permission,
    is_local_template_url,
    resolve_template_url_for_api,
)
from iac_code.types.permissions import PermissionResult, ToolPermissionContext

_TEMPLATE_URL_PROPERTY = {
    "type": "string",
    "minLength": 1,
    "description": "Local file path, OSS URL, or HTTP(S) URL for the ROS template. Maps to ROS TemplateURL.",
}
_REGION_ID_PROPERTY = {
    "type": "string",
    "minLength": 1,
    "description": "Alibaba Cloud region ID, for example cn-hangzhou. Defaults to the configured region.",
}
_PARAMETERS_PROPERTY = {
    "type": "object",
    "description": (
        "Template parameters as a plain key/value object. Do not use ROS flat Parameters.N.ParameterKey fields."
    ),
    "propertyNames": {"not": {"pattern": r"^Parameters\."}},
}
_TOOL_ACTIONS = {
    "ros_validate_template": "ValidateTemplate",
    "ros_get_template_parameter_constraints": "GetTemplateParameterConstraints",
    "ros_preview_template": "PreviewStack",
    "ros_estimate_template_cost": "GetTemplateEstimateCost",
}


def _schema(
    *,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _copy_delegate_context(context: ToolContext) -> ToolContext:
    return ToolContext(
        cwd=context.cwd,
        event_queue=context.event_queue,
        tool_use_id=context.tool_use_id,
        additional_directories=context.additional_directories,
        trusted_read_directories=context.trusted_read_directories,
        relative_read_directories=context.relative_read_directories,
        strict_read_directories=context.strict_read_directories,
        read_path_violation_behavior=context.read_path_violation_behavior,
        pipeline_mode=False,
        permission_context=context.permission_context,
    )


def _resolve_template_url_for_api(template_url: str, context: ToolContext) -> str:
    return resolve_template_url_for_api(template_url, context)


def render_ros_template_tool_result_message(
    tool_name: str,
    output: str,
    *,
    is_error: bool = False,
    verbose: bool = False,
) -> str | None:
    action = _TOOL_ACTIONS.get(tool_name)
    if action is None:
        return None

    aliyun_api = AliyunApi()
    if not is_error and not verbose:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            aliyun_api._last_action = action
            aliyun_api._last_result = parsed
    return aliyun_api.render_tool_result_message(output, is_error=is_error, verbose=verbose)


class _RosTemplateTool(Tool):
    action: str

    def _aliyun_tool_input(self, *, tool_input: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        aliyun_input: dict[str, Any] = {
            "product": "ros",
            "action": self.action,
        }
        if params is not None:
            aliyun_input["params"] = params
        if region_id := tool_input.get("region_id"):
            aliyun_input["region_id"] = region_id
        return aliyun_input

    async def _call_ros_api(
        self,
        *,
        tool_input: dict[str, Any],
        context: ToolContext,
        params: dict[str, Any],
    ) -> ToolResult:
        params = dict(params)
        template_url = params.get("TemplateURL")
        if isinstance(template_url, str) and template_url:
            params["TemplateURL"] = _resolve_template_url_for_api(template_url, context)
        aliyun_api = AliyunApi()
        result = await aliyun_api.execute(
            tool_input=self._aliyun_tool_input(tool_input=tool_input, params=params),
            context=_copy_delegate_context(context),
        )
        self._last_action = getattr(aliyun_api, "_last_action", "")
        self._last_result = getattr(aliyun_api, "_last_result", None)
        return result

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def _check_local_template_url_read_permission(
        self,
        input: dict,
        context: ToolPermissionContext,
    ) -> PermissionResult | None:
        template_url = input.get("template_url")
        if not isinstance(template_url, str) or not template_url or not is_local_template_url(template_url):
            return None

        return check_local_template_url_read_permission(template_url, context)

    @staticmethod
    def _with_aliyun_audit(path_result: PermissionResult, aliyun_result: PermissionResult) -> PermissionResult:
        if aliyun_result.audit is None:
            return path_result
        reason_type = path_result.reason.type if path_result.reason is not None else None
        reason_detail = path_result.reason.detail if path_result.reason is not None else None
        return PermissionResult(
            behavior=path_result.behavior,
            message=path_result.message,
            reason=path_result.reason,
            suggestions=path_result.suggestions,
            audit=replace(
                aliyun_result.audit,
                scope="once",
                rule_source=None,
                rule=None,
                reason_type=reason_type,
                reason_detail=reason_detail,
            ),
        )

    async def check_permissions(self, input: dict, context=None):
        aliyun_result = await AliyunApi().check_permissions(self._aliyun_tool_input(tool_input=input), context)
        if aliyun_result.behavior == "deny":
            return aliyun_result

        if isinstance(context, ToolPermissionContext):
            if path_result := self._check_local_template_url_read_permission(input, context):
                return self._with_aliyun_audit(path_result, aliyun_result)

        return aliyun_result

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        return input.get("template_url")

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        return render_ros_template_tool_result_message(self.name, output, is_error=is_error, verbose=verbose)

    def get_activity_description(self, input: dict | None = None) -> str | None:
        if input is None:
            return None
        template_url = input.get("template_url", "")
        return _("Calling ROS template tool for {template_url}...").format(template_url=template_url)

    def streaming_preview_fields(self) -> list[str]:
        return ["template_url"]


class RosValidateTemplateTool(_RosTemplateTool):
    action = "ValidateTemplate"

    @property
    def name(self) -> str:
        return "ros_validate_template"

    @property
    def description(self) -> str:
        return "Validate a ROS template by template_url. Use this instead of aliyun_api ValidateTemplate."

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Validate Template")

    @property
    def input_schema(self) -> dict[str, Any]:
        return _schema(
            properties={
                "template_url": _TEMPLATE_URL_PROPERTY,
                "region_id": _REGION_ID_PROPERTY,
            },
            required=["template_url"],
        )

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._call_ros_api(
            tool_input=tool_input,
            context=context,
            params={"TemplateURL": tool_input["template_url"]},
        )


class RosGetTemplateParameterConstraintsTool(_RosTemplateTool):
    action = "GetTemplateParameterConstraints"

    @property
    def name(self) -> str:
        return "ros_get_template_parameter_constraints"

    @property
    def description(self) -> str:
        return (
            "Get ROS template parameter constraints by template_url and optional parameters. "
            "Use this instead of aliyun_api GetTemplateParameterConstraints."
        )

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Template Parameters")

    @property
    def input_schema(self) -> dict[str, Any]:
        return _schema(
            properties={
                "template_url": _TEMPLATE_URL_PROPERTY,
                "region_id": _REGION_ID_PROPERTY,
                "parameters": _PARAMETERS_PROPERTY,
            },
            required=["template_url"],
        )

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        params: dict[str, Any] = {"TemplateURL": tool_input["template_url"]}
        if "parameters" in tool_input:
            params["Parameters"] = dict(tool_input["parameters"])
        return await self._call_ros_api(tool_input=tool_input, context=context, params=params)


class RosPreviewTemplateTool(_RosTemplateTool):
    action = "PreviewStack"

    @property
    def name(self) -> str:
        return "ros_preview_template"

    @property
    def description(self) -> str:
        return (
            "Preview a ROS template stack by template_url, stack_name, and parameters. "
            "Use instead of aliyun_api PreviewStack."
        )

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Preview Stack")

    @property
    def input_schema(self) -> dict[str, Any]:
        return _schema(
            properties={
                "template_url": _TEMPLATE_URL_PROPERTY,
                "region_id": _REGION_ID_PROPERTY,
                "stack_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Unique preview stack name. This is not a template parameter.",
                },
                "parameters": _PARAMETERS_PROPERTY,
            },
            required=["template_url", "stack_name", "parameters"],
        )

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._call_ros_api(
            tool_input=tool_input,
            context=context,
            params={
                "TemplateURL": tool_input["template_url"],
                "StackName": tool_input["stack_name"],
                "Parameters": dict(tool_input["parameters"]),
            },
        )


class RosEstimateTemplateCostTool(_RosTemplateTool):
    action = "GetTemplateEstimateCost"

    @property
    def name(self) -> str:
        return "ros_estimate_template_cost"

    @property
    def description(self) -> str:
        return (
            "Estimate ROS template cost by template_url and parameters. "
            "Use this instead of aliyun_api GetTemplateEstimateCost."
        )

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Estimate Cost")

    @property
    def input_schema(self) -> dict[str, Any]:
        return _schema(
            properties={
                "template_url": _TEMPLATE_URL_PROPERTY,
                "region_id": _REGION_ID_PROPERTY,
                "parameters": _PARAMETERS_PROPERTY,
            },
            required=["template_url", "parameters"],
        )

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._call_ros_api(
            tool_input=tool_input,
            context=context,
            params={
                "TemplateURL": tool_input["template_url"],
                "Parameters": dict(tool_input["parameters"]),
            },
        )
