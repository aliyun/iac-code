"""Abstract base class for cloud provider stack lifecycle tools."""

from __future__ import annotations

import asyncio
import json
import time
from abc import abstractmethod
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.cloud.types import ResourceStatus, StackStatus, translate_status
from iac_code.types.stream_events import StackProgressEvent

POLL_INTERVAL = 5


class BaseCloudStack(Tool):
    """Abstract base class for cloud provider stack lifecycle tools.

    Subclasses must implement:
    - provider_name: Identifies the cloud provider (e.g. "ros")
    - supported_actions: List of valid stack action names
    - call_action: Starts the stack operation and returns the stack_id
    - get_stack_status: Polls the current status of a stack
    - get_stack_resources: Gets the current resource list for a stack
    """

    poll_interval: int = POLL_INTERVAL

    @property
    def timeout(self) -> float | None:
        """Stack operations may run for a long time; default to 1 hour."""
        return 3600.0

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """The cloud provider name (e.g. 'ros')."""
        ...

    @property
    @abstractmethod
    def supported_actions(self) -> list[str]:
        """List of supported stack action names."""
        ...

    @abstractmethod
    async def call_action(self, action: str, params: dict, region: str) -> str:
        """Start a stack operation and return the stack_id.

        Args:
            action: The action name to call.
            params: Parameters for the action.
            region: The region to perform the operation in.

        Returns:
            The stack_id for the created/modified/deleted stack.
        """
        ...

    @abstractmethod
    async def get_stack_status(self, stack_id: str, region: str) -> StackStatus:
        """Poll the current status of a stack.

        Args:
            stack_id: The stack identifier.
            region: The region the stack is in.

        Returns:
            Current StackStatus.
        """
        ...

    @abstractmethod
    async def get_stack_resources(self, stack_id: str, region: str) -> list[ResourceStatus]:
        """Get the current resource list for a stack.

        Args:
            stack_id: The stack identifier.
            region: The region the stack is in.

        Returns:
            List of ResourceStatus objects.
        """
        ...

    @property
    def name(self) -> str:
        return f"{self.provider_name}_stack"

    def _get_default_region(self) -> str:
        """Return the configured default region, or empty string if unknown."""
        return ""

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
                    "description": "The stack lifecycle action to perform.",
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

    def needs_event_queue(self) -> bool:
        return True

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return False

    def is_destructive(self, input: dict | None = None) -> bool:
        return True

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("CloudStack")

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

    def is_action_success(self, action: str, status: StackStatus) -> bool:
        return status.is_success

    def is_action_terminal(self, action: str, status: StackStatus) -> bool:
        return status.is_terminal

    def on_terminal_status(
        self,
        action: str,
        params: dict,
        region: str,
        status: StackStatus,
        resources: list[ResourceStatus],
        elapsed_seconds: int,
    ) -> None:
        return None

    def on_polling_error(
        self,
        action: str,
        params: dict,
        region: str,
        stack_id: str,
        error_stage: str,
        error: Exception,
    ) -> None:
        return None

    def on_polling_cancelled(
        self,
        action: str,
        params: dict,
        region: str,
        stack_id: str,
        elapsed_seconds: int,
    ) -> None:
        return None

    @staticmethod
    def _clean_error_message(msg: str) -> str:
        """Strip raw API response data from error messages."""
        idx = msg.find(" Response: {")
        if idx > 0:
            msg = msg[:idx]
        return msg.strip()

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        if verbose:
            return output.strip()
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            if is_error:
                return self._clean_error_message(output)
            return output.strip()[:200]
        name = data.get("stack_name", "")
        stack_id = data.get("stack_id", "")
        status = translate_status(data.get("status", ""))
        elapsed = data.get("elapsed_seconds", 0)
        label = f"{name}({stack_id})" if stack_id else name
        return f"{label} {status} ({elapsed}s)"

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        action = tool_input.get("action", "")
        if action not in self.supported_actions:
            return ToolResult.error(f"Invalid action '{action}'. Supported actions: {self.supported_actions}")

        params = tool_input.get("params") or {}
        region = self._resolve_region(tool_input)

        try:
            stack_id = await self.call_action(action, params, region)
        except Exception as e:
            return ToolResult.error(f"[{action}] {e}")

        start_time = time.monotonic()

        try:
            while True:
                await asyncio.sleep(self._poll_interval)

                try:
                    status = await self.get_stack_status(stack_id, region)
                except Exception as e:
                    try:
                        self.on_polling_error(action, params, region, stack_id, "status", e)
                    except Exception:
                        pass
                    return ToolResult.error(f"[GetStackStatus] {e}")

                try:
                    resources = await self.get_stack_resources(stack_id, region)
                except Exception as e:
                    if self.is_action_terminal(action, status):
                        resources = []
                    else:
                        try:
                            self.on_polling_error(action, params, region, stack_id, "resources", e)
                        except Exception:
                            pass
                        return ToolResult.error(f"[GetStackResources] {e}")

                elapsed = int(time.monotonic() - start_time)

                if context.event_queue is not None:
                    event = StackProgressEvent(
                        stack_id=status.stack_id,
                        stack_name=status.stack_name,
                        status=status.status,
                        progress_percentage=status.progress_percentage,
                        resources=[
                            {
                                "name": r.name,
                                "resource_type": r.resource_type,
                                "status": r.status,
                                "status_reason": r.status_reason,
                            }
                            for r in resources
                        ],
                        elapsed_seconds=elapsed,
                    )
                    await context.event_queue.put(event)

                if self.is_action_terminal(action, status):
                    action_success = self.is_action_success(action, status)
                    result_data = {
                        "stack_id": status.stack_id,
                        "stack_name": status.stack_name,
                        "status": status.status,
                        "status_reason": status.status_reason,
                        "progress_percentage": status.progress_percentage,
                        "elapsed_seconds": elapsed,
                        "is_success": action_success,
                    }
                    try:
                        self.on_terminal_status(action, params, region, status, resources, elapsed)
                    except Exception:
                        pass
                    if action_success:
                        return ToolResult.success(json.dumps(result_data, ensure_ascii=False, indent=2))
                    else:
                        return ToolResult.error(json.dumps(result_data, ensure_ascii=False, indent=2))
        except (KeyboardInterrupt, asyncio.CancelledError):
            elapsed = int(time.monotonic() - start_time)
            try:
                self.on_polling_cancelled(action, params, region, stack_id, elapsed)
            except Exception:
                pass
            raise

    @property
    def _poll_interval(self) -> float:
        return self.poll_interval
