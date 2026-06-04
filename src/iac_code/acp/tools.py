from __future__ import annotations

import asyncio
from contextlib import suppress

import acp

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult

TERMINAL_TIMEOUT = 300  # 5 minutes default timeout


class ACPTerminalBashTool(Tool):
    def __init__(self, original: Tool, conn: acp.Client, session_id: str) -> None:
        self._original = original
        self._conn = conn
        self._session_id = session_id

    @property
    def name(self) -> str:
        return self._original.name

    @property
    def description(self) -> str:
        return self._original.description

    @property
    def input_schema(self) -> dict:
        return self._original.input_schema

    @property
    def timeout(self) -> float | None:
        return self._original.timeout

    @property
    def supports_blanket_allow(self) -> bool:
        return self._original.supports_blanket_allow

    def user_facing_name(self, input: dict | None = None) -> str:
        return self._original.user_facing_name(input)

    def get_activity_description(self, input: dict | None = None) -> str | None:
        return self._original.get_activity_description(input)

    def get_tool_use_summary(self, input: dict | None = None) -> str | None:
        return self._original.get_tool_use_summary(input)

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        return self._original.render_tool_use_message(input, verbose=verbose)

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        return self._original.render_tool_result_message(output, is_error=is_error, verbose=verbose)

    def render_tool_use_error_message(self, error: str) -> str | None:
        return self._original.render_tool_use_error_message(error)

    def streaming_preview_fields(self) -> list[str]:
        return self._original.streaming_preview_fields()

    def is_read_only(self, input: dict | None = None) -> bool:
        return self._original.is_read_only(input)

    def is_concurrency_safe(self, tool_input: dict) -> bool:
        return self._original.is_concurrency_safe(tool_input)

    def is_destructive(self, input: dict | None = None) -> bool:
        return self._original.is_destructive(input)

    async def check_permissions(self, input: dict, context: dict | None = None):
        return await self._original.check_permissions(input, context)

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        command = tool_input.get("command")
        if not command:
            return ToolResult.error(_("Bash command is required."))

        timeout = tool_input.get("timeout", TERMINAL_TIMEOUT)
        terminal_id: str | None = None
        try:
            created = await self._conn.create_terminal(
                command=command,
                session_id=self._session_id,
                cwd=context.cwd,
                output_byte_limit=50_000,
            )
            terminal_id = created.terminal_id

            try:

                async def _wait_and_fetch():
                    _exit = await self._conn.wait_for_terminal_exit(
                        session_id=self._session_id,
                        terminal_id=terminal_id,
                    )
                    _out = await self._conn.terminal_output(
                        session_id=self._session_id,
                        terminal_id=terminal_id,
                    )
                    return _exit, _out

                exit_status, output = await asyncio.wait_for(_wait_and_fetch(), timeout=timeout)
            except asyncio.TimeoutError:
                with suppress(Exception):
                    await self._conn.kill_terminal(session_id=self._session_id, terminal_id=terminal_id)
                return ToolResult.error(_("Command timed out after {timeout} seconds").format(timeout=timeout))

            if output.exit_status:
                exit_status = output.exit_status

            # TODO: Push TerminalToolCallContent via session_update for streaming output.
            # Currently Tool.execute only returns ToolResult; streaming will be handled
            # at the session layer in a future phase.

            if exit_status.signal:
                return ToolResult.error(
                    _("Command terminated by signal: {signal}").format(signal=exit_status.signal) + "\n" + output.output
                )
            if exit_status.exit_code not in (None, 0):
                return ToolResult.error(
                    _("Command failed with exit code {exit_code}").format(exit_code=exit_status.exit_code)
                    + "\n"
                    + output.output
                )
            return ToolResult.success(output.output)
        except asyncio.CancelledError:
            if terminal_id is not None:
                with suppress(Exception):
                    await self._conn.kill_terminal(session_id=self._session_id, terminal_id=terminal_id)
                with suppress(Exception):
                    await self._conn.release_terminal(session_id=self._session_id, terminal_id=terminal_id)
            raise
        finally:
            if terminal_id is not None:
                with suppress(Exception):
                    await self._conn.release_terminal(session_id=self._session_id, terminal_id=terminal_id)


def replace_bash_with_acp_terminal(tool_registry, client_capabilities, conn: acp.Client, session_id: str) -> set[str]:
    """Replace the local bash tool with an ACP terminal-backed tool.

    Returns the set of tool names that were replaced (whose output is
    streamed via the ACP terminal and should be marked as already displayed).
    """
    if not client_capabilities or not client_capabilities.terminal:
        return set()
    bash = tool_registry.get("bash")
    if bash is None:
        return set()
    tool_registry.register(ACPTerminalBashTool(bash, conn, session_id))
    return {"bash"}
