"""GlobTool - fast file pattern matching using glob."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.path_safety import check_read_path, get_iac_code_application_root, resolve_read_path
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult, ToolPermissionContext
from iac_code.utils.platform import normalize_user_path


def _glob_pattern_may_escape_root(pattern: str) -> bool:
    normalized = normalize_user_path(pattern).replace("\\", "/")
    return os.path.isabs(normalized) or any(part == ".." for part in normalized.split("/"))


def _search_root(path: str, cwd: str, *, relative_read_directories: list[str] | None = None) -> Path:
    root = Path(normalize_user_path(path))
    if not root.is_absolute():
        root = Path(resolve_read_path(str(root), cwd, relative_read_directories=relative_read_directories))
    return root


def _glob_pattern_parts(pattern: str) -> tuple[str, ...]:
    normalized = normalize_user_path(pattern).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return tuple(part for part in normalized.split("/") if part and part != ".")


def _pattern_contains_recursive_glob(pattern: str) -> bool:
    return "**" in _glob_pattern_parts(pattern)


def _path_is_under_real_root(path: str, root: str) -> bool:
    path_r = os.path.realpath(path)
    root_r = os.path.realpath(root)
    if path_r == root_r:
        return True
    return path_r.startswith(root_r.rstrip(os.sep) + os.sep)


def _path_is_under_any_real_root(path: str, roots: list[str]) -> bool:
    return any(_path_is_under_real_root(path, root) for root in roots if root)


def _matches_glob_pattern(relative_path: str, pattern: str) -> bool:
    path_parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part and part != ".")
    pattern_parts = _glob_pattern_parts(pattern)
    if not pattern_parts:
        return False

    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)

        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )

        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], pattern_part)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def _allowed_roots(
    search_root: Path, *, additional_directories: list[str], trusted_read_directories: list[str]
) -> list[str]:
    return [
        str(search_root),
        *additional_directories,
        *trusted_read_directories,
        str(get_iac_code_application_root()),
    ]


def _glob_matches(
    search_root: Path,
    pattern: str,
    *,
    allowed_roots: list[str] | None = None,
) -> tuple[list[Path], list[Path]]:
    if not _pattern_contains_recursive_glob(pattern):
        return [p for p in search_root.glob(pattern) if p.is_file()], []

    matches: list[Path] = []
    unsafe_directories: list[Path] = []
    visited_dirs: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(search_root, followlinks=True):
        dir_real = os.path.realpath(dirpath)
        if dir_real in visited_dirs:
            dirnames[:] = []
            continue
        visited_dirs.add(dir_real)

        safe_dirnames: list[str] = []
        for dirname in sorted(dirnames):
            child = Path(dirpath) / dirname
            if allowed_roots is not None and not _path_is_under_any_real_root(str(child), allowed_roots):
                unsafe_directories.append(child)
                continue
            safe_dirnames.append(dirname)
        dirnames[:] = safe_dirnames

        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            try:
                relative_path = path.relative_to(search_root)
            except ValueError:
                continue
            if _matches_glob_pattern(str(relative_path), pattern) and path.is_file():
                matches.append(path)

    return matches, unsafe_directories


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
        path = tool_input.get("path", context.cwd)

        search_root = _search_root(path, context.cwd, relative_read_directories=context.relative_read_directories)

        if not search_root.exists():
            return ToolResult.error(f"Path not found: {path}")

        if not search_root.is_dir():
            return ToolResult.error(f"Not a directory: {path}")

        try:
            allowed_roots = _allowed_roots(
                search_root,
                additional_directories=context.additional_directories,
                trusted_read_directories=context.trusted_read_directories,
            )
            matches, _ = _glob_matches(search_root, pattern, allowed_roots=allowed_roots)
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
            relative_read_directories=context.relative_read_directories,
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
                search_root = _search_root(
                    path,
                    context.cwd,
                    relative_read_directories=context.relative_read_directories,
                )
                matches, unsafe_directories = _glob_matches(
                    search_root,
                    pattern,
                    allowed_roots=_allowed_roots(
                        search_root,
                        additional_directories=context.additional_directories,
                        trusted_read_directories=context.trusted_read_directories,
                    ),
                )
            except Exception:
                detail = _("glob pattern outside allowed directories")
                return PermissionResult(
                    behavior="ask",
                    message=detail,
                    reason=PermissionDecisionReason(type="path_constraint", detail=detail),
                )
            if unsafe_directories:
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
                    relative_read_directories=context.relative_read_directories,
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
