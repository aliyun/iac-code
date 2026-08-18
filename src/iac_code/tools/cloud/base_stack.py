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
from iac_code.types.stream_events import ResourceObservedEvent, StackOperationStartedEvent, StackProgressEvent

STACK_RESULT_METADATA_KEY = "stack_result"
_STACK_OPERATION_METADATA_KEYS = (
    "provider",
    "action",
    "stack_id",
    "stack_name",
    "region_id",
    "error_stage",
)


def stack_result_from_metadata(metadata: Any) -> dict[str, Any] | None:
    """Return the structured terminal stack result carried beside display content."""
    if not isinstance(metadata, dict):
        return None
    result = metadata.get(STACK_RESULT_METADATA_KEY)
    return dict(result) if isinstance(result, dict) else None


def persisted_stack_metadata(metadata: Any) -> dict[str, Any]:
    """Keep only reviewed stack metadata needed by replay and pipeline recovery."""
    if not isinstance(metadata, dict):
        return {}
    persisted: dict[str, Any] = {}
    for key in _STACK_OPERATION_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, str):
            persisted[key] = value
    if result := stack_result_from_metadata(metadata):
        persisted[STACK_RESULT_METADATA_KEY] = result
    return persisted


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

    def permission_audit_operation(self, input: dict | None = None) -> dict[str, object]:
        tool_input = input or {}
        params = tool_input.get("params")
        params = params if isinstance(params, dict) else {}
        operation: dict[str, object] = {
            "product": self.provider_name,
            "action": str(tool_input.get("action") or ""),
            "region": str(tool_input.get("region_id") or params.get("RegionId") or ""),
        }
        stack_name = params.get("StackName")
        stack_id = params.get("StackId")
        if isinstance(stack_name, str) and stack_name:
            operation["stackName"] = stack_name
        if isinstance(stack_id, str) and stack_id:
            operation["stackId"] = stack_id
        return operation

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("CloudStack")

    def _resolve_region(self, input: dict) -> str:
        return input.get("region_id") or self._get_default_region()

    def _call_action_kwargs(self, context: ToolContext) -> dict[str, Any]:
        return {}

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

    def _started_stack_metadata(
        self,
        action: str,
        params: dict,
        region: str,
        stack_id: str,
        *,
        error_stage: str | None = None,
    ) -> dict[str, str]:
        metadata = {
            "provider": self.provider_name,
            "action": action,
            "stack_id": stack_id,
            "region_id": region,
        }
        stack_name = params.get("StackName") or params.get("stack_name")
        if stack_name:
            metadata["stack_name"] = str(stack_name)
        if error_stage:
            metadata["error_stage"] = error_stage
        return metadata

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
            stack_id = await self.call_action(action, params, region, **self._call_action_kwargs(context))
        except Exception as e:
            return ToolResult.error(f"[{action}] {e}")

        if context.event_queue is not None and action == "CreateStack" and stack_id:
            await context.event_queue.put(
                ResourceObservedEvent(
                    provider=self.provider_name,
                    resource_type="stack",
                    resource_id=stack_id,
                    resource_name=str(params.get("StackName") or params.get("stack_name") or ""),
                    region_id=region,
                    action=action,
                    tool_name=self.name,
                    tool_use_id=context.tool_use_id,
                )
            )

        return await self.wait_for_stack_operation(action, params, region, stack_id, context)

    async def wait_for_stack_operation(
        self,
        action: str,
        params: dict,
        region: str,
        stack_id: str,
        context: ToolContext,
    ) -> ToolResult:
        """Poll an already-started stack operation until it reaches a terminal state."""
        start_time = time.monotonic()
        # t0 signal so the web output panel shows *_IN_PROGRESS immediately for non-create
        # actions (delete/update/continue), instead of waiting for the first poll (~POLL_INTERVAL).
        # CreateStack already gets its t0 via ResourceObservedEvent in execute(); this deliberately
        # separate event type is ignored by the a2a translator, so stack_current_changed semantics
        # stay untouched.
        if context.event_queue is not None and stack_id and action != "CreateStack":
            await context.event_queue.put(
                StackOperationStartedEvent(
                    provider=self.provider_name,
                    stack_id=stack_id,
                    stack_name=str(params.get("StackName") or params.get("stack_name") or ""),
                    region_id=region,
                    action=action,
                    tool_name=self.name,
                    tool_use_id=context.tool_use_id,
                )
            )
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
                    return ToolResult(
                        content=f"[GetStackStatus] {e}",
                        is_error=True,
                        metadata=self._started_stack_metadata(
                            action,
                            params,
                            region,
                            stack_id,
                            error_stage="status",
                        ),
                    )

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
                        return ToolResult(
                            content=f"[GetStackResources] {e}",
                            is_error=True,
                            metadata=self._started_stack_metadata(
                                action,
                                params,
                                region,
                                stack_id,
                                error_stage="resources",
                            ),
                        )

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
                        region_id=region,
                        tool_use_id=context.tool_use_id,
                    )
                    await context.event_queue.put(event)

                if self.is_action_terminal(action, status):
                    action_success = self.is_action_success(action, status)
                    result_data: dict[str, object] = {
                        "stack_id": status.stack_id,
                        "stack_name": status.stack_name,
                        "status": status.status,
                        "status_reason": status.status_reason,
                        "progress_percentage": status.progress_percentage,
                        "elapsed_seconds": elapsed,
                        "is_success": action_success,
                    }
                    if action_success and status.outputs:
                        result_data["outputs"] = status.outputs
                    try:
                        self.on_terminal_status(action, params, region, status, resources, elapsed)
                    except Exception:
                        pass
                    metadata: dict[str, Any] = self._started_stack_metadata(
                        action,
                        params,
                        region,
                        status.stack_id,
                    )
                    metadata[STACK_RESULT_METADATA_KEY] = result_data
                    return ToolResult(
                        content=json.dumps(result_data, ensure_ascii=False, indent=2),
                        is_error=not action_success,
                        metadata=metadata,
                    )
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
