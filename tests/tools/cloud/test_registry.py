from unittest.mock import MagicMock

from iac_code.tools.base import ToolRegistry
from iac_code.tools.cloud.registry import register_cloud_tools


class TestRegisterCloudTools:
    def test_registers_aliyun_tools_when_configured(self):
        registry = ToolRegistry()
        credentials = MagicMock()
        credentials.has_provider.side_effect = lambda name: name == "aliyun"
        register_cloud_tools(registry, credentials)
        assert registry.get("aliyun_api") is not None
        assert registry.get("ros_stack") is not None
        assert registry.get("ros_stack_instances") is not None

    def test_does_not_register_when_not_configured(self):
        registry = ToolRegistry()
        credentials = MagicMock()
        credentials.has_provider.return_value = False
        register_cloud_tools(registry, credentials)
        assert registry.get("aliyun_api") is None
        assert registry.get("ros_stack") is None
        assert registry.get("ros_stack_instances") is None
