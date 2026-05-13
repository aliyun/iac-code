from __future__ import annotations

import asyncio

import acp
import pytest

from iac_code.acp.server import ACPServer
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage


class FakeConn:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))


class SlowFakeLoop:
    """A FakeLoop that sleeps in run_streaming so we can cancel mid-flight."""

    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text="started")
        await asyncio.sleep(10)  # long enough to be cancelled
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class FastFakeLoop:
    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text=f"echo: {prompt}")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


_session_counter = 0


class FakeRuntime:
    def __init__(self, loop=None) -> None:
        global _session_counter
        _session_counter += 1
        self.session_id = f"cancel-s{_session_counter}"
        self.agent_loop = loop or FastFakeLoop()
        self.tool_registry = None


def _patch_server(monkeypatch: pytest.MonkeyPatch, loop=None) -> None:
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: FakeRuntime(loop=loop),
    )
    monkeypatch.setattr(
        "iac_code.acp.server.replace_bash_with_acp_terminal",
        lambda *args, **kwargs: None,
    )


@pytest.fixture(autouse=True)
def _reset_counter() -> None:
    global _session_counter
    _session_counter = 0


@pytest.mark.asyncio
async def test_cancel_during_prompt_returns_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0: Cancel during active prompt execution."""
    _patch_server(monkeypatch, loop=SlowFakeLoop())
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    created = await server.new_session(cwd="/tmp")
    sid = created.session_id

    # Start prompt as a task
    prompt_task = asyncio.create_task(
        server.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text="slow")],
        )
    )
    # Let prompt start running
    await asyncio.sleep(0.05)

    # Cancel the session
    await server.cancel(session_id=sid)

    response = await prompt_task
    assert response.stop_reason == "cancelled"


@pytest.mark.asyncio
async def test_cancel_idle_session_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: Cancel an idle session with no active task."""
    _patch_server(monkeypatch)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    created = await server.new_session(cwd="/tmp")
    sid = created.session_id

    # Cancel an idle session — should not raise
    await server.cancel(session_id=sid)


@pytest.mark.asyncio
async def test_reprompt_after_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: Re-prompt after cancellation."""
    slow_loop = SlowFakeLoop()
    _patch_server(monkeypatch, loop=slow_loop)
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    created = await server.new_session(cwd="/tmp")
    sid = created.session_id

    # First prompt — cancel it
    prompt_task = asyncio.create_task(
        server.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text="first")],
        )
    )
    await asyncio.sleep(0.05)
    await server.cancel(session_id=sid)
    resp1 = await prompt_task
    assert resp1.stop_reason == "cancelled"

    # Replace the loop with a fast one for the second prompt
    server.sessions[sid].agent_loop = FastFakeLoop()

    # Second prompt — should succeed
    resp2 = await server.prompt(
        session_id=sid,
        prompt=[acp.schema.TextContentBlock(type="text", text="second")],
    )
    assert resp2.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_concurrent_cancel_multiple_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: Concurrently cancel multiple sessions."""
    _patch_server(monkeypatch, loop=SlowFakeLoop())
    conn = FakeConn()
    server = ACPServer()
    server.on_connect(conn)

    session_ids: list[str] = []
    prompt_tasks: list[asyncio.Task] = []

    for _ in range(3):
        created = await server.new_session(cwd="/tmp")
        session_ids.append(created.session_id)
        # All sessions share SlowFakeLoop, replace each with its own instance
        server.sessions[created.session_id].agent_loop = SlowFakeLoop()
        task = asyncio.create_task(
            server.prompt(
                session_id=created.session_id,
                prompt=[acp.schema.TextContentBlock(type="text", text="work")],
            )
        )
        prompt_tasks.append(task)

    await asyncio.sleep(0.05)

    # Cancel all sessions concurrently
    await asyncio.gather(*(server.cancel(session_id=sid) for sid in session_ids))

    responses = await asyncio.gather(*prompt_tasks)
    for resp in responses:
        assert resp.stop_reason == "cancelled"
