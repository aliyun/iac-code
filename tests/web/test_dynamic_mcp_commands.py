import asyncio
import threading
import time

import httpx
import pytest


class _PromptProvider:
    async def get_prompt(self, args: str, _context) -> str:
        return "Expanded MCP prompt: {}".format(args)


def _mcp_registry():
    from iac_code.commands.registry import CommandRegistry, PromptCommand
    from iac_code.skills.frontmatter import SkillFrontmatter
    from iac_code.skills.skill_definition import SkillDefinition
    from iac_code.types.skill_source import SkillSource

    command = PromptCommand(
        name="mcp__remote__review",
        description="Review using remote MCP",
        skill=SkillDefinition(
            name="mcp__remote__review",
            description="Review using remote MCP",
            frontmatter=SkillFrontmatter(description="Review using remote MCP"),
            content="",
            source=SkillSource.PROJECT,
            file_path="mcp://remote/prompt/review",
            content_length=0,
            _prompt_provider=_PromptProvider(),
        ),
        source=SkillSource.PROJECT,
    )
    registry = CommandRegistry()
    registry.register(command)
    return registry


class _DynamicRuntime:
    def __init__(self) -> None:
        self.command_registry = _mcp_registry()
        self.agent_loop = object()
        self.mcp_manager = None
        self.mcp_config_warnings = []
        self.mcp_pending_configs = []
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_suggestions_use_dynamic_mcp_registry_and_close_ephemeral_runtimes(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    runtimes: list[_DynamicRuntime] = []

    def create_runtime(_options):
        runtime = _DynamicRuntime()
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="dynamic-suggestions")
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        slash = await client.get(
            "/api/suggestions",
            params={"kind": "command", "q": "mcp", "sessionId": session.session_id},
        )
        skill = await client.get(
            "/api/suggestions",
            params={"kind": "skill", "q": "mcp", "sessionId": session.session_id},
        )

    assert slash.status_code == 200
    assert skill.status_code == 200
    assert slash.json()["suggestions"][0]["value"] == "/mcp__remote__review"
    assert skill.json()["suggestions"][0]["value"] == "$mcp__remote__review"
    assert len(runtimes) == 1
    assert all(runtime.closed for runtime in runtimes)


@pytest.mark.asyncio
async def test_slow_mcp_runtime_does_not_block_slash_menu(tmp_path, monkeypatch) -> None:
    """A slow MCP connect must not stall the slash menu: built-in commands come back
    immediately from the static registry while the dynamic snapshot warms in the background."""
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    release = threading.Event()

    def create_runtime(_options):
        release.wait(timeout=2)
        return _DynamicRuntime()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="slow-mcp-slash-menu")
    app = create_app(session_manager=manager)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            started_at = time.monotonic()
            response = await client.get(
                "/api/suggestions",
                params={"kind": "command", "q": "", "sessionId": session.session_id},
            )
            elapsed = time.monotonic() - started_at
    finally:
        release.set()

    assert response.status_code == 200
    # Did NOT wait for the ~2s runtime build.
    assert elapsed < 0.8
    # Static built-in commands are still served.
    suggestions = response.json()["suggestions"]
    assert suggestions
    assert all(item["value"].startswith("/") for item in suggestions)


@pytest.mark.asyncio
async def test_dynamic_suggestion_runtime_creation_does_not_block_other_requests(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    release = threading.Event()

    def create_runtime(_options):
        release.wait(timeout=1)
        return _DynamicRuntime()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="nonblocking-dynamic-suggestions")
    app = create_app(session_manager=manager)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            started_at = time.monotonic()
            suggestion_task = asyncio.create_task(
                client.get(
                    "/api/suggestions",
                    params={"kind": "command", "q": "mcp", "sessionId": session.session_id},
                )
            )
            health_task = asyncio.create_task(client.get("/health"))
            health_response = await health_task
            health_elapsed = time.monotonic() - started_at
            suggestion_response = await suggestion_task
    finally:
        release.set()
        timer.cancel()

    assert health_response.status_code == 200
    assert health_elapsed < 0.15
    assert suggestion_response.status_code == 200


@pytest.mark.asyncio
async def test_concurrent_dynamic_suggestions_share_one_runtime_snapshot(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    calls = 0
    runtimes: list[_DynamicRuntime] = []
    lock = threading.Lock()

    def create_runtime(_options):
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.1)
        runtime = _DynamicRuntime()
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="single-flight-suggestions")
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        responses = await asyncio.gather(
            *(
                client.get(
                    "/api/suggestions",
                    params={"kind": kind, "q": query, "sessionId": session.session_id},
                )
                for kind, query in (("command", "m"), ("command", "mcp"), ("skill", "m"), ("skill", "mcp"))
            )
        )

    deadline = time.monotonic() + 2.0
    while (not runtimes or not runtimes[0].closed) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert all(response.status_code == 200 for response in responses)
    assert calls == 1
    assert len(runtimes) == 1
    assert runtimes[0].closed is True


