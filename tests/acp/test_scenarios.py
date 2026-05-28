"""Functional scenario tests.

server bootstrap, content degradation, terminal tools, model info, commands:
  A3     - unsupported content block degradation
  B9     - _detect_terminal_tools auto-detection
  C10-C12 - new_session model info response
  D13-D15 - AvailableCommandsUpdate push

load, fork, close, config:
  A1-A5  - load_session (active/storage/nonexistent/history types/commands)
  B6-B10 - fork_session (active/storage/nonexistent/independent/truncated)
  C11-C14 - close_session (active/idempotent/cancel/reject-after)
  D15-D17 - set_config_option (update/snapshot/accumulate)

multimodal helpers:
  C9-C11 - AudioContentBlock degradation and multimodal conversion
  D12-D16 - create_* helper functions

_meta extensions, structured logging, graceful shutdown:
  A1-A4  - _meta timing and usage
  B5-B9  - structured logging
  D15    - graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from unittest.mock import MagicMock

import acp
import acp.schema
import pytest

from iac_code.acp.convert import (
    ACPEventConverter,
    acp_blocks_to_multimodal,
    acp_blocks_to_prompt_text,
    create_audio_content_block,
    create_file_content_block,
    create_image_content_block,
    create_multimodal_message_chunk,
)
from iac_code.acp.metrics import ACPMetrics
from iac_code.acp.server import ACPServer
from iac_code.acp.session import ACPSession, _history_message_to_updates
from iac_code.acp.state import ToolCallState, TurnState
from iac_code.acp.tools import ACPTerminalBashTool
from iac_code.agent.message import Message, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.stream_events import (
    MessageEndEvent,
    TextDeltaEvent,
    ToolResultEvent,
    Usage,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeConn:
    """Minimal fake ACP client that records session_update calls."""

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
        yield TextDeltaEvent(text=f"echo: {prompt}")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class SlowFakeLoop:
    """A loop that blocks so we can test cancellation."""

    def __init__(self) -> None:
        self.context_manager = FakeContextManager()

    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="started")
        await asyncio.sleep(5)
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class FakeRuntime:
    def __init__(self, session_id: str = "test-session") -> None:
        self.session_id = session_id
        self.agent_loop = FakeLoop()
        self.tool_registry = None


def _patch_server(monkeypatch: pytest.MonkeyPatch, session_id: str = "test-session") -> None:
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: FakeRuntime(session_id=options.session_id or session_id),
    )
    monkeypatch.setattr(
        "iac_code.acp.server.replace_bash_with_acp_terminal",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")


def _write_session_file(sessions_dir, session_id: str, messages: list[dict] | None = None) -> None:
    """Write a JSONL session file to the fake storage directory.

    ``sessions_dir`` is treated as the project subdirectory (already sanitised
    for the target cwd); this helper only handles file creation.
    """
    os.makedirs(sessions_dir, exist_ok=True)
    if messages is None:
        messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    lines = [json.dumps(m) for m in messages]
    (sessions_dir / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


class _DummyTool(Tool):
    def __init__(self, name: str = "read_file") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "dummy"

    @property
    def input_schema(self) -> dict:
        return {"type": "object"}

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        return ToolResult.success("ok")


class _P14FakeLoop:
    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self.tool_registry = tool_registry

    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="ok")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class _P14FakeRuntime:
    def __init__(self, session_id: str = "phase14-s1", tool_registry: ToolRegistry | None = None) -> None:
        self.session_id = session_id
        self.tool_registry = tool_registry or ToolRegistry()
        self.agent_loop = _P14FakeLoop(self.tool_registry)


def _patch_server_p14(monkeypatch: pytest.MonkeyPatch, runtime: _P14FakeRuntime | None = None) -> None:
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: runtime or _P14FakeRuntime(),
    )
    monkeypatch.setattr(
        "iac_code.acp.server.replace_bash_with_acp_terminal",
        lambda *args, **kwargs: set(),
    )


# ===========================================================================
# load_session
# ===========================================================================


@pytest.mark.asyncio
async def test_a1_load_session_active_session_returns_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: Active session returns directly without loading from storage."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id

    result = await server.load_session(cwd="/tmp", session_id=sid)
    assert isinstance(result, acp.LoadSessionResponse)
    assert result.models is not None
    assert result.models.current_model_id == "fake-model"
    assert sid in server.sessions


@pytest.mark.asyncio
async def test_a2_load_session_restores_and_replays_history(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A2: Restore from storage and replay history, verify client receives session_update events."""
    _patch_server(monkeypatch, session_id="stored-load")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    project_dir = tmp_path / "projects" / "-tmp"
    _write_session_file(
        project_dir,
        "stored-load",
        [
            {"role": "user", "content": [{"type": "text", "text": "hi there"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hello back"}]},
        ],
    )

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    result = await server.load_session(cwd="/tmp", session_id="stored-load")
    assert isinstance(result, acp.LoadSessionResponse)
    assert "stored-load" in server.sessions

    ctx = server.sessions["stored-load"].agent_loop.context_manager
    assert len(ctx.loaded_messages) == 2

    await asyncio.sleep(0.1)

    update_types = [type(u).__name__ for _, u in conn.updates]
    assert "UserMessageChunk" in update_types or "AvailableCommandsUpdate" in update_types


@pytest.mark.asyncio
async def test_a3_load_session_nonexistent_returns_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A3: load_session returns error for nonexistent session."""
    _patch_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    with pytest.raises(acp.RequestError):
        await server.load_session(cwd="/tmp", session_id="nonexistent-id")


def test_a4_history_message_to_updates_all_types() -> None:
    """A4: History message conversion covers all message types."""
    user_text = Message(role="user", content="hello")
    updates = _history_message_to_updates(user_text)
    assert len(updates) == 1
    assert updates[0].session_update == "user_message_chunk"

    asst_text = Message(role="assistant", content=[TextBlock(text="reply")])
    updates = _history_message_to_updates(asst_text)
    assert len(updates) == 1
    assert updates[0].session_update == "agent_message_chunk"
    assert updates[0].content.text == "reply"

    asst_think = Message(role="assistant", content=[ThinkingBlock(thinking="let me think")])
    updates = _history_message_to_updates(asst_think)
    assert len(updates) == 1
    assert updates[0].session_update == "agent_thought_chunk"
    assert updates[0].content.text == "let me think"

    asst_tool = Message(
        role="assistant",
        content=[ToolUseBlock(id="tool-1", name="bash", input={"cmd": "ls"})],
    )
    updates = _history_message_to_updates(asst_tool)
    assert len(updates) == 2
    assert updates[0].session_update == "tool_call"
    assert updates[0].tool_call_id == "tool-1"
    assert updates[0].status == "completed"
    assert updates[1].session_update == "tool_call_update"
    assert updates[1].tool_call_id == "tool-1"
    assert updates[1].status == "completed"

    user_result = Message(
        role="user",
        content=[ToolResultBlock(tool_use_id="tool-1", content="file list", is_error=False)],
    )
    updates = _history_message_to_updates(user_result)
    assert len(updates) == 1
    assert updates[0].session_update == "tool_call_update"
    assert updates[0].status == "completed"

    user_err = Message(
        role="user",
        content=[ToolResultBlock(tool_use_id="tool-2", content="error occurred", is_error=True)],
    )
    updates = _history_message_to_updates(user_err)
    assert len(updates) == 1
    assert updates[0].status == "failed"

    asst_str = Message(role="assistant", content="plain text assistant")
    updates = _history_message_to_updates(asst_str)
    assert len(updates) == 1
    assert updates[0].session_update == "agent_message_chunk"
    assert updates[0].content.text == "plain text assistant"


@pytest.mark.asyncio
async def test_a5_load_session_pushes_available_commands(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A5: Pushes AvailableCommandsUpdate after load_session."""
    _patch_server(monkeypatch, session_id="cmd-load")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    _write_session_file(tmp_path / "projects" / "-tmp", "cmd-load")

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    await server.load_session(cwd="/tmp", session_id="cmd-load")

    cmd_updates = [u for _, u in conn.updates if isinstance(u, acp.schema.AvailableCommandsUpdate)]
    assert len(cmd_updates) >= 1
    assert len(cmd_updates[0].available_commands) >= 1


# ===========================================================================
# fork_session
# ===========================================================================


@pytest.mark.asyncio
async def test_b6_fork_active_session_creates_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """B6: Fork an active session creates a new independent session with a different session_id."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    orig = await server.new_session(cwd="/tmp")
    orig_sid = orig.session_id

    orig_ctx = server.sessions[orig_sid].agent_loop.context_manager
    orig_ctx._messages = [Message(role="user", content="original message")]

    fork_resp = await server.fork_session(cwd="/tmp", session_id=orig_sid)
    new_sid = fork_resp.session_id

    assert new_sid != orig_sid
    assert new_sid in server.sessions
    assert orig_sid in server.sessions

    forked_ctx = server.sessions[new_sid].agent_loop.context_manager
    assert len(forked_ctx.loaded_messages) == 1


@pytest.mark.asyncio
async def test_b7_fork_from_storage(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """B7: Fork a historical session from storage."""
    _patch_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    _write_session_file(
        tmp_path / "projects" / "-tmp",
        "history-src",
        [{"role": "user", "content": [{"type": "text", "text": "stored msg"}]}],
    )

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    fork_resp = await server.fork_session(cwd="/tmp", session_id="history-src")
    new_sid = fork_resp.session_id

    assert new_sid in server.sessions
    forked_ctx = server.sessions[new_sid].agent_loop.context_manager
    assert len(forked_ctx.loaded_messages) == 1


@pytest.mark.asyncio
async def test_b8_fork_nonexistent_session_returns_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """B8: Fork a nonexistent source session returns error."""
    _patch_server(monkeypatch)
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    with pytest.raises(acp.RequestError):
        await server.fork_session(cwd="/tmp", session_id="ghost-session")


@pytest.mark.asyncio
async def test_b9_fork_independent_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """B9: Forked session can prompt independently without affecting the source session."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    orig = await server.new_session(cwd="/tmp")
    orig_sid = orig.session_id

    fork_resp = await server.fork_session(cwd="/tmp", session_id=orig_sid)
    fork_sid = fork_resp.session_id

    resp = await server.prompt(
        session_id=fork_sid,
        prompt=[acp.schema.TextContentBlock(type="text", text="forked prompt")],
    )
    assert resp.stop_reason == "end_turn"
    assert orig_sid in server.sessions


@pytest.mark.asyncio
async def test_b10_fork_replays_truncated_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """B10: Fork replays truncated history - only copies the source session's current history."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    orig = await server.new_session(cwd="/tmp")
    orig_sid = orig.session_id

    source_ctx = server.sessions[orig_sid].agent_loop.context_manager
    source_ctx._messages = [
        Message(role="user", content="msg1"),
        Message(role="assistant", content="reply1"),
    ]

    fork_resp = await server.fork_session(cwd="/tmp", session_id=orig_sid)
    fork_sid = fork_resp.session_id

    forked_ctx = server.sessions[fork_sid].agent_loop.context_manager
    assert len(forked_ctx.loaded_messages) == 2

    await asyncio.sleep(0.1)

    fork_updates = [(sid, u) for sid, u in conn.updates if sid == fork_sid]
    assert len(fork_updates) >= 1


# ===========================================================================
# close_session
# ===========================================================================


@pytest.mark.asyncio
async def test_c11_close_active_session_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """C11: Close an active session successfully, session is removed, subsequent operations return session_not_found."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id
    assert sid in server.sessions

    result = await server.close_session(session_id=sid)
    assert isinstance(result, acp.schema.CloseSessionResponse)
    assert sid not in server.sessions

    with pytest.raises(acp.RequestError):
        await server.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text="hi")],
        )


