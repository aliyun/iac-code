"""ROS StackGroup Instances tool for Alibaba Cloud Resource Orchestration Service."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from alibabacloud_ros20190910 import models as ros_models

from iac_code.i18n import _
from iac_code.services.cloud_credentials import CloudCredentials
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory
from iac_code.tools.cloud.types import InstanceStatus
from iac_code.types.stream_events import StackInstancesProgressEvent

SUPPORTED_ACTIONS = [
    "CreateStackInstances",
    "UpdateStackInstances",
    "DeleteStackInstances",
]

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "STOPPED"}
DONE_STATUSES = {"SUCCEEDED", "CURRENT", "FAILED", "STOPPED"}


class RosStackInstances(Tool):
    """Alibaba Cloud ROS StackGroup instances lifecycle tool.

    Manages creating, updating, and deleting stack instances within a stack group,
    with real-time progress polling via operation ID.
    """

    poll_interval: int = 5

    @property
    def timeout(self) -> float | None:
        """Stack instance operations may run for a long time; default to 1 hour."""
        return 3600.0

    @property
    def name(self) -> str:
        return "ros_stack_instances"

    @property
    def supported_actions(self) -> list[str]:
        return SUPPORTED_ACTIONS

    @property
    def description(self) -> str:
        return (
            "Manage Alibaba Cloud ROS (Resource Orchestration Service) StackGroup instances lifecycle. "
            "Supports creating, updating, and deleting stack instances with "
            "real-time progress polling via operation ID."
        )

    def _get_default_region(self) -> str:
        credentials = CloudCredentials()
        cred = credentials.get_provider("aliyun")
        return cred.region_id if cred else ""

    @property
    def input_schema(self) -> dict[str, Any]:
        region_desc = "The region to perform the action in."
        default_region = self._get_default_region()
        if default_region:
            region_desc += f" Defaults to '{default_region}'."
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": self.supported_actions,
                    "description": "The stack instances lifecycle action to perform.",
                },
                "params": {
                    "type": "object",
                    "description": "Parameters to pass to the action.",
                },
                "region_id": {
                    "type": "string",
                    "description": region_desc,
                },
            },
            "required": ["action"],
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return False

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return False

    def is_destructive(self, input: dict | None = None) -> bool:
        return True

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("CloudStackInstances")

    def _resolve_region(self, input: dict) -> str:
        return input.get("region_id") or self._get_default_region()

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        action = input.get("action", "")
        region = self._resolve_region(input)
        parts = [p for p in [action, region] if p]
        return " ".join(parts) if parts else None

    def get_activity_description(self, input: dict | None = None) -> str | None:
        if input is None:
            return None
        action = input.get("action", "")
        region = self._resolve_region(input)
        display = f"{action} {region}" if region else action
        return _("Running {action}...").format(action=display)

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        if verbose:
            return output.strip()
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return output.strip()[:200]
        name = data.get("stack_group_name", "")
        status = data.get("status", "")
        elapsed = data.get("elapsed_seconds", 0)
        return f"{name} {status} ({elapsed}s)"

    def _get_client(self, region: str) -> Any:
        credentials = CloudCredentials()
        cred = credentials.get_provider("aliyun")
        return RosClientFactory.create(cred, region_id=region)

    async def _initiate(self, client: Any, action: str, params: dict) -> str:
        """Start the stack instances operation and return the operation_id."""
        if action == "CreateStackInstances":
            request = ros_models.CreateStackInstancesRequest().from_map(params)
            response = client.create_stack_instances(request)
            return response.body.operation_id
        elif action == "UpdateStackInstances":
            request = ros_models.UpdateStackInstancesRequest().from_map(params)
            response = client.update_stack_instances(request)
            return response.body.operation_id
        elif action == "DeleteStackInstances":
            request = ros_models.DeleteStackInstancesRequest().from_map(params)
            response = client.delete_stack_instances(request)
            return response.body.operation_id
        raise ValueError(f"Unsupported action: {action}")

    async def _get_operation_status(self, client: Any, operation_id: str, region: str) -> str:
        """Poll the current status of a stack group operation."""
        request = ros_models.GetStackGroupOperationRequest(operation_id=operation_id, region_id=region)
        response = client.get_stack_group_operation(request)
        return response.body.to_map().get("Status", "RUNNING")

    async def _get_instances(self, client: Any, stack_group_name: str, region: str) -> list[InstanceStatus]:
        """Get the current list of stack instances for a stack group."""
        request = ros_models.ListStackInstancesRequest(stack_group_name=stack_group_name, region_id=region)
        response = client.list_stack_instances(request)
        data = response.body.to_map()
        instances = []
        for item in data.get("StackInstances", []):
            instances.append(
                InstanceStatus(
                    account_id=item.get("AccountId", ""),
                    region_id=item.get("RegionId", ""),
                    status=item.get("Status", ""),
                    status_reason=item.get("StatusReason", ""),
                    elapsed_seconds=item.get("ElapsedSeconds", 0),
                )
            )
        return instances

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        action = tool_input.get("action", "")
        if action not in self.supported_actions:
            return ToolResult.error(f"Invalid action '{action}'. Supported actions: {self.supported_actions}")

        params = tool_input.get("params") or {}
        region = self._resolve_region(tool_input)
        stack_group_name = params.get("StackGroupName", "")

        client = self._get_client(region)

        try:
            operation_id = await self._initiate(client, action, params)
        except Exception as e:
            return ToolResult.error(f"[{action}] {e}")

        start_time = time.monotonic()

        while True:
            await asyncio.sleep(self.__class__.poll_interval)

            try:
                poll_client = self._get_client(region)
                status = await self._get_operation_status(poll_client, operation_id, region)
            except Exception as e:
                return ToolResult.error(f"[GetStackGroupOperation] {e}")

            try:
                instances = await self._get_instances(poll_client, stack_group_name, region)
            except Exception as e:
                return ToolResult.error(f"[ListStackInstances] {e}")

            elapsed = int(time.monotonic() - start_time)

            # Calculate progress percentage based on done instances
            total_count = len(instances)
            done_count = sum(1 for inst in instances if inst.status in DONE_STATUSES)
            progress_percentage = int(done_count / total_count * 100) if total_count > 0 else 0

            if context.event_queue is not None:
                event = StackInstancesProgressEvent(
                    stack_group_name=stack_group_name,
                    operation_id=operation_id,
                    status=status,
                    progress_percentage=progress_percentage,
                    instances=[
                        {
                            "account_id": inst.account_id,
                            "region_id": inst.region_id,
                            "status": inst.status,
                            "status_reason": inst.status_reason,
                            "elapsed_seconds": inst.elapsed_seconds,
                        }
                        for inst in instances
                    ],
                    elapsed_seconds=elapsed,
                )
                await context.event_queue.put(event)

            if status in TERMINAL_STATUSES:
                is_success = status == "SUCCEEDED"
                result_data = {
                    "stack_group_name": stack_group_name,
                    "operation_id": operation_id,
                    "status": status,
                    "progress_percentage": progress_percentage,
                    "elapsed_seconds": elapsed,
                    "is_success": is_success,
                }
                if is_success:
                    return ToolResult.success(json.dumps(result_data, ensure_ascii=False, indent=2))
                else:
                    return ToolResult.error(json.dumps(result_data, ensure_ascii=False, indent=2))
