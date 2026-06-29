"""Additional tests for server.py to improve coverage.

Targets uncovered lines:
- 181-183, 186-188: list_sessions edge cases
- 242-246, 252-256: load_session history loading and replay
- 274, 281-290: fork_session connection check and history collection
- 397: _create_runtime_with_auth_check non-auth exception re-raise
- 430-431: _replay_session_history error logging
- 436, 444-450, 448, 461: _push_available_commands edge cases
- 504-518: _cleanup_idle_sessions periodic cleanup
- 533-535: _convert_mcp_servers logging
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import acp
import acp.schema
import pytest

from iac_code.acp.server import (
    SESSION_IDLE_TIMEOUT,
    ACPServer,
    _convert_mcp_servers,
    _runtime_command_memory_manager,
)
from iac_code.acp.session import ACPSession
from iac_code.agent.message import Message
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeConn:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))


class FakeContextManager:
    def __init__(self) -> None:
        self.loaded_messages: list[Message] = []
        self._messages: list[Message] = []

    def load_messages(self, messages: list[Message]) -> None:
        self.loaded_messages = list(messages)
        self._messages = list(messages)

    def get_messages(self) -> list[Message]:
        return list(self._messages)


class FakeLoop:
    def __init__(self) -> None:
        self.context_manager = FakeContextManager()

    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="ok")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class FakeRuntime:
    def __init__(self, session_id: str = "test-session") -> None:
        self.session_id = session_id
        self.agent_loop = FakeLoop()
        self.tool_registry = None


class _NamedMemoryManager:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    def list_memories(self):
        return [
            {
                "name": self.name,
                "type": "user",
                "description": self.description,
                "content": "remembered",
            }
        ]


def test_runtime_command_memory_manager_prefers_legacy_manager() -> None:
    legacy = object()
    project = object()

    runtime = type("Runtime", (), {"legacy_memory_manager": legacy, "memory_manager": project})()

    assert _runtime_command_memory_manager(runtime) is legacy
    assert _runtime_command_memory_manager(type("Runtime", (), {"memory_manager": project})()) is project


@pytest.mark.asyncio
async def test_acp_memory_folder_session_uses_runtime_command_memory_manager() -> None:
    legacy = _NamedMemoryManager("legacy-memory", "Legacy")
    project = _NamedMemoryManager("project-memory", "Project")
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "s-memory",
            "agent_loop": FakeLoop(),
            "legacy_memory_manager": legacy,
            "memory_manager": project,
        },
    )()
    server = ACPServer()
    server.conn = FakeConn()

    session = server._create_acp_session_from_runtime(runtime=runtime, mcp_configs=[])

    response = await session.prompt([acp.schema.TextContentBlock(type="text", text="/memory-folder")])

    assert response.stop_reason == "end_turn"
    assert server.conn.updates
    text = server.conn.updates[0][1].content.text
    assert "legacy-memory - Legacy" in text
    assert "project-memory" not in text


def _patch_server(monkeypatch, session_id: str = "test-session") -> None:
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: FakeRuntime(session_id=options.session_id or session_id),
    )
    monkeypatch.setattr("iac_code.acp.server.replace_bash_with_acp_terminal", lambda *a, **k: None)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")


def _write_session_file(sessions_dir: Path, session_id: str, messages: list[dict] | None = None) -> None:
    os.makedirs(sessions_dir, exist_ok=True)
    if messages is None:
        messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    lines = [json.dumps(m) for m in messages]
    (sessions_dir / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ===========================================================================
# list_sessions - lines 181-183, 186-188
# ===========================================================================


@pytest.mark.asyncio
async def test_list_sessions_with_cwd_project_dir_exists(monkeypatch, tmp_path) -> None:
    """list_sessions with cwd: project_dir exists with session files."""
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    project_dir = tmp_path / "projects" / "-tmp"
    _write_session_file(project_dir, "sess-1")
    _write_session_file(project_dir, "sess-2")

    server = ACPServer()
    resp = await server.list_sessions(cwd="/tmp")

    session_ids = [s.session_id for s in resp.sessions]
    assert "sess-1" in session_ids
    assert "sess-2" in session_ids
    assert resp.next_cursor is None


@pytest.mark.asyncio
async def test_list_sessions_with_cwd_includes_directory_sessions_and_names(monkeypatch, tmp_path) -> None:
    """list_sessions with cwd includes directory-format sessions and uses metadata names as titles."""
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    storage = SessionStorage()
    storage.save("/tmp", "named-dir-session", [Message(role="user", content="hello")])
    storage.rename_session("/tmp", "named-dir-session", "deploy-prod", git_branch="main")

    server = ACPServer()
    resp = await server.list_sessions(cwd="/tmp")

    sessions_by_id = {session.session_id: session for session in resp.sessions}
    assert "named-dir-session" in sessions_by_id
    assert sessions_by_id["named-dir-session"].title == "deploy-prod"
    assert sessions_by_id["named-dir-session"].cwd == "/tmp"


@pytest.mark.asyncio
async def test_list_sessions_with_cwd_project_dir_not_exists(monkeypatch, tmp_path) -> None:
    """list_sessions with cwd but project_dir does not exist → empty list."""
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    server = ACPServer()
    resp = await server.list_sessions(cwd="/nonexistent/path")

    assert resp.sessions == []


@pytest.mark.asyncio
async def test_list_sessions_no_cwd_projects_root_exists(monkeypatch, tmp_path) -> None:
    """list_sessions without cwd: global project list from projects_root."""
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    # Create session files in multiple project dirs
    proj1 = tmp_path / "projects" / "proj1"
    proj2 = tmp_path / "projects" / "proj2"
    _write_session_file(proj1, "global-sess-1")
    _write_session_file(proj2, "global-sess-2")

    server = ACPServer()
    resp = await server.list_sessions(cwd=None)

    session_ids = [s.session_id for s in resp.sessions]
    assert "global-sess-1" in session_ids
    assert "global-sess-2" in session_ids


@pytest.mark.asyncio
async def test_list_sessions_no_cwd_projects_root_not_exists(monkeypatch, tmp_path) -> None:
    """list_sessions without cwd: projects_root doesn't exist → empty."""
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path / "nope")

    server = ACPServer()
    resp = await server.list_sessions(cwd=None)

    assert resp.sessions == []


