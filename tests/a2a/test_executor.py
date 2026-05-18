import asyncio
from pathlib import Path

import pytest
from a2a.types import TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.types.stream_events import PermissionRequestEvent, TextDeltaEvent, ToolResultEvent

from .fakes import FakeAgentLoop, FakeEventQueue, FakeRequestContext, FakeRuntime, pending_future


def dump(event):
    return MessageToDict(event, preserving_proto_field_name=False)


@pytest.mark.asyncio
async def test_executor_runs_prompt_and_finishes_input_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    assert loop.prompts == ["hello"]
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert states[0] == "TASK_STATE_SUBMITTED"
    assert "TASK_STATE_WORKING" in states
    assert states[-1] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "".join(record.output_text) == "hi"


@pytest.mark.asyncio
async def test_executor_passes_artifact_store_to_stream_event_publisher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact_store = object()
    seen_artifact_stores: list[object | None] = []
    seen_auto_approve_permissions: list[bool] = []

    async def spy_publish_stream_event(
        event_queue,
        *,
        task_id,
        context_id,
        event,
        artifact_store=None,
        permission_resolver=None,
        auto_approve_permissions=False,
    ):
        seen_artifact_stores.append(artifact_store)
        seen_auto_approve_permissions.append(auto_approve_permissions)
        return None

    loop = FakeAgentLoop(
        [
            ToolResultEvent(
                tool_use_id="tool-1",
                tool_name="write_file",
                result={"artifact": {"filename": "out.txt", "content": "hello", "mediaType": "text/plain"}},
                is_error=False,
            )
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.a2a.executor.publish_stream_event", spy_publish_stream_event)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", artifact_store=artifact_store)

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert seen_artifact_stores == [artifact_store]
    assert seen_auto_approve_permissions == [False]


@pytest.mark.asyncio
async def test_executor_auto_approves_permissions_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    future = pending_future()
    loop = FakeAgentLoop(
        [
            PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-1",
                response_future=future,
            )
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        auto_approve_permissions=True,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert future.result() is True
    permission_events = [
        dump(event)["metadata"]["iac_code"]["permission"]
        for event in queue.events
        if "permission" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert permission_events[0]["autoApproved"] is True


@pytest.mark.asyncio
async def test_executor_persists_terminal_task_state_and_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="persisted output")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    persistence = A2APersistenceStore(tmp_path / "state")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    snapshot = persistence.load_task("task-1")
    assert snapshot is not None
    assert snapshot.state == "input-required"
    assert snapshot.output_text == ["persisted output"]


@pytest.mark.asyncio
async def test_executor_persists_working_state_for_interrupted_restoration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(60)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    persistence = A2APersistenceStore(tmp_path / "state")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})
    queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(context, queue))
    await started.wait()

    task_snapshot = persistence.load_task("task-1")
    context_snapshot = persistence.load_context("ctx-1")
    assert task_snapshot is not None
    assert task_snapshot.state == "working"
    assert context_snapshot is not None
    assert context_snapshot.active_task_id == "task-1"

    await executor.cancel(context, queue)
    await running


@pytest.mark.asyncio
async def test_executor_notifies_push_for_terminal_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class SpyPushNotifier:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def notify_task_state(self, **kwargs) -> bool:
            self.calls.append(kwargs)
            return True

    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    notifier = SpyPushNotifier()
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", push_notifier=notifier)

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert notifier.calls == [{"task_id": "task-1", "context_id": "ctx-1", "state": "input-required"}]


@pytest.mark.asyncio
async def test_executor_logs_and_swallows_push_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingPushNotifier:
        async def notify_task_state(self, **kwargs) -> bool:
            raise RuntimeError("push endpoint down")

    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("internal failure")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", push_notifier=FailingPushNotifier())

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert "A2A push notification failed" in caplog.text


@pytest.mark.asyncio
async def test_executor_rejects_invalid_workspace(tmp_path: Path) -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path / "missing")}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "workspace" in dumped["status"]["message"]["parts"][0]["text"].lower()


