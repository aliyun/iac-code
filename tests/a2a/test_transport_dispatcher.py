import asyncio
import base64
import contextlib
import json
import shutil
import threading
from types import SimpleNamespace

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import Message, Part, Role, SubscribeToTaskRequest, Task, TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.struct_pb2 import Value

from iac_code.a2a.input_required import PERMISSION_QUERY_PREFIX
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.pipeline_transport_delivery import (
    PipelineTransportDeliveryClosedError,
    bind_pipeline_transport_delivery_tracker,
    close_pipeline_transport_delivery_tracker,
    create_pipeline_transport_delivery_tracker,
    pipeline_transport_delivery_tracking_enabled,
    register_pipeline_transport_delivery,
)
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.a2a.transports.dispatcher import (
    A2AJsonRpcDispatcher,
    A2ARuntimeComponents,
    IacCodeRequestHandler,
    _StreamingASGITransport,
    create_runtime_components,
)
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import PermissionRequestEvent, TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime, pending_future

_STREAM_TEST_TIMEOUT = 5


@pytest.mark.asyncio
async def test_closed_transport_tracker_reports_closed_stage_on_registration() -> None:
    tracker = create_pipeline_transport_delivery_tracker()
    close_pipeline_transport_delivery_tracker(tracker)
    stages: list[str] = []

    with bind_pipeline_transport_delivery_tracker(tracker):
        completion = register_pipeline_transport_delivery(
            object(),
            stage_observer=lambda stage, _at_ns: stages.append(stage),
        )

    with pytest.raises(PipelineTransportDeliveryClosedError):
        await completion
    assert stages == ["registered", "closed"]


@pytest.mark.asyncio
async def test_dispatcher_handles_unary_v03_message(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hello from dispatcher")])

    def factory(options):
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)
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
    session_id = components.task_store._contexts[response["result"]["contextId"]].session_id
    assert response["result"]["metadata"]["iac_code"]["iacCodeSessionId"] == session_id
    assert loop.prompts == ["hello"]
    await components.aclose()


@pytest.mark.asyncio
async def test_dispatcher_rejects_explicit_invalid_run_mode(tmp_path) -> None:
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)

    response = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "invalid-run-mode",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "msg-invalid-run-mode",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path), "run_mode": "pipline"}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        }
    )

    assert response["id"] == "invalid-run-mode"
    assert response["error"]["code"] == -32602
    assert response["error"]["message"] == "Unsupported run mode."
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
async def test_handler_reconciles_terminal_task_when_pipeline_sidecar_is_waiting_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    context_id = "ctx-1"
    task_id = "task-1"
    call_context = ServerCallContext()
    store = A2ATaskStore()
    ctx = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda session_id: SimpleNamespace(session_id=session_id),
    )
    ctx.active_task_id = task_id
    store.mirror_context(ctx)
    await store.save(
        Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
        ),
        call_context,
    )

    pending_input = {
        "inputId": "input-confirm_and_select-1",
        "kind": "candidate_selection",
        "prompt": "请选择方案",
        "options": [{"name": "方案A", "candidate_index": 0}],
    }
    pending_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": task_id,
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": pending_input,
        "data": pending_input,
    }
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=ctx.session_id)
    A2APipelineJournal(pipeline_dir).append(pending_event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending_event]))
    observed: dict[str, int] = {}

    async def sdk_send(_handler, _params, sdk_context):
        task = await store.get(task_id, sdk_context)
        assert task is not None
        observed["state"] = task.status.state
        return task

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send", sdk_send)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    params = SimpleNamespace(message=SimpleNamespace(task_id=task_id, context_id=context_id))

    result = await handler.on_message_send(params, call_context)

    assert isinstance(result, Task)
    assert observed["state"] == TaskState.TASK_STATE_INPUT_REQUIRED
    assert result.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    session_dir = SessionStorage().session_dir(str(cwd), ctx.session_id)
    context_snapshot = json.loads((session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))
    assert context_snapshot["active_task_id"] is None