# ===========================================================================
# load_session - lines 242-246, 252-256 (history load + replay)
# ===========================================================================


@pytest.mark.asyncio
async def test_load_session_with_history_loads_messages_and_replays(monkeypatch, tmp_path) -> None:
    """load_session loads history into context_manager and creates replay task."""
    _patch_server(monkeypatch, session_id="hist-load")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    _write_session_file(
        tmp_path / "projects" / "-tmp",
        "hist-load",
        [
            {"role": "user", "content": [{"type": "text", "text": "msg1"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "reply1"}]},
        ],
    )

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    result = await server.load_session(cwd="/tmp", session_id="hist-load")
    assert isinstance(result, acp.LoadSessionResponse)

    # Verify history was loaded into context_manager (line 243)
    session = server.sessions["hist-load"]
    ctx = session.agent_loop.context_manager
    assert len(ctx.loaded_messages) == 2

    # Wait for replay task (lines 252-253)
    await asyncio.sleep(0.15)


@pytest.mark.asyncio
async def test_load_session_empty_history_skips_load_and_replay(monkeypatch, tmp_path) -> None:
    """load_session with empty history file skips load_messages and replay."""
    _patch_server(monkeypatch, session_id="empty-hist")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    # Write empty session file
    project_dir = tmp_path / "projects" / "-tmp"
    os.makedirs(project_dir, exist_ok=True)
    (project_dir / "empty-hist.jsonl").write_text("", encoding="utf-8")

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    result = await server.load_session(cwd="/tmp", session_id="empty-hist")
    assert isinstance(result, acp.LoadSessionResponse)

    session = server.sessions["empty-hist"]
    ctx = session.agent_loop.context_manager
    # Empty history → load_messages not called
    assert ctx.loaded_messages == []


