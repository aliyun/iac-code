"""ListFiles tool - lists directory contents."""

from __future__ import annotations

import os
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult


class ListFilesTool(Tool):
    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. Returns file and directory names "
            "with indicators for type (file/directory). Useful for exploring "
            "project structure."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list. Defaults to current working directory.",
                },
            },
            "required": [],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        path = tool_input.get("path", context.cwd)

        if not os.path.isabs(path):
            path = os.path.join(context.cwd, path)

        if not os.path.exists(path):
            return ToolResult.error(f"Path not found: {path}")

        if not os.path.isdir(path):
            return ToolResult.error(f"Not a directory: {path}")

        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return ToolResult.error(f"Permission denied: {path}")
        except Exception as e:
            return ToolResult.error(f"Error listing directory: {e}")

        if not entries:
            return ToolResult.success(f"Directory {path} is empty.")

        lines = []
        for entry in entries:
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path):
                lines.append(f"  {entry}/")
            else:
                size = os.path.getsize(full_path)
                lines.append(f"  {entry} ({_format_size(size)})")

        result = f"Directory: {path}\n\n" + "\n".join(lines)
        return ToolResult.success(result)

    # UI rendering methods
    def render_tool_use_message(self, input: dict, *, verbose: bool = False):
        path = input.get("path", ".")
        return path

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False):
        if is_error:
            return output
        lines = [line for line in output.strip().splitlines() if line.startswith("  ")]
        summary = _("Found {count} items").format(count=len(lines))
        if verbose:
            return f"{summary}\n" + "\n".join(f"    {line.strip()}" for line in lines)
        return summary

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("List")

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            return _("Listing {path}").format(path=input.get("path", "."))
        return _("Listing files...")

    def is_read_only(self, input: dict | None = None) -> bool:
        return True


def _format_size(size: int | float) -> str:
    """Format file size in human readable form."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