@pytest.mark.asyncio
async def test_c12_close_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """C12: Close an already closed/nonexistent session is idempotent."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    result = await server.close_session(session_id="never-existed")
    assert isinstance(result, acp.schema.CloseSessionResponse)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id
    await server.close_session(session_id=sid)
    result2 = await server.close_session(session_id=sid)
    assert isinstance(result2, acp.schema.CloseSessionResponse)


@pytest.mark.asyncio
async def test_c13_close_cancels_running_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """C13: Close cancels a running prompt."""
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")

    slow_loop = SlowFakeLoop()

    def make_runtime(options):
        rt = FakeRuntime(session_id=options.session_id or "slow-session")
        rt.agent_loop = slow_loop
        return rt

    monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", make_runtime)
    monkeypatch.setattr("iac_code.acp.server.replace_bash_with_acp_terminal", lambda *a, **k: None)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id

    prompt_task = asyncio.create_task(
        server.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text="slow work")],
        )
    )
    await asyncio.sleep(0.05)

    await server.close_session(session_id=sid)

    result = await prompt_task
    assert result.stop_reason == "cancelled"
    assert sid not in server.sessions


@pytest.mark.asyncio
async def test_c14_close_then_prompt_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """C14: Prompt is rejected after close."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id

    await server.close_session(session_id=sid)

    with pytest.raises(acp.RequestError):
        await server.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text="rejected")],
        )


