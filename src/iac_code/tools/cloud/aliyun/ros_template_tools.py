"""Dedicated ROS template tools with narrow input schemas."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
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
from iac_code.tools.cloud.aliyun.template_source import (
    is_local_template_url,
    resolve_template_url_for_api,
)
from iac_code.types.permissions import PermissionResult, ToolPermissionContext

_REQUEST_ID_PATTERN = re.compile(r'"?RequestId"?\s*[:=]\s*"?[0-9A-Fa-f-]+"?')


def validation_result_digest(content: str) -> str:
    """Stable short digest identifying a validation failure payload.

    Request-scoped identifiers are stripped so that the same template error
    produces the same digest across repeated remote validation calls.
    """

    normalized = _REQUEST_ID_PATTERN.sub("RequestId=<volatile>", content)
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:8]


@dataclass
class _FailedValidationRecord:
    template_fingerprint: str
    result_digest: str
    failure_count: int
    last_error: str
    blocked_attempts: int = 0


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


def validate_delegated_tool_input(tool_input: Mapping[str, Any], *, action: str) -> bool:
    try:
        jsonschema.validate(instance=dict(tool_input), schema=delegated_input_schema(action))
    except (jsonschema.ValidationError, ValueError):
        return False
    return True


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

    def __init__(self, *, delegated_executor: Any | None = None) -> None:
        super().__init__(delegated_executor=delegated_executor)
        self._failed_validations: dict[tuple[str, str], _FailedValidationRecord] = {}

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

    def _template_fingerprint(
        self,
        tool_input: Mapping[str, Any],
        context: ToolContext,
    ) -> tuple[tuple[str, str], str | None]:
        template_url = tool_input.get("template_url")
        key = (str(template_url), str(tool_input.get("region_id") or ""))
        if not isinstance(template_url, str) or not is_local_template_url(template_url):
            return key, None
        try:
            resolved = resolve_template_url_for_api(template_url, context)
            content = Path(resolved).read_bytes()
        except OSError:
            return key, None
        return key, hashlib.sha256(content).hexdigest()

    def _repeated_failure_result(self, record: _FailedValidationRecord, template_url: str) -> ToolResult:
        message = _(
            "Repeated ROS validation blocked: the template at {template_url} has not changed since the last "
            "failed validation (result_digest={digest}, failed attempts={count}). Calling ros_validate_template "
            "again would return the same error. Diagnose the template syntax root cause from the error below, "
            "fix the template file first, and only revalidate after the file content changed.\n"
            "Last validation error:\n{error}"
        ).format(
            template_url=template_url,
            digest=record.result_digest,
            count=record.failure_count,
            error=record.last_error,
        )
        return ToolResult.error(message)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        if self._delegated_executor is None:
            return await self._call_ros_api(tool_input=tool_input, context=context)
        guard_key, fingerprint = self._template_fingerprint(tool_input, context)
        if fingerprint is not None:
            record = self._failed_validations.get(guard_key)
            if record is not None and record.template_fingerprint == fingerprint:
                record.blocked_attempts += 1
                return self._repeated_failure_result(record, str(tool_input.get("template_url")))
        result = await self._call_ros_api(tool_input=tool_input, context=context)
        if fingerprint is None:
            return result
        if not result.is_error:
            self._failed_validations.pop(guard_key, None)
            return result
        digest = validation_result_digest(result.content)
        previous = self._failed_validations.get(guard_key)
        failure_count = previous.failure_count + 1 if previous is not None and previous.result_digest == digest else 1
        self._failed_validations[guard_key] = _FailedValidationRecord(
            template_fingerprint=fingerprint,
            result_digest=digest,
            failure_count=failure_count,
            last_error=result.content,
        )
        annotation = _(
            "\n\nros_validate_template failure result_digest={digest}. Locate and fix the template syntax root "
            "cause before revalidating; revalidating the unchanged template will be blocked."
        ).format(digest=digest)
        return ToolResult(
            content=result.content + annotation,
            is_error=True,
            new_messages=list(result.new_messages),
            context_modifier=result.context_modifier,
            metadata=result.metadata,
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
