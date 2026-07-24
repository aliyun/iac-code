import asyncio
import base64
import json
import shutil
from types import SimpleNamespace

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import SubscribeToTaskRequest, Task, TaskState, TaskStatus, TaskStatusUpdateEvent

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
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime

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
async def test_dispatcher_stream_redacts_sensitive_message_metadata_echo(monkeypatch, tmp_path) -> None:
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
        "cwd": ".",
        "iac_code_model": "qwen3.6-plus",
        "iac_code_api_key": "***",
        "alibaba_cloud_access_key_id": "***",
        "alibaba_cloud_access_key_secret": "***",
        "alibaba_cloud_security_token": "***",
        "alibaba_cloud_region_id": "cn-hangzhou",
    }
    assert echoed_metadata["custom"] == {
        "apikey": "***",
        "nested": [{"accessKeySecret": "***"}],
    }
    rendered = str(events[0])
    assert "provider-secret" not in rendered
    assert "ak-id-secret" not in rendered
    assert "ak-secret" not in rendered
    assert "sts-token-secret" not in rendered
    assert "custom-api-key" not in rendered
    assert "nested-ak-secret" not in rendered
    await components.aclose()


@pytest.mark.asyncio
async def test_dispatcher_rejects_pipeline_image_before_executor_runs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
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
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
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


def _active_task_identity(components: A2ARuntimeComponents) -> SimpleNamespace:
    tasks = list(components.task_store._tasks.values())  # noqa: SLF001
    assert len(tasks) == 1
    task = tasks[0]
    return SimpleNamespace(task_id=task.task_id, context_id=task.context_id)
