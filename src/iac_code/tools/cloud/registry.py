from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.base import ToolRegistry

ALIYUN_TOOL_NAMES = (
    "aliyun_api",
    "aliyun_doc_search",
    "ros_stack",
    "ros_stack_instances",
)


def register_cloud_tools(registry: "ToolRegistry", credentials: "CloudCredentials") -> None:
    for tool_name in ALIYUN_TOOL_NAMES:
        registry.unregister(tool_name)

    if credentials.has_provider("aliyun"):
        from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
        from iac_code.tools.cloud.aliyun.aliyun_doc_search import AliyunDocSearch
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack
        from iac_code.tools.cloud.aliyun.ros_stack_instances import RosStackInstances

        registry.register(AliyunApi())
        registry.register(AliyunDocSearch())
        registry.register(RosStack())
        registry.register(RosStackInstances())
