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

from iac_code.a2a.backup import run_sync_fenced
from iac_code.desktop.external_env import create_subprocess_exec, guarded_command, spawn_env_kwargs
from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.file_pages import cursor_binding, decode_cursor, encode_cursor, positive_int
from iac_code.tools.path_safety import (
    check_read_path,
    get_iac_code_application_root,
    get_knowledge_root,
    knowledge_allowed_roots,
    knowledge_uri,
    resolve_read_path_with_source,
)
from iac_code.tools.result_storage import DEFAULT_MAX_INLINE_CHARS, RESULT_URI_PREFIX, resolve_result_reference
from iac_code.types.permissions import PermissionResult, ToolPermissionContext


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


def _path_is_under_any_real_root(path: str, roots: list[str]) -> bool:
    return any(_path_is_under_real_root(path, root) for root in roots if root)


def _contains_directory_symlink(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    if os.path.islink(path):
        return True

    for dirpath, dirnames, _filenames in os.walk(path, followlinks=False):
        for dirname in dirnames:
            child = os.path.join(dirpath, dirname)
            if os.path.islink(child) and os.path.isdir(child):
                return True
    return False


def _effective_allowed_roots(path: str, context: ToolContext) -> list[str]:
    if context.strict_read_directories:
        return list(context.strict_read_directories)
    return [
        path,
        *context.additional_directories,
        *context.trusted_read_directories,
        str(get_iac_code_application_root()),
    ]


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
    cmd: list[str] = ["rg", "--sort", "path"]
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

    proc = await create_subprocess_exec(
        *guarded_command(cmd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        **spawn_env_kwargs(),
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
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
    allowed_roots: list[str] | None = None,
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
    safe_roots = list(allowed_roots) if allowed_roots is not None else [search_root]
    visited_dirs: set[str] = set()

    entries = (
        [(os.path.dirname(path), [], [os.path.basename(path)])]
        if os.path.isfile(path)
        else (os.walk(path, followlinks=True))
    )
    for dirpath, dirnames, filenames in entries:
        if files_matched >= max_results:
            break
        dir_real = os.path.realpath(dirpath)
        if dir_real in visited_dirs:
            dirnames[:] = []
            continue
        visited_dirs.add(dir_real)
        dirnames[:] = sorted(
            dirname for dirname in dirnames if _path_is_under_any_real_root(os.path.join(dirpath, dirname), safe_roots)
        )
        for filename in sorted(filenames):
            if files_matched >= max_results:
                break
            filepath = os.path.join(dirpath, filename)
            relative_path = os.path.relpath(filepath, path)
            if glob and not _matches_path_glob(relative_path, glob):
                continue
            if not _path_is_under_any_real_root(filepath, safe_roots):
                continue
            try:
                with open(filepath, encoding="utf-8", errors="replace", newline="\n") as fh:
                    matched_lines: list[str] = []
                    char_offset = 0
                    for lineno, line in enumerate(fh, start=1):
                        match = compiled.search(line)
                        if match:
                            if output_mode == "positions":
                                for occurrence in compiled.finditer(line):
                                    matched_lines.append(
                                        f"{filepath}:{lineno}: character {char_offset + occurrence.start()}"
                                    )
                                    if len(results) + len(matched_lines) >= max_results:
                                        return "\n".join(results + matched_lines)
                            elif output_mode == "content":
                                matched_lines.append(f"{filepath}:{lineno}:{line.rstrip()}")
                            else:
                                matched_lines.append(filepath)
                                break  # files_with_matches — one hit is enough
                        char_offset += len(line)
                        if len(results) + len(matched_lines) >= max_results:
                            return "\n".join(results + matched_lines)

            except (PermissionError, OSError):
                continue

            if matched_lines:
                if output_mode in {"content", "positions"}:
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
            "Call this tool directly instead of running grep/rg through bash. "
            "Returns matching file paths or matching lines depending on output_mode. "
            "Supports knowledge:// paths and current-session tool-result:// references. "
            "Results have continuation cursors: repeat the same query with cursor to get the next page. "
            "Use positions mode to find line numbers and 0-based character offsets without returning long lines."
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
                    "description": ("Search directory: absolute, relative, or knowledge://. Defaults to cwd."),
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
                    "enum": ["files_with_matches", "content", "positions"],
                    "description": (
                        "Controls output format. "
                        "'files_with_matches' (default) returns only the paths of matching files. "
                        "'content' returns each matching line with its file path and line number."
                    ),
                },
                "cursor": {"type": "string", "description": "Continuation cursor for the same query."},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Defaults to 100.",
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern: str = tool_input["pattern"]
        raw_path: str = tool_input.get("path", context.cwd)
        glob: str | None = tool_input.get("glob")
        case_insensitive: bool = bool(tool_input.get("case_insensitive", False))
        output_mode: str = tool_input.get("output_mode", "files_with_matches")
        try:
            max_results = positive_int(tool_input.get("max_results", 100), "max_results")
        except ValueError as error:
            return ToolResult.error(str(error))

        try:
            effective_path = (
                resolve_result_reference(raw_path, context.tool_result_directory)
                if raw_path.startswith(RESULT_URI_PREFIX)
                else raw_path
            )
            resolution = resolve_read_path_with_source(
                effective_path,
                context.cwd,
                relative_read_directories=context.relative_read_directories,
            )
        except (ValueError, OSError, RuntimeError) as error:
            return ToolResult.error(str(error))
        path = resolution.path

        if not os.path.exists(path):
            return ToolResult.error(f"Path not found: {knowledge_uri(path)}")

        try:
            binding = cursor_binding(
                path,
                [pattern, glob, case_insensitive, output_mode],
                reference=raw_path if raw_path.startswith(RESULT_URI_PREFIX) else None,
            )
            state = decode_cursor(tool_input["cursor"], binding) if tool_input.get("cursor") else {"offset": 0}
            if set(state) != {"offset"}:
                raise ValueError("Invalid search cursor.")
            offset = state["offset"]
        except (ValueError, OSError) as error:
            return ToolResult.error(str(error))
        scan_limit = offset + max_results + 1
        allowed_roots = knowledge_allowed_roots(raw_path, _effective_allowed_roots(path, context))
        if (
            output_mode != "positions"
            and _is_rg_available()
            and not resolution.used_relative_root
            and not _contains_directory_symlink(path)
        ):
            returncode, stdout, stderr = await _run_rg(
                pattern,
                path,
                glob=glob,
                case_insensitive=case_insensitive,
                output_mode=output_mode,
                max_results=scan_limit,
            )
            # rg exits with 1 when no matches found (not an error)
            if returncode > 1:
                return ToolResult.error(f"ripgrep error: {stderr.strip()}")
            output = stdout.strip()
        else:
            # Pure-Python fallback walks the tree and reads files synchronously;
            # offload it so it never starves the shared event loop.
            output = (
                await run_sync_fenced(
                    _python_grep,
                    pattern,
                    path,
                    glob=glob,
                    case_insensitive=case_insensitive,
                    output_mode=output_mode,
                    max_results=scan_limit,
                    allowed_roots=allowed_roots,
                )
            ).strip()

        if not output:
            return ToolResult.success("No matches")
        if output.startswith("Invalid pattern:"):
            return ToolResult.error(output)

        lines = output.splitlines()
        candidates = lines[offset : offset + max_results]
        selected: list[str] = []
        used = 0
        for candidate in candidates:
            if len(candidate) > DEFAULT_MAX_INLINE_CHARS - 2_048:
                # Return a location rather than silently cutting a matching line.
                if raw_path.startswith(RESULT_URI_PREFIX):
                    suffix = candidate.removeprefix(path + ":").split(":", 1)[0]
                    candidate = f"{raw_path}:{suffix}: matching line exceeds page size; read this line with read_file."
                else:
                    match = re.match(r"^(.*?):(\d+):", candidate)
                    candidate = (
                        f"{match[1]}:{match[2]}: matching line exceeds page size; use read_file."
                        if match
                        else candidate
                    )
            if selected and used + len(candidate) + 1 > DEFAULT_MAX_INLINE_CHARS - 2_048:
                break
            selected.append(candidate)
            used += len(candidate) + 1
        next_offset = offset + len(selected)
        more = next_offset < len(lines)
        cursor = encode_cursor(binding, {"offset": next_offset}) if more else None
        output = "\n".join(selected)
        if raw_path.startswith(RESULT_URI_PREFIX):
            output = output.replace(path + ":", raw_path + ":")
            if output_mode == "files_with_matches":
                output = output.replace(path, raw_path)

        prefix = str(get_knowledge_root()) + os.sep
        displayed = []
        path_labels = {}
        for line in output.splitlines():
            if line.startswith(prefix):
                relative, separator, suffix = (
                    line[len(prefix) :].partition(":")
                    if output_mode == "content"
                    else (
                        line[len(prefix) :],
                        "",
                        "",
                    )
                )
                if relative not in path_labels:
                    path_labels[relative] = knowledge_uri(prefix + relative)
                line = path_labels[relative] + separator + suffix
            displayed.append(line)
        content = "\n".join(displayed)
        if more or offset or output_mode == "positions":
            total = f"at least {len(lines)}" if more else str(len(lines))
            content += f"\nMatches this page: {len(selected)}; total matches: {total}; more: {str(more).lower()}"
            if cursor:
                content += f"\nNext cursor: {cursor}"
        return ToolResult(
            content=content,
            metadata={
                "search_page": {
                    "count": len(selected),
                    "has_more": more,
                    "next_cursor": cursor,
                }
            },
        )

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        if not isinstance(context, ToolPermissionContext):
            return await super().check_permissions(input, context)

        if str(input.get("path", "")).startswith(RESULT_URI_PREFIX):
            try:
                resolve_result_reference(input["path"], context.tool_result_directory)
            except (ValueError, OSError):
                return PermissionResult(behavior="deny", message="Invalid current-session tool result reference.")
            return PermissionResult(behavior="allow")
        decision = check_read_path(
            input.get("path", context.cwd),
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
