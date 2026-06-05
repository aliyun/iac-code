"""GrepTool - content search using ripgrep with Python fallback."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import sys
from functools import lru_cache
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.path_safety import check_read_path
from iac_code.types.permissions import PermissionResult, ToolPermissionContext
from iac_code.utils.platform import normalize_user_path


def _is_rg_available() -> bool:
    """Check whether ripgrep (rg) is available on the system PATH."""
    return shutil.which("rg") is not None


def _normalize_glob_path(path: str) -> str:
    """Normalize paths for rg-style glob matching."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _matches_path_glob(relative_path: str, glob_pattern: str) -> bool:
    """Match a relative path against the subset of rg glob semantics used by the tool."""
    normalized_path = _normalize_glob_path(relative_path)
    normalized_pattern = _normalize_glob_path(glob_pattern)

    if "/" not in normalized_pattern:
        return fnmatch.fnmatchcase(normalized_path.rsplit("/", 1)[-1], normalized_pattern)

    path_parts = tuple(part for part in normalized_path.split("/") if part)
    pattern_parts = tuple(part for part in normalized_pattern.split("/") if part)

    @lru_cache(maxsize=None)
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


def _glob_uses_path_segments(glob_pattern: str) -> bool:
    return "/" in _normalize_glob_path(glob_pattern)


def _absolutize_rg_path(root: str, rg_path: str) -> str:
    if os.path.isabs(rg_path):
        return rg_path
    if rg_path in {".", ""}:
        return root
    normalized = rg_path.replace("\\", os.sep).replace("/", os.sep)
    if normalized.startswith(f".{os.sep}"):
        normalized = normalized[2:]
    return os.path.join(root, normalized)


def _absolutize_rg_output(stdout: str, root: str, *, output_mode: str) -> str:
    if not stdout:
        return stdout

    lines: list[str] = []
    for line in stdout.splitlines():
        if output_mode == "content":
            filename, sep, rest = line.partition(":")
            if sep:
                lines.append(f"{_absolutize_rg_path(root, filename)}:{rest}")
            else:
                lines.append(line)
        else:
            lines.append(_absolutize_rg_path(root, line))

    trailing_newline = "\n" if stdout.endswith("\n") else ""
    return "\n".join(lines) + trailing_newline


def _normalize_real_path(path: str) -> str:
    normalized = os.path.realpath(path).replace("\\", "/")
    if sys.platform in {"win32", "darwin"}:
        return normalized.lower()
    return normalized


def _path_is_under_real_root(path: str, root: str) -> bool:
    path_r = _normalize_real_path(path)
    root_r = _normalize_real_path(root)
    if path_r == root_r:
        return True
    return path_r.startswith(root_r.rstrip("/") + "/")


async def _run_rg(
    pattern: str,
    path: str,
    *,
    glob: str | None = None,
    case_insensitive: bool = False,
    output_mode: str = "files_with_matches",
    max_results: int = 100,
) -> tuple[int, str, str]:
    """Run ripgrep and return (returncode, stdout, stderr)."""
    cmd: list[str] = ["rg"]
    cwd: str | None = None
    search_path = path
    absolutize_output = False

    if glob and os.path.isdir(path) and _glob_uses_path_segments(glob):
        cwd = path
        search_path = "."
        absolutize_output = True

    if case_insensitive:
        cmd.append("--ignore-case")

    if output_mode == "content":
        cmd.extend(["--line-number", "--with-filename"])
    else:
        cmd.append("--files-with-matches")

    if glob:
        cmd.extend(["--glob", glob])

    # For content mode, --max-count limits matches per file.
    # We apply a total limit by post-processing the output lines.
    if output_mode == "content":
        cmd.extend(["--max-count", str(max_results)])

    cmd.append("--")
    cmd.append(pattern)
    cmd.append(search_path)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode(errors="replace")
    if absolutize_output:
        stdout = _absolutize_rg_output(stdout, path, output_mode=output_mode)

    return (
        proc.returncode or 0,
        stdout,
        stderr_bytes.decode(errors="replace"),
    )


