from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.base import ToolRegistry
    from iac_code.tools.cloud.aliyun.runtime import AliyunRuntimeServices


ANONYMOUS_ALIYUN_TOOL_NAMES = ("aliyun_doc_search", "aliyun_api_doc")
CREDENTIAL_GATED_ALIYUN_TOOL_NAMES = (
    "aliyun_api",
    "ros_validate_template",
    "ros_get_template_parameter_constraints",
    "ros_preview_template",
    "ros_estimate_template_cost",
    "ros_stack",
    "ros_stack_instances",
    "ros_stack_group",
    "ros_template",
    "ros_template_scratch",
    "ros_diagnostic",
    "ros_resource_type_registration",
    "ros_tag",
)
ALIYUN_TOOL_NAMES = ANONYMOUS_ALIYUN_TOOL_NAMES + CREDENTIAL_GATED_ALIYUN_TOOL_NAMES


def register_cloud_tools(
    registry: ToolRegistry,
    credentials: CloudCredentials,
    services: AliyunRuntimeServices,
) -> None:
    """Refresh credential-gated tools while retaining anonymous document tools."""

    from iac_code.tools.cloud.aliyun.aliyun_api_doc import AliyunApiDoc
    from iac_code.tools.cloud.aliyun.aliyun_doc_search import AliyunDocSearch

    doc_search = registry.get("aliyun_doc_search")
    if doc_search is None:
        registry.register(AliyunDocSearch())
    api_doc = registry.get("aliyun_api_doc")
    if api_doc is None or getattr(api_doc, "_services", None) is not services:
        registry.register(AliyunApiDoc(services))

    for tool_name in CREDENTIAL_GATED_ALIYUN_TOOL_NAMES:
        registry.unregister(tool_name)
    if not credentials.has_provider("aliyun"):
        return

    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
    from iac_code.tools.cloud.aliyun.ros_lifecycle import (
        RosDiagnosticTool,
        RosResourceTypeRegistrationTool,
        RosStackGroupTool,
        RosTagTool,
        RosTemplateScratchTool,
        RosTemplateTool,
    )
    from iac_code.tools.cloud.aliyun.ros_stack import RosStack
    from iac_code.tools.cloud.aliyun.ros_stack_instances import RosStackInstances
    from iac_code.tools.cloud.aliyun.ros_template_tools import (
        RosEstimateTemplateCostTool,
        RosGetTemplateParameterConstraintsTool,
        RosPreviewTemplateTool,
        RosValidateTemplateTool,
    )

    delegated = services.delegated_executor_factory
    action_group = services.action_group_executor_factory
    registry.register(AliyunApi(services=services))
    registry.register(RosValidateTemplateTool(delegated_executor=delegated("ValidateTemplate")))
    registry.register(
        RosGetTemplateParameterConstraintsTool(delegated_executor=delegated("GetTemplateParameterConstraints"))
    )
    registry.register(RosPreviewTemplateTool(delegated_executor=delegated("PreviewStack")))
    registry.register(RosEstimateTemplateCostTool(delegated_executor=delegated("GetTemplateEstimateCost")))
    registry.register(RosStack())
    registry.register(RosStackInstances())
    registry.register(RosStackGroupTool(delegated_executor=action_group(RosStackGroupTool.operation_spec)))
    registry.register(RosTemplateTool(delegated_executor=action_group(RosTemplateTool.operation_spec)))
    registry.register(RosTemplateScratchTool(delegated_executor=action_group(RosTemplateScratchTool.operation_spec)))
    registry.register(RosDiagnosticTool(delegated_executor=action_group(RosDiagnosticTool.operation_spec)))
    registry.register(
        RosResourceTypeRegistrationTool(delegated_executor=action_group(RosResourceTypeRegistrationTool.operation_spec))
    )
    registry.register(RosTagTool(delegated_executor=action_group(RosTagTool.operation_spec)))
