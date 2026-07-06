from unittest.mock import MagicMock

from iac_code.tools.base import ToolRegistry
from iac_code.tools.cloud.aliyun.ros_template_tools import (
    RosEstimateTemplateCostTool,
    RosGetTemplateParameterConstraintsTool,
    RosPreviewTemplateTool,
    RosValidateTemplateTool,
)
from iac_code.tools.cloud.registry import register_cloud_tools


class TestRegisterCloudTools:
    def test_registers_aliyun_tools_when_configured(self):
        registry = ToolRegistry()
        credentials = MagicMock()
        credentials.has_provider.side_effect = lambda name: name == "aliyun"
        register_cloud_tools(registry, credentials)
        assert registry.get("aliyun_api") is not None
        assert registry.get("aliyun_doc_search") is not None
        assert registry.get("ros_validate_template") is None
        assert registry.get("ros_get_template_parameter_constraints") is None
        assert registry.get("ros_preview_template") is None
        assert registry.get("ros_estimate_template_cost") is None
        assert registry.get("ros_stack") is not None
        assert registry.get("ros_stack_instances") is not None

    def test_does_not_register_when_not_configured(self):
        registry = ToolRegistry()
        credentials = MagicMock()
        credentials.has_provider.return_value = False
        register_cloud_tools(registry, credentials)
        assert registry.get("aliyun_api") is None
        assert registry.get("aliyun_doc_search") is None
        assert registry.get("ros_validate_template") is None
        assert registry.get("ros_get_template_parameter_constraints") is None
        assert registry.get("ros_preview_template") is None
        assert registry.get("ros_estimate_template_cost") is None
        assert registry.get("ros_stack") is None
        assert registry.get("ros_stack_instances") is None

    def test_removes_stale_pipeline_only_ros_template_tools(self):
        registry = ToolRegistry()
        registry.register(RosValidateTemplateTool())
        registry.register(RosGetTemplateParameterConstraintsTool())
        registry.register(RosPreviewTemplateTool())
        registry.register(RosEstimateTemplateCostTool())
        credentials = MagicMock()
        credentials.has_provider.side_effect = lambda name: name == "aliyun"

        register_cloud_tools(registry, credentials)

        assert registry.get("aliyun_api") is not None
        assert registry.get("aliyun_doc_search") is not None
        assert registry.get("ros_validate_template") is None
        assert registry.get("ros_get_template_parameter_constraints") is None
        assert registry.get("ros_preview_template") is None
        assert registry.get("ros_estimate_template_cost") is None
        assert registry.get("ros_stack") is not None
        assert registry.get("ros_stack_instances") is not None

    def test_removes_stale_aliyun_tools_when_credentials_become_unavailable(self):
        registry = ToolRegistry()
        credentials = MagicMock()
        credentials.has_provider.side_effect = [True, False]

        register_cloud_tools(registry, credentials)
        assert registry.get("aliyun_api") is not None
        assert registry.get("aliyun_doc_search") is not None
        assert registry.get("ros_validate_template") is None
        assert registry.get("ros_get_template_parameter_constraints") is None
        assert registry.get("ros_preview_template") is None
        assert registry.get("ros_estimate_template_cost") is None
        assert registry.get("ros_stack") is not None
        assert registry.get("ros_stack_instances") is not None

        register_cloud_tools(registry, credentials)

        assert registry.get("aliyun_api") is None
        assert registry.get("aliyun_doc_search") is None
        assert registry.get("ros_validate_template") is None
        assert registry.get("ros_get_template_parameter_constraints") is None
        assert registry.get("ros_preview_template") is None
        assert registry.get("ros_estimate_template_cost") is None
        assert registry.get("ros_stack") is None
        assert registry.get("ros_stack_instances") is None