@pytest.mark.asyncio
async def test_handler_restores_backup_before_hydrating_omitted_pipeline_task_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    context_id = "ctx-restore"
    task_id = "task-restore"
    store = A2ATaskStore()
    ctx = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda session_id: SimpleNamespace(session_id=session_id),
    )
    storage = SessionStorage()
    storage.save(str(cwd), ctx.session_id, [])
    pending_input = {
        "inputId": "input-confirm_and_select-1",
        "kind": "candidate_selection",
        "prompt": "请选择方案",
        "options": [{"name": "方案A", "candidate_index": 0}],
    }
    pending_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": task_id,
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": pending_input,
        "data": pending_input,
    }
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=ctx.session_id)
    A2APipelineJournal(pipeline_dir).append(pending_event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending_event]))
    backup_service = SessionBackupService(storage, retry_delays=())
    backup_service.initialize_session(str(cwd), ctx.session_id)
    backup_service.backup_session(str(cwd), ctx.session_id, reason=BackupReason.INPUT_REQUIRED, critical=True)
    primary_session_dir = storage.session_dir(str(cwd), ctx.session_id)
    shutil.rmtree(primary_session_dir)

    class FakeExecutor:
        async def _reconcile_session_before_route(self, *, context_id: str, cwd: str):
            assert context_id == "ctx-restore"
            return await asyncio.to_thread(backup_service.reconcile_session, cwd, ctx.session_id)

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = FakeExecutor()
    params = SimpleNamespace(message=SimpleNamespace(task_id=None, context_id=context_id))

    await handler._hydrate_recoverable_pipeline_task_id(params)

    assert params.message.task_id == task_id
    assert storage.session_dir(str(cwd), ctx.session_id).is_dir()


@pytest.mark.asyncio
async def test_dispatcher_stream_backpressures_asgi_until_consumer_resumes() -> None:
    first_chunk_consumed = asyncio.Event()

    async def app(_scope, _receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'data: {"id":"1","result":{"index":1}}\n\n',
                "more_body": True,
            }
        )
        first_chunk_consumed.set()
        await send(
            {
                "type": "http.response.body",
                "body": b'data: {"id":"1","result":{"index":2}}\n\n',
                "more_body": False,
            }
        )

    dispatcher = A2AJsonRpcDispatcher(SimpleNamespace(app=app))
    stream = dispatcher.dispatch_stream({"jsonrpc": "2.0", "id": "1"})

    first = await anext(stream)
    assert first["result"]["index"] == 1
    assert first_chunk_consumed.is_set() is False

    second = await anext(stream)
    assert first_chunk_consumed.is_set() is True
    assert second["result"]["index"] == 2
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_streaming_asgi_transport_cancels_app_before_response_start() -> None:
    app_started = asyncio.Event()
    app_cancelled = asyncio.Event()

    async def app(_scope, _receive, _send) -> None:
        app_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            app_cancelled.set()
            raise

    transport = _StreamingASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://transport.local") as client:
        request_task = asyncio.create_task(client.get("/"))
        await app_started.wait()

        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task

    assert app_cancelled.is_set()
    assert not any(task.get_name() == "a2a-streaming-asgi-dispatch" and not task.done() for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_message_stream_acknowledges_transport_delivery_only_when_resumed(monkeypatch) -> None:
    observed: dict[str, asyncio.Future[None]] = {}
    stages: list[str] = []
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )

    async def sdk_stream(_handler, _params, _context):
        observed["completion"] = register_pipeline_transport_delivery(
            update,
            stage_observer=lambda stage, _at_ns: stages.append(stage),
        )
        yield update

    async def hydrate(_params) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = object()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    params = SimpleNamespace(message=SimpleNamespace(task_id=None))

    stream = handler.on_message_send_stream(params, object())
    assert await anext(stream) is update
    assert observed["completion"].done() is False
    assert stages == ["registered", "dequeued"]

    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    assert observed["completion"].done() is True
    assert stages == ["registered", "dequeued", "acknowledged"]
    assert pipeline_transport_delivery_tracking_enabled() is False


@pytest.mark.asyncio
async def test_message_stream_does_not_acknowledge_transport_delivery_when_closed(monkeypatch) -> None:
    observed: dict[str, asyncio.Future[None]] = {}
    stages: list[str] = []
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )

    async def sdk_stream(_handler, _params, _context):
        observed["completion"] = register_pipeline_transport_delivery(
            update,
            stage_observer=lambda stage, _at_ns: stages.append(stage),
        )
        yield update

    async def hydrate(_params) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = object()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    params = SimpleNamespace(message=SimpleNamespace(task_id=None))

    stream = handler.on_message_send_stream(params, object())
    assert await anext(stream) is update
    await stream.aclose()

    assert isinstance(observed["completion"].exception(), PipelineTransportDeliveryClosedError)
    assert stages == ["registered", "dequeued", "closed"]
    assert pipeline_transport_delivery_tracking_enabled() is False


