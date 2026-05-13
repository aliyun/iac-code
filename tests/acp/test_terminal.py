from __future__ import annotations

import asyncio

import acp
import pytest

from iac_code.acp.tools import TERMINAL_TIMEOUT, ACPTerminalBashTool, replace_bash_with_acp_terminal
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class OriginalBash(Tool):
    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Run bash commands"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    def is_read_only(self, input: dict | None = None) -> bool:
        return False

    def is_destructive(self, input: dict | None = None) -> bool:
        return True

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        return ToolResult.success("local")


class FakeConn:
    """Configurable fake ACP client connection."""

    def __init__(
        self,
        *,
        exit_code: int | None = 0,
        signal: str | None = None,
        output: str = "ok",
        truncated: bool = False,
        create_error: Exception | None = None,
        wait_error: Exception | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.signal = signal
        self.output = output
        self.truncated = truncated
        self.create_error = create_error
        self.wait_error = wait_error
        self.released = False
        self.killed = False

    async def create_terminal(self, **kwargs):
        if self.create_error:
            raise self.create_error
        return acp.schema.CreateTerminalResponse(terminal_id="term1")

    async def wait_for_terminal_exit(self, **kwargs):
        if self.wait_error:
            raise self.wait_error
        return acp.schema.WaitForTerminalExitResponse(exit_code=self.exit_code, signal=self.signal)

    async def terminal_output(self, **kwargs):
        return acp.schema.TerminalOutputResponse(output=self.output, truncated=self.truncated, exit_status=None)

    async def release_terminal(self, **kwargs):
        self.released = True

    async def kill_terminal(self, **kwargs):
        self.killed = True


# ---------------------------------------------------------------------------
# Command execution failure – non-zero exit code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_command_nonzero_exit_code() -> None:
    conn = FakeConn(exit_code=1, output="error output")
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    result = await tool.execute(tool_input={"command": "false"}, context=ToolContext(cwd="/tmp"))

    assert result.is_error is True
    assert "exit code 1" in result.content
    assert "error output" in result.content
    assert conn.released is True


# ---------------------------------------------------------------------------
# Command killed by signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_command_killed_by_signal() -> None:
    conn = FakeConn(signal="SIGKILL", output="partial")
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    result = await tool.execute(tool_input={"command": "sleep 999"}, context=ToolContext(cwd="/tmp"))

    assert result.is_error is True
    assert "SIGKILL" in result.content
    assert conn.released is True


# ---------------------------------------------------------------------------
# Terminal creation failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_creation_failure() -> None:
    conn = FakeConn(create_error=RuntimeError("connection lost"))
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    with pytest.raises(RuntimeError, match="connection lost"):
        await tool.execute(tool_input={"command": "echo hi"}, context=ToolContext(cwd="/tmp"))

    # release_terminal should NOT be called because terminal_id was never set
    assert conn.released is False


# ---------------------------------------------------------------------------
# Missing command returns error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_missing_command() -> None:
    conn = FakeConn()
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    result = await tool.execute(tool_input={}, context=ToolContext(cwd="/tmp"))

    assert result.is_error is True
    assert "required" in result.content.lower()


# ---------------------------------------------------------------------------
# No terminal capability keeps original bash
# ---------------------------------------------------------------------------


def test_no_terminal_capability_keeps_original_bash() -> None:
    registry = ToolRegistry()
    registry.register(OriginalBash())

    # No terminal capability
    class NoTermCaps:
        terminal = None

    replace_bash_with_acp_terminal(registry, NoTermCaps(), conn=FakeConn(), session_id="s1")
    assert isinstance(registry.get("bash"), OriginalBash)


def test_none_capabilities_keeps_original_bash() -> None:
    registry = ToolRegistry()
    registry.register(OriginalBash())

    replace_bash_with_acp_terminal(registry, None, conn=FakeConn(), session_id="s1")
    assert isinstance(registry.get("bash"), OriginalBash)


# ---------------------------------------------------------------------------
# Terminal capability replaces bash
# ---------------------------------------------------------------------------


def test_terminal_capability_replaces_bash() -> None:
    registry = ToolRegistry()
    registry.register(OriginalBash())

    class WithTermCaps:
        terminal = True

    replace_bash_with_acp_terminal(registry, WithTermCaps(), conn=FakeConn(), session_id="s1")
    tool = registry.get("bash")
    assert isinstance(tool, ACPTerminalBashTool)


# ---------------------------------------------------------------------------
# Tool proxy attributes
# ---------------------------------------------------------------------------


def test_terminal_tool_proxy_attributes() -> None:
    original = OriginalBash()
    tool = ACPTerminalBashTool(original, FakeConn(), "s1")

    assert tool.name == original.name
    assert tool.description == original.description
    assert tool.input_schema == original.input_schema
    assert tool.timeout == original.timeout
    assert tool.is_read_only() == original.is_read_only()
    assert tool.is_destructive() == original.is_destructive()


# ---------------------------------------------------------------------------
# Terminal release called on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_release_called_on_success() -> None:
    conn = FakeConn()
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    await tool.execute(tool_input={"command": "echo ok"}, context=ToolContext(cwd="/tmp"))

    assert conn.released is True


# ---------------------------------------------------------------------------
# Terminal release called on exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_release_called_on_exception() -> None:
    conn = FakeConn(wait_error=RuntimeError("boom"))
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    with pytest.raises(RuntimeError, match="boom"):
        await tool.execute(tool_input={"command": "echo ok"}, context=ToolContext(cwd="/tmp"))

    # release_terminal is called in finally block even when wait_for_terminal_exit raises
    assert conn.released is True


# ---------------------------------------------------------------------------
# Terminal output truncated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_output_truncated() -> None:
    conn = FakeConn(truncated=True, output="partial output...")
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    result = await tool.execute(tool_input={"command": "cat big_file"}, context=ToolContext(cwd="/tmp"))

    assert result.is_error is False
    assert result.content == "partial output..."
    assert conn.released is True


# ---------------------------------------------------------------------------
# CancelledError triggers kill_terminal and release_terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_cancel_triggers_kill() -> None:
    class SlowConn(FakeConn):
        async def wait_for_terminal_exit(self, **kwargs):
            await asyncio.sleep(10)  # will be cancelled
            return acp.schema.WaitForTerminalExitResponse(exit_code=0, signal=None)

    conn = SlowConn()
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    task = asyncio.create_task(tool.execute(tool_input={"command": "sleep 999"}, context=ToolContext(cwd="/tmp")))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn.killed is True
    assert conn.released is True


# ---------------------------------------------------------------------------
# Timeout kills terminal and returns error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_timeout_kills_and_returns_error() -> None:
    class SlowConn(FakeConn):
        async def wait_for_terminal_exit(self, **kwargs):
            await asyncio.sleep(10)  # will time out
            return acp.schema.WaitForTerminalExitResponse(exit_code=0, signal=None)

    conn = SlowConn()
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    result = await tool.execute(
        tool_input={"command": "sleep 999", "timeout": 0.1},
        context=ToolContext(cwd="/tmp"),
    )

    assert result.is_error is True
    assert "timed out" in result.content
    assert "0.1" in result.content
    assert conn.killed is True
    assert conn.released is True


# ---------------------------------------------------------------------------
# Default timeout constant is 300 seconds
# ---------------------------------------------------------------------------


def test_terminal_default_timeout_constant() -> None:
    assert TERMINAL_TIMEOUT == 300


# ---------------------------------------------------------------------------
# Custom timeout from tool_input is respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_custom_timeout_respected() -> None:
    """When timeout is provided, it should be used instead of the default."""

    class SlowConn(FakeConn):
        async def wait_for_terminal_exit(self, **kwargs):
            await asyncio.sleep(10)
            return acp.schema.WaitForTerminalExitResponse(exit_code=0, signal=None)

    conn = SlowConn()
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    result = await tool.execute(
        tool_input={"command": "long_running", "timeout": 0.05},
        context=ToolContext(cwd="/tmp"),
    )

    assert result.is_error is True
    assert "0.05" in result.content
    assert conn.killed is True


# ---------------------------------------------------------------------------
# Basic terminal test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acp_terminal_bash_returns_terminal_output() -> None:
    conn = FakeConn(output="ok")
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    result = await tool.execute(tool_input={"command": "echo ok"}, context=ToolContext(cwd="/tmp"))

    assert result.content == "ok"
    assert result.is_error is False
    assert conn.released is True


# ---------------------------------------------------------------------------
# check_permissions delegates to original tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_tool_check_permissions_delegates() -> None:
    """check_permissions should delegate to the original tool."""
    original = OriginalBash()
    tool = ACPTerminalBashTool(original, FakeConn(), "s1")

    result = await tool.check_permissions({"command": "ls"}, context=None)
    # OriginalBash is destructive, so it asks for permission
    assert result.behavior == "ask"


# ---------------------------------------------------------------------------
# output.exit_status overrides wait_for_terminal_exit result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_output_exit_status_overrides() -> None:
    """When output.exit_status is set, it overrides the wait_for_terminal_exit status."""

    class ConnWithOutputExitStatus(FakeConn):
        async def terminal_output(self, **kwargs):
            return acp.schema.TerminalOutputResponse(
                output="failed output",
                truncated=False,
                exit_status=acp.schema.TerminalExitStatus(exit_code=42, signal=None),
            )

    conn = ConnWithOutputExitStatus(exit_code=0)  # wait returns 0 but output overrides to 42
    tool = ACPTerminalBashTool(OriginalBash(), conn, "s1")

    result = await tool.execute(tool_input={"command": "fail"}, context=ToolContext(cwd="/tmp"))

    assert result.is_error is True
    assert "exit code 42" in result.content
    assert conn.released is True


# ---------------------------------------------------------------------------
# replace_bash_with_acp_terminal returns empty when no bash in registry
# ---------------------------------------------------------------------------


def test_replace_bash_no_bash_in_registry() -> None:
    """When bash tool is not registered, returns empty set."""
    registry = ToolRegistry()
    # Registry has no tools

    class WithTermCaps:
        terminal = True

    result = replace_bash_with_acp_terminal(registry, WithTermCaps(), conn=FakeConn(), session_id="s1")
    assert result == set()
