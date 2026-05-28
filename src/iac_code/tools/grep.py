"""GrepTool - content search using ripgrep with Python fallback."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from typing import Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.utils.platform import normalize_user_path


def _is_rg_available() -> bool:
    """Check whether ripgrep (rg) is available on the system PATH."""
    return shutil.which("rg") is not None


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
    cmd.append(path)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode(errors="replace"),
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

    for dirpath, _dirnames, filenames in os.walk(path):
        if files_matched >= max_results:
            break
        for filename in sorted(filenames):
            if files_matched >= max_results:
                break
            if glob and not fnmatch.fnmatch(filename, glob):
                continue
            filepath = os.path.join(dirpath, filename)
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