@pytest.mark.asyncio
async def test_pipeline_message_stream_does_not_bind_subscriber_delivery_to_producer(monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    tracking_states = []
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )

    async def sdk_stream(_handler, _params, _context):
        tracking_states.append(pipeline_transport_delivery_tracking_enabled())
        yield update

    async def hydrate(_params) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = object()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    params = SimpleNamespace(message=SimpleNamespace(task_id=None))

    stream = handler.on_message_send_stream(params, object())
    assert await anext(stream) is update
    await stream.aclose()

    assert tracking_states == [False]
    assert pipeline_transport_delivery_tracking_enabled() is False


@pytest.mark.asyncio
async def test_message_stream_queues_input_required_followup_instead_of_routing_as_interrupt(monkeypatch) -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        ),
        call_context,
    )
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = asyncio.current_task()
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    sdk_stream_called = False

    async def sdk_stream(_handler, _params, _context):
        nonlocal sdk_stream_called
        sdk_stream_called = True
        yield update

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    class ActiveTaskRegistry:
        async def get(self, _task_id):
            return object()

    async def fail_active_stream(*_args, **_kwargs):
        raise AssertionError("input-required follow-up must not use the active interrupt route")
        yield  # pragma: no cover

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._active_task_registry = ActiveTaskRegistry()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    handler._on_active_message_send_stream = fail_active_stream
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    events = await _collect_async(handler.on_message_send_stream(params, call_context))

    assert events == [update]
    assert sdk_stream_called is True


@pytest.mark.asyncio
async def test_message_stream_routes_permission_response_to_active_input_required_task(monkeypatch) -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        ),
        call_context,
    )
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = asyncio.current_task()
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    response = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-task-1-tool-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }
    message = Message(
        message_id="message-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text="{} {}".format(PERMISSION_QUERY_PREFIX, json.dumps(response)))],
    )
    active_stream_called = False

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    class ActiveTaskRegistry:
        async def get(self, _task_id):
            return object()

    async def active_stream(*_args, **_kwargs):
        nonlocal active_stream_called
        active_stream_called = True
        yield update

    async def fail_sdk_stream(*_args, **_kwargs):
        raise AssertionError("permission response must not wait in the SDK task queue")
        yield  # pragma: no cover

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", fail_sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._active_task_registry = ActiveTaskRegistry()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    handler._on_active_message_send_stream = active_stream

    events = await _collect_async(handler.on_message_send_stream(SimpleNamespace(message=message), call_context))

    assert events == [update]
    assert active_stream_called is True
    assert message.task_id == "task-1"


@pytest.mark.asyncio
async def test_text_gateway_sideband_permission_response_hydrates_task_and_returns_short_ack(monkeypatch) -> None:
    call_context = ServerCallContext()
    response = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "permission-opaque",
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }
    message = Message(
        message_id="message-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text="{} {}".format(PERMISSION_QUERY_PREFIX, json.dumps(response)))],
    )
    ack_data = Value()
    ack_data.struct_value.update(
        {
            "schemaVersion": 1,
            "kind": "permission_ack",
            "inputId": "permission-opaque",
            "toolUseId": "tool-1",
            "decision": "allow_once",
            "accepted": True,
        }
    )
    ack = Message(
        message_id="permission-ack-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_AGENT,
        parts=[Part(data=ack_data, media_type="application/json")],
    )

    class Executor:
        async def resolve_sideband_permission(self, _response):
            return ack

    async def fail_sdk_stream(*_args, **_kwargs):
        raise AssertionError("sideband permission response must not tap the active task")
        yield  # pragma: no cover

    async def fail_sdk_send(*_args, **_kwargs):
        raise AssertionError("sideband permission response must not enter the normal message route")

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", fail_sdk_stream)
    monkeypatch.setattr(DefaultRequestHandler, "on_message_send", fail_sdk_send)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.agent_executor = Executor()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    params = SimpleNamespace(message=message)

    assert await handler.on_message_send(params, call_context) is ack
    assert message.task_id == "task-1"
    assert await _collect_async(handler.on_message_send_stream(params, call_context)) == [ack]


