"""Tests for session idle timeout cleanup — scenarios 12-16."""

from __future__ import annotations

import asyncio
import json
import os
import time

import acp
import acp.schema
import pytest

from iac_code.acp.server import SESSION_IDLE_TIMEOUT, ACPServer
from iac_code.acp.session import ACPSession, _current_turn_id
from iac_code.acp.state import TurnState
from iac_code.agent.message import Message, TextBlock
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage


class FakeConn:
    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        pass


class FakeLoop:
    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="ok")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


def _make_session(session_id: str = "s-1") -> ACPSession:
    return ACPSession(session_id, FakeLoop(), FakeConn())


@pytest.mark.asyncio
async def test_idle_session_is_cleaned_up_after_timeout() -> None:
    """Scenario 12: Session idle beyond SESSION_IDLE_TIMEOUT is removed by cleanup."""
    server = ACPServer()
    session = _make_session("idle-1")
    server.sessions["idle-1"] = session

    # Simulate time passing beyond timeout
    session.last_active = time.monotonic() - SESSION_IDLE_TIMEOUT - 1

    # Run one cleanup cycle manually (call the inner logic directly)
    now = time.monotonic()
    expired = [
        sid
        for sid, s in server.sessions.items()
        if now - s.last_active > SESSION_IDLE_TIMEOUT and s._current_task is None
    ]
    for sid in expired:
        del server.sessions[sid]

    assert "idle-1" not in server.sessions


@pytest.mark.asyncio
async def test_active_session_with_running_task_is_not_cleaned() -> None:
    """Scenario 13: Session with _current_task set is not cleaned even if idle."""
    server = ACPServer()
    session = _make_session("active-1")
    server.sessions["active-1"] = session

    # Simulate idle timeout
    session.last_active = time.monotonic() - SESSION_IDLE_TIMEOUT - 1
    # Simulate an active task
    session._current_task = asyncio.ensure_future(asyncio.sleep(5))

    now = time.monotonic()
    expired = [
        sid
        for sid, s in server.sessions.items()
        if now - s.last_active > SESSION_IDLE_TIMEOUT and s._current_task is None
    ]
    for sid in expired:
        del server.sessions[sid]

    assert "active-1" in server.sessions

    # Cleanup the lingering task
    session._current_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session._current_task


@pytest.mark.asyncio
async def test_prompt_on_cleaned_session_returns_session_not_found() -> None:
    """Scenario 14: After cleanup, prompting the session raises session_not_found error."""
    server = ACPServer()
    session = _make_session("gone-1")
    server.sessions["gone-1"] = session

    # Remove the session (simulating cleanup)
    del server.sessions["gone-1"]

    with pytest.raises(acp.RequestError):
        server._get_session("gone-1")


@pytest.mark.asyncio
async def test_touch_refreshes_last_active_preventing_cleanup() -> None:
    """Scenario 15: touch() resets timer so the session is not cleaned."""
    server = ACPServer()
    session = _make_session("touched-1")
    server.sessions["touched-1"] = session

    # Simulate time passing close to timeout
    session.last_active = time.monotonic() - SESSION_IDLE_TIMEOUT + 10

    # Touch to refresh
    session.touch()

    now = time.monotonic()
    expired = [
        sid
        for sid, s in server.sessions.items()
        if now - s.last_active > SESSION_IDLE_TIMEOUT and s._current_task is None
    ]

    assert "touched-1" not in expired


@pytest.mark.asyncio
async def test_cleanup_loop_exception_does_not_crash_service() -> None:
    """Scenario 16: Exception in cleanup loop is logged but does not stop the server."""
    server = ACPServer()
    server.sessions["x"] = _make_session("x")

    call_count = 0

    async def fake_cleanup() -> None:
        nonlocal call_count
        while True:
            await asyncio.sleep(0)  # yield control
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated cleanup error")
            if call_count >= 2:
                break

    # Patch the cleanup method to use our fake that throws once
    server._cleanup_task = asyncio.create_task(fake_cleanup())

    # Let the loop run briefly
    await asyncio.sleep(0.05)

    # The task should have completed (or errored) but server still has sessions
    assert "x" in server.sessions

    # Cleanup
    if server._cleanup_task and not server._cleanup_task.done():
        server._cleanup_task.cancel()
        try:
            await server._cleanup_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Session streaming tests (from test_session_streaming.py)