def _python_grep(
    pattern: str,
    path: str,
    *,
    glob: str | None = None,
    case_insensitive: bool = False,
    output_mode: str = "files_with_matches",
    max_results: int = 100,
) -> str:
    """Pure-Python fallback for grep using re + os.walk."""
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        return f"Invalid pattern: {e}"

    results: list[str] = []
    files_matched = 0
    search_root = os.path.realpath(path)

    for dirpath, _dirnames, filenames in os.walk(path):
        if files_matched >= max_results:
            break
        for filename in sorted(filenames):
            if files_matched >= max_results:
                break
            filepath = os.path.join(dirpath, filename)
            relative_path = os.path.relpath(filepath, path)
            if glob and not _matches_path_glob(relative_path, glob):
                continue
            if not _path_is_under_real_root(filepath, search_root):
                continue
            try:
                with open(filepath, encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except (PermissionError, OSError):
                continue

            matched_lines: list[str] = []
            for lineno, line in enumerate(lines, start=1):
                if compiled.search(line):
                    if output_mode == "content":
                        matched_lines.append(f"{filepath}:{lineno}:{line.rstrip()}")
                    else:
                        matched_lines.append(filepath)
                        break  # files_with_matches — one hit is enough

            if matched_lines:
                if output_mode == "content":
                    results.extend(matched_lines)
                else:
                    results.append(filepath)
                    files_matched += 1

    return "\n".join(results)


class GrepTool(Tool):
    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search for a regex pattern across file contents. Uses ripgrep (rg) when "
            "available for speed, otherwise falls back to a pure-Python implementation. "
            "Returns matching file paths or matching lines depending on output_mode."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regular expression pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": ("The directory to search in. Defaults to current working directory."),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Glob pattern to filter files, e.g. '*.py'. "
                        "Only files whose names match this pattern will be searched."
                    ),
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Perform a case-insensitive search. Defaults to false.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content"],
                    "description": (
                        "Controls output format. "
                        "'files_with_matches' (default) returns only the paths of matching files. "
                        "'content' returns each matching line with its file path and line number."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Defaults to 100.",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern: str = tool_input["pattern"]
        path: str = normalize_user_path(tool_input.get("path", context.cwd))
        glob: str | None = tool_input.get("glob")
        case_insensitive: bool = bool(tool_input.get("case_insensitive", False))
        output_mode: str = tool_input.get("output_mode", "files_with_matches")
        max_results: int = int(tool_input.get("max_results", 100))

        # Resolve relative paths against cwd
        if not os.path.isabs(path):
            path = os.path.join(context.cwd, path)

        if not os.path.exists(path):
            return ToolResult.error(f"Path not found: {path}")

        if _is_rg_available():
            returncode, stdout, stderr = await _run_rg(
                pattern,
                path,
                glob=glob,
                case_insensitive=case_insensitive,
                output_mode=output_mode,
                max_results=max_results,
            )
            # rg exits with 1 when no matches found (not an error)
            if returncode > 1:
                return ToolResult.error(f"ripgrep error: {stderr.strip()}")
            output = stdout.strip()
        else:
            output = _python_grep(
                pattern,
                path,
                glob=glob,
                case_insensitive=case_insensitive,
                output_mode=output_mode,
                max_results=max_results,
            ).strip()

        if not output:
            return ToolResult.success("No matches")

        # Enforce max_results on total output lines
        lines = output.splitlines()
        if len(lines) > max_results:
            output = "\n".join(lines[:max_results])

        return ToolResult.success(output)

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        if not isinstance(context, ToolPermissionContext):
            return await super().check_permissions(input, context)

        decision = check_read_path(
            input.get("path", context.cwd),
            cwd=context.cwd,
            additional_directories=context.additional_directories,
            trusted_read_directories=context.trusted_read_directories,
        )
        if decision.behavior == "allow":
            return PermissionResult(behavior="allow")
        return decision.to_permission_result()

    # UI rendering methods
    def render_tool_use_message(self, input: dict, *, verbose: bool = False):
        pattern = input.get("pattern", "")
        if not pattern:
            return None
        parts = [f'pattern: "{pattern}"']
        path = input.get("path", "")
        if path:
            parts.append(f'path: "{path}"')
        return ", ".join(parts)

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False):
        if is_error:
            return output
        if output == "No matches":
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
            return _("Searching for {pattern}").format(pattern=pattern)
        return _("Searching content...")

    def is_read_only(self, input: dict | None = None) -> bool:
        return True
