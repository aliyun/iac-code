"""Abstract base class for cloud provider API tools."""

from __future__ import annotations

import json
from abc import abstractmethod
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult


class BaseCloudApi(Tool):
    """Abstract base class for cloud provider API tools.

    Subclasses must implement:
    - provider_name: Identifies the cloud provider (e.g. "ros", "aws")
    - supported_actions: List of valid API action names
    - call_action: Executes the actual API call
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """The cloud provider name (e.g. 'ros')."""
        ...

    @property
    @abstractmethod
    def supported_actions(self) -> list[str]:
        """List of supported API action names."""
        ...

    @abstractmethod
    async def call_action(self, action: str, params: dict, region: str) -> dict:
        """Execute a cloud API action.

        Args:
            action: The action name to call.
            params: Parameters for the action.
            region: The region to call the action in.

        Returns:
            The response dict from the cloud API.
        """
        ...

    @property
    def name(self) -> str:
        return f"{self.provider_name}_api"

    def _get_default_region(self) -> str:
        """Return the configured default region, or empty string if unknown."""
        return ""

    @property
    def input_schema(self) -> dict[str, Any]:
        region_desc = "The region to call the action in."
        default_region = self._get_default_region()
        if default_region:
            region_desc += f" Defaults to '{default_region}'."
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": self.supported_actions,
                    "description": "The API action to call.",
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
        if input is None:
            return False
        action = input.get("action", "")
        return action.startswith(("Get", "List", "Describe", "Query", "Validate"))

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return self.is_read_only(tool_input)

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("CloudAPI")

    def _resolve_region(self, input: dict) -> str:
        return input.get("region_id") or self._get_default_region()

    def _get_action_display_detail(self, input: dict) -> str:
        """Return the key detail to display alongside the action name.

        Defaults to region. Subclasses can override for action-specific display.
        """
        return self._resolve_region(input)

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        action = input.get("action", "")
        detail = self._get_action_display_detail(input)
        parts = [p for p in [action, detail] if p]
        return " ".join(parts) if parts else None

    def get_activity_description(self, input: dict | None = None) -> str | None:
        if input is None:
            return None
        action = input.get("action", "")
        detail = self._get_action_display_detail(input)
        display = f"{action} {detail}" if detail else action
        return _("Calling {action}...").format(action=display)

    def _summarize_success_result(self, action: str, result: dict) -> str:
        """Generate a smart summary of a successful API result.

        Subclasses can override for provider-specific logic.
        """
        return _("Call succeeded")

    @staticmethod
    def _clean_error_message(msg: str) -> str:
        """Strip raw API response data from error messages."""
        # Remove trailing " Response: {...}" from SDK exception strings
        idx = msg.find(" Response: {")
        if idx > 0:
            msg = msg[:idx]
        return msg.strip()

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        if is_error:
            return self._clean_error_message(output)
        if verbose:
            return output.strip()
        action = getattr(self, "_last_action", "")
        result = getattr(self, "_last_result", None)
        if action and result is not None:
            return self._summarize_success_result(action, result)
        lines = output.strip().splitlines()
        return _("Received response ({count} lines)").format(count=len(lines))

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        action = tool_input.get("action", "")
        if action not in self.supported_actions:
            return ToolResult.error(f"Invalid action '{action}'. Supported actions: {self.supported_actions}")

        params = tool_input.get("params") or {}
        region = self._resolve_region(tool_input)

        try:
            result = await self.call_action(action, params, region)
            self._last_action = action
            self._last_result = result
            return ToolResult.success(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            self._last_action = ""
            self._last_result = None
            return ToolResult.error(str(e))