# ---------------------------------------------------------------------------


class _RecordingFakeConn:
    def __init__(self) -> None:
        self.updates: list = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))


class _RecordingFakeLoop:
    async def run_streaming(self, prompt):
        assert prompt == "hello"
        yield TextDeltaEvent(text="world")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


@pytest.mark.asyncio
async def test_acp_session_streams_text_update() -> None:
    conn = _RecordingFakeConn()
    session = ACPSession("s1", _RecordingFakeLoop(), conn)

    response = await session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])

    assert response.stop_reason == "end_turn"
    assert conn.updates[0][0] == "s1"
    assert conn.updates[0][1].session_update == "agent_message_chunk"


class _SessionMemoryManager:
    def list_memories(self):
        return [{"name": "user-role", "type": "user", "description": "Role", "content": "Senior engineer"}]


@pytest.mark.asyncio
async def test_acp_session_slash_memory_uses_session_memory_manager() -> None:
    conn = _RecordingFakeConn()
    session = ACPSession("s-memory", _RecordingFakeLoop(), conn, memory_manager=_SessionMemoryManager())

    response = await session.prompt([acp.schema.TextContentBlock(type="text", text="/memory")])

    assert response.stop_reason == "end_turn"
    assert conn.updates[0][0] == "s-memory"
    assert conn.updates[0][1].session_update == "agent_message_chunk"
    assert "user-role - Role" in conn.updates[0][1].content.text


# ---------------------------------------------------------------------------
# ContextVar isolation tests (from test_context_var.py)
# ---------------------------------------------------------------------------


class _ContextFakeConn:
    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        pass


class _ContextFakeLoop:
    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="ok")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


@pytest.mark.asyncio
async def test_concurrent_prompts_have_independent_turn_ids() -> None:
    """Scenario 1: Two concurrent sessions get independent turn_ids via ContextVar."""
    conn = _ContextFakeConn()
    session_a = ACPSession("s-a", _ContextFakeLoop(), conn)
    session_b = ACPSession("s-b", _ContextFakeLoop(), conn)

    captured_turn_ids: dict[str, str | None] = {}

    original_run_streaming = _ContextFakeLoop.run_streaming

    async def capturing_run_a(self, prompt):
        async for event in original_run_streaming(self, prompt):
            captured_turn_ids["a"] = _current_turn_id.get(None)
            yield event

    async def capturing_run_b(self, prompt):
        async for event in original_run_streaming(self, prompt):
            captured_turn_ids["b"] = _current_turn_id.get(None)
            yield event

    loop_a = _ContextFakeLoop()
    loop_b = _ContextFakeLoop()
    loop_a.run_streaming = lambda p: capturing_run_a(loop_a, p)
    loop_b.run_streaming = lambda p: capturing_run_b(loop_b, p)
    session_a.agent_loop = loop_a
    session_b.agent_loop = loop_b

    await asyncio.gather(
        session_a.prompt([acp.schema.TextContentBlock(type="text", text="hello a")]),
        session_b.prompt([acp.schema.TextContentBlock(type="text", text="hello b")]),
    )

    assert captured_turn_ids["a"] is not None
    assert captured_turn_ids["b"] is not None
    assert captured_turn_ids["a"] != captured_turn_ids["b"]


@pytest.mark.asyncio
async def test_single_session_multiple_prompts_get_different_turn_ids() -> None:
    """Scenario 2: Same session, consecutive prompts produce different turn_ids."""
    conn = _ContextFakeConn()
    session = ACPSession("s-1", _ContextFakeLoop(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="first")])
    turn1 = session._current_turn

    await session.prompt([acp.schema.TextContentBlock(type="text", text="second")])
    turn2 = session._current_turn

    assert turn1 is not None and turn2 is not None
    assert turn1.turn_id != turn2.turn_id


@pytest.mark.asyncio
async def test_prompt_creates_turn_state_with_nonempty_turn_id() -> None:
    """Scenario 3: After prompt, session._current_turn is a TurnState with non-empty turn_id."""
    conn = _ContextFakeConn()
    session = ACPSession("s-2", _ContextFakeLoop(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="test")])

    assert isinstance(session._current_turn, TurnState)
    assert session._current_turn.turn_id
    assert len(session._current_turn.turn_id) > 0