@pytest.mark.asyncio
async def test_executor_rejects_workspace_outside_allowed_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(allowed))
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(outside)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "workspace" in dumped["status"]["message"]["parts"][0]["text"].lower()


@pytest.mark.asyncio
async def test_executor_reports_invalid_task_id() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(task_id="../bad")

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "Invalid A2A id"


@pytest.mark.asyncio
async def test_executor_rejects_empty_prompt_before_creating_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_if_called(options):
        raise AssertionError("runtime should not be created for empty prompt")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fail_if_called)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(text="   ", metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A2A server currently accepts text input only."


@pytest.mark.asyncio
async def test_cancel_bypasses_context_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(60)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})
    queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(context, queue))
    await started.wait()

    await executor.cancel(context, queue)
    await running

    assert dump(queue.events[-1])["status"]["state"] == "TASK_STATE_CANCELED"


@pytest.mark.asyncio
async def test_same_context_concurrent_message_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(60)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    first = FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}})
    second = FakeRequestContext(task_id="task-2", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}})
    first_queue = FakeEventQueue()
    second_queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(first, first_queue))
    await started.wait()

    await executor.execute(second, second_queue)
    await executor.cancel(first, first_queue)
    await running

    dumped = dump(second_queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "already working" in dumped["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_same_context_lock_race_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class ContendedLock:
        def __init__(self) -> None:
            self.acquire_requested = asyncio.Event()
            self.acquire_waiter = asyncio.get_running_loop().create_future()

        def acquire(self) -> asyncio.Future[bool]:
            self.acquire_requested.set()
            return self.acquire_waiter

        def release(self) -> None:
            raise AssertionError("release should not be called when acquire times out")

    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="never")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda sid: runtime,
    )
    lock = ContendedLock()
    ctx.lock = lock

    async def deterministic_timeout(awaitable, timeout):
        assert awaitable is lock.acquire_waiter
        assert timeout == 1
        raise TimeoutError

    monkeypatch.setattr("iac_code.a2a.executor.asyncio.wait_for", deterministic_timeout)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-2",
            context_id="ctx-1",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert lock.acquire_requested.is_set()
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "already working" in dumped["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_independent_contexts_execute_concurrently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prompts: list[str] = []

    class FastLoop:
        async def run_streaming(self, prompt: str):
            prompts.append(prompt)
            await asyncio.sleep(0)
            yield TextDeltaEvent(text=prompt)

    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(agent_loop=FastLoop(), session_id=options.session_id),
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await asyncio.gather(
        executor.execute(
            FakeRequestContext(
                task_id="task-1", context_id="ctx-1", text="one", metadata={"iac_code": {"cwd": str(tmp_path)}}
            ),
            FakeEventQueue(),
        ),
        executor.execute(
            FakeRequestContext(
                task_id="task-2", context_id="ctx-2", text="two", metadata={"iac_code": {"cwd": str(tmp_path)}}
            ),
            FakeEventQueue(),
        ),
    )

    assert sorted(prompts) == ["one", "two"]


@pytest.mark.asyncio
async def test_auth_error_is_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def raise_auth_error(options):
        raise ValueError("provider not configured: secret internal detail")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", raise_auth_error)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert (
        dumped["status"]["message"]["parts"][0]["text"]
        == "Authentication required. Please configure your API credentials."
    )


@pytest.mark.asyncio
async def test_retryable_executor_error_returns_input_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class TimeoutLoop:
        async def run_streaming(self, prompt: str):
            raise TimeoutError("upstream timed out")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=TimeoutLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."


@pytest.mark.asyncio
async def test_retryable_setup_error_returns_input_required(tmp_path: Path) -> None:
    class TimeoutTaskStore(A2ATaskStore):
        async def ensure_task_not_expired(self, task_id: str) -> None:
            raise TimeoutError("task store timed out")

    store = TimeoutTaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."


@pytest.mark.asyncio
async def test_unexpected_error_is_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("internal path /secret/config.yml leaked")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "An internal error occurred."
