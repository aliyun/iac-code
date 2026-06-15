import asyncio
from types import SimpleNamespace

import pytest

from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.transports.dispatcher import (
    A2AJsonRpcDispatcher,
    A2ARuntimeComponents,
    IacCodeRequestHandler,
    create_runtime_components,
)
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime


@pytest.mark.asyncio
async def test_dispatcher_handles_unary_v03_message(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hello from dispatcher")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)

    response = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        }
    )

    assert response["id"] == "1"
    assert response["result"]["status"]["state"] == "input-required"
    assert loop.prompts == ["hello"]
    await components.aclose()


@pytest.mark.asyncio
async def test_dispatcher_stream_yields_events(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="streamed")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)

    events = [
        event
        async for event in dispatcher.dispatch_stream(
            {
                "jsonrpc": "2.0",
                "id": "2",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": "msg-2",
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hello"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            }
        )
    ]

    assert any(event["result"]["status"]["state"] == "working" for event in events)
    assert events[-1]["result"]["status"]["state"] == "input-required"
    await components.aclose()


@pytest.mark.asyncio
async def test_dispatcher_routes_second_pipeline_stream_as_interrupt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class BlockingPipeline:
        pipeline_name = "selling"
        sidecar_status = None
        sidecar_restore_result = None

        def __init__(self) -> None:
            self.session = SimpleNamespace(session_dir=tmp_path / "sidecar")
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.interrupts: list[str] = []

        async def run(self, prompt: str):
            yield TextDeltaEvent(text="before interrupt")
            self.started.set()
            await self.release.wait()
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821602.0,
                data={},
            )

        async def handle_user_interrupt(self, message: str):
            self.interrupts.append(message)
            return SimpleNamespace(
                action="supplement",
                reason="added context",
                rollback_target=None,
                candidate_scope=None,
                supplement_target=None,
            )

    pipeline = BlockingPipeline()
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr(
        "iac_code.a2a.pipeline_executor.create_agent_runtime",
        lambda options: SimpleNamespace(provider_manager=object(), tool_registry=object()),
    )
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)

    first_events: list[dict] = []

    async def consume_first_stream() -> None:
        async for event in dispatcher.dispatch_stream(
            {
                "jsonrpc": "2.0",
                "id": "first",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": "msg-first",
                        "role": "user",
                        "parts": [{"kind": "text", "text": "start"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            }
        ):
            first_events.append(event)

    first_task = asyncio.create_task(consume_first_stream())
    await asyncio.wait_for(pipeline.started.wait(), timeout=1)
    identity = _active_task_identity(components)

    async def consume_second_stream() -> None:
        async for _event in dispatcher.dispatch_stream(
            {
                "jsonrpc": "2.0",
                "id": "second",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": "msg-second",
                        "role": "user",
                        "parts": [{"kind": "text", "text": "please add this"}],
                        "contextId": identity.context_id,
                        "taskId": identity.task_id,
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            }
        ):
            pass

    second_task = asyncio.create_task(consume_second_stream())
    for _ in range(100):
        if pipeline.interrupts:
            break
        await asyncio.sleep(0.01)

    try:
        assert pipeline.interrupts == ["please add this"]
        event_types = [event["eventType"] for event in A2APipelineJournal(pipeline.session.session_dir).read_all()]
        assert "interrupt_received" in event_types
        assert "interrupt_classified" in event_types
        await asyncio.wait_for(second_task, timeout=1)
    finally:
        if not second_task.done():
            second_task.cancel()
            try:
                await second_task
            except asyncio.CancelledError:
                pass
        pipeline.release.set()
        await asyncio.wait_for(first_task, timeout=1)
        await dispatcher.aclose()
        await components.aclose()


def test_create_runtime_components_returns_shared_objects() -> None:
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)

    assert isinstance(components, A2ARuntimeComponents)
    assert components.handler is not None
    assert components.task_store is not None


@pytest.mark.asyncio
async def test_dispatcher_reuses_http_client(monkeypatch) -> None:
    created = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}

    class FakeHTTPClient:
        def __init__(self, **kwargs) -> None:
            nonlocal created
            created += 1

        async def post(self, *args, **kwargs):
            return FakeResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.httpx.AsyncClient", FakeHTTPClient)
    dispatcher = A2AJsonRpcDispatcher(create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242))

    await dispatcher.dispatch({"jsonrpc": "2.0", "id": "1", "method": "message/send"})
    await dispatcher.dispatch({"jsonrpc": "2.0", "id": "2", "method": "message/send"})
    await dispatcher.aclose()

    assert created == 1


@pytest.mark.asyncio
async def test_active_message_stream_cancellation_cancels_producer() -> None:
    producer_cancelled = asyncio.Event()

    class FakeAgentExecutor:
        async def execute(self, request_context, event_queue_agent):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                producer_cancelled.set()
                raise

    class FakeRequestContextBuilder:
        async def build(self, **kwargs):
            return SimpleNamespace()

    class FakeTappedQueue:
        async def dequeue_event(self):
            await asyncio.Event().wait()

        async def close(self, *, immediate: bool = False) -> None:
            return None

        async def _put_internal(self, item) -> None:
            return None

        def task_done(self) -> None:
            return None

    class FakeSubscribers:
        def __init__(self, tapped_queue: FakeTappedQueue) -> None:
            self.tapped_queue = tapped_queue

        async def tap(self) -> FakeTappedQueue:
            return self.tapped_queue

    class FakeActiveTask:
        def __init__(self) -> None:
            self.task_id = "task-1"
            self._lock = asyncio.Lock()
            self._is_finished = asyncio.Event()
            self._reference_count = 0
            self._event_queue_agent = SimpleNamespace()
            self._event_queue_subscribers = FakeSubscribers(FakeTappedQueue())

        async def _maybe_cleanup(self) -> None:
            return None

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.agent_executor = FakeAgentExecutor()
    handler._request_context_builder = FakeRequestContextBuilder()
    active_task = FakeActiveTask()
    params = SimpleNamespace(message=SimpleNamespace(context_id="ctx-1"), configuration=None)
    task = SimpleNamespace(id="task-1")

    async def consume() -> None:
        async for _event in handler._on_active_message_send_stream(
            params, object(), task=task, active_task=active_task
        ):
            pass

    stream_task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    stream_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stream_task, timeout=1)
    assert producer_cancelled.is_set()
    assert active_task._reference_count == 0


def _active_task_identity(components: A2ARuntimeComponents) -> SimpleNamespace:
    tasks = list(components.task_store._tasks.values())  # noqa: SLF001
    assert len(tasks) == 1
    task = tasks[0]
    return SimpleNamespace(task_id=task.task_id, context_id=task.context_id)