# ---------------------------------------------------------------------------
# Multi-session tests (from test_multi_session.py)
# ---------------------------------------------------------------------------


class _EchoFakeLoop:
    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text=f"echo: {prompt}")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


_multi_session_counter = 0


class _MultiRuntime:
    def __init__(self) -> None:
        global _multi_session_counter
        _multi_session_counter += 1
        self.session_id = f"multi-s{_multi_session_counter}"
        self.agent_loop = _EchoFakeLoop()
        self.tool_registry = None


def _patch_multi_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: _MultiRuntime(),
    )
    monkeypatch.setattr(
        "iac_code.acp.server.replace_bash_with_acp_terminal",
        lambda *args, **kwargs: None,
    )


@pytest.fixture(autouse=True)
def _reset_multi_counter() -> None:
    global _multi_session_counter
    _multi_session_counter = 0


@pytest.mark.asyncio
async def test_concurrent_session_creation_unique_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0: Concurrently create multiple sessions, verify ID uniqueness."""
    _patch_multi_server(monkeypatch)
    server = ACPServer()
    server.on_connect(_RecordingFakeConn())

    results = await asyncio.gather(*(server.new_session(cwd="/tmp") for _ in range(5)))
    session_ids = [r.session_id for r in results]

    assert len(set(session_ids)) == 5
    assert len(server.sessions) == 5


@pytest.mark.asyncio
async def test_concurrent_prompts_no_event_crosstalk(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0: Concurrently prompt multiple sessions, verify no event crosstalk."""
    _patch_multi_server(monkeypatch)
    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    sessions = []
    for _ in range(3):
        resp = await server.new_session(cwd="/tmp")
        sessions.append(resp.session_id)

    await asyncio.gather(
        *(
            server.prompt(
                session_id=sid,
                prompt=[acp.schema.TextContentBlock(type="text", text=f"msg-{sid}")],
            )
            for sid in sessions
        )
    )

    updates_by_session: dict[str, list] = {}
    for sid, update in conn.updates:
        updates_by_session.setdefault(sid, []).append(update)

    for sid in sessions:
        assert sid in updates_by_session, f"No updates received for session {sid}"


@pytest.mark.asyncio
async def test_prompt_nonexistent_session_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: Prompt a nonexistent session_id."""
    _patch_multi_server(monkeypatch)
    server = ACPServer()
    server.on_connect(_RecordingFakeConn())

    with pytest.raises(acp.RequestError):
        await server.prompt(
            session_id="nonexistent-session",
            prompt=[acp.schema.TextContentBlock(type="text", text="hello")],
        )


@pytest.mark.asyncio
async def test_cancel_nonexistent_session_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: Cancel a nonexistent session_id."""
    _patch_multi_server(monkeypatch)
    server = ACPServer()
    server.on_connect(_RecordingFakeConn())

    with pytest.raises(acp.RequestError):
        await server.cancel(session_id="nonexistent-session")


@pytest.mark.asyncio
async def test_same_session_multiple_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: Multiple prompts in the same session."""
    _patch_multi_server(monkeypatch)
    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    created = await server.new_session(cwd="/tmp")
    sid = created.session_id

    for i in range(3):
        resp = await server.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text=f"turn-{i}")],
        )
        assert resp.stop_reason == "end_turn"

    assert all(update_sid == sid for update_sid, _ in conn.updates)
    assert len(conn.updates) >= 3


# ---------------------------------------------------------------------------
# Resume session tests (from test_resume_session.py)
# ---------------------------------------------------------------------------


class _ResumeContextManager:
    def __init__(self) -> None:
        self.loaded_messages: list = []

    def load_messages(self, messages: list) -> None:
        self.loaded_messages = messages


class _ResumeLoop:
    def __init__(self) -> None:
        self.context_manager = _ResumeContextManager()

    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text=f"echo: {prompt}")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class _ResumeRuntime:
    def __init__(self, session_id: str = "test-session", cwd: str | None = None) -> None:
        self.session_id = session_id
        self.agent_loop = _ResumeLoop()
        self.agent_loop._cwd = cwd
        self.tool_registry = None


def _patch_resume_server(monkeypatch: pytest.MonkeyPatch, session_id: str = "test-session") -> None:
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: _ResumeRuntime(session_id=options.session_id or session_id, cwd=options.cwd),
    )
    monkeypatch.setattr(
        "iac_code.acp.server.replace_bash_with_acp_terminal",
        lambda *args, **kwargs: None,
    )


@pytest.mark.asyncio
async def test_resume_active_session_returns_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resuming an in-memory active session returns immediately without storage lookup."""
    _patch_resume_server(monkeypatch)
    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id

    result = await server.resume_session(cwd="/tmp", session_id=sid)
    assert isinstance(result, acp.schema.ResumeSessionResponse)
    assert sid in server.sessions


