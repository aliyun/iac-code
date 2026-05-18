"""Bash tool - executes shell commands."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from iac_code.i18n import _

if TYPE_CHECKING:
    from iac_code.types.permissions import PermissionResult
from iac_code.tools.base import Tool, ToolContext, ToolResult


class BashTool(Tool):
    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command in the system's default shell. "
            "Use this for running programs, installing packages, searching code, "
            "running tests, git operations, and other system tasks. "
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

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        command = tool_input["command"]
        timeout = tool_input.get("timeout", 120)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return ToolResult.error(
                    _("Command timed out after {timeout} seconds: {command}").format(timeout=timeout, command=command)
                )

            stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""

            # Build result
            parts = []
            if stdout_str:
                parts.append(f"STDOUT:\n{stdout_str}")
            if stderr_str:
                parts.append(f"STDERR:\n{stderr_str}")
            parts.append(f"Exit code: {process.returncode}")

            output = "\n".join(parts)

            if process.returncode != 0:
                return ToolResult.error(output)

            return ToolResult.success(output)

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

        command = input.get("command", "")
        if not command:
            return PermissionResult(behavior="allow")

        if isinstance(context, ToolPermissionContext):
            from iac_code.tools.bash.permissions import bash_tool_has_permission

            return await bash_tool_has_permission(command, context)

        if self.is_read_only(input):
            return PermissionResult(behavior="allow")
        return PermissionResult(behavior="ask", message=_("Allow {}?").format(self.user_facing_name(input)))
