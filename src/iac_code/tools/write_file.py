"""WriteFile tool - creates or overwrites files."""

from __future__ import annotations

import os
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult


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
                    "description": "The path to write the file to.",
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

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        path = tool_input["path"]
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

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            return _("Writing {path}").format(path=input.get("path", ""))
        return _("Writing file...")

    def is_read_only(self, input: dict | None = None) -> bool:
        return False

    def is_destructive(self, input: dict | None = None) -> bool:
        return True