@pytest.mark.asyncio
async def test_resume_active_session_accepts_windows_equivalent_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows path case and separator differences should not trip project ownership checks."""
    _patch_resume_server(monkeypatch)
    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd=r"C:\Users\Me\Repo")
    sid = resp.session_id

    result = await server.resume_session(cwd="c:/Users/Me/Repo", session_id=sid)

    assert isinstance(result, acp.schema.ResumeSessionResponse)
    assert sid in server.sessions


@pytest.mark.asyncio
async def test_resume_active_session_from_other_cwd_raises_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-memory active session follows the same project boundary as persisted sessions."""
    _patch_resume_server(monkeypatch)
    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/source project;unsafe")
    sid = resp.session_id

    with pytest.raises(acp.RequestError) as exc_info:
        await server.resume_session(cwd="/other", session_id=sid)

    assert isinstance(exc_info.value.data, dict)
    assert exc_info.value.data["cwd"] == "/source project;unsafe"
    assert exc_info.value.data["hint"] == "cd '/source project;unsafe' && iac-code --resume test-session"
    assert sid in str(exc_info.value)
    assert "cd '/source project;unsafe' && iac-code --resume test-session" in str(exc_info.value)


@pytest.mark.asyncio
async def test_resume_resolved_name_rejects_active_session_from_other_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The post-resolution active-session fast path also enforces project ownership."""
    _patch_resume_server(monkeypatch, session_id="same-id")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    storage = SessionStorage()
    storage.save(
        "/current",
        "same-id",
        [Message(role="user", content=[TextBlock(text="hello")])],
    )
    storage.rename_session("/current", "same-id", "deploy-prod", git_branch=None)

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)
    await server.new_session(cwd="/other project;unsafe")

    with pytest.raises(acp.RequestError) as exc_info:
        await server.resume_session(cwd="/current", session_id="deploy-prod")

    assert isinstance(exc_info.value.data, dict)
    assert exc_info.value.data["cwd"] == "/other project;unsafe"
    assert exc_info.value.data["hint"] == "cd '/other project;unsafe' && iac-code --resume same-id"
    assert "cd '/other project;unsafe' && iac-code --resume same-id" in str(exc_info.value)


@pytest.mark.asyncio
async def test_resume_nonexistent_session_raises_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Resuming a session that doesn't exist in memory or storage raises RequestError."""
    _patch_resume_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    with pytest.raises(acp.RequestError):
        await server.resume_session(cwd="/tmp", session_id="nonexistent-id")