# ===========================================================================
# fork_session - lines 274, 281-290
# ===========================================================================


@pytest.mark.asyncio
async def test_fork_session_conn_none_raises_error(monkeypatch) -> None:
    """fork_session with conn=None raises internal_error (line 274)."""
    _patch_server(monkeypatch)
    server = ACPServer()
    # conn is None

    with pytest.raises(acp.RequestError):
        await server.fork_session(cwd="/tmp", session_id="any")


@pytest.mark.asyncio
async def test_fork_session_from_active_session_no_context_manager(monkeypatch) -> None:
    """fork_session from active session where context_manager is None (line 280-282)."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    # Create a session with agent_loop lacking context_manager
    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id
    # Remove context_manager attribute
    server.sessions[sid].agent_loop.context_manager = None
    delattr(server.sessions[sid].agent_loop, "context_manager")

    fork_resp = await server.fork_session(cwd="/tmp", session_id=sid)
    # Fork succeeds with empty history
    assert fork_resp.session_id in server.sessions


@pytest.mark.asyncio
async def test_fork_session_from_storage_loads_history(monkeypatch, tmp_path) -> None:
    """fork_session from storage when source session is not active (lines 283-288)."""
    _patch_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    _write_session_file(
        tmp_path / "projects" / "-tmp",
        "stored-src",
        [
            {"role": "user", "content": [{"type": "text", "text": "stored"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
        ],
    )

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    fork_resp = await server.fork_session(cwd="/tmp", session_id="stored-src")
    new_sid = fork_resp.session_id

    # Verify history was loaded
    ctx = server.sessions[new_sid].agent_loop.context_manager
    assert len(ctx.loaded_messages) == 2

    await asyncio.sleep(0.1)


# ===========================================================================
# _create_runtime_with_auth_check - non-auth exception sanitization
# ===========================================================================


@pytest.mark.asyncio
async def test_create_runtime_non_auth_exception_returns_public_error(monkeypatch) -> None:
    """Non-auth runtime creation errors are returned as sanitized ACP errors."""

    def _raise_generic(options):
        raise RuntimeError("disk full for sk-live-secret at /Users/alice/.iac-code/settings.yml")

    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", _raise_generic)
    monkeypatch.setattr("iac_code.acp.server.replace_bash_with_acp_terminal", lambda *a, **k: None)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")

    server = ACPServer()
    server.on_connect(FakeConn())

    with pytest.raises(acp.RequestError) as exc_info:
        await server.new_session(cwd="/tmp")

    rendered = str(exc_info.value.data)
    assert "sk-live-secret" not in rendered
    assert "/Users/alice" not in rendered
    assert exc_info.value.data["error_id"]


# ===========================================================================
# _replay_session_history - lines 430-431 (error logging)
# ===========================================================================


@pytest.mark.asyncio
async def test_replay_session_history_logs_exception(monkeypatch, caplog) -> None:
    """_replay_session_history logs exception but does not propagate (lines 430-431)."""

    class FailingReplaySession:
        id = "fail-replay"

        async def replay_history(self, history):
            raise RuntimeError("replay error")

    server = ACPServer()
    session = FailingReplaySession()

    with caplog.at_level(logging.ERROR, logger="iac_code.acp.server"):
        await server._replay_session_history(session, [Message(role="user", content="hi")])

    assert any("Failed to replay history" in rec.message for rec in caplog.records)


# ===========================================================================
# _push_available_commands - lines 436, 444-450, 461
# ===========================================================================


@pytest.mark.asyncio
async def test_push_available_commands_conn_none_returns_early() -> None:
    """_push_available_commands returns early when conn is None (line 436)."""
    server = ACPServer()
    server.conn = None
    # Should not raise
    await server._push_available_commands("some-session")


@pytest.mark.asyncio
async def test_push_available_commands_with_arg_names_fallback(monkeypatch) -> None:
    """Commands with arg_names (but no arg_hint) use fallback hint (line 448)."""
    from iac_code.commands.registry import CommandRegistry, LocalCommand

    # Create a registry with a command that has arg_names but no arg_hint
    cmd_with_arg_names = LocalCommand(
        name="testcmd",
        description="Test command",
        handler=None,
        arg_names=["file", "line"],
        arg_hint=None,
    )

    def fake_registry():
        reg = CommandRegistry()
        reg.register(cmd_with_arg_names)
        return reg

    monkeypatch.setattr("iac_code.acp.server.create_default_registry", fake_registry)
    # Also allow the command through the whitelist
    monkeypatch.setattr("iac_code.acp.server.ACP_SUPPORTED_COMMANDS", {"testcmd"})

    conn = FakeConn()
    server = ACPServer()
    server.conn = conn
    server.sessions["sess-1"] = ACPSession("sess-1", FakeLoop(), conn, command_registry=fake_registry())

    await server._push_available_commands("sess-1")

    cmd_updates = [u for _, u in conn.updates if isinstance(u, acp.schema.AvailableCommandsUpdate)]
    assert len(cmd_updates) == 1
    commands = cmd_updates[0].available_commands
    assert len(commands) == 1
    assert commands[0].name == "testcmd"
    # Fallback: hint built from arg_names
    assert commands[0].input is not None
    assert commands[0].input.root.hint == "[file] [line]"


@pytest.mark.asyncio
async def test_push_available_commands_no_matching_commands(monkeypatch) -> None:
    """_push_available_commands with no matching commands returns early (line 461)."""
    from iac_code.commands.registry import CommandRegistry

    def empty_registry():
        return CommandRegistry()

    monkeypatch.setattr("iac_code.acp.server.create_default_registry", empty_registry)

    conn = FakeConn()
    server = ACPServer()
    server.conn = conn
    server.sessions["sess-1"] = ACPSession("sess-1", FakeLoop(), conn, command_registry=empty_registry())

    await server._push_available_commands("sess-1")

    # No session_update calls since commands list is empty
    assert len(conn.updates) == 0


# ===========================================================================
# _cleanup_idle_sessions - lines 504-518
# ===========================================================================


@pytest.mark.asyncio
async def test_cleanup_idle_sessions_removes_expired(monkeypatch) -> None:
    """_cleanup_idle_sessions removes sessions that exceed idle timeout (lines 504-518)."""
    server = ACPServer()
    conn = FakeConn()

    loop = FakeLoop()
    session = ACPSession("idle-sess", loop, conn)
    # Make it look idle
    session.last_active = time.monotonic() - SESSION_IDLE_TIMEOUT - 100
    session._current_task = None
    server.sessions["idle-sess"] = session
    server.metrics.record_session_created()

    # Patch CLEANUP_INTERVAL to be very short
    monkeypatch.setattr("iac_code.acp.server.CLEANUP_INTERVAL", 0.01)

    # Start cleanup loop
    task = asyncio.create_task(server._cleanup_idle_sessions())

    # Let it run one iteration
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Session should be removed
    assert "idle-sess" not in server.sessions


@pytest.mark.asyncio
async def test_cleanup_idle_sessions_keeps_active_sessions(monkeypatch) -> None:
    """_cleanup_idle_sessions keeps sessions that are not idle."""
    server = ACPServer()
    conn = FakeConn()

    loop = FakeLoop()
    session = ACPSession("active-sess", loop, conn)
    session.last_active = time.monotonic()  # just now
    session._current_task = None
    server.sessions["active-sess"] = session

    monkeypatch.setattr("iac_code.acp.server.CLEANUP_INTERVAL", 0.01)

    task = asyncio.create_task(server._cleanup_idle_sessions())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "active-sess" in server.sessions


@pytest.mark.asyncio
async def test_cleanup_idle_sessions_skips_busy_sessions(monkeypatch) -> None:
    """_cleanup_idle_sessions skips sessions with _current_task set."""
    server = ACPServer()
    conn = FakeConn()

    loop = FakeLoop()
    session = ACPSession("busy-sess", loop, conn)
    session.last_active = time.monotonic() - SESSION_IDLE_TIMEOUT - 100
    session._current_task = asyncio.Future()  # simulate running task
    server.sessions["busy-sess"] = session

    monkeypatch.setattr("iac_code.acp.server.CLEANUP_INTERVAL", 0.01)

    task = asyncio.create_task(server._cleanup_idle_sessions())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Should NOT be removed because _current_task is not None
    assert "busy-sess" in server.sessions


@pytest.mark.asyncio
async def test_cleanup_idle_sessions_handles_exception(monkeypatch, caplog) -> None:
    """_cleanup_idle_sessions logs exception but continues (lines 517-518)."""
    server = ACPServer()

    # Make sessions.items() raise on first iteration
    class ExplodingDict(dict):
        _count = 0

        def items(self):
            self._count += 1
            if self._count == 1:
                raise RuntimeError("unexpected error")
            return super().items()

    server.sessions = ExplodingDict()
    server.sessions["s1"] = MagicMock()

    monkeypatch.setattr("iac_code.acp.server.CLEANUP_INTERVAL", 0.01)

    with caplog.at_level(logging.ERROR, logger="iac_code.acp.server"):
        task = asyncio.create_task(server._cleanup_idle_sessions())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert any("Error during session cleanup" in rec.message for rec in caplog.records)


# ===========================================================================
# _convert_mcp_servers - lines 533-535 (logging)
# ===========================================================================


def test_convert_mcp_servers_logs_config(monkeypatch, caplog) -> None:
    """_convert_mcp_servers logs received configs (lines 533-535)."""

    # Mock convert_mcp_configs in iac_code.acp.mcp (where it's imported from)
    def fake_convert(servers):
        return [{"name": "test-server", "type": "stdio", "command": "echo"}]

    monkeypatch.setattr("iac_code.acp.mcp.convert_mcp_configs", fake_convert)

    with caplog.at_level(logging.INFO, logger="iac_code.acp.server"):
        result = _convert_mcp_servers([MagicMock()])

    assert len(result) == 1
    assert result[0]["name"] == "test-server"
    assert any("MCP server config" in rec.message for rec in caplog.records)


def test_convert_mcp_servers_empty_input() -> None:
    """_convert_mcp_servers with None/empty returns empty list."""
    assert _convert_mcp_servers(None) == []
    assert _convert_mcp_servers([]) == []


def test_convert_mcp_servers_swallows_conversion_errors(monkeypatch, caplog) -> None:
    """A malformed MCP entry from the client must not abort new_session.

    Conversion failures should be logged at error level and the function
    should return an empty list so the session can still start.
    """

    def _explode(servers):  # pragma: no cover - exercised via patch
        raise RuntimeError("boom while converting MCP")

    monkeypatch.setattr("iac_code.acp.mcp.convert_mcp_configs", _explode)

    with caplog.at_level(logging.ERROR, logger="iac_code.acp.server"):
        result = _convert_mcp_servers([MagicMock(), MagicMock()])

    assert result == []
    assert any("Failed to convert MCP server configs" in rec.message for rec in caplog.records)


# ===========================================================================
# _cleanup_idle_sessions — must skip sessions whose _current_task is running
# ===========================================================================


@pytest.mark.asyncio
async def test_cleanup_idle_sessions_skips_session_with_running_task(monkeypatch) -> None:
    """A session whose ``_current_task`` is not yet done must NOT be cleaned,
    even if ``last_active`` says it's been idle for longer than the timeout.
    """
    server = ACPServer()
    conn = FakeConn()
    session = ACPSession("running-sess", FakeLoop(), conn)
    # Looks idle by clock...
    session.last_active = time.monotonic() - SESSION_IDLE_TIMEOUT - 100
    # ...but a prompt task is still in flight.
    long_running = asyncio.ensure_future(asyncio.sleep(10))
    session._current_task = long_running
    server.sessions["running-sess"] = session

    monkeypatch.setattr("iac_code.acp.server.CLEANUP_INTERVAL", 0.01)

    task = asyncio.create_task(server._cleanup_idle_sessions())
    try:
        await asyncio.sleep(0.05)
        assert "running-sess" in server.sessions
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        long_running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await long_running


@pytest.mark.asyncio
async def test_cleanup_idle_sessions_collects_session_with_finished_task(monkeypatch) -> None:
    """If the session's ``_current_task`` is already done it counts as idle and
    should be cleaned up normally."""
    server = ACPServer()
    conn = FakeConn()
    session = ACPSession("finished-sess", FakeLoop(), conn)
    session.last_active = time.monotonic() - SESSION_IDLE_TIMEOUT - 100
    # A task that has already finished must not protect the session.
    finished = asyncio.ensure_future(asyncio.sleep(0))
    await finished
    session._current_task = finished
    server.sessions["finished-sess"] = session

    monkeypatch.setattr("iac_code.acp.server.CLEANUP_INTERVAL", 0.01)

    task = asyncio.create_task(server._cleanup_idle_sessions())
    try:
        # Allow at least one cleanup pass.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if "finished-sess" not in server.sessions:
                break
        assert "finished-sess" not in server.sessions
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ===========================================================================
# _validate_cwd - cwd path traversal prevention
# ===========================================================================


class TestValidateCwd:
    """Ensure _validate_cwd rejects invalid or disallowed paths."""

    def test_rejects_relative_path(self, monkeypatch) -> None:
        """Relative paths are rejected."""
        monkeypatch.setattr(
            "iac_code.acp.server.allowed_cwd_roots",
            lambda: [Path("/")],
        )
        server = ACPServer()
        with pytest.raises(acp.RequestError):
            server._validate_cwd("relative/path")

    def test_rejects_empty_string(self, monkeypatch) -> None:
        """Empty cwd is rejected."""
        monkeypatch.setattr(
            "iac_code.acp.server.allowed_cwd_roots",
            lambda: [Path("/")],
        )
        server = ACPServer()
        with pytest.raises(acp.RequestError):
            server._validate_cwd("")

    def test_rejects_path_outside_allowed_roots(self, monkeypatch) -> None:
        """A path outside allowed roots is rejected."""
        monkeypatch.setattr(
            "iac_code.acp.server.allowed_cwd_roots",
            lambda: [Path("/allowed/root")],
        )
        server = ACPServer()
        with pytest.raises(acp.RequestError):
            server._validate_cwd("/some/other/path")

    def test_accepts_path_within_allowed_roots(self, monkeypatch, tmp_path) -> None:
        """A path within an allowed root is accepted (returns original cwd)."""
        monkeypatch.setattr(
            "iac_code.acp.server.allowed_cwd_roots",
            lambda: [tmp_path],
        )
        sub = tmp_path / "project"
        sub.mkdir()
        server = ACPServer()
        result = server._validate_cwd(str(sub))
        assert result == str(sub)

    def test_rejects_non_directory(self, monkeypatch, tmp_path) -> None:
        """A path that exists but is not a directory is rejected."""
        monkeypatch.setattr(
            "iac_code.acp.server.allowed_cwd_roots",
            lambda: [tmp_path],
        )
        regular_file = tmp_path / "file.txt"
        regular_file.write_text("content", encoding="utf-8")
        server = ACPServer()
        with pytest.raises(acp.RequestError):
            server._validate_cwd(str(regular_file))
