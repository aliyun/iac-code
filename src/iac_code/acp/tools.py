from __future__ import annotations

import asyncio
from contextlib import suppress

import acp

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

    def is_read_only(self, input: dict | None = None) -> bool:
        return self._original.is_read_only(input)

    def is_destructive(self, input: dict | None = None) -> bool:
        return self._original.is_destructive(input)

    async def check_permissions(self, input: dict, context: dict | None = None):
        return await self._original.check_permissions(input, context)

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        command = tool_input.get("command")
        if not command:
            return ToolResult.error("Bash command is required.")

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
                async with asyncio.timeout(timeout):
                    exit_status = await self._conn.wait_for_terminal_exit(
                        session_id=self._session_id,
                        terminal_id=terminal_id,
                    )
                    output = await self._conn.terminal_output(
                        session_id=self._session_id,
                        terminal_id=terminal_id,
                    )
            except TimeoutError:
                with suppress(Exception):
                    await self._conn.kill_terminal(session_id=self._session_id, terminal_id=terminal_id)
                return ToolResult.error(f"Command timed out after {timeout} seconds")

            if output.exit_status:
                exit_status = output.exit_status

            # TODO: Push TerminalToolCallContent via session_update for streaming output.
            # Currently Tool.execute only returns ToolResult; streaming will be handled
            # at the session layer in a future phase.

            if exit_status.signal:
                return ToolResult.error(f"Command terminated by signal: {exit_status.signal}\n{output.output}")
            if exit_status.exit_code not in (None, 0):
                return ToolResult.error(f"Command failed with exit code {exit_status.exit_code}\n{output.output}")
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