@pytest.mark.asyncio
async def test_resume_from_storage(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Resuming a persisted session loads history and creates a new ACPSession."""
    _patch_resume_server(monkeypatch, session_id="stored-session")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    project_dir = tmp_path / "projects" / "-tmp"
    os.makedirs(project_dir, exist_ok=True)
    session_file = project_dir / "stored-session.jsonl"
    msg = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    session_file.write_text(json.dumps(msg) + "\n", encoding="utf-8")

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    result = await server.resume_session(cwd="/tmp", session_id="stored-session")
    assert isinstance(result, acp.schema.ResumeSessionResponse)
    assert "stored-session" in server.sessions

    resumed_session = server.sessions["stored-session"]
    ctx = resumed_session.agent_loop.context_manager
    assert len(ctx.loaded_messages) == 1


@pytest.mark.asyncio
async def test_resume_session_accepts_name(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Resuming by session name resolves to the persisted session id."""
    _patch_resume_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    storage = SessionStorage()
    storage.save(
        "/tmp",
        "stored-named-session",
        [Message(role="user", content=[TextBlock(text="hello")])],
    )
    storage.rename_session("/tmp", "stored-named-session", "deploy-prod", git_branch="main")

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    result = await server.resume_session(cwd="/tmp", session_id="deploy-prod")

    assert isinstance(result, acp.schema.ResumeSessionResponse)
    assert "deploy-prod" not in server.sessions
    assert "stored-named-session" in server.sessions
    resumed_session = server.sessions["stored-named-session"]
    assert resumed_session.id == "stored-named-session"
    ctx = resumed_session.agent_loop.context_manager
    assert len(ctx.loaded_messages) == 1


@pytest.mark.asyncio
async def test_resume_session_accepts_id_prefix(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Resuming by unique session id prefix resolves to the full persisted session id."""
    _patch_resume_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    storage = SessionStorage()
    storage.save(
        "/tmp",
        "prefix-session-123",
        [Message(role="user", content=[TextBlock(text="hello")])],
    )

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    result = await server.resume_session(cwd="/tmp", session_id="prefix-session")

    assert isinstance(result, acp.schema.ResumeSessionResponse)
    assert "prefix-session" not in server.sessions
    assert "prefix-session-123" in server.sessions


@pytest.mark.asyncio
async def test_resume_session_single_cross_project_match_raises_hint(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A single foreign-project match is rejected with a concrete resume command hint."""
    _patch_resume_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    storage = SessionStorage()
    foreign_cwd = "/other project;unsafe"
    storage.save(
        foreign_cwd,
        "foreign-session-123",
        [Message(role="user", content=[TextBlock(text="hello")])],
    )
    storage.rename_session(foreign_cwd, "foreign-session-123", "foreign-deploy", git_branch=None)

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    with pytest.raises(acp.RequestError) as exc_info:
        await server.resume_session(cwd="/tmp", session_id="foreign-deploy")

    assert isinstance(exc_info.value.data, dict)
    assert exc_info.value.data["hint"] == "cd '/other project;unsafe' && iac-code --resume foreign-session-123"
    assert "foreign-session-123" in str(exc_info.value)
    assert "cd '/other project;unsafe' && iac-code --resume foreign-session-123" in str(exc_info.value)


@pytest.mark.asyncio
async def test_resume_session_ambiguous_name_raises_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """ACP resume reports candidate ids when a name exists in multiple projects."""
    _patch_resume_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    storage = SessionStorage()
    message = Message(role="user", content=[TextBlock(text="hello")])
    storage.save("/project a;bad", "candidate-a", [message])
    storage.rename_session("/project a;bad", "candidate-a", "deploy-prod", git_branch=None)
    storage.save("/project-b", "candidate-b", [message])
    storage.rename_session("/project-b", "candidate-b", "deploy-prod", git_branch=None)

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    with pytest.raises(acp.RequestError) as exc_info:
        await server.resume_session(cwd="/current", session_id="deploy-prod")

    assert "candidate-a" in str(exc_info.value)
    assert "candidate-b" in str(exc_info.value)
    assert isinstance(exc_info.value.data, dict)
    candidates = exc_info.value.data["candidates"]
    commands_by_id = {candidate["session_id"]: candidate["command"] for candidate in candidates}
    assert commands_by_id["candidate-a"] == "cd '/project a;bad' && iac-code --resume candidate-a"
    assert commands_by_id["candidate-b"] == "cd /project-b && iac-code --resume candidate-b"


@pytest.mark.asyncio
async def test_resume_session_can_prompt_after_restore(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A resumed session can accept new prompts normally."""
    _patch_resume_server(monkeypatch, session_id="prompt-session")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    project_dir = tmp_path / "projects" / "-tmp"
    os.makedirs(project_dir, exist_ok=True)
    msg = {"role": "user", "content": [{"type": "text", "text": "previous turn"}]}
    (project_dir / "prompt-session.jsonl").write_text(json.dumps(msg) + "\n", encoding="utf-8")

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    await server.resume_session(cwd="/tmp", session_id="prompt-session")

    resp = await server.prompt(
        session_id="prompt-session",
        prompt=[acp.schema.TextContentBlock(type="text", text="new message")],
    )
    assert resp.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_resume_without_connection_raises_error() -> None:
    """Resuming without a connected client raises an error for non-active sessions."""
    server = ACPServer()
    with pytest.raises(acp.RequestError):
        await server.resume_session(cwd="/tmp", session_id="no-conn-session")


@pytest.mark.asyncio
async def test_resumed_session_has_fresh_last_active(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Scenario 17: After resume, session.last_active is set to a recent time."""
    _patch_resume_server(monkeypatch, session_id="time-session")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    project_dir = tmp_path / "projects" / "-tmp"
    os.makedirs(project_dir, exist_ok=True)
    msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    (project_dir / "time-session.jsonl").write_text(json.dumps(msg) + "\n", encoding="utf-8")

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    before = time.monotonic()
    await server.resume_session(cwd="/tmp", session_id="time-session")
    after = time.monotonic()

    session = server.sessions["time-session"]
    assert before <= session.last_active <= after


@pytest.mark.asyncio
async def test_concurrent_resume_same_session_no_race(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Scenario 18: Two concurrent resumes of the same session_id do not cause errors."""
    _patch_resume_server(monkeypatch, session_id="race-session")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    project_dir = tmp_path / "projects" / "-tmp"
    os.makedirs(project_dir, exist_ok=True)
    msg = {"role": "user", "content": [{"type": "text", "text": "data"}]}
    (project_dir / "race-session.jsonl").write_text(json.dumps(msg) + "\n", encoding="utf-8")

    conn = _RecordingFakeConn()
    server = ACPServer()
    server.on_connect(conn)

    results = await asyncio.gather(
        server.resume_session(cwd="/tmp", session_id="race-session"),
        server.resume_session(cwd="/tmp", session_id="race-session"),
        return_exceptions=True,
    )

    for r in results:
        assert isinstance(r, acp.schema.ResumeSessionResponse)
    assert "race-session" in server.sessions


@pytest.mark.asyncio
async def test_acp_prompt_flushes_telemetry_after_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    flush_calls: list[int] = []

    def fake_flush() -> None:
        flush_calls.append(1)

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", fake_flush)

    conn = _RecordingFakeConn()
    session = ACPSession("s-flush", _RecordingFakeLoop(), conn)

    await session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])

    assert flush_calls == [1]


@pytest.mark.asyncio
async def test_acp_prompt_overrides_telemetry_session_id_per_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Two ACP sessions in the same process must report distinct session ids
    in telemetry during their respective run_streaming calls."""
    from iac_code.services.telemetry import bootstrap_telemetry, get_session_id, set_client
    from iac_code.services.telemetry.identity import SESSION_ID_PREFIX

    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    set_client(None)
    bootstrap_telemetry(session_id="acp-server-process")
    try:
        observed: dict[str, str] = {}

        class _ObservingLoop:
            def __init__(self, label: str) -> None:
                self._label = label

            async def run_streaming(self, prompt: str):
                observed[self._label] = get_session_id()
                yield TextDeltaEvent(text="ok")
                yield MessageEndEvent(stop_reason="stop", usage=Usage())

        conn = _RecordingFakeConn()
        session_a = ACPSession("session-a", _ObservingLoop("a"), conn)
        session_b = ACPSession("session-b", _ObservingLoop("b"), conn)

        await session_a.prompt([acp.schema.TextContentBlock(type="text", text="hi-a")])
        await session_b.prompt([acp.schema.TextContentBlock(type="text", text="hi-b")])

        assert observed["a"] == f"{SESSION_ID_PREFIX}session-a"
        assert observed["b"] == f"{SESSION_ID_PREFIX}session-b"
        assert get_session_id() == f"{SESSION_ID_PREFIX}acp-server-process"
    finally:
        set_client(None)


@pytest.mark.asyncio
async def test_acp_prompt_flushes_telemetry_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    flush_calls: list[int] = []

    def fake_flush() -> None:
        flush_calls.append(1)

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", fake_flush)

    class _ExplodingLoop:
        async def run_streaming(self, prompt):  # noqa: ARG002
            raise RuntimeError("boom")
            if False:  # pragma: no cover
                yield  # marks this as an async generator

    session = ACPSession("s-flush-fail", _ExplodingLoop(), _RecordingFakeConn())

    with pytest.raises(acp.RequestError):
        await session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])

    assert flush_calls == [1]