# ===========================================================================
# dynamic_config
# ===========================================================================


@pytest.mark.asyncio
async def test_d15_set_config_option_updates_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """D15: set_config_option updates session configuration."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id

    result = await server.set_config_option(config_id="temperature", session_id=sid, value="0.7")
    assert result is None

    session = server.sessions[sid]
    assert session.config["temperature"] == "0.7"


@pytest.mark.asyncio
async def test_d16_get_config_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """D16: Get current config snapshot - config property returns an independent copy."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id

    session = server.sessions[sid]
    session.update_config({"max_tokens": 4096})

    snapshot = session.config
    assert snapshot == {"max_tokens": 4096}

    snapshot["extra"] = "value"
    assert "extra" not in session.config


@pytest.mark.asyncio
async def test_d17_config_merge_accumulates(monkeypatch: pytest.MonkeyPatch) -> None:
    """D17: Config merge - multiple set operations accumulate."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    resp = await server.new_session(cwd="/tmp")
    sid = resp.session_id

    await server.set_config_option(config_id="temperature", session_id=sid, value="0.5")
    await server.set_config_option(config_id="max_tokens", session_id=sid, value=2048)
    await server.set_config_option(config_id="temperature", session_id=sid, value="0.9")

    session = server.sessions[sid]
    assert session.config == {"temperature": "0.9", "max_tokens": 2048}


# ===========================================================================
# Multimodal content helpers
# ===========================================================================


def test_audio_content_block_degrades_to_text_tag() -> None:
    """C9: Audio block degrades to [audio: mime_type] tag in acp_blocks_to_prompt_text."""
    blocks = [acp.schema.AudioContentBlock(type="audio", data="base64data", mime_type="audio/wav")]
    result = acp_blocks_to_prompt_text(blocks)
    assert result == "[audio: audio/wav]"


def test_audio_content_block_degrades_with_custom_mime() -> None:
    """C9 addendum: Custom mime_type also degrades correctly."""
    blocks = [acp.schema.AudioContentBlock(type="audio", data="abc", mime_type="audio/mp3")]
    result = acp_blocks_to_prompt_text(blocks)
    assert result == "[audio: audio/mp3]"


def test_audio_in_multimodal_retains_data() -> None:
    """C10: AudioContentBlock in acp_blocks_to_multimodal retains mime_type and data."""
    blocks = [acp.schema.AudioContentBlock(type="audio", data="dGVzdA==", mime_type="audio/wav")]
    parts = acp_blocks_to_multimodal(blocks)

    assert len(parts) == 1
    assert parts[0]["type"] == "audio"
    assert parts[0]["mime_type"] == "audio/wav"
    assert parts[0]["data"] == "dGVzdA=="


def test_mixed_text_image_audio_multimodal_conversion() -> None:
    """C11: Blocks containing text, image, and audio are correctly converted to a multimodal list."""
    blocks = [
        acp.schema.TextContentBlock(type="text", text="Hello world"),
        acp.schema.ImageContentBlock(type="image", data="imgdata==", mime_type="image/png"),
        acp.schema.AudioContentBlock(type="audio", data="audiodata==", mime_type="audio/ogg"),
    ]
    parts = acp_blocks_to_multimodal(blocks)

    assert len(parts) == 3
    assert parts[0] == {"type": "text", "text": "Hello world"}
    assert parts[1] == {"type": "image", "mime_type": "image/png", "data": "imgdata=="}
    assert parts[2] == {"type": "audio", "mime_type": "audio/ogg", "data": "audiodata=="}


def test_mixed_blocks_prompt_text_degradation() -> None:
    """C11 addendum: Mixed blocks all degrade to text in prompt_text mode."""
    blocks = [
        acp.schema.TextContentBlock(type="text", text="intro"),
        acp.schema.ImageContentBlock(type="image", data="img", mime_type="image/jpeg"),
        acp.schema.AudioContentBlock(type="audio", data="aud", mime_type="audio/wav"),
    ]
    result = acp_blocks_to_prompt_text(blocks)

    assert "intro" in result
    assert "[image: image/jpeg]" in result
    assert "[audio: audio/wav]" in result


def test_create_image_content_block_correct_structure() -> None:
    """D12: create_image_content_block returns ImageContentBlock with correct fields."""
    block = create_image_content_block(data="abc123==", mime_type="image/png")

    assert isinstance(block, acp.schema.ImageContentBlock)
    assert block.type == "image"
    assert block.data == "abc123=="
    assert block.mime_type == "image/png"


def test_create_image_content_block_default_mime_type() -> None:
    """D12 addendum: Default mime_type is image/png."""
    block = create_image_content_block(data="x")
    assert block.mime_type == "image/png"


def test_create_audio_content_block_correct_structure() -> None:
    """D13: create_audio_content_block returns AudioContentBlock with correct fields."""
    block = create_audio_content_block(data="audiobase64==", mime_type="audio/wav")

    assert isinstance(block, acp.schema.AudioContentBlock)
    assert block.type == "audio"
    assert block.data == "audiobase64=="
    assert block.mime_type == "audio/wav"


def test_create_audio_content_block_default_mime_type() -> None:
    """D13 addendum: Default mime_type is audio/wav."""
    block = create_audio_content_block(data="y")
    assert block.mime_type == "audio/wav"


def test_create_file_content_block_embedded_resource() -> None:
    """D14: create_file_content_block returns EmbeddedResourceContentBlock with embedded BlobResourceContents."""
    block = create_file_content_block(data="filedata==", filename="report.pdf", mime_type="application/pdf")

    assert isinstance(block, acp.schema.EmbeddedResourceContentBlock)
    assert block.type == "resource"

    resource = block.resource
    assert isinstance(resource, acp.schema.BlobResourceContents)
    assert resource.uri == "report.pdf"
    assert resource.mime_type == "application/pdf"
    assert resource.blob == "filedata=="


def test_create_multimodal_message_chunk_single_block() -> None:
    """D15: A single content block is wrapped as AgentMessageChunk."""
    img_block = create_image_content_block(data="img", mime_type="image/png")
    chunk = create_multimodal_message_chunk([img_block])

    assert isinstance(chunk, acp.schema.AgentMessageChunk)
    assert chunk.session_update == "agent_message_chunk"
    assert chunk.content == img_block


def test_create_multimodal_message_chunk_multiple_blocks() -> None:
    """D15 addendum: Multiple blocks still return a valid AgentMessageChunk (first block)."""
    img = create_image_content_block(data="img", mime_type="image/png")
    audio = create_audio_content_block(data="aud", mime_type="audio/wav")
    chunk = create_multimodal_message_chunk([img, audio])

    assert isinstance(chunk, acp.schema.AgentMessageChunk)
    assert chunk.session_update == "agent_message_chunk"
    assert chunk.content == img


def test_create_image_content_block_with_uri() -> None:
    """D16: When uri parameter is provided, ImageContentBlock contains the uri field."""
    block = create_image_content_block(data="img", mime_type="image/png", uri="https://example.com/image.png")

    assert isinstance(block, acp.schema.ImageContentBlock)
    assert block.uri == "https://example.com/image.png"
    assert block.data == "img"


def test_create_image_content_block_without_uri() -> None:
    """D16 addendum: Default uri is None when not provided."""
    block = create_image_content_block(data="img")
    assert block.uri is None


def test_mixed_content_blocks_to_text() -> None:
    blocks = [
        acp.schema.TextContentBlock(type="text", text="Hello"),
        acp.schema.EmbeddedResourceContentBlock(
            type="resource",
            resource=acp.schema.TextResourceContents(
                uri="file:///test.py",
                text="print('hi')",
            ),
        ),
    ]
    result = acp_blocks_to_prompt_text(blocks)
    assert "Hello" in result
    assert "file:///test.py" in result
    assert "print('hi')" in result


def test_image_content_block_graceful_degradation() -> None:
    blocks = [
        acp.schema.ImageContentBlock(
            type="image",
            data="base64data",
            mimeType="image/png",
        ),
    ]
    result = acp_blocks_to_prompt_text(blocks)
    assert "[image: image/png]" == result


def test_audio_content_block_graceful_degradation() -> None:
    blocks = [
        acp.schema.AudioContentBlock(
            type="audio",
            data="base64data",
            mimeType="audio/wav",
        ),
    ]
    result = acp_blocks_to_prompt_text(blocks)
    assert "[audio: audio/wav]" == result


def test_multimodal_text_only() -> None:
    blocks = [acp.schema.TextContentBlock(type="text", text="hello")]
    parts = acp_blocks_to_multimodal(blocks)
    assert parts == [{"type": "text", "text": "hello"}]


def test_multimodal_image_preserves_data() -> None:
    blocks = [
        acp.schema.ImageContentBlock(
            type="image",
            data="iVBORw0KGgoAAAANS==",
            mimeType="image/png",
        ),
    ]
    parts = acp_blocks_to_multimodal(blocks)
    assert len(parts) == 1
    assert parts[0]["type"] == "image"
    assert parts[0]["mime_type"] == "image/png"
    assert parts[0]["data"] == "iVBORw0KGgoAAAANS=="


def test_multimodal_mixed_blocks() -> None:
    blocks = [
        acp.schema.TextContentBlock(type="text", text="Look at this:"),
        acp.schema.ImageContentBlock(
            type="image",
            data="abc123",
            mimeType="image/jpeg",
        ),
    ]
    parts = acp_blocks_to_multimodal(blocks)
    assert len(parts) == 2
    assert parts[0] == {"type": "text", "text": "Look at this:"}
    assert parts[1]["type"] == "image"


def test_resource_content_block_to_text() -> None:
    blocks = [
        acp.schema.ResourceContentBlock(
            type="resource_link",
            uri="file:///path/to/file",
            name="test.py",
        ),
    ]
    result = acp_blocks_to_prompt_text(blocks)
    assert "file:///path/to/file" in result
    assert "test.py" in result


def test_empty_content_blocks() -> None:
    result = acp_blocks_to_prompt_text([])
    assert result == ""


def test_single_text_content_block() -> None:
    blocks = [acp.schema.TextContentBlock(type="text", text="Simple text")]
    result = acp_blocks_to_prompt_text(blocks)
    assert result.strip() == "Simple text"


def test_multiple_text_blocks_joined() -> None:
    blocks = [
        acp.schema.TextContentBlock(type="text", text="Part 1"),
        acp.schema.TextContentBlock(type="text", text="Part 2"),
    ]
    result = acp_blocks_to_prompt_text(blocks)
    assert "Part 1" in result
    assert "Part 2" in result


def test_multimodal_audio_preserves_data() -> None:
    blocks = [
        acp.schema.AudioContentBlock(
            type="audio",
            data="AAAA",
            mimeType="audio/wav",
        ),
    ]
    parts = acp_blocks_to_multimodal(blocks)
    assert len(parts) == 1
    assert parts[0]["type"] == "audio"
    assert parts[0]["mime_type"] == "audio/wav"
    assert parts[0]["data"] == "AAAA"


def test_multimodal_mixed_with_audio() -> None:
    blocks = [
        acp.schema.TextContentBlock(type="text", text="Listen:"),
        acp.schema.AudioContentBlock(
            type="audio",
            data="audiodata",
            mimeType="audio/mp3",
        ),
        acp.schema.ImageContentBlock(
            type="image",
            data="imgdata",
            mimeType="image/png",
        ),
    ]
    parts = acp_blocks_to_multimodal(blocks)
    assert len(parts) == 3
    assert parts[0] == {"type": "text", "text": "Listen:"}
    assert parts[1]["type"] == "audio"
    assert parts[2]["type"] == "image"


def test_create_image_content_block_defaults() -> None:
    block = create_image_content_block(data="abc123")
    assert isinstance(block, acp.schema.ImageContentBlock)
    assert block.data == "abc123"
    assert block.mime_type == "image/png"
    assert block.uri is None


def test_create_audio_content_block_defaults() -> None:
    block = create_audio_content_block(data="wav_data")
    assert isinstance(block, acp.schema.AudioContentBlock)
    assert block.data == "wav_data"
    assert block.mime_type == "audio/wav"


def test_create_audio_content_block_custom_mime() -> None:
    block = create_audio_content_block(data="mp3_data", mime_type="audio/mp3")
    assert isinstance(block, acp.schema.AudioContentBlock)
    assert block.mime_type == "audio/mp3"


def test_create_file_content_block() -> None:
    block = create_file_content_block(
        data="ZmlsZQ==",
        filename="report.pdf",
        mime_type="application/pdf",
    )
    assert isinstance(block, acp.schema.EmbeddedResourceContentBlock)
    resource = block.resource
    assert isinstance(resource, acp.schema.BlobResourceContents)
    assert resource.blob == "ZmlsZQ=="
    assert resource.uri == "report.pdf"
    assert resource.mime_type == "application/pdf"


def test_create_multimodal_message_chunk_single() -> None:
    img = create_image_content_block(data="abc", mime_type="image/png")
    chunk = create_multimodal_message_chunk([img])
    assert isinstance(chunk, acp.schema.AgentMessageChunk)
    assert chunk.content == img


def test_create_multimodal_message_chunk_multiple() -> None:
    img = create_image_content_block(data="abc")
    audio = create_audio_content_block(data="def")
    chunk = create_multimodal_message_chunk([img, audio])
    assert isinstance(chunk, acp.schema.AgentMessageChunk)
    assert chunk.content == img


# ===========================================================================
# _meta extensions, structured logging, graceful shutdown
# ===========================================================================


class _P24FakeLoop:
    """Agent loop that emits a simple stream with optional usage."""

    def __init__(self, *, usage: Usage | None = None) -> None:
        self._usage = usage or Usage()

    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="ok")
        yield MessageEndEvent(stop_reason="stop", usage=self._usage)


def _make_p24_session(
    session_id: str = "s-test",
    *,
    usage: Usage | None = None,
    metrics: ACPMetrics | None = None,
) -> tuple[ACPSession, FakeConn]:
    conn = FakeConn()
    loop = _P24FakeLoop(usage=usage)
    session = ACPSession(session_id, loop, conn, metrics=metrics)
    return session, conn


class TestMetaTimingAndUsage:
    """A1–A4: _meta field extensions for timing and token usage."""

    @pytest.mark.asyncio
    async def test_prompt_response_contains_timing_elapsed_ms(self) -> None:
        """A1: PromptResponse.field_meta includes timing.elapsed_ms >= 0."""
        session, _ = _make_p24_session()
        resp = await session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])

        assert hasattr(resp, "field_meta") and resp.field_meta is not None
        timing = resp.field_meta.get("timing", {})
        assert "elapsed_ms" in timing
        assert isinstance(timing["elapsed_ms"], int)
        assert timing["elapsed_ms"] >= 0

    @pytest.mark.asyncio
    async def test_prompt_response_contains_usage_metadata(self) -> None:
        """A2: PromptResponse.field_meta includes usage with input/output/total tokens."""
        usage = Usage(input_tokens=10, output_tokens=20)
        session, _ = _make_p24_session(usage=usage)
        resp = await session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])

        meta = resp.field_meta
        assert meta is not None
        assert "usage" in meta
        assert meta["usage"]["input_tokens"] == 10
        assert meta["usage"]["output_tokens"] == 20
        assert meta["usage"]["total_tokens"] == usage.total_tokens

    def test_tool_result_event_contains_timing_meta(self) -> None:
        """A3: ToolResultEvent conversion attaches timing.elapsed_ms in field_meta."""
        turn_state = TurnState(turn_id="turn-1")
        tc = turn_state.start_tool_call("tc-1", "bash")
        tc.start_time = time.monotonic() - 0.05

        converter = ACPEventConverter(turn_id="turn-1", turn_state=turn_state)
        event = ToolResultEvent(tool_use_id="tc-1", tool_name="bash", result="done", is_error=False)
        updates = converter.event_to_updates(event)

        assert len(updates) == 2
        progress = updates[0]
        assert hasattr(progress, "field_meta") and progress.field_meta is not None
        timing = progress.field_meta.get("timing", {})
        assert "elapsed_ms" in timing
        assert timing["elapsed_ms"] >= 0

    def test_tool_call_state_elapsed_ms_is_non_negative(self) -> None:
        """A4: ToolCallState.elapsed_ms returns non-negative integer."""
        tc = ToolCallState(tool_call_id="tc-x", tool_name="grep")
        assert isinstance(tc.elapsed_ms, int)
        assert tc.elapsed_ms >= 0

    def test_tool_call_state_elapsed_ms_increases_over_time(self) -> None:
        """A4 (supplement): elapsed_ms grows as time passes."""
        tc = ToolCallState(tool_call_id="tc-y", tool_name="bash")
        tc.start_time = time.monotonic() - 0.1
        assert tc.elapsed_ms >= 80


class TestStructuredLogging:
    """B5–B9: Structured log messages for key server operations."""

    @pytest.fixture
    def _patch_runtime(self, monkeypatch):
        class _FakeLoop:
            context_manager = None

            async def run_streaming(self, prompt):
                yield TextDeltaEvent(text="ok")
                yield MessageEndEvent(stop_reason="stop", usage=Usage())

        class _FakeRuntime:
            session_id = "test-session"
            agent_loop = _FakeLoop()
            tool_registry = None

        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda options: _FakeRuntime())

    def _make_server_with_conn(self) -> tuple:
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        return server, conn

    @pytest.mark.asyncio
    async def test_initialize_logs_info_with_protocol_version(self, _patch_runtime, caplog) -> None:
        """B5: initialize emits INFO log containing protocol version."""
        server, _ = self._make_server_with_conn()
        with caplog.at_level(logging.INFO, logger="iac_code.acp.server"):
            await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        assert any("protocol_version" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_new_session_logs_info_with_session_id(self, _patch_runtime, caplog) -> None:
        """B6: new_session emits INFO log containing session_id."""
        server, _ = self._make_server_with_conn()
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        with caplog.at_level(logging.INFO, logger="iac_code.acp.server"):
            resp = await server.new_session(cwd="/tmp")

        assert any("session_id" in rec.message or resp.session_id in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_prompt_logs_debug_with_session_id_and_elapsed(self, caplog) -> None:
        """B7: prompt start/end emits DEBUG logs with session_id and elapsed_ms."""
        session, _ = _make_p24_session("log-session")

        with caplog.at_level(logging.DEBUG, logger="iac_code.acp.session"):
            await session.prompt([acp.schema.TextContentBlock(type="text", text="hello")])

        messages = [rec.message for rec in caplog.records]
        assert any("Prompt started" in m and "log-session" in m for m in messages)
        assert any("Prompt completed" in m and "elapsed_ms" in m for m in messages)

    @pytest.mark.asyncio
    async def test_close_session_logs_info(self, _patch_runtime, caplog) -> None:
        """B8: close_session emits INFO log."""
        server, _ = self._make_server_with_conn()
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())
        resp = await server.new_session(cwd="/tmp")

        with caplog.at_level(logging.INFO, logger="iac_code.acp.server"):
            await server.close_session(session_id=resp.session_id)

        assert any("closed" in rec.message.lower() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_auth_error_logs_warning(self, caplog) -> None:
        """B9: Authentication error during prompt emits WARNING log."""

        class _AuthErrorLoop:
            async def run_streaming(self, prompt):
                raise ValueError("No provider configured. Please run /auth to configure.")
                yield  # noqa: RET503

        conn = FakeConn()
        session = ACPSession("auth-sess", _AuthErrorLoop(), conn)

        with caplog.at_level(logging.WARNING, logger="iac_code.acp.session"):
            with pytest.raises(acp.RequestError):
                await session.prompt([acp.schema.TextContentBlock(type="text", text="hi")])

        assert any(rec.levelno >= logging.WARNING for rec in caplog.records)
        assert any("auth" in rec.message.lower() for rec in caplog.records)


class TestGracefulShutdown:
    """D15: shutdown stops the cleanup loop task."""

    @pytest.mark.asyncio
    async def test_shutdown_stops_cleanup_loop(self) -> None:
        """D15: After shutdown_all_sessions, _cleanup_task is None."""
        server = ACPServer()
        await server._start_cleanup_loop()
        assert server._cleanup_task is not None

        await server.shutdown_all_sessions()
        assert server._cleanup_task is None

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_sessions_and_empties_dict(self) -> None:
        """D13+D14 (verification): shutdown closes sessions and empties the dict."""
        server = ACPServer()
        conn = FakeConn()
        loop = _P24FakeLoop()
        for i in range(3):
            sid = f"sess-{i}"
            session = ACPSession(sid, loop, conn)
            server.sessions[sid] = session
            server.metrics.record_session_created()

        assert server.metrics.active_sessions == 3

        await server.shutdown_all_sessions()
        assert len(server.sessions) == 0
        assert server.metrics.active_sessions == 0


# ===========================================================================
# Unsupported content block degradation
# ===========================================================================


class _FakeUnsupportedBlock:
    """A content block type that is not handled by the converter."""


def test_unsupported_content_block_degrades_in_prompt_text() -> None:
    """A3: Unknown content block type degrades to [Unsupported ...] tag (prompt_text)."""
    blocks = [_FakeUnsupportedBlock()]
    result = acp_blocks_to_prompt_text(blocks)
    assert "Unsupported" in result
    assert "_FakeUnsupportedBlock" in result


def test_unsupported_content_block_degrades_in_multimodal() -> None:
    """A3: Unknown content block type degrades to text type dict (multimodal)."""
    blocks = [_FakeUnsupportedBlock()]
    parts = acp_blocks_to_multimodal(blocks)
    assert len(parts) == 1
    assert parts[0]["type"] == "text"
    assert "Unsupported" in parts[0]["text"]


# ===========================================================================
# Terminal tool detection
# ===========================================================================


def test_detect_terminal_tools_finds_acp_terminal_bash() -> None:
    """B9: _detect_terminal_tools correctly identifies ACPTerminalBashTool instances from tool_registry."""
    registry = ToolRegistry()
    original = _DummyTool("bash")
    terminal_tool = ACPTerminalBashTool(original, MagicMock(), "s1")
    registry.register(terminal_tool)
    registry.register(_DummyTool("read_file"))

    loop = MagicMock()
    loop.tool_registry = registry

    session = ACPSession.__new__(ACPSession)
    session.agent_loop = loop
    names = session._detect_terminal_tools()

    assert names == {"bash"}


def test_detect_terminal_tools_empty_when_no_terminal_tool() -> None:
    """B9: Returns empty set when registry has no ACPTerminalBashTool."""
    registry = ToolRegistry()
    registry.register(_DummyTool("bash"))

    loop = MagicMock()
    loop.tool_registry = registry

    session = ACPSession.__new__(ACPSession)
    session.agent_loop = loop
    names = session._detect_terminal_tools()

    assert names == set()


def test_detect_terminal_tools_no_registry() -> None:
    """B9: Returns empty set when agent_loop has no tool_registry attribute."""
    loop = object()  # no tool_registry attr

    session = ACPSession.__new__(ACPSession)
    session.agent_loop = loop
    names = session._detect_terminal_tools()

    assert names == set()


# ===========================================================================
# Model state return scenarios
# ===========================================================================


@pytest.mark.asyncio
async def test_new_session_response_contains_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """C10: new_session response contains models field with current active model."""
    _patch_server_p14(monkeypatch)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")

    server = ACPServer()
    server.conn = FakeConn()
    await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

    resp = await server.new_session(cwd="/tmp")

    assert resp.models is not None
    assert resp.models.current_model_id == "fake-model"
    assert len(resp.models.available_models) >= 1


@pytest.mark.asyncio
async def test_model_info_contains_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """C11: ModelInfo contains model_id and name."""
    _patch_server_p14(monkeypatch)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "openai")

    server = ACPServer()
    server.conn = FakeConn()
    await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

    resp = await server.new_session(cwd="/tmp")

    model_info = resp.models.available_models[0]
    assert model_info.model_id == "fake-model"
    assert model_info.name == "fake-model"


@pytest.mark.asyncio
async def test_new_session_with_no_saved_model_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """C12: new_session uses default model when no model is configured."""
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: None)
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: _P14FakeRuntime(),
    )
    monkeypatch.setattr(
        "iac_code.acp.server.replace_bash_with_acp_terminal",
        lambda *args, **kwargs: set(),
    )
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: None)

    server = ACPServer()
    server.conn = FakeConn()
    await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

    resp = await server.new_session(cwd="/tmp")

    assert resp.session_id is not None
    assert resp.models is not None
    assert resp.models.current_model_id is not None


# ===========================================================================
# Available commands push scenarios
# ===========================================================================


@pytest.mark.asyncio
async def test_new_session_pushes_available_commands_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """D13: conn.session_update is called to push AvailableCommandsUpdate after session creation."""
    _patch_server_p14(monkeypatch)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")

    conn = FakeConn()
    server = ACPServer()
    server.conn = conn
    await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

    await server.new_session(cwd="/tmp")

    cmd_updates = [u for _, u in conn.updates if isinstance(u, acp.schema.AvailableCommandsUpdate)]
    assert len(cmd_updates) >= 1, "Expected at least one AvailableCommandsUpdate push"


@pytest.mark.asyncio
async def test_pushed_command_list_is_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """D14: Pushed command list contains at least one registered command."""
    _patch_server_p14(monkeypatch)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")

    conn = FakeConn()
    server = ACPServer()
    server.conn = conn
    await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

    await server.new_session(cwd="/tmp")

    cmd_updates = [u for _, u in conn.updates if isinstance(u, acp.schema.AvailableCommandsUpdate)]
    assert len(cmd_updates) >= 1
    commands = cmd_updates[0].available_commands
    assert len(commands) >= 1, "AvailableCommandsUpdate should contain at least one command"


@pytest.mark.asyncio
async def test_pushed_commands_match_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """D15: Pushed command names match the commands registered in commands/registry."""
    _patch_server_p14(monkeypatch)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")

    conn = FakeConn()
    server = ACPServer()
    server.conn = conn
    await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

    await server.new_session(cwd="/tmp")

    cmd_updates = [u for _, u in conn.updates if isinstance(u, acp.schema.AvailableCommandsUpdate)]
    assert len(cmd_updates) >= 1
    pushed_names = {cmd.name for cmd in cmd_updates[0].available_commands}

    from iac_code.acp.slash_registry import ACP_SUPPORTED_COMMANDS

    expected_names = set(ACP_SUPPORTED_COMMANDS)

    assert pushed_names == expected_names, f"Mismatch: pushed={pushed_names}, expected={expected_names}"


@pytest.mark.asyncio
async def test_resume_session_pushes_available_commands(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """resume_session should push available_commands_update to the client."""
    _patch_server(monkeypatch, session_id="resume-cmd")
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)

    # Write a session file so resume_session can find it
    _write_session_file(
        tmp_path / "projects" / "-tmp",
        "resume-cmd",
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    )

    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    await server.resume_session(cwd="/tmp", session_id="resume-cmd")

    cmd_updates = [u for _, u in conn.updates if isinstance(u, acp.schema.AvailableCommandsUpdate)]
    assert len(cmd_updates) >= 1, "Expected at least one AvailableCommandsUpdate push after resume_session"
    assert len(cmd_updates[0].available_commands) >= 1


@pytest.mark.asyncio
async def test_pushed_commands_include_input_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands with arg_hint or arg_names should have input.hint in the push."""
    _patch_server_p14(monkeypatch)
    monkeypatch.setattr("iac_code.acp.server.get_active_provider_key", lambda: "dashscope")

    conn = FakeConn()
    server = ACPServer()
    server.conn = conn
    await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

    await server.new_session(cwd="/tmp")

    cmd_updates = [u for _, u in conn.updates if isinstance(u, acp.schema.AvailableCommandsUpdate)]
    assert len(cmd_updates) >= 1
    commands_by_name = {cmd.name: cmd for cmd in cmd_updates[0].available_commands}

    # "debug" uses arg_hint="[on|off]"
    debug_cmd = commands_by_name["debug"]
    assert debug_cmd.input is not None
    assert debug_cmd.input.root.hint == "[on|off]"

    # Only ACP-supported commands are pushed, model/effort are excluded
    assert "model" not in commands_by_name
    assert "effort" not in commands_by_name

    # "clear" has no arg_hint or arg_names -> input should be None
    clear_cmd = commands_by_name["clear"]
    assert clear_cmd.input is None
