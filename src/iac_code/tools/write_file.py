"""WriteFile tool - creates or overwrites files."""

from __future__ import annotations

import os
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.path_safety import check_write_path
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult, ToolPermissionContext
from iac_code.utils.platform import normalize_user_path


class WriteFileTool(Tool):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Creates the file if it doesn't exist, or "
            "overwrites it if it does. Creates parent directories as needed. "
            "Use EditFile for making targeted changes to existing files."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "The path to write the file to. "
                        "Always emit this field FIRST in the JSON arguments, before 'content'."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file.",
                },
            },
            "required": ["path", "content"],
        }

    def normalize_input(self, tool_input: dict[str, Any]) -> None:
        if "file_path" in tool_input and "path" not in tool_input:
            tool_input["path"] = tool_input.pop("file_path")

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        if not isinstance(context, ToolPermissionContext):
            return await super().check_permissions(input, context)

        path = input.get("path") or input.get("file_path")
        if not path:
            detail = _("write file path is required")
            return PermissionResult(
                behavior="ask",
                message=detail,
                reason=PermissionDecisionReason(type="path_constraint", detail=detail),
            )

        decision = check_write_path(
            path,
            cwd=context.cwd,
            additional_directories=context.additional_directories,
        )
        if decision.behavior == "ask":
            return decision.to_permission_result()
        return await super().check_permissions(input, context)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        path = normalize_user_path(tool_input["path"])
        content = tool_input["content"]

        if not os.path.isabs(path):
            path = os.path.join(context.cwd, path)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except PermissionError:
            return ToolResult.error(f"Permission denied: {path}")
        except Exception as e:
            return ToolResult.error(f"Error writing file: {e}")

        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult.success(_("Successfully wrote {lines} lines to {path}").format(lines=lines, path=path))

    # UI rendering methods
    def render_tool_use_message(self, input: dict, *, verbose: bool = False):
        path = input.get("path", "")
        if not path:
            return None
        return path

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False):
        # Write results are already short, show same in both modes
        return output

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("Write")

    def streaming_preview_fields(self) -> list[str]:
        return ["path"]

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            return _("Writing {path}").format(path=input.get("path", ""))
        return _("Writing file...")

    def is_read_only(self, input: dict | None = None) -> bool:
        return False

    def is_destructive(self, input: dict | None = None) -> bool:
        return True
