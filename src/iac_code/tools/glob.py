"""GlobTool - fast file pattern matching using glob."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.path_safety import check_read_path
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult, ToolPermissionContext
from iac_code.utils.platform import normalize_user_path


def _glob_pattern_may_escape_root(pattern: str) -> bool:
    normalized = normalize_user_path(pattern).replace("\\", "/")
    return os.path.isabs(normalized) or any(part == ".." for part in normalized.split("/"))


def _search_root(path: str, cwd: str) -> Path:
    root = Path(normalize_user_path(path))
    if not root.is_absolute():
        root = Path(cwd) / root
    return root


class GlobTool(Tool):
    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Fast file pattern matching using glob patterns. Searches for files "
            "matching the given pattern and returns matching file paths sorted by "
            "modification time (newest first). Use ** for recursive matching."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against, e.g. '**/*.py' or 'src/**/*.ts'.",
                },
                "path": {
                    "type": "string",
                    "description": "The directory to search in. Defaults to current working directory.",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = tool_input["pattern"]
        path = normalize_user_path(tool_input.get("path", context.cwd))

        search_root = Path(path)
        if not search_root.is_absolute():
            search_root = Path(context.cwd) / path

        if not search_root.exists():
            return ToolResult.error(f"Path not found: {path}")

        if not search_root.is_dir():
            return ToolResult.error(f"Not a directory: {path}")

        try:
            matches = [p for p in search_root.glob(pattern) if p.is_file()]
        except Exception as e:
            return ToolResult.error(f"Error during glob: {e}")

        if not matches:
            return ToolResult.success("No files found")

        # Sort by mtime descending (newest first)
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        # Return relative paths
        relative_paths = [str(p.relative_to(search_root)) for p in matches]
        return ToolResult.success("\n".join(relative_paths))

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        if not isinstance(context, ToolPermissionContext):
            return await super().check_permissions(input, context)

        path = input.get("path", context.cwd)
        decision = check_read_path(
            path,
            cwd=context.cwd,
            additional_directories=context.additional_directories,
            trusted_read_directories=context.trusted_read_directories,
        )
        if decision.behavior == "allow":
            pattern = input.get("pattern", "")
            if _glob_pattern_may_escape_root(pattern):
                detail = _("glob pattern outside allowed directories")
                return PermissionResult(
                    behavior="ask",
                    message=detail,
                    reason=PermissionDecisionReason(type="path_constraint", detail=detail),
                )
            try:
                matches = [p for p in _search_root(path, context.cwd).glob(pattern) if p.is_file()]
            except Exception:
                detail = _("glob pattern outside allowed directories")
                return PermissionResult(
                    behavior="ask",
                    message=detail,
                    reason=PermissionDecisionReason(type="path_constraint", detail=detail),
                )
            for match in matches:
                match_decision = check_read_path(
                    str(match),
                    cwd=context.cwd,
                    additional_directories=context.additional_directories,
                    trusted_read_directories=context.trusted_read_directories,
                )
                if match_decision.behavior == "ask":
                    return match_decision.to_permission_result()
            return PermissionResult(behavior="allow")
        return decision.to_permission_result()

    # UI rendering methods
    def render_tool_use_message(self, input: dict, *, verbose: bool = False):
        pattern = input.get("pattern", "")
        if not pattern:
            return None
        path = input.get("path", "")
        if path:
            return f'pattern: "{pattern}", path: "{path}"'
        return f'pattern: "{pattern}"'

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False):
        if is_error:
            return output
        if output == "No files found":
            return _("Found 0 files")
        lines = output.strip().splitlines()
        count = len(lines)
        summary = _("Found {count} files").format(count=count)
        if verbose:
            return f"{summary}\n" + "\n".join(f"    {line}" for line in lines)
        return summary

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("Search")

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            pattern = input.get("pattern", "")
            return _("Searching {pattern}").format(pattern=pattern)
        return _("Searching files...")

    def is_read_only(self, input: dict | None = None) -> bool:
        return True
