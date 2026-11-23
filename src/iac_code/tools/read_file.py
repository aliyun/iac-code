"""ReadFile tool - reads file contents with optional line range."""

from __future__ import annotations

import os
from typing import Any

from iac_code.i18n import _
from iac_code.tools import path_safety
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.file_pages import read_text_page
from iac_code.tools.path_safety import check_read_path, knowledge_uri, resolve_read_path
from iac_code.tools.result_storage import RESULT_URI_PREFIX, resolve_result_reference
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult, ToolPermissionContext

MAX_READ_BYTES = 10 * 1024 * 1024
MAX_READ_LINES = 50_000


def _path_is_under(path: str, root: str) -> bool:
    return path_safety._path_is_under(path, root)


def _resolve_input_path(
    path: str,
    cwd: str,
    *,
    relative_read_directories: list[str] | None = None,
) -> str:
    return resolve_read_path(path, cwd, relative_read_directories=relative_read_directories)


class ReadFileTool(Tool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Supports knowledge:// paths for the local Wiki. "
            "Call this tool directly instead of running cat/head/tail through bash. "
            "You can optionally specify a line range "
            "to read only a portion of the file. Use start_line and end_line for "
            "large files to read specific sections. Results are paginated within 20,000 characters. "
            "Use tool-result:// references from any tool output as paths. "
            "Use start_char/end_char (0-based, end exclusive) for long lines. "
            "Repeat the same path and range with the returned cursor to continue without losing content."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, knowledge:// path, or current-session tool-result:// reference.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "The starting line number to read from (1-based, inclusive). Optional.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "The ending line number to read to (1-based, inclusive). Optional.",
                },
                "start_char": {"type": "integer", "minimum": 0},
                "end_char": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 2048, "maximum": 20000},
                "cursor": {"type": "string"},
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

        if path.startswith(RESULT_URI_PREFIX):
            try:
                resolve_result_reference(path, context.tool_result_directory)
            except (ValueError, OSError):
                return PermissionResult(behavior="deny", message="Invalid current-session tool result reference.")
            return PermissionResult(behavior="allow")

        decision = check_read_path(
            path,
            cwd=context.cwd,
            additional_directories=context.additional_directories,
            trusted_read_directories=context.trusted_read_directories,
            relative_read_directories=context.relative_read_directories,
            strict_read_directories=context.strict_read_directories,
            read_path_violation_behavior=context.read_path_violation_behavior,
        )
        if decision.behavior == "allow":
            return PermissionResult(behavior="allow")
        return decision.to_permission_result()

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_path = tool_input["path"]
        try:
            path = (
                resolve_result_reference(raw_path, context.tool_result_directory)
                if raw_path.startswith(RESULT_URI_PREFIX)
                else _resolve_input_path(
                    raw_path,
                    context.cwd,
                    relative_read_directories=context.relative_read_directories,
                )
            )
            from iac_code.a2a.backup import run_sync_fenced

            content, page = await run_sync_fenced(
                read_text_page,
                path,
                raw_path if raw_path.startswith(RESULT_URI_PREFIX) else knowledge_uri(path),
                tool_input,
                max_read_bytes=MAX_READ_BYTES,
                max_read_lines=MAX_READ_LINES,
            )
            return ToolResult(content=content, metadata={"file_page": page})
        except FileNotFoundError:
            return ToolResult.error(f"File not found: {locals().get('path', raw_path)}")
        except PermissionError:
            return ToolResult.error(f"Permission denied: {raw_path}")
        except UnicodeDecodeError:
            return ToolResult.error(f"Cannot read binary file: {raw_path}")
        except (ValueError, OSError, RuntimeError) as error:
            return ToolResult.error(str(error))

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
        body = output.split("\n\n", 1)
        total = len(body[1].splitlines()) if len(body) == 2 else max(0, len(output.splitlines()) - 2)
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
