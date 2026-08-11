"""Dedicated ROS console tools backed by the shared Alibaba Cloud runtime."""

from __future__ import annotations

from typing import Any, ClassVar

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.tools.cloud.aliyun.runtime import AliyunActionGroupSpec
from iac_code.types.permissions import PermissionResult, ToolPermissionContext


class RosLifecycleTool(Tool):
    """Base class for one immutable, operation-scoped group of ROS APIs."""

    tool_name: ClassVar[str]
    tool_description: ClassVar[str]
    read_actions: ClassVar[tuple[str, ...]]
    write_actions: ClassVar[tuple[str, ...]]
    actions: ClassVar[tuple[str, ...]]
    operation_spec: ClassVar[AliyunActionGroupSpec]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not all(hasattr(cls, attribute) for attribute in ("tool_name", "read_actions", "write_actions")):
            return
        cls.actions = cls.read_actions + cls.write_actions
        cls.operation_spec = AliyunActionGroupSpec(
            public_tool_name=cls.tool_name,
            product="ros",
            version="2019-09-10",
            actions=frozenset(cls.actions),
            write_actions=frozenset(cls.write_actions),
        )

    def __init__(self, *, delegated_executor: Any | None = None) -> None:
        self._delegated_executor = delegated_executor

    @property
    def name(self) -> str:
        return self.tool_name

    @property
    def description(self) -> str:
        return self.tool_description

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(self.actions)},
                "params": {
                    "type": "object",
                    "description": "ROS API parameters using their public OpenAPI field names.",
                },
                "region_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Alibaba Cloud region ID. Defaults to the configured region.",
                },
            },
            "required": ["action", "params"],
            "additionalProperties": False,
        }

    @property
    def requires_runtime_execution_class(self) -> bool:
        return True

    @property
    def supports_blanket_allow(self) -> bool:
        return False

    @property
    def uses_operation_scoped_permissions(self) -> bool:
        return True

    def _delegated_executor_error(self, tool_input: dict[str, Any]) -> str:
        return public_aliyun_error(
            "aliyun_delegated_executor_required",
            product="ros",
            version=self.operation_spec.version,
            action=tool_input.get("action"),
            region_id=tool_input.get("region_id"),
        )

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        if self._delegated_executor is None:
            return PermissionResult(behavior="deny", message=self._delegated_executor_error(input))
        if not isinstance(context, ToolPermissionContext):
            context = ToolPermissionContext(cwd=context.get("cwd", "") if isinstance(context, dict) else "")
        return await self._delegated_executor.check_permissions(input, context)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        if self._delegated_executor is None:
            return ToolResult.error(self._delegated_executor_error(tool_input))
        return await self._delegated_executor.execute(tool_input, context)

    def is_read_only(self, input: dict | None = None) -> bool:
        return bool(input) and input.get("action") in self.read_actions

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return self.is_read_only(tool_input)

    def is_destructive(self, input: dict | None = None) -> bool:
        return bool(input) and input.get("action") in self.write_actions

    def permission_audit_operation(self, input: dict | None = None) -> dict[str, object]:
        tool_input = input or {}
        return {
            "product": "ros",
            "action": str(tool_input.get("action", "")),
            "region": str(tool_input.get("region_id", "")),
        }

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        del verbose
        action = input.get("action", "")
        region = input.get("region_id", "")
        return " ".join(part for part in (action, region) if isinstance(part, str) and part)

    def get_activity_description(self, input: dict | None = None) -> str | None:
        if input is None:
            return None
        return _("Calling {tool} action {action}...").format(
            tool=self.user_facing_name(input),
            action=input.get("action", ""),
        )

    def streaming_preview_fields(self) -> list[str]:
        return ["action", "region_id"]


class RosStackGroupTool(RosLifecycleTool):
    tool_name = "ros_stack_group"
    tool_description = "Read and manage ROS stack groups with an explicit allowlisted action."
    read_actions = (
        "GetStackGroup",
        "ListStackGroups",
        "GetStackGroupOperation",
        "ListStackGroupOperations",
        "ListStackGroupOperationResults",
    )
    write_actions = (
        "CreateStackGroup",
        "UpdateStackGroup",
        "DeleteStackGroup",
        "DetectStackGroupDrift",
        "StopStackGroupOperation",
        "ImportStacksToStackGroup",
    )

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Stack Group")


class RosTemplateTool(RosLifecycleTool):
    tool_name = "ros_template"
    tool_description = "Read and manage private ROS templates with an explicit allowlisted action."
    read_actions = ("GetTemplate", "ListTemplates", "ListTemplateVersions")
    write_actions = ("CreateTemplate", "UpdateTemplate", "DeleteTemplate", "SetTemplatePermission")

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Template")


class RosTemplateScratchTool(RosLifecycleTool):
    tool_name = "ros_template_scratch"
    tool_description = "Read and manage ROS template scratches with an explicit allowlisted action."
    read_actions = ("GetTemplateScratch", "ListTemplateScratches")
    write_actions = (
        "CreateTemplateScratch",
        "UpdateTemplateScratch",
        "DeleteTemplateScratch",
        "GenerateTemplateByScratch",
    )

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Template Scratch")


class RosDiagnosticTool(RosLifecycleTool):
    tool_name = "ros_diagnostic"
    tool_description = "Read, create, or delete ROS diagnostic reports with an explicit allowlisted action."
    read_actions = ("GetDiagnostic", "ListDiagnostics")
    write_actions = ("CreateDiagnostic", "DeleteDiagnostic")

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Diagnostic")


class RosResourceTypeRegistrationTool(RosLifecycleTool):
    tool_name = "ros_resource_type_registration"
    tool_description = "Read and manage private ROS resource types and modules with an explicit allowlisted action."
    read_actions = (
        "GetResourceType",
        "GetResourceTypeTemplate",
        "ListResourceTypes",
        "ListResourceTypeRegistrations",
        "ListResourceTypeVersions",
    )
    write_actions = ("RegisterResourceType", "DeregisterResourceType", "SetResourceType")

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Resource Type")


class RosTagTool(RosLifecycleTool):
    tool_name = "ros_tag"
    tool_description = "Read or manage ROS resource tags with an explicit allowlisted action."
    read_actions = ("ListTagKeys", "ListTagValues", "ListTagResources")
    write_actions = ("TagResources", "UntagResources")

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Tags")
