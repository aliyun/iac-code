from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.base import ToolRegistry


def register_cloud_tools(registry: "ToolRegistry", credentials: "CloudCredentials") -> None:
    if credentials.has_provider("aliyun"):
        from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
        from iac_code.tools.cloud.aliyun.aliyun_doc_search import AliyunDocSearch
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack
        from iac_code.tools.cloud.aliyun.ros_stack_instances import RosStackInstances

        registry.register(AliyunApi())
        registry.register(AliyunDocSearch())
        registry.register(RosStack())
        registry.register(RosStackInstances())
