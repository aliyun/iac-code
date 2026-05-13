"""ReadFile tool - reads file contents with optional line range."""

from __future__ import annotations

import os
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult


class ReadFileTool(Tool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. You can optionally specify a line range "
            "to read only a portion of the file. Use start_line and end_line for "
            "large files to read specific sections."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file to read. Can be absolute or relative to working directory.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "The starting line number to read from (1-based, inclusive). Optional.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "The ending line number to read to (1-based, inclusive). Optional.",
                },
            },
            "required": ["path"],
        }

    def normalize_input(self, tool_input: dict[str, Any]) -> None:
        if "file_path" in tool_input and "path" not in tool_input:
            tool_input["path"] = tool_input.pop("file_path")

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        path = tool_input["path"]
        start_line = tool_input.get("start_line")
        end_line = tool_input.get("end_line")

        # Resolve relative paths
        if not os.path.isabs(path):
            path = os.path.join(context.cwd, path)

        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return ToolResult.error(f"File not found: {path}")
        except PermissionError:
            return ToolResult.error(f"Permission denied: {path}")
        except UnicodeDecodeError:
            return ToolResult.error(f"Cannot read binary file: {path}")
        except Exception as e:
            return ToolResult.error(f"Error reading file: {e}")

        total_lines = len(lines)

        # Apply line range if specified
        if start_line is not None or end_line is not None:
            start = (start_line or 1) - 1  # Convert to 0-based
            end = end_line or total_lines
            start = max(0, start)
            end = min(total_lines, end)
            selected = lines[start:end]
            # Add line numbers
            numbered = [f"{i + start + 1:>6}\t{line}" for i, line in enumerate(selected)]
            content = "".join(numbered)
            return ToolResult.success(f"File: {path} (lines {start + 1}-{end} of {total_lines})\n\n{content}")

        # Return full file
        if total_lines > 0:
            numbered = [f"{i + 1:>6}\t{line}" for i, line in enumerate(lines)]
            content = "".join(numbered)
        else:
            content = "(empty file)"

        return ToolResult.success(f"File: {path} ({total_lines} lines)\n\n{content}")

    # UI rendering methods
    def render_tool_use_message(self, input: dict, *, verbose: bool = False):
        path = input.get("path", "")
        if not path:
            return None
        display_path = path if verbose else os.path.basename(path)
        parts = [display_path]
        if input.get("start_line") and input.get("end_line"):
            parts.append(f"lines {input['start_line']}-{input['end_line']}")
        elif input.get("start_line"):
            parts.append(f"from line {input['start_line']}")
        return " · ".join(parts)

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False):
        if is_error:
            return output
        lines = output.splitlines()
        total = len(lines) - 2  # subtract header + blank line
        if total < 0:
            total = 0
        if verbose:
            return output.strip()
        return _("Read {total} lines").format(total=total)

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("Read")

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            return _("Reading {path}").format(path=input.get("path", ""))
        return _("Reading file...")

    def is_read_only(self, input: dict | None = None) -> bool:
        return True