@pytest.mark.asyncio
async def test_dispatcher_permission_followup_resumes_live_normal_stream(monkeypatch, tmp_path) -> None:
    future = pending_future()
    loop = FakeAgentLoop(
        [
            PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-1",
                response_future=future,
                continuation_frame={
                    "assistantMessageRef": "session.jsonl:0",
                    "assistantMessageDigest": "a" * 64,
                    "orderedToolUseIds": ["tool-1"],
                    "currentIndex": 0,
                    "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
                },
            ),
            TextDeltaEvent(text="after permission"),
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_args, **_kwargs: True)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)
    first_events: list[dict] = []

    async def consume_first_stream() -> None:
        async for event in dispatcher.dispatch_stream(
            {
                "jsonrpc": "2.0",
                "id": "permission-first",
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "messageId": "permission-message-first",
                        "role": "ROLE_USER",
                        "parts": [{"text": "start"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            }
        ):
            first_events.append(event)

    first_task = asyncio.create_task(consume_first_stream())
    input_event = None
    envelope = None
    for _ in range(_STREAM_TEST_TIMEOUT * 100):
        for event in first_events:
            result = event.get("result", {})
            payload = result.get("statusUpdate") or result.get("task") or result
            metadata = payload.get("metadata") or {}
            candidate = metadata.get("iac_code", {}).get("input")
            if isinstance(candidate, dict) and candidate.get("kind") == "permission":
                input_event = event
                envelope = candidate
                break
        if input_event is not None:
            break
        await asyncio.sleep(0.01)
    assert input_event is not None
    assert envelope is not None
    await asyncio.wait_for(first_task, timeout=_STREAM_TEST_TIMEOUT)
    assert not await components.task_store.is_task_active(envelope["requestTaskId"])

    async def consume_second_stream() -> list[dict]:
        return [
            event
            async for event in dispatcher.dispatch_stream(
                {
                    "jsonrpc": "2.0",
                    "id": "permission-second",
                    "method": "SendStreamingMessage",
                    "params": {
                        "message": {
                            "messageId": "permission-message-second",
                            "role": "ROLE_USER",
                            "contextId": envelope["contextId"],
                            "parts": [
                                {
                                    "text": "{} {}".format(
                                        PERMISSION_QUERY_PREFIX,
                                        json.dumps(
                                            {
                                                "schemaVersion": 1,
                                                "kind": "permission",
                                                "requestTaskId": envelope["requestTaskId"],
                                                "contextId": envelope["contextId"],
                                                "inputId": envelope["inputId"],
                                                "toolUseId": envelope["toolUseId"],
                                                "decision": "allow_once",
                                            },
                                        ),
                                    )
                                }
                            ],
                            "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                        },
                        "configuration": {"acceptedOutputModes": ["text/plain"]},
                    },
                }
            )
        ]

    second_task = asyncio.create_task(consume_second_stream())
    for _ in range(_STREAM_TEST_TIMEOUT * 100):
        if future.done():
            break
        await asyncio.sleep(0.01)

    try:
        assert future.done() and future.result() is True
        second_events = await asyncio.wait_for(second_task, timeout=_STREAM_TEST_TIMEOUT)
        assert all("error" not in event for event in second_events)
        assert any("after permission" in json.dumps(event, ensure_ascii=False) for event in second_events)
    finally:
        if not second_task.done():
            second_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second_task
        if not first_task.done():
            first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_task
        await dispatcher.aclose()
        await components.aclose()


@pytest.mark.asyncio
async def test_dispatcher_stream_preserves_message_metadata_echo_without_safe_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("IAC_CODE_A2A_SAFE_MODE", raising=False)
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
                "id": "metadata-redaction",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": "msg-metadata-redaction",
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hello"}],
                        "metadata": {
                            "iac_code": {
                                "cwd": str(tmp_path),
                                "iac_code_model": "qwen3.6-plus",
                                "iac_code_api_key": "provider-secret",
                                "alibaba_cloud_access_key_id": "ak-id-secret",
                                "alibaba_cloud_access_key_secret": "ak-secret",
                                "alibaba_cloud_security_token": "sts-token-secret",
                                "alibaba_cloud_region_id": "cn-hangzhou",
                            },
                            "custom": {
                                "apikey": "custom-api-key",
                                "nested": [{"accessKeySecret": "nested-ak-secret"}],
                            },
                        },
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            }
        )
    ]

    echoed_metadata = events[0]["result"]["history"][0]["metadata"]
    assert echoed_metadata["iac_code"] == {
        "cwd": str(tmp_path),
        "iac_code_model": "qwen3.6-plus",
        "iac_code_api_key": "provider-secret",
        "alibaba_cloud_access_key_id": "ak-id-secret",
        "alibaba_cloud_access_key_secret": "ak-secret",
        "alibaba_cloud_security_token": "sts-token-secret",
        "alibaba_cloud_region_id": "cn-hangzhou",
    }
    assert echoed_metadata["custom"] == {
        "apikey": "custom-api-key",
        "nested": [{"accessKeySecret": "nested-ak-secret"}],
    }
    rendered = str(events[0])
    assert "provider-secret" in rendered
    assert "ak-id-secret" in rendered
    assert "ak-secret" in rendered
    assert "sts-token-secret" in rendered
    assert "custom-api-key" in rendered
    assert "nested-ak-secret" in rendered
    await components.aclose()


