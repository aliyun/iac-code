"""EditFile tool - makes targeted edits to files using search and replace."""

from __future__ import annotations

import os
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult


class EditFileTool(Tool):
    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Make targeted edits to a file using search and replace. The old_string must "
            "match exactly one location in the file. For creating new files, use WriteFile instead. "
            "Always read the file first to understand its current content before editing."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file to edit.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact string to search for in the file. Must match exactly one location.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The string to replace old_string with.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    def normalize_input(self, tool_input: dict[str, Any]) -> None:
        if "file_path" in tool_input and "path" not in tool_input:
            tool_input["path"] = tool_input.pop("file_path")

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        path = tool_input["path"]
        old_string = tool_input["old_string"]
        new_string = tool_input["new_string"]

        if not os.path.isabs(path):
            path = os.path.join(context.cwd, path)

        if not os.path.exists(path):
            return ToolResult.error(f"File not found: {path}")

        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult.error(f"Error reading file: {e}")

        # Check for exact match
        count = content.count(old_string)
        if count == 0:
            return ToolResult.error(
                f"old_string not found in {path}. Make sure the string matches exactly, "
                "including whitespace and indentation."
            )
        if count > 1:
            return ToolResult.error(
                f"old_string found {count} times in {path}. It must match exactly once. "
                "Include more surrounding context to make the match unique."
            )

        # Perform the replacement
        new_content = content.replace(old_string, new_string, 1)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return ToolResult.error(f"Error writing file: {e}")

        return ToolResult.success(f"Successfully edited {path}")

    # UI rendering methods
    def render_tool_use_message(self, input: dict, *, verbose: bool = False):
        path = input.get("path", "")
        if not path:
            return None
        if verbose and (input.get("old_string") or input.get("new_string")):
            old = input.get("old_string", "")
            preview = old[:60] + "…" if len(old) > 60 else old
            preview = preview.replace("\n", "↵")
            return f"{path} · {preview}"
        return path

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False):
        if verbose:
            return output
        # Compact: just the success/error summary line
        first_line = output.split("\n", 1)[0] if output else output
        return first_line

    def user_facing_name(self, input: dict | None = None) -> str:
        if input and input.get("old_string") == "":
            return _("Create")
        return _("Update")

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            return _("Editing {path}").format(path=input.get("path", ""))
        return _("Editing file...")

    def is_destructive(self, input: dict | None = None) -> bool:
        return True
