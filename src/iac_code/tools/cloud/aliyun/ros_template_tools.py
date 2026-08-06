"""Dedicated ROS template tools with narrow input schemas."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

import jsonschema

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.aliyun_api import (
    LOCAL_TEMPLATE_BODY_SENTINEL,
    LOCAL_TEMPLATE_PATH_FIELD,
    AliyunApi,
)
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.tools.cloud.aliyun.template_source import is_local_template_url
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


def build_delegated_call_shape(tool_input: Mapping[str, Any], *, action: str) -> dict[str, Any]:
    """Build the stable API call shape used by ROS permission and execution."""

    params: dict[str, Any] = {}
    template_url = tool_input.get("template_url")
    if template_url is not None:
        if isinstance(template_url, str) and is_local_template_url(template_url):
            params["TemplateBody"] = LOCAL_TEMPLATE_BODY_SENTINEL
        else:
            params["TemplateURL"] = copy.deepcopy(template_url)
    stack_name = tool_input.get("stack_name")
    if stack_name is not None:
        params["StackName"] = copy.deepcopy(stack_name)
    parameters = tool_input.get("parameters")
    if isinstance(parameters, Mapping):
        for index, (key, value) in enumerate(parameters.items(), start=1):
            params["Parameters.{}.ParameterKey".format(index)] = str(key)
            params["Parameters.{}.ParameterValue".format(index)] = copy.deepcopy(value)
    shape: dict[str, Any] = {
        "product": "ros",
        "version": "2019-09-10",
        "action": action,
        "params": params,
    }
    if region_id := tool_input.get("region_id"):
        shape["region_id"] = copy.deepcopy(region_id)
    if isinstance(template_url, str) and is_local_template_url(template_url):
        shape[LOCAL_TEMPLATE_PATH_FIELD] = template_url
    return shape


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


def delegated_input_schema(action: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "template_url": _TEMPLATE_URL_PROPERTY,
        "region_id": _REGION_ID_PROPERTY,
    }
    required = ["template_url"]
    if action == "ValidateTemplate":
        return _schema(properties=properties, required=required)
    if action == "GetTemplateParameterConstraints":
        properties["parameters"] = _PARAMETERS_PROPERTY
        return _schema(properties=properties, required=required)
    if action == "PreviewStack":
        properties.update(
            {
                "stack_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Unique preview stack name. This is not a template parameter.",
                },
                "parameters": _PARAMETERS_PROPERTY,
            }
        )
        return _schema(properties=properties, required=["template_url", "stack_name", "parameters"])
    if action == "GetTemplateEstimateCost":
        properties["parameters"] = _PARAMETERS_PROPERTY
        return _schema(properties=properties, required=["template_url", "parameters"])
    raise ValueError("unsupported_delegated_action")


def delegated_tool_input_error(tool_input: Mapping[str, Any], *, action: str) -> str | None:
    """Return a stable error code naming the offending fields, or None when valid.

    Missing required fields become ``missing_required_parameters:<names>`` so the
    public error message can name them and the caller can fix the input on the
    next call instead of repeating the same failing call.
    """

    try:
        schema = delegated_input_schema(action)
    except ValueError:
        return "invalid_tool_input"
    try:
        jsonschema.validate(instance=dict(tool_input), schema=schema)
    except jsonschema.ValidationError:
        missing = [name for name in schema["required"] if name not in tool_input]
        if missing:
            return "missing_required_parameters:" + ",".join(missing)
        return "invalid_tool_input"
    return None


def validate_delegated_tool_input(tool_input: Mapping[str, Any], *, action: str) -> bool:
    return delegated_tool_input_error(tool_input, action=action) is None


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

    aliyun_api = AliyunApi.isolated_for_tests()
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

    def __init__(self, *, delegated_executor: Any | None = None) -> None:
        self._delegated_executor = delegated_executor

    @property
    def requires_runtime_execution_class(self) -> bool:
        return True

    async def _call_ros_api(
        self,
        *,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if self._delegated_executor is None:
            return ToolResult.error(self._delegated_executor_error())
        result = await self._delegated_executor.execute(tool_input, context)
        if not result.is_error:
            try:
                parsed = json.loads(result.content)
            except (TypeError, ValueError):
                parsed = None
            self._last_action = self.action
            self._last_result = parsed if isinstance(parsed, dict) else None
        return result

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    async def check_permissions(self, input: dict, context=None):
        if self._delegated_executor is None:
            return PermissionResult(behavior="deny", message=self._delegated_executor_error())
        if not isinstance(context, ToolPermissionContext):
            context = ToolPermissionContext(cwd=context.get("cwd", "") if isinstance(context, dict) else "")
        return await self._delegated_executor.check_permissions(input, context)

    def _delegated_executor_error(self) -> str:
        return public_aliyun_error(
            "aliyun_delegated_executor_required",
            product="ROS",
            action=self.action,
        )

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
        return delegated_input_schema(self.action)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._call_ros_api(tool_input=tool_input, context=context)


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
        return delegated_input_schema(self.action)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._call_ros_api(tool_input=tool_input, context=context)


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
        return delegated_input_schema(self.action)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._call_ros_api(tool_input=tool_input, context=context)


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
        return delegated_input_schema(self.action)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._call_ros_api(tool_input=tool_input, context=context)
