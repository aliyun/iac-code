"""ReadFile tool - reads file contents with optional line range."""

from __future__ import annotations

import codecs
import os
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.path_safety import _path_is_under, check_read_path, resolve_candidate
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult, ToolPermissionContext

MAX_READ_BYTES = 10 * 1024 * 1024
MAX_READ_LINES = 50_000


def _resolve_input_path(
    path: str,
    cwd: str,
    *,
    relative_read_directories: list[str] | None = None,
) -> str:
    primary = resolve_candidate(path, cwd)
    if os.path.isabs(os.path.expanduser(path)) or os.path.exists(primary):
        return primary

    for root in relative_read_directories or []:
        candidate = resolve_candidate(path, root)
        if _path_is_under(candidate, root) and os.path.exists(candidate):
            return candidate
    return primary


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

    @property
    def supports_blanket_allow(self) -> bool:
        return False

    def normalize_input(self, tool_input: dict[str, Any]) -> None:
        if "file_path" in tool_input and "path" not in tool_input:
            tool_input["path"] = tool_input.pop("file_path")

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        if not isinstance(context, ToolPermissionContext):
            return await super().check_permissions(input, context)

        path = input.get("path") or input.get("file_path")
        if not path:
            message = _("Read file path is required.")
            return PermissionResult(
                behavior="ask",
                message=message,
                reason=PermissionDecisionReason(type="path_constraint", detail=message),
            )

        decision = check_read_path(
            path,
            cwd=context.cwd,
            additional_directories=context.additional_directories,
            trusted_read_directories=context.trusted_read_directories,
        )
        if decision.behavior == "allow":
            return PermissionResult(behavior="allow")
        return decision.to_permission_result()

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_input_path(
            tool_input["path"],
            context.cwd,
            relative_read_directories=context.relative_read_directories,
        )
        start_line = tool_input.get("start_line")
        end_line = tool_input.get("end_line")

        try:
            selected_lines, total_lines, truncated = self._read_limited_lines(path, start_line, end_line)
        except FileNotFoundError:
            return ToolResult.error(f"File not found: {path}")
        except PermissionError:
            return ToolResult.error(f"Permission denied: {path}")
        except UnicodeDecodeError:
            return ToolResult.error(f"Cannot read binary file: {path}")
        except Exception as e:
            return ToolResult.error(f"Error reading file: {e}")

        # Apply line range if specified
        if start_line is not None or end_line is not None:
            start = max(1, start_line or 1)
            end = min(total_lines, end_line or total_lines)
            # Add line numbers
            numbered = [f"{line_number:>6}\t{line}" for line_number, line in selected_lines]
            content = "".join(numbered)
            suffix = ", truncated" if truncated else ""
            return ToolResult.success(f"File: {path} (lines {start}-{end} of {total_lines}{suffix})\n\n{content}")

        # Return full file
        if total_lines > 0:
            numbered = [f"{line_number:>6}\t{line}" for line_number, line in selected_lines]
            content = "".join(numbered)
        else:
            content = "(empty file)"

        suffix = ", truncated" if truncated else ""
        return ToolResult.success(f"File: {path} ({total_lines} lines{suffix})\n\n{content}")

    def _read_limited_lines(
        self,
        path: str,
        start_line: int | None,
        end_line: int | None,
    ) -> tuple[list[tuple[int, str]], int, bool]:
        selected: list[tuple[int, str]] = []
        total_lines = 0
        bytes_read = 0
        truncated = False
        start = max(1, start_line or 1)
        decoder = codecs.getincrementaldecoder("utf-8")()

        with open(path, "rb") as f:
            while True:
                if total_lines >= MAX_READ_LINES:
                    truncated = bool(f.read(1))
                    break

                remaining_bytes = MAX_READ_BYTES - bytes_read
                if remaining_bytes <= 0:
                    truncated = bool(f.read(1))
                    break

                raw_line = f.readline(remaining_bytes + 1)
                if not raw_line:
                    break

                if len(raw_line) > remaining_bytes:
                    raw_line = raw_line[:remaining_bytes]
                    truncated = True

                bytes_read += len(raw_line)
                total_lines += 1
                line = decoder.decode(raw_line, final=False)
                if total_lines >= start and (end_line is None or total_lines <= end_line):
                    selected.append((total_lines, line))

                if truncated:
                    break

            if not truncated:
                remainder = decoder.decode(b"", final=True)
                if remainder and total_lines >= start and (end_line is None or total_lines <= end_line):
                    if selected and selected[-1][0] == total_lines:
                        line_number, line = selected[-1]
                        selected[-1] = (line_number, line + remainder)
                    else:
                        selected.append((total_lines, remainder))

        return selected, total_lines, truncated

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

    def streaming_preview_fields(self) -> list[str]:
        return ["path"]

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            return _("Reading {path}").format(path=input.get("path", ""))
        return _("Reading file...")

    def is_read_only(self, input: dict | None = None) -> bool:
        return True
