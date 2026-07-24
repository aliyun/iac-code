import asyncio
import threading
import time
from contextlib import contextmanager

import pytest


class _ClosableRuntime:
    def __init__(self, agent_loop, *, command_registry=None) -> None:
        self.agent_loop = agent_loop
        self.command_registry = command_registry
        self.tool_registry = {}
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_web_turn_runtime_creation_does_not_block_event_loop(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input):
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    release = threading.Event()
    agent_runtime = _ClosableRuntime(FakeAgentLoop())

    def create_runtime(_session, _manager, **_kwargs):
        release.wait(timeout=1)
        return agent_runtime

    monkeypatch.setattr(runtime_module, "create_session_agent_runtime", create_runtime)
    monkeypatch.setattr(runtime_module, "flush_telemetry", lambda: None)
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-nonblocking-runtime")
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        started_at = time.monotonic()
        turn_task = asyncio.create_task(
            WebSessionRuntime(session, manager=manager).start_turn(
                WebTurnRequest(text="hello", image_ids=[], file_refs=[])
            )
        )
        await asyncio.sleep(0.01)
        event_loop_delay = time.monotonic() - started_at
        result = await turn_task
    finally:
        release.set()
        timer.cancel()

    assert event_loop_delay < 0.15
    assert result["accepted"] is True
    assert agent_runtime.closed is True


