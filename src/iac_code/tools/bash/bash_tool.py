"""Bash tool - executes shell commands."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from iac_code.a2a.backup import run_sync_fenced
from iac_code.desktop.external_env import (
    create_subprocess_exec,
    create_subprocess_shell,
    guarded_shell_command,
    is_desktop_runtime,
    spawn_env_kwargs,
)
from iac_code.i18n import _
from iac_code.tools.bash.result_references import resolve_command_references
from iac_code.tools.result_storage import (
    DEFAULT_MAX_INLINE_CHARS,
    describe_result,
    resolve_result_reference,
    result_digest,
)
from iac_code.utils.platform import PlatformInfo, kill_process_tree

if TYPE_CHECKING:
    from iac_code.types.permissions import PermissionResult
from iac_code.tools.base import Tool, ToolContext, ToolResult


class _PreparedBashInput(dict):
    def __init__(self, value: dict[str, Any], references: list[tuple[str, str]], source_command: str):
        super().__init__(value)
        self.references = tuple(references)
        self.source_command = source_command


class BashTool(Tool):
    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command in the system's default shell. "
            "Use this for running programs, installing packages, running tests, git operations, "
            "and other tasks requiring shell execution. Prefer the `grep`, `glob`, and `read_file` tools "
            "for content searches, file discovery, and file reads when available. "
            "Use tool-result:// references as complete file arguments (unquoted, single-quoted, "
            "or double-quoted); they resolve to the current session's files before permission checks. "
            "For python -c, pass the reference via argv instead of embedding it in program text. "
            "Commands are executed with a timeout of 120 seconds by default."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds. Defaults to 120.",
                },
            },
            "required": ["command"],
        }

    def execution_timeout(self, tool_input: dict[str, Any]) -> float | None:
        return _requested_timeout(tool_input) + 10.0

    def prepare_contextual_input(self, tool_input: dict[str, Any], context: Any) -> dict[str, Any]:
        if isinstance(tool_input, _PreparedBashInput):
            return tool_input
        command = tool_input.get("command", "")
        if not isinstance(command, str):
            return tool_input
        source_command = resolve_command_references(command, getattr(context, "tool_result_directory", None))
        if source_command != command:
            from iac_code.services.execution_policy import check

            binding = getattr(context, "invocation_binding", None)
            source_input = {**tool_input, "command": source_command}
            if error := check("bash", source_input, binding.session_id if binding else "", cwd=context.cwd):
                raise ValueError(error)
        references: list[tuple[str, str]] = []
        resolved = resolve_command_references(
            command,
            getattr(context, "tool_result_directory", None),
            snapshot=True,
            references=references,
        )
        return (
            _PreparedBashInput({**tool_input, "command": resolved}, references, source_command)
            if references
            else tool_input
        )

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        timeout = _requested_timeout(tool_input)

        try:
            prepared = self.prepare_contextual_input(tool_input, context)
            command = prepared["command"]
            if isinstance(prepared, _PreparedBashInput):
                for reference, path in prepared.references:
                    # The private input is never overwritten by our writers.
                    # Check it again on permission-continuation/recovery paths.
                    if ".bash-inputs" in Path(path).parts:
                        if Path(path).name.startswith(".missing-"):
                            if Path(path).exists() or Path(path).is_symlink():
                                raise ValueError("Unexpected file at a missing Bash result input.")
                        elif result_digest(path) != Path(path).stem:
                            raise ValueError("Bash result input has changed.")
            if sys.platform == "win32":
                info = await run_sync_fenced(PlatformInfo.detect) if is_desktop_runtime() else PlatformInfo.detect()
                process = await create_subprocess_exec(
                    info.shell_path,
                    "-c",
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=context.cwd,
                    **spawn_env_kwargs(),
                )
            else:
                guarded = guarded_shell_command(command)
                if guarded is not None:
                    process = await create_subprocess_exec(
                        *guarded,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=context.cwd,
                        start_new_session=True,
                        **spawn_env_kwargs(),
                    )
                else:
                    process = await create_subprocess_shell(
                        command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=context.cwd,
                        start_new_session=True,
                    )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                await kill_process_tree(process)
                try:
                    await asyncio.wait_for(process.communicate(), timeout=5)
                except (asyncio.TimeoutError, OSError):
                    pass
                timeout_message = _("Command timed out after {timeout} seconds: {command}").format(
                    timeout=timeout, command=command
                )
                return ToolResult(
                    content=timeout_message,
                    is_error=True,
                    metadata={"exitCode": 124, "stdout": "", "stderr": timeout_message},
                )
            except asyncio.CancelledError:
                await kill_process_tree(process)
                raise

            stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""
            exit_code = process.returncode if process.returncode is not None else 0
            metadata = {"exitCode": exit_code, "stdout": stdout_str, "stderr": stderr_str}

            if (
                isinstance(prepared, _PreparedBashInput)
                and exit_code == 0
                and not stderr_str
                and len(stdout_str) > DEFAULT_MAX_INLINE_CHARS
            ):
                digest = hashlib.sha256(stdout_str.encode("utf-8")).hexdigest()
                without_newline = (
                    hashlib.sha256(stdout_str[:-1].encode("utf-8")).hexdigest() if stdout_str.endswith("\n") else None
                )
                for reference, path in prepared.references:
                    source_digest = Path(path).stem
                    if Path(path).parent.name == ".bash-inputs" and source_digest in {digest, without_newline}:
                        added_newline = source_digest != digest
                        source_output = stdout_str[:-1] if added_newline else stdout_str
                        newline_note = (
                            "\nStdout is the complete referenced input plus one trailing LF (\\n). "
                            "The original file is unchanged; this note preserves the additional newline."
                            if added_newline
                            else ""
                        )
                        return ToolResult(
                            describe_result(reference, source_output, tool_name="bash")
                            + "\nExit code: 0. The command reproduced its complete input; reuse this reference "
                            "with a focused query instead of reading the whole file again." + newline_note,
                            metadata=metadata,
                        )

            # Build result
            parts = []
            if stdout_str:
                parts.append(f"STDOUT:\n{stdout_str}")
            if stderr_str:
                parts.append(f"STDERR:\n{stderr_str}")
            parts.append(f"Exit code: {exit_code}")

            output = "\n".join(parts)

            if exit_code != 0:
                return ToolResult(content=output, is_error=True, metadata=metadata)

            return ToolResult(content=output, metadata=metadata)

        except Exception as e:
            return ToolResult.error(_("Error executing command: {}").format(e))

    # UI rendering methods
    MAX_COMMAND_DISPLAY_CHARS = 160
    MAX_COMMAND_DISPLAY_LINES = 2
    MAX_OUTPUT_LINES = 20

    def render_tool_use_message(self, input: dict, *, verbose: bool = False):
        cmd = input.get("command", "")
        if not cmd:
            return None
        if not verbose:
            lines = cmd.split("\n")
            if len(lines) > self.MAX_COMMAND_DISPLAY_LINES:
                cmd = "\n".join(lines[: self.MAX_COMMAND_DISPLAY_LINES])
            if len(cmd) > self.MAX_COMMAND_DISPLAY_CHARS:
                cmd = cmd[: self.MAX_COMMAND_DISPLAY_CHARS]
            return cmd.strip() + "…" if cmd != input.get("command", "") else cmd
        return cmd

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False):
        lines = output.strip().splitlines()
        if not verbose and len(lines) > self.MAX_OUTPUT_LINES:
            return (
                "\n".join(lines[: self.MAX_OUTPUT_LINES])
                + "\n... "
                + _("{count} more lines").format(count=len(lines) - self.MAX_OUTPUT_LINES)
            )
        return output.strip()

    def render_tool_use_error_message(self, error: str):
        return error

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("Bash")

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            cmd = input.get("command", "")
            short = cmd[:50] + "..." if len(cmd) > 50 else cmd
            return _("Running {cmd}").format(cmd=short)
        return _("Running command...")

    def get_tool_use_summary(self, input: dict | None = None) -> str | None:
        if input:
            return input.get("command", "")[:80]
        return None

    @property
    def supports_blanket_allow(self) -> bool:
        return False

    def is_read_only(self, input: dict | None = None) -> bool:
        return False

    def is_destructive(self, input: dict | None = None) -> bool:
        return False

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        from iac_code.types.permissions import PermissionResult, ToolPermissionContext

        try:
            input = self.prepare_contextual_input(input, context)
        except (ValueError, OSError) as error:
            return PermissionResult(behavior="deny", message=str(error))
        command = input.get("command", "")
        if not command:
            return PermissionResult(behavior="allow")

        if isinstance(context, ToolPermissionContext):
            from iac_code.tools.bash.permissions import bash_tool_has_permission

            if isinstance(input, _PreparedBashInput):
                from iac_code.tools.path_safety import check_read_path

                source_context = context
                root = getattr(context, "tool_result_directory", None)
                # A valid result reference can select another transcript within
                # the same root session. Grant only those input files when this
                # runtime already trusts its own result directory; strict roots
                # and all command deny/ask rules are still enforced.
                if (
                    root
                    and check_read_path(
                        root,
                        cwd=context.cwd,
                        additional_directories=context.additional_directories,
                        trusted_read_directories=context.trusted_read_directories,
                        strict_read_directories=context.strict_read_directories,
                        read_path_violation_behavior=context.read_path_violation_behavior,
                    ).behavior
                    == "allow"
                ):
                    sources = [
                        resolve_result_reference(reference, root, must_exist=False) for reference, _ in input.references
                    ]
                    source_context = replace(
                        context,
                        trusted_read_directories=[*context.trusted_read_directories, *sources],
                    )
                source_result = await bash_tool_has_permission(input.source_command, source_context)
                actual_result = await bash_tool_has_permission(command, context)
                order = {"deny": 0, "ask": 1, "passthrough": 2, "allow": 3}
                return min([source_result, actual_result], key=lambda result: order[result.behavior])
            return await bash_tool_has_permission(command, context)

        if self.is_read_only(input):
            return PermissionResult(behavior="allow")
        return PermissionResult(behavior="ask", message=_("Allow {}?").format(self.user_facing_name(input)))


def _requested_timeout(tool_input: dict[str, Any]) -> float:
    value = tool_input.get("timeout", 120)
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return 120.0
    return timeout if timeout > 0 else 120.0