@pytest.mark.asyncio
async def test_dynamic_command_runtime_creation_does_not_block_other_requests(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    release = threading.Event()

    def create_runtime(_options):
        release.wait(timeout=1)
        return _DynamicRuntime()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="nonblocking-dynamic-command")
    started = asyncio.Event()

    class RecordingTurnRuntime:
        async def start_turn(self, request):
            started.set()
            return {"accepted": True, "turnId": request.turn_id}

    app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingTurnRuntime())
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            started_at = time.monotonic()
            command_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session.session_id}/commands",
                    json={"command": "/mcp__remote__review details"},
                )
            )
            await asyncio.sleep(0)
            health_response = await client.get("/health")
            health_elapsed = time.monotonic() - started_at
            command_response = await command_task
            await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        release.set()
        timer.cancel()

    assert health_response.status_code == 200
    assert health_elapsed < 0.15
    assert command_response.status_code == 202


@pytest.mark.parametrize("lifecycle_action", ["archive", "delete"])
@pytest.mark.asyncio
async def test_dynamic_command_runtime_creation_blocks_session_lifecycle(
    tmp_path,
    monkeypatch,
    lifecycle_action,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    runtime_started = threading.Event()
    release_runtime = threading.Event()

    def create_runtime(_options):
        runtime_started.set()
        release_runtime.wait(timeout=1)
        return _DynamicRuntime()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="dynamic-command-lifecycle")

    class RecordingTurnRuntime:
        async def start_turn(self, request):
            return {"accepted": True, "turnId": request.turn_id}

    app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingTurnRuntime())

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            command_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session.session_id}/commands",
                    json={"command": "/mcp__remote__review details"},
                )
            )
            runtime_did_start = await asyncio.wait_for(asyncio.to_thread(runtime_started.wait, 1), timeout=2)
            assert runtime_did_start is True
            if lifecycle_action == "archive":
                lifecycle_response = await client.patch(
                    f"/api/sessions/{session.session_id}",
                    json={"archived": True},
                )
            else:
                lifecycle_response = await client.delete(f"/api/sessions/{session.session_id}")
            release_runtime.set()
            command_response = await asyncio.wait_for(command_task, timeout=2)
    finally:
        release_runtime.set()

    assert lifecycle_response.status_code == 409
    if lifecycle_action == "archive":
        assert lifecycle_response.json()["error"]["code"] == "session_busy"
    else:
        assert lifecycle_response.json()["reason"] == "turn already running"
    assert command_response.status_code == 202


@pytest.mark.asyncio
async def test_cancelled_dynamic_command_releases_session_reservation_and_closes_late_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    runtime_started = threading.Event()
    release_runtime = threading.Event()
    runtimes: list[_DynamicRuntime] = []

    def create_runtime(_options):
        runtime_started.set()
        release_runtime.wait()
        runtime = _DynamicRuntime()
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="cancelled-dynamic-command")
    app = create_app(session_manager=manager)

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            command_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session.session_id}/commands",
                    json={"command": "/mcp__remote__review details"},
                )
            )
            runtime_did_start = await asyncio.wait_for(asyncio.to_thread(runtime_started.wait, 1), timeout=2)
            assert runtime_did_start is True

            command_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command_task

            archive = await client.patch(
                f"/api/sessions/{session.session_id}",
                json={"archived": True},
            )
    finally:
        release_runtime.set()

    assert archive.status_code == 200
    for _ in range(100):
        if runtimes and runtimes[0].closed:
            break
        await asyncio.sleep(0.01)
    assert len(runtimes) == 1
    assert runtimes[0].closed is True


@pytest.mark.asyncio
async def test_interrupting_dynamic_command_cancels_discovery_before_archive(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    runtime_started = threading.Event()
    release_runtime = threading.Event()
    runtimes: list[_DynamicRuntime] = []

    def create_runtime(_options):
        runtime_started.set()
        release_runtime.wait(timeout=1)
        runtime = _DynamicRuntime()
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="interrupted-dynamic-command")
    app = create_app(session_manager=manager)

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            command_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session.session_id}/commands",
                    json={"command": "/mcp__remote__review details"},
                )
            )
            runtime_did_start = await asyncio.wait_for(asyncio.to_thread(runtime_started.wait, 1), timeout=2)
            assert runtime_did_start is True

            interrupted = await client.post(
                f"/api/sessions/{session.session_id}/interrupt",
                json={"message": ""},
            )
            command_response = await asyncio.wait_for(command_task, timeout=0.5)
            archive = await client.patch(
                f"/api/sessions/{session.session_id}",
                json={"archived": True},
            )
    finally:
        release_runtime.set()

    assert interrupted.status_code == 200
    assert command_response.status_code == 409
    assert command_response.json()["canceled"] is True
    assert archive.status_code == 200
    for _ in range(100):
        if runtimes and runtimes[0].closed:
            break
        await asyncio.sleep(0.01)
    assert len(runtimes) == 1
    assert runtimes[0].closed is True