@pytest.mark.asyncio
async def test_web_turn_cancellation_closes_runtime_created_after_cancellation(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    class FakeAgentLoop:
        async def run_streaming(self, _user_input):
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    factory_started = threading.Event()
    release_factory = threading.Event()
    agent_runtime = _ClosableRuntime(FakeAgentLoop())

    def create_runtime(_session, _manager, **_kwargs):
        factory_started.set()
        release_factory.wait(timeout=1)
        return agent_runtime

    monkeypatch.setattr(runtime_module, "create_session_agent_runtime", create_runtime)
    monkeypatch.setattr(runtime_module, "flush_telemetry", lambda: None)
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-cancel-runtime-creation")
    turn_task = asyncio.create_task(
        WebSessionRuntime(session, manager=manager).start_turn(WebTurnRequest(text="hello", image_ids=[], file_refs=[]))
    )

    assert await asyncio.to_thread(factory_started.wait, 1)
    turn_task.cancel()
    release_factory.set()
    result = await turn_task
    for _attempt in range(50):
        if agent_runtime.closed:
            break
        await asyncio.sleep(0.01)

    assert result["reason"] == "turn canceled"
    assert agent_runtime.closed is True


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_web_turn_closes_agent_runtime_and_flushes_telemetry_on_every_exit(
    tmp_path,
    monkeypatch,
    outcome: str,
) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    trace: list[object] = []

    class FakeAgentLoop:
        async def run_streaming(self, _user_input):
            trace.append("stream")
            if outcome == "error":
                raise RuntimeError("turn failed")
            if outcome == "cancel":
                raise asyncio.CancelledError
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    agent_runtime = _ClosableRuntime(FakeAgentLoop())
    original_close = agent_runtime.aclose

    async def close_with_trace() -> None:
        trace.append("close")
        await original_close()

    agent_runtime.aclose = close_with_trace
    monkeypatch.setattr(runtime_module, "create_agent_runtime", lambda _options: agent_runtime)
    monkeypatch.setattr(runtime_module, "load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(runtime_module, "flush_telemetry", lambda: trace.append("flush"))

    @contextmanager
    def fake_use_session_id(session_id: str):
        trace.append(("telemetry-enter", session_id))
        yield
        trace.append(("telemetry-exit", session_id))

    monkeypatch.setattr(runtime_module, "use_session_id", fake_use_session_id)

    async def run_turn():
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-telemetry")
        return await WebSessionRuntime(session, manager=manager).start_turn(
            WebTurnRequest(text="hello", image_ids=[], file_refs=[])
        )

    result = asyncio.run(run_turn())

    assert agent_runtime.closed is True
    assert trace.count("close") == 1
    assert trace.count("flush") == 1
    assert trace[0] == ("telemetry-enter", "session-telemetry")
    assert trace.index("close") < trace.index("flush")
    if outcome == "success":
        assert result["accepted"] is True
    elif outcome == "cancel":
        assert result["reason"] == "turn canceled"
    else:
        assert result["reason"] == "runtime error"


def test_web_turn_passes_original_permission_event_to_audit_boundary(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, Usage
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    captured: dict[str, object] = {}

    class FakeAgentLoop:
        async def run_streaming(self, _user_input):
            future = asyncio.get_running_loop().create_future()
            event = PermissionRequestEvent(
                tool_name="bash",
                tool_input={"command": "pwd"},
                tool_use_id="tool-1",
                response_future=future,
            )
            captured["event"] = event
            yield event
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    agent_runtime = _ClosableRuntime(FakeAgentLoop())
    monkeypatch.setattr(runtime_module, "create_agent_runtime", lambda _options: agent_runtime)
    monkeypatch.setattr(runtime_module, "load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(runtime_module, "flush_telemetry", lambda: None)

    async def run_turn():
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-audit")

        def add_permission_request(_session, _payload, *, future, audit_event):
            captured["audit_event"] = audit_event
            future.set_result(True)
            return "request-1"

        monkeypatch.setattr(manager, "add_permission_request", add_permission_request)
        monkeypatch.setattr(manager, "discard_permission_request", lambda *_args, **_kwargs: None)
        return await WebSessionRuntime(session, manager=manager).start_turn(
            WebTurnRequest(text="hello", image_ids=[], file_refs=[])
        )

    result = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert captured["audit_event"] is captured["event"]


def test_web_turn_dispatches_dynamic_mcp_prompt_registry(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    registry = object()
    captured: dict[str, object] = {}

    class FakeAgentLoop:
        def run_streaming(self, _user_input):
            raise AssertionError("dynamic MCP prompt must not use the unexpanded stream")

    async def dynamic_stream():
        yield MessageEndEvent(stop_reason="stop", usage=Usage())

    async def fake_mcp_prompt_command_stream(*, agent_loop, commands, prompt, session_id):
        captured.update(
            agent_loop=agent_loop,
            commands=commands,
            prompt=prompt,
            session_id=session_id,
        )
        return dynamic_stream()

    agent_runtime = _ClosableRuntime(FakeAgentLoop(), command_registry=registry)
    monkeypatch.setattr(runtime_module, "create_agent_runtime", lambda _options: agent_runtime)
    monkeypatch.setattr(runtime_module, "mcp_prompt_command_stream", fake_mcp_prompt_command_stream)
    monkeypatch.setattr(runtime_module, "load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(runtime_module, "flush_telemetry", lambda: None)

    async def run_turn():
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-mcp")
        return await WebSessionRuntime(session, manager=manager).start_turn(
            WebTurnRequest(text="/mcp__remote__review details", image_ids=[], file_refs=[])
        )

    result = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert captured["commands"] is registry
    assert captured["prompt"] == "/mcp__remote__review details"
    assert captured["session_id"] == "session-mcp"


def test_web_turn_publishes_initial_and_changed_mcp_status(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    listeners = []
    status_calls: list[tuple[object, object, object]] = []

    class FakeAgentLoop:
        async def run_streaming(self, _user_input):
            await listeners[0]("remote", "prompts")
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    agent_runtime = _ClosableRuntime(FakeAgentLoop(), command_registry=object())
    agent_runtime.mcp_manager = object()
    agent_runtime.mcp_config_warnings = ["warning"]
    agent_runtime.mcp_pending_configs = ["pending"]
    agent_runtime.add_mcp_change_listener = listeners.append

    def fake_status(manager, *, warnings, pending_configs):
        status_calls.append((manager, warnings, pending_configs))
        return {"servers": [], "warnings": [{"message": "pending"}]}

    monkeypatch.setattr(runtime_module, "create_agent_runtime", lambda _options: agent_runtime)
    monkeypatch.setattr(runtime_module, "mcp_status_metadata", fake_status)
    monkeypatch.setattr(runtime_module, "load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(runtime_module, "flush_telemetry", lambda: None)

    async def run_turn():
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-mcp-status")
        result = await WebSessionRuntime(session, manager=manager).start_turn(
            WebTurnRequest(text="hello", image_ids=[], file_refs=[])
        )
        return result, session.events.replay_after(0)

    result, events = asyncio.run(run_turn())

    assert result["accepted"] is True
    assert [event["type"] for event in events].count("mcp.status.updated") == 2
    assert status_calls == [
        (agent_runtime.mcp_manager, ["warning"], ["pending"]),
        (agent_runtime.mcp_manager, ["warning"], ["pending"]),
    ]


def test_web_telemetry_flush_has_a_time_bound(monkeypatch) -> None:
    from iac_code.web import runtime as runtime_module

    async def blocked_to_thread(_callback):
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime_module.asyncio, "to_thread", blocked_to_thread)
    monkeypatch.setattr(runtime_module, "WEB_TELEMETRY_FLUSH_TIMEOUT_SECONDS", 0.005, raising=False)

    async def flush_with_outer_deadline() -> None:
        await asyncio.wait_for(runtime_module.flush_web_telemetry(), timeout=0.05)

    asyncio.run(flush_with_outer_deadline())


def test_web_turn_bounds_runtime_close_and_always_clears_active_state(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import MessageEndEvent, Usage
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import WebSessionRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    close_started = False

    class FakeAgentLoop:
        async def run_streaming(self, _user_input):
            yield MessageEndEvent(stop_reason="stop", usage=Usage())

    class HangingRuntime(_ClosableRuntime):
        async def aclose(self) -> None:
            nonlocal close_started
            close_started = True
            await asyncio.Event().wait()

    agent_runtime = HangingRuntime(FakeAgentLoop())
    monkeypatch.setattr(runtime_module, "create_agent_runtime", lambda _options: agent_runtime)
    monkeypatch.setattr(runtime_module, "load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(runtime_module, "flush_telemetry", lambda: None)
    monkeypatch.setattr(runtime_module, "WEB_RUNTIME_CLOSE_TIMEOUT_SECONDS", 0.005, raising=False)

    async def scenario():
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-hanging-close")
        result = await asyncio.wait_for(
            WebSessionRuntime(session, manager=manager).start_turn(
                WebTurnRequest(text="hello", image_ids=[], file_refs=[])
            ),
            timeout=0.1,
        )
        return result, session

    result, session = asyncio.run(scenario())

    assert result["accepted"] is True
    assert close_started is True
    assert session.active_turn_task is None
    assert session.active_agent_loop is None
    assert session.active_turn_id is None
    assert session.active_turn_floor_sequence is None


@pytest.mark.asyncio
async def test_prime_session_context_overhead_caches_from_built_runtime(tmp_path, monkeypatch) -> None:
    """切换会话时建一次 runtime,把系统提示 + 工具定义开销缓存到会话,并关闭 runtime。"""
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import prime_session_context_overhead
    from iac_code.web.session_manager import WebSessionManager

    class FakeContextManager:
        def get_usage(self):
            return {"system_prompt_tokens": 9000, "tool_definition_tokens": 4000}

    class FakeAgentLoop:
        context_manager = FakeContextManager()

    runtime = _ClosableRuntime(FakeAgentLoop())
    built = []

    async def fake_create(session, manager, **_kwargs):
        built.append(session)
        return runtime

    monkeypatch.setattr(runtime_module, "create_session_agent_runtime_in_thread", fake_create)

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-prime-overhead")
    assert session.context_system_prompt_tokens == 0
    assert session.context_tool_definition_tokens == 0

    await prime_session_context_overhead(session, manager)

    assert built == [session]
    assert runtime.closed is True
    assert session.context_system_prompt_tokens == 9000
    assert session.context_tool_definition_tokens == 4000


@pytest.mark.asyncio
async def test_prime_session_context_overhead_skips_when_already_known(tmp_path, monkeypatch) -> None:
    """已有开销(实时回合已算或此前已建过一次)时不再建 runtime。"""
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import prime_session_context_overhead
    from iac_code.web.session_manager import WebSessionManager

    async def fail_create(*_args, **_kwargs):
        raise AssertionError("runtime should not be built when overhead is already known")

    monkeypatch.setattr(runtime_module, "create_session_agent_runtime_in_thread", fail_create)

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-prime-skip")
    session.context_system_prompt_tokens = 8000

    await prime_session_context_overhead(session, manager)

    assert session.context_system_prompt_tokens == 8000
    assert session.context_tool_definition_tokens == 0


@pytest.mark.asyncio
async def test_prime_session_context_overhead_degrades_on_build_failure(tmp_path, monkeypatch) -> None:
    """建 runtime 失败时安全降级为 0,不抛出、不阻断会话切换。"""
    from iac_code.web import runtime as runtime_module
    from iac_code.web.runtime import prime_session_context_overhead
    from iac_code.web.session_manager import WebSessionManager

    async def boom_create(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(runtime_module, "create_session_agent_runtime_in_thread", boom_create)

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-prime-fail")

    await prime_session_context_overhead(session, manager)

    assert session.context_system_prompt_tokens == 0
    assert session.context_tool_definition_tokens == 0