@pytest.mark.asyncio
async def test_dispatcher_rejects_pipeline_image_before_executor_runs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    monkeypatch.setattr(
        "iac_code.a2a.parts.maybe_resize_and_downsample",
        lambda raw: SimpleNamespace(data=raw, media_type="image/png"),
    )
    monkeypatch.setattr("iac_code.a2a.executor.is_model_multimodal", lambda *args, **kwargs: False)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("executor should not run for invalid image input")

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2AExecutor.execute", fail_if_called)
    components = create_runtime_components(model="text-only-model", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)

    response = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "image-invalid",
            "method": "SendStreamingMessage",
            "params": {
                "message": {
                    "messageId": "msg-image-invalid",
                    "contextId": "ctx-image-invalid",
                    "role": "ROLE_USER",
                    "parts": [
                        {
                            "data": {
                                "filename": "initial.png",
                                "bytes": base64.b64encode(b"fake image").decode("ascii"),
                            },
                            "mediaType": "image/png",
                        }
                    ],
                    "metadata": {"iac_code": {"cwd": str(tmp_path), "run_mode": "pipeline"}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        }
    )

    assert response["id"] == "image-invalid"
    assert response["error"]["code"] == -32602
    assert response["error"]["message"] == "Current model text-only-model does not support image input."
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
    await asyncio.wait_for(pipeline.started.wait(), timeout=_STREAM_TEST_TIMEOUT)
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
    for _ in range(_STREAM_TEST_TIMEOUT * 100):
        if pipeline.interrupts:
            break
        await asyncio.sleep(0.01)

    try:
        assert pipeline.interrupts == ["please add this"]
        event_types = [event["eventType"] for event in A2APipelineJournal(pipeline.session.session_dir).read_all()]
        assert "interrupt_received" in event_types
        assert "interrupt_classified" in event_types
        await asyncio.wait_for(second_task, timeout=_STREAM_TEST_TIMEOUT)
    finally:
        if not second_task.done():
            second_task.cancel()
            try:
                await second_task
            except asyncio.CancelledError:
                pass
        pipeline.release.set()
        await asyncio.wait_for(first_task, timeout=_STREAM_TEST_TIMEOUT)
        await dispatcher.aclose()
        await components.aclose()


@pytest.mark.asyncio
async def test_dispatcher_resumes_candidate_selection_submitted_during_input_backup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_A2A_EXTREME_PERFORMANCE", "true")
    backup_started = threading.Event()
    release_backup = threading.Event()

    class BlockingBackupService(SessionBackupService):
        def __init__(self) -> None:
            super().__init__(retry_delays=())

        def backup_session(self, _cwd, _session_id, *, reason, critical, publication_proofs=None) -> None:
            del critical, publication_proofs
            if reason == BackupReason.INPUT_REQUIRED:
                backup_started.set()
                if not release_backup.wait(timeout=_STREAM_TEST_TIMEOUT):
                    raise TimeoutError("test backup gate was not released")

    class CandidatePipeline:
        pipeline_name = "selling"
        sidecar_status = None
        sidecar_restore_result = None

        def __init__(self) -> None:
            self.session = SimpleNamespace(session_dir=tmp_path / "sidecar")
            self.resume_prompts: list[str] = []

        async def run(self, _prompt: str):
            yield PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="selection",
                timestamp=1717821601.0,
                data={
                    "kind": "candidate_selection",
                    "prompt": "请选择方案",
                    "options": [{"candidate_index": 0, "name": "方案 A"}],
                },
            )

        async def resume(self, prompt: str):
            self.resume_prompts.append(prompt)
            yield PipelineEvent(
                type=PipelineEventType.USER_INPUT_RECEIVED,
                step_id="selection",
                timestamp=1717821602.0,
                data={"kind": "candidate_selection", "selected_index": 0},
            )
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821603.0,
                data={"total_steps": 1},
            )

        def should_switch_to_normal(self, _data: dict) -> bool:
            return False

    pipeline = CandidatePipeline()
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr(
        "iac_code.a2a.pipeline_executor.create_agent_runtime",
        lambda options: SimpleNamespace(provider_manager=object(), tool_registry=object()),
    )
    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        backup_service=BlockingBackupService(),
    )
    dispatcher = A2AJsonRpcDispatcher(components)
    first_events: list[dict] = []
    second_events: list[dict] = []

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
    assert await asyncio.to_thread(backup_started.wait, _STREAM_TEST_TIMEOUT)
    identity = _active_task_identity(components)

    async def consume_second_stream() -> None:
        async for event in dispatcher.dispatch_stream(
            {
                "jsonrpc": "2.0",
                "id": "second",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": "msg-second",
                        "role": "user",
                        "parts": [{"kind": "text", "text": '{"selected_candidate_index": 0}'}],
                        "contextId": identity.context_id,
                        "taskId": identity.task_id,
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            }
        ):
            second_events.append(event)

    second_task = asyncio.create_task(consume_second_stream())
    diagnostic: dict[str, object] = {}
    try:
        for _ in range(_STREAM_TEST_TIMEOUT * 100):
            runtime = components.task_store._contexts[identity.context_id].runtime
            if getattr(runtime, "pending_resume_input", None) is not None:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Candidate selection was not staged during the critical backup")
        release_backup.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=_STREAM_TEST_TIMEOUT)
        runtime = components.task_store._contexts[identity.context_id].runtime
        task_record = components.task_store._tasks[identity.task_id]
        diagnostic = {
            "pending_resume_input": getattr(runtime, "pending_resume_input", None) is not None,
            "pending_resume_error": repr(getattr(runtime, "pending_resume_error", None)),
            "pending_resume_settled": getattr(runtime, "pending_resume_settled").is_set(),
            "pending_resume_boundary_in_flight": getattr(runtime, "pending_resume_boundary_in_flight", None),
            "restart_after_interrupt": getattr(runtime, "restart_after_interrupt", None),
            "restart_requested": getattr(runtime, "restart_requested").is_set(),
            "active_owner_done": getattr(runtime, "active_owner_task", None) is None
            or getattr(runtime, "active_owner_task").done(),
            "task_state": task_record.state,
            "first_event_count": len(first_events),
            "second_event_count": len(second_events),
        }
    finally:
        release_backup.set()
        for task in (first_task, second_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await dispatcher.aclose()
        await components.aclose()

    event_types = [event["eventType"] for event in A2APipelineJournal(pipeline.session.session_dir).read_all()]
    assert pipeline.resume_prompts == ['{"selected_candidate_index": 0}'], json.dumps(diagnostic, sort_keys=True)
    assert "input_received" in event_types
    assert not {"interrupt_received", "interrupt_classified"}.intersection(event_types)
    assert first_events or second_events


@pytest.mark.asyncio
async def test_subscribe_to_task_stops_after_input_required_status(monkeypatch) -> None:
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    store = A2ATaskStore()
    call_context = ServerCallContext()
    await store.save(task, call_context)
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = asyncio.current_task()

    async def hanging_sdk_subscription(self, params, context):
        yield TaskStatusUpdateEvent(
            task_id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        yield TaskStatusUpdateEvent(
            task_id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(DefaultRequestHandler, "on_subscribe_to_task", hanging_sdk_subscription)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._validate_extensions = lambda context: None

    events = await asyncio.wait_for(
        _collect_async(handler.on_subscribe_to_task(SubscribeToTaskRequest(id="task-1"), call_context)),
        timeout=0.5,
    )

    assert [event.status.state for event in events] == [
        TaskState.TASK_STATE_WORKING,
        TaskState.TASK_STATE_INPUT_REQUIRED,
    ]


@pytest.mark.asyncio
async def test_subscribe_to_task_recovers_terminal_snapshot_when_sdk_stream_ends_early(monkeypatch) -> None:
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    store = A2ATaskStore()
    call_context = ServerCallContext()
    await store.save(task, call_context)
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = asyncio.current_task()

    async def truncated_sdk_subscription(self, params, context):
        yield TaskStatusUpdateEvent(
            task_id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        final_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        )
        await store.save(final_task, context)

    monkeypatch.setattr(DefaultRequestHandler, "on_subscribe_to_task", truncated_sdk_subscription)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._validate_extensions = lambda context: None

    events = await _collect_async(handler.on_subscribe_to_task(SubscribeToTaskRequest(id="task-1"), call_context))

    assert [event.status.state for event in events] == [
        TaskState.TASK_STATE_WORKING,
        TaskState.TASK_STATE_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_subscribe_to_task_does_not_duplicate_terminal_sdk_event(monkeypatch) -> None:
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    store = A2ATaskStore()
    call_context = ServerCallContext()
    await store.save(task, call_context)
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = asyncio.current_task()

    async def complete_sdk_subscription(self, params, context):
        final_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        )
        await store.save(final_task, context)
        yield final_task

    monkeypatch.setattr(DefaultRequestHandler, "on_subscribe_to_task", complete_sdk_subscription)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._validate_extensions = lambda context: None

    events = await _collect_async(handler.on_subscribe_to_task(SubscribeToTaskRequest(id="task-1"), call_context))

    assert len(events) == 1
    assert events[0].status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_create_runtime_components_returns_shared_objects() -> None:
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)

    try:
        assert isinstance(components, A2ARuntimeComponents)
        assert components.handler is not None
        assert components.task_store is not None
    finally:
        await components.aclose()


@pytest.mark.asyncio
async def test_create_runtime_components_registers_shared_runtime_owner(tmp_path) -> None:
    from iac_code.a2a.runtime_registry import get_runtime_owner

    persistence_dir = tmp_path / "a2a"
    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=persistence_dir,
    )

    owner = get_runtime_owner(persistence_root=persistence_dir)
    assert owner is not None
    assert owner.task_store is components.task_store
    assert owner.model == "qwen3.6-plus"

    await components.aclose()

    assert get_runtime_owner(persistence_root=persistence_dir) is None


@pytest.mark.parametrize("failing_stage", ["agent_card", "handler"])
def test_create_runtime_components_does_not_register_owner_before_initialization_completes(
    monkeypatch,
    tmp_path,
    failing_stage: str,
) -> None:
    from iac_code.a2a.runtime_registry import get_runtime_owner

    persistence_dir = tmp_path / failing_stage

    def fail(*args, **kwargs):
        raise RuntimeError("initialization failed")

    target = "build_agent_card" if failing_stage == "agent_card" else "IacCodeRequestHandler"
    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.{}".format(target), fail)

    with pytest.raises(RuntimeError, match="initialization failed"):
        create_runtime_components(
            model="qwen3.6-plus",
            host="127.0.0.1",
            port=41242,
            persistence_dir=persistence_dir,
        )

    assert get_runtime_owner(persistence_root=persistence_dir) is None


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
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)

    try:
        await dispatcher.dispatch({"jsonrpc": "2.0", "id": "1", "method": "message/send"})
        await dispatcher.dispatch({"jsonrpc": "2.0", "id": "2", "method": "message/send"})
        await dispatcher.aclose()
    finally:
        await components.aclose()

    assert created == 1


async def _collect_async(iterator):
    return [item async for item in iterator]


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
        await asyncio.wait_for(stream_task, timeout=_STREAM_TEST_TIMEOUT)
    assert producer_cancelled.is_set()
    assert active_task._reference_count == 0


@pytest.mark.asyncio
async def test_active_message_producer_failure_log_uses_strict_sanitizer(caplog, tmp_path) -> None:
    server_path = str(tmp_path / "private" / "result.json")

    async def fail() -> None:
        raise RuntimeError(f"failed at {server_path} with password=real-secret")

    producer = asyncio.create_task(fail())
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    await handler._cleanup_active_message_producer(producer, f"task-at-{server_path}")

    record = next(record for record in caplog.records if record.message.startswith("Active task message producer"))
    assert server_path not in record.message
    assert "real-secret" not in record.message
    assert "[PATH]" in record.message
    assert "[REDACTED]" in record.message


def _active_task_identity(components: A2ARuntimeComponents) -> SimpleNamespace:
    tasks = list(components.task_store._tasks.values())  # noqa: SLF001
    assert len(tasks) == 1
    task = tasks[0]
    return SimpleNamespace(task_id=task.task_id, context_id=task.context_id)