@pytest.mark.asyncio
async def test_interrupting_dynamic_command_during_owner_handoff_cleans_reservation(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    runtime_started = threading.Event()
    release_runtime = threading.Event()

    def create_runtime(_options):
        runtime_started.set()
        release_runtime.wait()
        return _DynamicRuntime()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="dynamic-command-handoff-cancel")
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        command_task = asyncio.create_task(
            client.post(
                f"/api/sessions/{session.session_id}/commands",
                json={"command": "/mcp__remote__review details"},
            )
        )
        runtime_did_start = await asyncio.wait_for(asyncio.to_thread(runtime_started.wait, 1), timeout=2)
        assert runtime_did_start is True

        await session.turn_admission_lock.acquire()
        interrupt_task = asyncio.create_task(
            client.post(
                f"/api/sessions/{session.session_id}/interrupt",
                json={"message": ""},
            )
        )
        for _ in range(100):
            if len(session.turn_admission_lock._waiters or ()) >= 1:
                break
            await asyncio.sleep(0.01)
        assert len(session.turn_admission_lock._waiters or ()) == 1
        release_runtime.set()
        for _ in range(100):
            if len(session.turn_admission_lock._waiters or ()) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(session.turn_admission_lock._waiters or ()) == 2
        session.turn_admission_lock.release()

        interrupted = await asyncio.wait_for(interrupt_task, timeout=1)
        command_response = await asyncio.wait_for(command_task, timeout=1)

    assert interrupted.status_code == 200
    assert command_response.status_code == 409
    assert command_response.json()["canceled"] is True
    assert session.active_turn_task is None
    assert session.turn_admission_lock.locked() is False


@pytest.mark.asyncio
async def test_cancelling_dynamic_command_during_owner_handoff_cleans_reservation(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    runtime_started = threading.Event()
    release_runtime = threading.Event()

    def create_runtime(_options):
        runtime_started.set()
        release_runtime.wait()
        return _DynamicRuntime()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="dynamic-command-handoff-client-cancel")
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        command_task = asyncio.create_task(
            client.post(
                f"/api/sessions/{session.session_id}/commands",
                json={"command": "/mcp__remote__review details"},
            )
        )
        runtime_did_start = await asyncio.wait_for(asyncio.to_thread(runtime_started.wait, 1), timeout=2)
        assert runtime_did_start is True

        await session.turn_admission_lock.acquire()
        release_runtime.set()
        for _ in range(100):
            if len(session.turn_admission_lock._waiters or ()) >= 1:
                break
            await asyncio.sleep(0.01)
        assert len(session.turn_admission_lock._waiters or ()) == 1
        command_task.cancel()
        session.turn_admission_lock.release()

        with pytest.raises(asyncio.CancelledError):
            await command_task

    assert session.active_turn_task is None
    assert session.turn_admission_lock.locked() is False


@pytest.mark.asyncio
async def test_command_route_invokes_dynamic_mcp_prompt_before_closing_runtime(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    dynamic_runtime = _DynamicRuntime()
    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: dynamic_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="dynamic-dispatch")
    started = asyncio.Event()
    requests = []

    class RecordingTurnRuntime:
        async def start_turn(self, request):
            requests.append(request)
            started.set()
            return {"accepted": True, "turnId": request.turn_id}

    app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingTurnRuntime())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/sessions/{session.session_id}/commands",
            json={"command": "/mcp__remote__review details"},
        )
        await asyncio.wait_for(started.wait(), timeout=1)

    assert response.status_code == 202
    assert requests[0].text == "Expanded MCP prompt: details"
    assert requests[0].source == "skill"
    assert dynamic_runtime.closed is True


@pytest.mark.asyncio
async def test_busy_dynamic_command_does_not_expand_prompt_before_reservation(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    dynamic_runtime = _DynamicRuntime()
    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: dynamic_runtime)
    process_calls = 0

    async def process_prompt_command(*_args, **_kwargs):
        nonlocal process_calls
        process_calls += 1
        raise AssertionError("busy dynamic command must not expand its prompt")

    monkeypatch.setattr("iac_code.skills.processor.process_prompt_command", process_prompt_command)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="busy-dynamic-command")
    blocker = asyncio.create_task(asyncio.Event().wait())
    session.active_turn_task = blocker
    app = create_app(session_manager=manager)

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                f"/api/sessions/{session.session_id}/commands",
                json={"command": "/mcp__remote__review details"},
            )
    finally:
        session.active_turn_task = None
        blocker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocker

    assert response.status_code == 409
    assert process_calls == 0
    assert dynamic_runtime.closed is False
