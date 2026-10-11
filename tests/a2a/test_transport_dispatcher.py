import asyncio
import base64
import contextlib
import json
import shutil
import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.agent_execution.active_task import _RequestCompleted, _RequestStarted
from a2a.server.agent_execution.active_task_registry import ActiveTaskRegistry
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue_v2 import EventQueueSource, QueueShutDown
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from iac_code.a2a.backup import NATURAL_HANDOFF_VERSION, SessionBackupHandoff
from iac_code.a2a.execution_control import (
    NATURAL_COMPLETION_FINALIZED_GENERATION,
    ExecutionControlService,
    NaturalCompletionGenerationCarrier,
    RecoverableInputAdmissionCarrier,
    RecoverableInputAdmissionLease,
)
from iac_code.a2a.executor import IacCodeA2AExecutor, InputResponseExecutionControlConflictError
from iac_code.a2a.input_required import PERMISSION_QUERY_PREFIX
from iac_code.a2a.persistence import A2APersistenceStore
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
from iac_code.a2a.request_scoped_active_task import (
    DirectPipelineRouteGateCarrier,
    PipelineLifecycleEventQueueCarrier,
    RequestScopedActiveTask,
    RequestScopedActiveTaskRegistry,
)
from iac_code.a2a.resource_selector import RESOURCE_SELECTION_QUERY_PREFIX
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.a2a.transports.dispatcher import (
    A2AJsonRpcDispatcher,
    A2ARuntimeComponents,
    IacCodeRequestHandler,
    _StreamingASGITransport,
    create_runtime_components,
)
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.user_input import normalize_pipeline_user_input
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import PermissionRequestEvent, TextDeltaEvent

from .fakes import FakeAgentLoop, FakeEventQueue, FakeRuntime, pending_future

_STREAM_TEST_TIMEOUT = 5


def _durable_natural_finalization(
    completion_generation: int,
    *,
    context_id: str = "ctx-1",
    task_id: str = "task-1",
    execution_id: str = "exec-1",
) -> dict:
    receipt = {
        "version": NATURAL_HANDOFF_VERSION,
        "contextId": context_id,
        "taskId": task_id,
        "executionId": execution_id,
        "completionGeneration": completion_generation,
        "ownerGeneration": 1,
        "businessRevision": 0,
        "pendingJobId": None,
        "backupDisabled": True,
        "stagedCommitted": False,
        "snapshotGeneration": None,
        "snapshotCommitId": None,
        "businessDrained": True,
    }
    return {
        "contextId": context_id,
        "taskId": task_id,
        "executionId": execution_id,
        "ownerGeneration": 1,
        "revision": 1,
        "persistedRevision": 1,
        "phase": "terminated",
        "terminationReason": "natural_completion",
        "backup": {"status": "disabled"},
        "naturalHandoff": receipt,
        NATURAL_COMPLETION_FINALIZED_GENERATION: completion_generation,
    }


@pytest.mark.asyncio
async def test_background_service_start_waits_for_backup_recovery_before_admission() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Coordinator:
        async def recover(self) -> int:
            entered.set()
            await release.wait()
            return 1

    components = A2ARuntimeComponents(
        handler=SimpleNamespace(),
        task_store=SimpleNamespace(),
        card=None,
        app=SimpleNamespace(),
        _exit_stack=contextlib.AsyncExitStack(),
        backup_coordinator=Coordinator(),
    )
    starting = asyncio.create_task(components.start_background_services())
    await entered.wait()
    assert not starting.done()

    release.set()
    await starting


@pytest.mark.asyncio
async def test_background_service_start_fails_closed_when_backup_recovery_fails() -> None:
    class Coordinator:
        async def recover(self) -> int:
            raise RuntimeError("capture handoff failed")

    components = A2ARuntimeComponents(
        handler=SimpleNamespace(),
        task_store=SimpleNamespace(),
        card=None,
        app=SimpleNamespace(),
        _exit_stack=contextlib.AsyncExitStack(),
        backup_coordinator=Coordinator(),
    )

    with pytest.raises(RuntimeError, match="capture handoff failed"):
        await components.start_background_services()


@pytest.mark.asyncio
async def test_setup_active_task_attaches_admission_to_the_queued_request_context(monkeypatch) -> None:
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = A2ATaskStore()
    call_context = ServerCallContext()
    request_context = SimpleNamespace()
    handler._stage_recoverable_input_admission(call_context, "recovery-1")

    async def sdk_setup(_handler, _params, observed_call_context):
        assert observed_call_context is call_context
        return object(), request_context

    monkeypatch.setattr(DefaultRequestHandler, "_setup_active_task", sdk_setup)
    _, result_context = await handler._setup_active_task(object(), call_context)

    assert result_context is request_context
    assert RecoverableInputAdmissionCarrier.read(request_context) == "recovery-1"
    assert PipelineLifecycleEventQueueCarrier.read(request_context) is True
    assert call_context.state == {"iac_code.recoverable_input_admission": "recovery-1"}


@pytest.mark.asyncio
async def test_request_scoped_active_task_ignores_old_terminal_before_its_request_start(monkeypatch) -> None:
    request_id = uuid.uuid4()
    request_enqueued = asyncio.Event()
    call_context = ServerCallContext()
    request_context = RequestContext(call_context=call_context, task_id="task-1", context_id="ctx-1")
    active_task = RequestScopedActiveTask(
        agent_executor=SimpleNamespace(),
        task_id="task-1",
        task_manager=SimpleNamespace(),
    )

    async def enqueue_request(_request_context) -> uuid.UUID:
        request_enqueued.set()
        return request_id

    monkeypatch.setattr(active_task, "enqueue_request", enqueue_request)
    stream = active_task.subscribe(request=request_context)
    first_event = asyncio.create_task(anext(stream))
    await asyncio.wait_for(request_enqueued.wait(), timeout=_STREAM_TEST_TIMEOUT)

    old_terminal = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )
    current_update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    canonical_working = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    queued_events = (
        (old_terminal, None),
        (_RequestStarted(request_id, request_context), None),
        (old_terminal, canonical_working),
        (current_update, canonical_working),
    )
    for queued_event in queued_events:
        await active_task._event_queue_subscribers.enqueue_event(queued_event)

    assert await asyncio.wait_for(first_event, timeout=_STREAM_TEST_TIMEOUT) is current_update
    next_event = asyncio.create_task(anext(stream))
    await active_task._event_queue_subscribers.enqueue_event((_RequestCompleted(request_id), None))
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(next_event, timeout=_STREAM_TEST_TIMEOUT)

    await active_task._event_queue_agent.close(immediate=True)
    await active_task._event_queue_subscribers.close(immediate=True)


@pytest.mark.asyncio
async def test_request_scoped_active_task_isolates_two_concurrent_subscribers(monkeypatch) -> None:
    request_ids = [uuid.uuid4(), uuid.uuid4()]
    contexts = [
        RequestContext(call_context=ServerCallContext(), task_id="task-1", context_id="ctx-1") for _ in request_ids
    ]
    both_enqueued = asyncio.Event()
    active_task = RequestScopedActiveTask(
        agent_executor=SimpleNamespace(),
        task_id="task-1",
        task_manager=SimpleNamespace(),
    )
    enqueued = 0

    async def enqueue_request(request_context) -> uuid.UUID:
        nonlocal enqueued
        index = contexts.index(request_context)
        enqueued += 1
        if enqueued == 2:
            both_enqueued.set()
        return request_ids[index]

    monkeypatch.setattr(active_task, "enqueue_request", enqueue_request)
    streams = [active_task.subscribe(request=context) for context in contexts]
    first_events = [asyncio.create_task(anext(stream)) for stream in streams]
    await asyncio.wait_for(both_enqueued.wait(), timeout=_STREAM_TEST_TIMEOUT)

    old_terminal = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )
    updates = [
        TaskStatusUpdateEvent(
            task_id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=state),
        )
        for state in (TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_INPUT_REQUIRED)
    ]
    events = (
        old_terminal,
        _RequestStarted(request_ids[0], contexts[0]),
        updates[0],
        _RequestCompleted(request_ids[0]),
        _RequestStarted(request_ids[1], contexts[1]),
        updates[1],
        _RequestCompleted(request_ids[1]),
    )
    for event in events:
        await active_task._event_queue_subscribers.enqueue_event((event, None))

    assert await asyncio.wait_for(first_events[0], timeout=_STREAM_TEST_TIMEOUT) is updates[0]
    assert await asyncio.wait_for(first_events[1], timeout=_STREAM_TEST_TIMEOUT) is updates[1]
    for stream in streams:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=_STREAM_TEST_TIMEOUT)

    await active_task._event_queue_agent.close(immediate=True)
    await active_task._event_queue_subscribers.close(immediate=True)


@pytest.mark.asyncio
async def test_request_enqueue_failure_keeps_admission_owned_by_transport() -> None:
    released: list[str] = []
    call_context = ServerCallContext()
    call_context.state["iac_code.recoverable_input_admission"] = "recovery-1"
    request_context = RequestContext(call_context=call_context, task_id="task-1", context_id="ctx-1")

    async def release(admission: str) -> None:
        released.append(admission)

    lease = RecoverableInputAdmissionLease(
        "recovery-1",
        acknowledge_enqueue=lambda token: call_context.state.pop("iac_code.recoverable_input_admission", None) == token,
        release=release,
    )
    RecoverableInputAdmissionCarrier.attach(request_context, lease)
    active_task = RequestScopedActiveTask(
        agent_executor=SimpleNamespace(),
        task_id="task-1",
        task_manager=SimpleNamespace(),
    )
    active_task._request_queue.shutdown(immediate=True)

    stream = active_task.subscribe(request=request_context)
    with pytest.raises(QueueShutDown):
        await anext(stream)

    assert call_context.state == {"iac_code.recoverable_input_admission": "recovery-1"}
    assert released == []
    assert active_task._reference_count == 0
    assert active_task._event_queue_subscribers._sinks == set()
    await active_task._event_queue_agent.close(immediate=True)
    await active_task._event_queue_subscribers.close(immediate=True)


@pytest.mark.asyncio
async def test_request_scoped_active_task_surfaces_producer_error_before_request_start(monkeypatch) -> None:
    request_id = uuid.uuid4()
    request_enqueued = asyncio.Event()
    request_context = RequestContext(
        call_context=ServerCallContext(),
        task_id="task-1",
        context_id="ctx-1",
    )
    active_task = RequestScopedActiveTask(
        agent_executor=SimpleNamespace(),
        task_id="task-1",
        task_manager=SimpleNamespace(),
    )

    async def enqueue_request(_request_context) -> uuid.UUID:
        request_enqueued.set()
        return request_id

    monkeypatch.setattr(active_task, "enqueue_request", enqueue_request)
    stream = active_task.subscribe(request=request_context)
    result = asyncio.create_task(anext(stream))
    await asyncio.wait_for(request_enqueued.wait(), timeout=_STREAM_TEST_TIMEOUT)
    await active_task._event_queue_subscribers.enqueue_event((RuntimeError("producer failed"), None))
    await active_task._event_queue_subscribers.test_only_join_incoming_queue()
    await active_task._event_queue_subscribers.close(immediate=True)

    with pytest.raises(RuntimeError, match="producer failed"):
        await asyncio.wait_for(result, timeout=_STREAM_TEST_TIMEOUT)

    await active_task._event_queue_agent.close(immediate=True)


@pytest.mark.asyncio
async def test_request_scoped_registry_releases_admission_after_executor_failure() -> None:
    released: list[str] = []
    admission_released = asyncio.Event()
    executed = asyncio.Event()

    class FailingExecutor:
        async def execute(self, _request_context, _event_queue) -> None:
            executed.set()
            raise RuntimeError("executor failed")

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    async def release(admission: str) -> None:
        released.append(admission)
        admission_released.set()

    call_context = ServerCallContext()
    call_context.state["iac_code.recoverable_input_admission"] = "recovery-1"
    request_context = RequestContext(call_context=call_context, task_id="task-1", context_id="ctx-1")
    lease = RecoverableInputAdmissionLease(
        "recovery-1",
        acknowledge_enqueue=lambda token: call_context.state.pop("iac_code.recoverable_input_admission", None) == token,
        release=release,
    )
    RecoverableInputAdmissionCarrier.attach(request_context, lease)
    registry = RequestScopedActiveTaskRegistry(agent_executor=FailingExecutor(), task_store=A2ATaskStore())
    active_task = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )

    stream = active_task.subscribe(request=request_context)
    with pytest.raises(RuntimeError, match="executor failed"):
        await asyncio.wait_for(anext(stream), timeout=_STREAM_TEST_TIMEOUT)
    await asyncio.wait_for(executed.wait(), timeout=_STREAM_TEST_TIMEOUT)
    await asyncio.wait_for(admission_released.wait(), timeout=_STREAM_TEST_TIMEOUT)

    assert call_context.state == {}
    assert released == ["recovery-1"]


@pytest.mark.asyncio
async def test_request_scoped_registry_replaces_finished_task_after_durable_reopen() -> None:
    class IdleExecutor:
        async def execute(self, _request_context, _event_queue) -> None:
            return None

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    call_context = ServerCallContext()
    store = A2ATaskStore()
    reopened = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    await store.save(reopened, call_context)
    registry = RequestScopedActiveTaskRegistry(agent_executor=IdleExecutor(), task_store=store)
    finished = RequestScopedActiveTask(
        agent_executor=IdleExecutor(),
        task_id="task-1",
        task_manager=SimpleNamespace(),
    )
    finished._is_finished.set()
    registry._active_tasks["task-1"] = finished

    replacement = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )

    assert replacement is not finished
    assert await registry.get("task-1") is replacement

    replacement._producer_task.cancel()
    replacement._consumer_task.cancel()
    await asyncio.gather(replacement._producer_task, replacement._consumer_task, return_exceptions=True)
    await replacement._event_queue_agent.close(immediate=True)
    await replacement._event_queue_subscribers.close(immediate=True)


@pytest.mark.asyncio
async def test_request_scoped_registry_retires_unfinished_sdk_lifecycle_for_durable_recovery() -> None:
    class IdleExecutor:
        async def execute(self, _request_context, _event_queue) -> None:
            return None

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

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
    registry = RequestScopedActiveTaskRegistry(agent_executor=IdleExecutor(), task_store=store)
    stale = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    assert stale._is_finished.is_set() is False
    assert stale._producer_task is not None and not stale._producer_task.done()
    assert stale._consumer_task is not None and not stale._consumer_task.done()
    stale._task_manager._current_task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
    )
    canonical = await store.get("task-1", call_context)
    assert canonical is not None
    assert canonical.status.state == TaskState.TASK_STATE_INPUT_REQUIRED

    await registry.retire_for_recovery("task-1")

    assert await registry.get("task-1") is None
    assert stale._is_finished.is_set() is True
    assert stale._producer_task.done()
    assert stale._consumer_task.done()

    replacement = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    assert replacement is not stale
    recovered = await replacement.get_task()
    assert recovered.status.state == TaskState.TASK_STATE_INPUT_REQUIRED

    await registry.retire_for_recovery("task-1")


@pytest.mark.asyncio
async def test_recovery_replacement_rejects_a_contender_before_the_admitted_request() -> None:
    observed: list[str | None] = []

    class RecordingExecutor:
        async def execute(self, request_context, event_queue) -> None:
            observed.append(RecoverableInputAdmissionCarrier.read(request_context))
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="ctx-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

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
    registry = RequestScopedActiveTaskRegistry(agent_executor=RecordingExecutor(), task_store=store)
    assert (
        await registry.reconcile_and_replace_for_recovery(
            "task-1",
            call_context=call_context,
            context_id="ctx-1",
            acquire_admission=lambda: asyncio.sleep(0, result="recovery-1"),
        )
        == "recovery-1"
    )
    replacement = await registry.get("task-1")
    assert replacement is not None
    contender = RequestContext(call_context=ServerCallContext(), task_id="task-1", context_id="ctx-1")
    with pytest.raises(InvalidParamsError, match="recovery continuation is reserved"):
        await anext(replacement.subscribe(request=contender))

    admitted = RequestContext(call_context=call_context, task_id="task-1", context_id="ctx-1")
    RecoverableInputAdmissionCarrier.attach(admitted, "recovery-1")
    events = [event async for event in replacement.subscribe(request=admitted)]

    assert [event.status.state for event in events] == [TaskState.TASK_STATE_WORKING]
    assert observed == ["recovery-1"]
    assert replacement._recovery_admission is None
    await registry.retire_for_recovery("task-1")


@pytest.mark.asyncio
async def test_recovery_claim_blocks_an_old_lifecycle_contender_before_admission() -> None:
    class IdleExecutor:
        async def execute(self, _request_context, _event_queue) -> None:
            return None

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

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
    registry = RequestScopedActiveTaskRegistry(agent_executor=IdleExecutor(), task_store=store)
    old = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    contender_context = RequestContext(
        call_context=ServerCallContext(),
        task_id="task-1",
        context_id="ctx-1",
    )

    await old._lock.acquire()
    contender = asyncio.create_task(old.enqueue_request(contender_context))
    await asyncio.sleep(0)
    replacement_task = asyncio.create_task(
        registry.reconcile_and_replace_for_recovery(
            "task-1",
            call_context=call_context,
            context_id="ctx-1",
            acquire_admission=lambda: asyncio.sleep(0, result="recovery-1"),
        )
    )
    await asyncio.sleep(0)
    old._lock.release()

    with pytest.raises(InvalidParamsError, match="recovery replacement is pending"):
        await contender
    assert await replacement_task == "recovery-1"
    replacement = await registry.get("task-1")
    assert replacement is not None and replacement is not old
    await registry.cancel_recovery_reservation("task-1", "recovery-1")


@pytest.mark.asyncio
async def test_recovery_replacement_waits_for_an_already_enqueued_old_lifecycle_request() -> None:
    executed = asyncio.Event()
    working_save_started = asyncio.Event()
    release_working_save = asyncio.Event()

    class GatedTaskStore(A2ATaskStore):
        async def save(self, task, context=None) -> None:
            if task.status.state == TaskState.TASK_STATE_WORKING:
                working_save_started.set()
                await release_working_save.wait()
            await super().save(task, context)

    class RecordingExecutor:
        async def execute(self, _request_context, event_queue) -> None:
            executed.set()
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="ctx-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    call_context = ServerCallContext()
    store = GatedTaskStore()
    await store.save(
        Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        ),
        call_context,
    )
    registry = RequestScopedActiveTaskRegistry(agent_executor=RecordingExecutor(), task_store=store)
    old = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    contender_context = RequestContext(
        call_context=ServerCallContext(),
        task_id="task-1",
        context_id="ctx-1",
    )

    await old._request_lock.acquire()
    await old.enqueue_request(contender_context)
    while old._request_queue.qsize() > 0:
        await asyncio.sleep(0)

    async def acquire_admission() -> str | None:
        task = await store.get("task-1", call_context)
        assert task is not None
        return None if task.status.state == TaskState.TASK_STATE_WORKING else "recovery-1"

    released: list[str] = []

    replacement_task = asyncio.create_task(
        registry.reconcile_and_replace_for_recovery(
            "task-1",
            call_context=call_context,
            context_id="ctx-1",
            acquire_admission=acquire_admission,
            release_admission=lambda token: asyncio.sleep(0, result=released.append(token)),
        )
    )
    await asyncio.sleep(0)
    old._request_lock.release()

    try:
        await asyncio.wait_for(executed.wait(), timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.wait_for(working_save_started.wait(), timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.sleep(0)
        assert replacement_task.done() is False
        release_working_save.set()
        assert await asyncio.wait_for(replacement_task, timeout=_STREAM_TEST_TIMEOUT) is None
        assert await registry.get("task-1") is old
        assert old._is_finished.is_set() is False
        assert released == []
    finally:
        release_working_save.set()
        if not replacement_task.done():
            replacement_task.cancel()
            await asyncio.gather(replacement_task, return_exceptions=True)
        await registry.retire_for_recovery("task-1")


@pytest.mark.asyncio
async def test_recovery_drain_fails_closed_when_the_old_consumer_exits_before_projection() -> None:
    save_started = asyncio.Event()
    release_save = asyncio.Event()

    class FailingTaskStore(A2ATaskStore):
        async def save(self, task, context=None) -> None:
            if task.status.state == TaskState.TASK_STATE_WORKING:
                save_started.set()
                await release_save.wait()
                raise RuntimeError("projection failed")
            await super().save(task, context)

    class WorkingExecutor:
        async def execute(self, _request_context, event_queue) -> None:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="ctx-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    call_context = ServerCallContext()
    store = FailingTaskStore()
    await store.save(
        Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        ),
        call_context,
    )
    registry = RequestScopedActiveTaskRegistry(agent_executor=WorkingExecutor(), task_store=store)
    old = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    await old.enqueue_request(RequestContext(call_context=ServerCallContext(), task_id="task-1", context_id="ctx-1"))
    await asyncio.wait_for(save_started.wait(), timeout=_STREAM_TEST_TIMEOUT)
    while old.has_unfinished_requests():
        await asyncio.sleep(0)
    assert old._request_lock.locked()

    replacement_task = asyncio.create_task(
        registry.reconcile_and_replace_for_recovery(
            "task-1",
            call_context=call_context,
            context_id="ctx-1",
            acquire_admission=lambda: asyncio.sleep(0, result="recovery-1"),
        )
    )
    release_save.set()

    try:
        with pytest.raises(InvalidParamsError, match="ended before the accepted request settled"):
            await asyncio.wait_for(replacement_task, timeout=_STREAM_TEST_TIMEOUT)
        assert old._consumer_task is not None and old._consumer_task.done()
    finally:
        release_save.set()
        if not replacement_task.done():
            replacement_task.cancel()
            await asyncio.gather(replacement_task, return_exceptions=True)
        await registry.retire_for_recovery("task-1")


@pytest.mark.asyncio
async def test_recovery_drain_for_one_task_does_not_block_registry_operations_for_another() -> None:
    task_a_started = asyncio.Event()
    release_task_a = asyncio.Event()

    class BlockingExecutor:
        async def execute(self, request_context, event_queue) -> None:
            if request_context.task_id != "task-a":
                return
            task_a_started.set()
            await release_task_a.wait()
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-a",
                    context_id="ctx-a",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    call_context = ServerCallContext()
    store = A2ATaskStore()
    for task_id, context_id in (("task-a", "ctx-a"), ("task-b", "ctx-b")):
        await store.save(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
            ),
            call_context,
        )
    registry = RequestScopedActiveTaskRegistry(agent_executor=BlockingExecutor(), task_store=store)
    old_a = await registry.get_or_create(
        "task-a",
        call_context=call_context,
        context_id="ctx-a",
        create_task_if_missing=True,
    )
    await old_a.enqueue_request(RequestContext(call_context=ServerCallContext(), task_id="task-a", context_id="ctx-a"))
    await asyncio.wait_for(task_a_started.wait(), timeout=_STREAM_TEST_TIMEOUT)

    async def acquire_a() -> str | None:
        task = await store.get("task-a", call_context)
        assert task is not None
        return None if task.status.state == TaskState.TASK_STATE_WORKING else "recovery-1"

    recovery_a = asyncio.create_task(
        registry.reconcile_and_replace_for_recovery(
            "task-a",
            call_context=call_context,
            context_id="ctx-a",
            acquire_admission=acquire_a,
        )
    )
    await asyncio.sleep(0)

    try:
        task_b = await asyncio.wait_for(
            registry.get_or_create(
                "task-b",
                call_context=call_context,
                context_id="ctx-b",
                create_task_if_missing=True,
            ),
            timeout=0.5,
        )
        assert task_b.task_id == "task-b"
    finally:
        release_task_a.set()
        await asyncio.wait_for(recovery_a, timeout=_STREAM_TEST_TIMEOUT)
        await registry.retire_for_recovery("task-a")
        await registry.retire_for_recovery("task-b")


@pytest.mark.asyncio
async def test_cancelled_recovery_replacement_finishes_retiring_old_lifecycle() -> None:
    class IdleExecutor:
        async def execute(self, _request_context, _event_queue) -> None:
            return None

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

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
    registry = RequestScopedActiveTaskRegistry(agent_executor=IdleExecutor(), task_store=store)
    old = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_finished = asyncio.Event()

    async def gated_close(*, immediate: bool) -> None:
        assert immediate is True
        close_started.set()
        await release_close.wait()
        close_finished.set()

    old._event_queue_agent.close = gated_close
    released: list[str] = []
    replacement = asyncio.create_task(
        registry.reconcile_and_replace_for_recovery(
            "task-1",
            call_context=call_context,
            context_id="ctx-1",
            acquire_admission=lambda: asyncio.sleep(0, result="recovery-1"),
            release_admission=lambda token: asyncio.sleep(0, result=released.append(token)),
        )
    )
    await close_started.wait()

    replacement.cancel()
    await asyncio.sleep(0)
    assert replacement.done() is False

    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert close_finished.is_set()
    assert old._producer_task.done()
    assert old._consumer_task.done()
    assert await registry.get("task-1") is None
    assert released == ["recovery-1"]


@pytest.mark.asyncio
async def test_stale_sdk_status_event_restores_task_manager_to_canonical_projection() -> None:
    class IdleExecutor:
        async def execute(self, _request_context, _event_queue) -> None:
            return None

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    def status(state: TaskState.ValueType, seconds: int) -> TaskStatus:
        value = TaskStatus(state=state)
        value.timestamp.seconds = seconds
        return value

    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_INPUT_REQUIRED, 100)),
        call_context,
    )
    registry = RequestScopedActiveTaskRegistry(agent_executor=IdleExecutor(), task_store=store)
    active_task = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = asyncio.current_task()
    record.updated_at = 200
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_WORKING, 250)),
        call_context,
    )

    await active_task._event_queue_agent.enqueue_event(
        TaskStatusUpdateEvent(
            task_id="task-1",
            context_id="ctx-1",
            status=status(TaskState.TASK_STATE_CANCELED, 150),
        )
    )
    await active_task._event_queue_agent.test_only_join_incoming_queue()
    canonical = await store.get("task-1", call_context)

    assert canonical is not None
    assert canonical.status.state == TaskState.TASK_STATE_WORKING
    assert active_task._task_manager._current_task.status.state == TaskState.TASK_STATE_WORKING
    assert active_task._is_finished.is_set() is False

    await active_task._event_queue_agent.enqueue_event(
        TaskStatusUpdateEvent(
            task_id="task-1",
            context_id="ctx-1",
            status=status(TaskState.TASK_STATE_CANCELED, 300),
        )
    )
    await active_task._event_queue_agent.test_only_join_incoming_queue()
    canonical = await store.get("task-1", call_context)
    assert canonical is not None
    assert canonical.status.state == TaskState.TASK_STATE_CANCELED
    assert active_task._task_manager._current_task.status.state == TaskState.TASK_STATE_CANCELED
    assert active_task._is_finished.is_set() is True
    await registry.retire_for_recovery("task-1")


@pytest.mark.asyncio
async def test_stale_sdk_status_rebuilds_projection_when_owner_cache_is_older() -> None:
    def status(state: TaskState.ValueType, seconds: int) -> TaskStatus:
        value = TaskStatus(state=state)
        value.timestamp.seconds = seconds
        return value

    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_CANCELED, 100)),
        call_context,
    )
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.state = "working"
    record.updated_at = 200
    incoming = Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_FAILED, 150))

    await store.save(incoming, call_context)
    visible = await store.get("task-1", call_context)

    assert incoming.status.state == TaskState.TASK_STATE_WORKING
    assert incoming.status.timestamp.seconds == 200
    assert visible is not None
    assert visible.status.state == TaskState.TASK_STATE_WORKING
    assert visible.status.timestamp.seconds == 200


@pytest.mark.asyncio
async def test_request_scoped_registry_old_cleanup_cannot_remove_replacement() -> None:
    registry = RequestScopedActiveTaskRegistry(agent_executor=SimpleNamespace(), task_store=A2ATaskStore())
    finished = RequestScopedActiveTask(
        agent_executor=SimpleNamespace(),
        task_id="task-1",
        task_manager=SimpleNamespace(),
    )
    replacement = RequestScopedActiveTask(
        agent_executor=SimpleNamespace(),
        task_id="task-1",
        task_manager=SimpleNamespace(),
    )
    registry._active_tasks["task-1"] = finished

    await registry._lock.acquire()
    try:
        registry._on_active_task_cleanup(finished)
        registry._active_tasks["task-1"] = replacement
    finally:
        registry._lock.release()
    await asyncio.gather(*tuple(registry._cleanup_tasks))

    assert await registry.get("task-1") is replacement

    await finished._event_queue_agent.close(immediate=True)
    await finished._event_queue_subscribers.close(immediate=True)
    await replacement._event_queue_agent.close(immediate=True)
    await replacement._event_queue_subscribers.close(immediate=True)


@pytest.mark.asyncio
async def test_reused_sdk_producer_reads_admission_from_each_request_context() -> None:
    observed: list[str | None] = []

    class RecordingExecutor:
        async def execute(self, request_context, event_queue) -> None:
            observed.append(RecoverableInputAdmissionCarrier.read(request_context))
            if request_context.current_task is None:
                await event_queue.enqueue_event(
                    Task(
                        id="task-1",
                        context_id="ctx-1",
                        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
                    )
                )
            else:
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id="task-1",
                        context_id="ctx-1",
                        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
                    )
                )

        async def cancel(self, _request_context, event_queue) -> None:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="ctx-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
                )
            )

    call_context = ServerCallContext()
    store = A2ATaskStore()
    registry = ActiveTaskRegistry(agent_executor=RecordingExecutor(), task_store=store)
    active_task = await registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )

    for admission in ("recovery-first", "recovery-second"):
        request_context = RequestContext(
            call_context=call_context,
            task_id="task-1",
            context_id="ctx-1",
        )
        RecoverableInputAdmissionCarrier.attach(request_context, admission)
        events = [event async for event in active_task.subscribe(request=request_context)]
        assert any(
            isinstance(event, (Task, TaskStatusUpdateEvent))
            and event.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
            for event in events
        )

    assert observed == ["recovery-first", "recovery-second"]
    await active_task.cancel(call_context)


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
@pytest.mark.parametrize(
    (
        "phase",
        "release_ready",
        "control_task_id",
        "admission_allowed",
        "sidecar_task_id",
        "expected_task_state",
        "expected_persisted_state",
        "expected_active_task_id",
    ),
    [
        pytest.param(
            "terminated",
            True,
            "task-1",
            True,
            "task-1",
            TaskState.TASK_STATE_INPUT_REQUIRED,
            "input-required",
            None,
            id="release-ready",
        ),
        pytest.param(
            "terminated",
            False,
            "task-1",
            True,
            "task-1",
            TaskState.TASK_STATE_INPUT_REQUIRED,
            "input-required",
            None,
            id="terminated-backup-blocked",
        ),
        pytest.param(
            "terminating",
            False,
            "task-1",
            False,
            "task-1",
            TaskState.TASK_STATE_FAILED,
            "failed",
            "task-1",
            id="termination-in-flight",
        ),
        pytest.param(
            "terminated",
            False,
            "task-other",
            False,
            "task-1",
            TaskState.TASK_STATE_FAILED,
            "failed",
            "task-1",
            id="blocked-control-task-mismatch",
        ),
        pytest.param(
            "terminated",
            False,
            "task-1",
            False,
            "task-1",
            TaskState.TASK_STATE_FAILED,
            "failed",
            "task-1",
            id="blocked-control-live-background-work",
        ),
        pytest.param(
            "terminated",
            False,
            "task-1",
            True,
            "task-other",
            TaskState.TASK_STATE_FAILED,
            "failed",
            "task-1",
            id="sidecar-task-mismatch",
        ),
    ],
)
async def test_handler_reconciles_terminal_task_when_pipeline_sidecar_is_waiting_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    phase: str,
    release_ready: bool,
    control_task_id: str,
    admission_allowed: bool,
    sidecar_task_id: str,
    expected_task_state: int,
    expected_persisted_state: str,
    expected_active_task_id: str | None,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    context_id = "ctx-1"
    task_id = "task-1"
    call_context = ServerCallContext()
    persistence = A2APersistenceStore(tmp_path / "a2a")
    store = A2ATaskStore(persistence=persistence)
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
    issued_admissions: list[str] = []
    released_admissions: list[str] = []

    async def reserve_admission(*, context_id: str, task_id: str, owner: str) -> str | None:
        assert context_id == "ctx-1"
        assert task_id == "task-1"
        if not admission_allowed:
            return None
        admission = f"recovery-{owner}-{task_id}"
        issued_admissions.append(admission)
        return admission

    async def release_admission(admission: str) -> None:
        released_admissions.append(admission)

    store.set_execution_control_provider(
        lambda _context_id: {
            "taskId": control_task_id,
            "phase": phase,
            "releaseReady": release_ready,
            "backup": {"status": "shared_committed" if release_ready else "blocked"},
        },
        lambda: phase == "terminating" or not release_ready,
        reserve_admission,
        release_admission,
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
        "taskId": sidecar_task_id,
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
    retired_sdk_tasks: list[str] = []

    class RecoveryAwareRegistry:
        async def reconcile_and_replace_for_recovery(
            self, recovered_task_id: str, *, acquire_admission, **_kwargs
        ) -> str | None:
            admission = await acquire_admission()
            if admission is not None:
                retired_sdk_tasks.append(recovered_task_id)
            return admission

        async def cancel_recovery_reservation(self, _task_id: str, _admission: str) -> None:
            return None

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._active_task_registry = RecoveryAwareRegistry()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    params = SimpleNamespace(message=SimpleNamespace(task_id=task_id, context_id=context_id))

    if expected_task_state == TaskState.TASK_STATE_INPUT_REQUIRED:
        original_save_task = persistence.save_task

        def fail_recovery_task_save(snapshot):
            if snapshot.state == "input-required":
                raise OSError("temporary recovery write failure")
            original_save_task(snapshot)

        monkeypatch.setattr(persistence, "save_task", fail_recovery_task_save)
        with pytest.raises(OSError, match="temporary recovery write failure"):
            await handler.on_message_send(params, call_context)
        assert observed == {}
        failed_recovery_task = await store.get(task_id, call_context)
        assert failed_recovery_task is not None
        assert failed_recovery_task.status.state == TaskState.TASK_STATE_FAILED
        assert persistence.load_task(task_id).state == "failed"
        assert persistence.load_context(context_id).active_task_id == task_id
        assert (await store.get_context_record(context_id)).active_task_id == task_id
        monkeypatch.setattr(persistence, "save_task", original_save_task)

    if not admission_allowed and sidecar_task_id == task_id:
        with pytest.raises(InvalidParamsError, match="already being recovered"):
            await handler.on_message_send(params, call_context)
        assert observed == {}
        assert issued_admissions == []
        assert released_admissions == []
        assert retired_sdk_tasks == []
        assert persistence.load_task(task_id).state == expected_persisted_state
        assert persistence.load_context(context_id).active_task_id == expected_active_task_id
        return

    result = await handler.on_message_send(params, call_context)

    assert isinstance(result, Task)
    assert observed["state"] == expected_task_state
    assert result.status.state == expected_task_state
    persisted_task = persistence.load_task(task_id)
    assert persisted_task is not None
    assert persisted_task.state == expected_persisted_state
    persisted_context = persistence.load_context(context_id)
    assert persisted_context is not None
    assert persisted_context.active_task_id == expected_active_task_id
    assert (await store.get_context_record(context_id)).active_task_id == expected_active_task_id
    session_dir = SessionStorage().session_dir(str(cwd), ctx.session_id)
    context_snapshot = json.loads((session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))
    assert context_snapshot["active_task_id"] == expected_active_task_id
    if expected_task_state == TaskState.TASK_STATE_INPUT_REQUIRED:
        assert len(issued_admissions) == 2
        assert released_admissions == issued_admissions
    else:
        assert issued_admissions == []
        assert released_admissions == []
    assert retired_sdk_tasks == ([task_id] if expected_task_state == TaskState.TASK_STATE_INPUT_REQUIRED else [])

    if expected_task_state == TaskState.TASK_STATE_INPUT_REQUIRED:
        for seconds, late_state in enumerate(
            (
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
                TaskState.TASK_STATE_COMPLETED,
            ),
            start=1,
        ):
            late_terminal = Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=late_state),
            )
            late_terminal.status.timestamp.FromSeconds(seconds)
            await store.save(late_terminal, call_context)

            visible_task = await store.get(task_id, call_context)
            assert visible_task is not None
            assert visible_task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
            persisted_task = persistence.load_task(task_id)
            assert persisted_task is not None
            assert persisted_task.state == "input-required"
            persisted_context = persistence.load_context(context_id)
            assert persisted_context is not None
            assert persisted_context.active_task_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "late_state",
    [
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_COMPLETED,
    ],
)
async def test_recovered_running_execution_rejects_older_terminal_projection(tmp_path, late_state: int) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    context_id = "ctx-1"
    task_id = "task-1"
    call_context = ServerCallContext()
    persistence = A2APersistenceStore(tmp_path / "a2a")
    store = A2ATaskStore(persistence=persistence)
    service = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=None)
    store.set_execution_control_provider(
        service.snapshot_for_context,
        service.has_active_work,
        service.reserve_recoverable_input_continuation,
        service.release_recoverable_input_continuation,
    )
    context_record = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda session_id: SimpleNamespace(session_id=session_id),
    )
    context_record.active_task_id = task_id
    store.mirror_context(context_record)
    failed = Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
    )
    failed.status.timestamp.FromSeconds(100)
    await store.save(failed, call_context)
    owner = store.owner_for_context(call_context)

    original = await service.begin_execution(
        context_id=context_id,
        task_id=task_id,
        owner=owner,
        cwd=str(cwd),
    )
    current = asyncio.current_task()
    assert current is not None
    await original.detach_task(current, execution_status="input-required")
    original.phase = "terminated"
    original.execution_status = "canceled"
    original.backup = {"status": "blocked", "error": "shared backup unavailable"}
    original.release_ready = False
    original.revision += 1
    await original._persist_snapshot(original.snapshot())

    recovered_task = Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    recovered_task.status.timestamp.FromSeconds(200)
    admission = await store.reconcile_recoverable_input_required_task(
        recovered_task,
        context_record,
        call_context,
    )
    assert admission is not None
    recovered = await service.begin_execution(
        context_id=context_id,
        task_id=task_id,
        owner=owner,
        cwd=str(cwd),
        continue_input_required=True,
        recoverable_input_admission=admission,
    )
    assert recovered.phase == "running"

    late_terminal = Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=late_state),
    )
    late_terminal.status.timestamp.FromSeconds(150)
    await store.save(late_terminal, call_context)

    visible = await store.get(task_id, call_context)
    assert visible is not None
    assert visible.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert persistence.load_task(task_id).state == "input-required"
    assert persistence.load_context(context_id).active_task_id is None

    await recovered.detach_task(current, execution_status="input-required")
    await service.close()


@pytest.mark.asyncio
async def test_handler_does_not_reconcile_a_running_sidecar_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    context_id = "ctx-1"
    task_id = "task-1"
    call_context = ServerCallContext()
    persistence = A2APersistenceStore(tmp_path / "a2a")
    store = A2ATaskStore(persistence=persistence)
    context_record = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda session_id: SimpleNamespace(session_id=session_id),
    )
    context_record.active_task_id = task_id
    store.mirror_context(context_record)
    await store.save(
        Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
        ),
        call_context,
    )
    admission_calls: list[str] = []

    async def reserve_admission(**_kwargs) -> str:
        admission_calls.append("called")
        return "unexpected"

    store.set_execution_control_provider(None, None, reserve_admission, None)
    include_running_values: list[bool] = []

    def running_sidecar_only(*, include_running: bool, **_kwargs) -> str | None:
        include_running_values.append(include_running)
        return task_id if include_running else None

    monkeypatch.setattr(
        "iac_code.a2a.transports.dispatcher.recoverable_task_id_from_sidecar",
        running_sidecar_only,
    )

    async def sdk_send(_handler, _params, sdk_context):
        return await store.get(task_id, sdk_context)

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send", sdk_send)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    params = SimpleNamespace(message=SimpleNamespace(task_id=task_id, context_id=context_id))

    result = await handler.on_message_send(params, call_context)

    assert isinstance(result, Task)
    assert result.status.state == TaskState.TASK_STATE_FAILED
    assert include_running_values == [False]
    assert admission_calls == []
    assert persistence.load_task(task_id).state == "failed"
    assert persistence.load_context(context_id).active_task_id == task_id


@pytest.mark.asyncio
async def test_handler_rejects_recovery_owned_by_another_process_before_sdk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    persistence_root = tmp_path / "a2a"
    persistence = A2APersistenceStore(persistence_root)
    context_id = "ctx-1"
    task_id = "task-1"
    call_context = ServerCallContext()
    first_service = ExecutionControlService(persistence_root=persistence_root, backup_service=None)
    second_service = ExecutionControlService(persistence_root=persistence_root, backup_service=None)
    first_store = A2ATaskStore(persistence=persistence)
    first_store.set_execution_control_provider(
        first_service.snapshot_for_context,
        first_service.has_active_work,
        first_service.reserve_recoverable_input_continuation,
        first_service.release_recoverable_input_continuation,
    )
    context_record = await first_store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda session_id: SimpleNamespace(session_id=session_id),
    )
    context_record.active_task_id = None
    first_store.mirror_context(context_record)
    await first_store.save(
        Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        ),
        call_context,
    )
    admission = await first_service.reserve_recoverable_input_continuation(
        context_id=context_id,
        task_id=task_id,
        owner=first_store.owner_for_context(call_context),
    )
    assert admission is not None

    second_store = A2ATaskStore(persistence=A2APersistenceStore(persistence_root))
    second_store.set_execution_control_provider(
        second_service.snapshot_for_context,
        second_service.has_active_work,
        second_service.reserve_recoverable_input_continuation,
        second_service.release_recoverable_input_continuation,
    )
    monkeypatch.setattr(
        "iac_code.a2a.transports.dispatcher.recoverable_task_id_from_sidecar",
        lambda **_kwargs: task_id,
    )
    sdk_called = False

    async def sdk_send(_handler, _params, _context):
        nonlocal sdk_called
        sdk_called = True
        return None

    async def hydrate(_params) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send", sdk_send)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = second_store
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    params = SimpleNamespace(message=SimpleNamespace(task_id=task_id, context_id=context_id))

    try:
        with pytest.raises(InvalidParamsError, match="already being recovered"):
            await handler.on_message_send(params, call_context)
        assert sdk_called is False
        assert persistence.load_task(task_id).state == "input-required"
        assert persistence.load_context(context_id).active_task_id is None
        recovered = await first_service.begin_execution(
            context_id=context_id,
            task_id=task_id,
            owner=first_store.owner_for_context(call_context),
            cwd=str(cwd),
            continue_input_required=True,
            recoverable_input_admission=admission,
        )
        assert recovered.phase == "running"
        current = asyncio.current_task()
        assert current is not None
        await recovered.detach_task(current, execution_status="input-required")
    finally:
        await first_service.release_recoverable_input_continuation(admission)
        await first_service.close()
        await second_service.close()


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

        async def resolve_omitted_pipeline_task_id(self, *, context_id: str, cwd: str, invocation_id: str):
            assert invocation_id
            await self._reconcile_session_before_route(context_id=context_id, cwd=cwd)
            return task_id

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = FakeExecutor()
    params = SimpleNamespace(message=SimpleNamespace(task_id=None, context_id=context_id))

    await handler._hydrate_recoverable_pipeline_task_id(params)

    assert params.message.task_id == task_id
    assert storage.session_dir(str(cwd), ctx.session_id).is_dir()


@pytest.mark.asyncio
async def test_handler_allocates_successor_when_canceled_task_still_has_running_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    cwd = tmp_path / "workspace"
    context_id = "ctx-1"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    store = A2ATaskStore(persistence=persistence)
    ctx = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda session_id: SimpleNamespace(session_id=session_id),
    )
    old_task = await store.get_or_create_task(task_id="task-old", context_id=context_id)
    old_task.state = "canceled"
    store.mirror_task(old_task)
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=ctx.session_id)
    running_event = {
        "schemaVersion": "1.0",
        "eventId": "evt-running",
        "sequence": 1,
        "eventType": "step_started",
        "pipelineRunId": context_id,
        "taskId": "task-old",
        "contextId": context_id,
        "status": "working",
        "step": {"id": "deploy", "runId": "deploy-1", "attempt": 1},
        "data": {},
    }
    A2APipelineJournal(pipeline_dir).append(running_event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([running_event]))
    sidecar_dir = SessionStorage().session_dir(str(cwd), ctx.session_id) / "pipeline"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "meta.yaml").write_text("status: canceled\ncurrent_step: deploy\n", encoding="utf-8")
    control_dir = persistence.root / "execution-control"
    control_dir.mkdir(parents=True)
    (control_dir / f"{context_id}.json").write_text(
        json.dumps(
            {
                "contextId": context_id,
                "taskId": "task-old",
                "executionId": "execution-old",
                "phase": "terminated",
                "executionStatus": "canceled",
                "terminationReason": "explicit_terminate",
                "releaseReady": True,
                "revision": 11,
                "persistedRevision": 11,
                "commitError": None,
                "blockers": [],
                "backup": {
                    "status": "staged_committed",
                    "generation": 2,
                    "commitId": "commit-cancel",
                },
            }
        ),
        encoding="utf-8",
    )
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = IacCodeA2AExecutor(task_store=store, model="test")
    params = SimpleNamespace(message=SimpleNamespace(task_id=None, context_id=context_id, message_id="request-1"))

    await handler._hydrate_recoverable_pipeline_task_id(params)
    first_successor = params.message.task_id
    params.message.task_id = None
    await handler._hydrate_recoverable_pipeline_task_id(params)

    assert first_successor != "task-old"
    assert params.message.task_id == first_successor


@pytest.mark.asyncio
async def test_default_handler_accepts_precreated_successor_task_for_omitted_task_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    store = A2ATaskStore()
    context_id = "ctx-successor"
    await store.get_or_create_context(
        context_id=context_id,
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: SimpleNamespace(session_id=session_id),
    )

    class Executor:
        async def resolve_omitted_pipeline_task_id(self, **_kwargs) -> str:
            return "task-successor"

        async def execute(self, request_context, event_queue) -> None:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=request_context.task_id,
                    context_id=request_context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                )
            )

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    handler = IacCodeRequestHandler(
        agent_executor=Executor(),
        task_store=store,
        agent_card=SimpleNamespace(capabilities=SimpleNamespace(streaming=True, extensions=[])),
    )
    message = Message(
        message_id="message-successor",
        context_id=context_id,
        role=Role.ROLE_USER,
        parts=[Part(text="continue")],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline", "cwd": str(tmp_path)}}, message.metadata)

    events = await _collect_async(
        handler.on_message_send_stream(SendMessageRequest(message=message), ServerCallContext())
    )

    assert await store.get("task-successor") is not None
    assert any(
        isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_COMPLETED
        for event in events
    )


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

    stream = handler.on_message_send_stream(params, SimpleNamespace())
    assert await anext(stream) is update
    assert observed["completion"].done() is False
    assert stages == ["registered", "dequeued"]

    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    assert observed["completion"].done() is True
    assert stages == ["registered", "dequeued", "acknowledged"]
    assert pipeline_transport_delivery_tracking_enabled() is False


@pytest.mark.asyncio
async def test_message_stream_finalizes_natural_execution_only_after_stream_exhaustion(monkeypatch) -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    order: list[str] = []

    async def sdk_stream(_handler, _params, _context):
        order.append("yield")
        yield update
        NaturalCompletionGenerationCarrier.attach(SimpleNamespace(call_context=_context), 7)
        order.append("exhausted")

    class Executor:
        async def finalize_natural_execution(self, **kwargs) -> dict:
            assert kwargs["completion_generation"] == 7
            order.append("finalized")
            return _durable_natural_finalization(kwargs["completion_generation"])

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = Executor()
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    stream = handler.on_message_send_stream(params, call_context)
    assert await anext(stream) is update
    assert order == ["yield"]
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    assert order == ["yield", "exhausted", "finalized"]


@pytest.mark.asyncio
async def test_message_stream_waits_for_staged_natural_handoff_before_eof(monkeypatch, tmp_path) -> None:
    capture_started = asyncio.Event()
    release_capture = asyncio.Event()

    class BlockingCoordinator:
        enabled = True

        async def register_boundary(self, **_kwargs) -> SessionBackupHandoff:
            capture_started.set()
            await release_capture.wait()
            return SessionBackupHandoff(
                business_revision=1,
                job_id="job-1",
                staged_committed=True,
                snapshot_generation=1,
                snapshot_commit_id="commit-1",
            )

    class PermissionRegistry:
        async def reversible_closing_tokens(self, _task_id: str) -> tuple[object, ...]:
            return ()

        async def reopen_task(self, _closing_token) -> None:
            return None

    call_context = ServerCallContext()
    store = A2ATaskStore()
    owner = store.owner_for_context(call_context)
    service = ExecutionControlService(
        persistence_root=tmp_path,
        backup_service=None,
        backup_coordinator=BlockingCoordinator(),
    )
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner=owner,
        cwd=str(tmp_path),
    )
    control.bind_session("session-1")
    blocker = await control.begin_activity("late-sidecar", check_gate=False)
    current = asyncio.current_task()
    assert current is not None
    completion_generation = await control.detach_task(
        current,
        execution_status="completed",
        natural_completion=True,
    )
    assert completion_generation is not None
    finalization = asyncio.create_task(
        control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=completion_generation,
        )
    )
    await asyncio.sleep(0)
    assert finalization.done() is False
    await control.end_activity(blocker.activity_id)
    await asyncio.wait_for(capture_started.wait(), timeout=1)

    terminal = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )

    async def sdk_stream(_handler, _params, context):
        NaturalCompletionGenerationCarrier.attach(SimpleNamespace(call_context=context), completion_generation)
        yield terminal

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    executor = IacCodeA2AExecutor.__new__(IacCodeA2AExecutor)
    executor._execution_control_service = service
    executor._permission_input_registry = PermissionRegistry()
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = executor
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    stream = handler.on_message_send_stream(params, call_context)
    eof = None
    try:
        assert await anext(stream) is terminal
        eof = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert eof.done() is False

        release_capture.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(eof, timeout=1)
        await asyncio.wait_for(finalization, timeout=1)
        receipt = control.natural_handoff_receipt()
        assert receipt is not None
        assert receipt["stagedCommitted"] is True
        assert receipt["snapshotGeneration"] == 1
        assert receipt["snapshotCommitId"] == "commit-1"
    finally:
        release_capture.set()
        if eof is not None:
            await asyncio.gather(eof, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_message_stream_allows_clean_eof_after_direct_shared_natural_handoff(monkeypatch, tmp_path) -> None:
    cwd = tmp_path / "workspace"
    session_id = "session-1"
    storage = SessionStorage(projects_dir=tmp_path / "local" / "projects")
    storage.save(str(cwd), session_id, [])
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    backup_service = SessionBackupService(storage, retry_delays=())
    backup_service.initialize_session(str(cwd), session_id)

    class PermissionRegistry:
        async def reversible_closing_tokens(self, _task_id: str) -> tuple[object, ...]:
            return ()

        async def reopen_task(self, _closing_token) -> None:
            return None

    call_context = ServerCallContext()
    store = A2ATaskStore()
    owner = store.owner_for_context(call_context)
    service = ExecutionControlService(
        persistence_root=tmp_path / "state",
        backup_service=backup_service,
    )
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner=owner,
        cwd=str(cwd),
    )
    control.bind_session(session_id)
    current = asyncio.current_task()
    assert current is not None
    completion_generation = await control.detach_task(
        current,
        execution_status="completed",
        natural_completion=True,
    )
    assert completion_generation is not None
    terminal = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )

    async def sdk_stream(_handler, _params, context):
        NaturalCompletionGenerationCarrier.attach(SimpleNamespace(call_context=context), completion_generation)
        yield terminal

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    executor = IacCodeA2AExecutor.__new__(IacCodeA2AExecutor)
    executor._execution_control_service = service
    executor._permission_input_registry = PermissionRegistry()
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = executor
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    stream = handler.on_message_send_stream(params, call_context)
    try:
        assert await anext(stream) is terminal
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        receipt = control.natural_handoff_receipt()
        assert receipt is not None
        assert receipt["backupDisabled"] is False
        assert receipt["snapshotGeneration"] == control.backup["generation"]
        assert receipt["snapshotCommitId"] == control.backup["commitId"]
    finally:
        await stream.aclose()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finalization_failure", ["blocked", "timeout"])
async def test_message_stream_natural_finalization_failure_is_not_clean_eof(
    monkeypatch,
    finalization_failure: str,
) -> None:
    call_context = ServerCallContext()
    terminal = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )

    async def sdk_stream(_handler, _params, context):
        NaturalCompletionGenerationCarrier.attach(SimpleNamespace(call_context=context), 7)
        yield terminal

    class Executor:
        async def finalize_natural_execution(self, **_kwargs):
            if finalization_failure == "timeout":
                raise TimeoutError("natural finalization timed out")
            return {
                "contextId": "ctx-1",
                "taskId": "task-1",
                "executionId": "exec-1",
                "ownerGeneration": 1,
                "revision": 1,
                "persistedRevision": 1,
                "phase": "terminated",
                "terminationReason": "natural_completion",
                "backup": {"status": "blocked", "error": "capture failed"},
                "naturalHandoff": None,
                NATURAL_COMPLETION_FINALIZED_GENERATION: None,
            }

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = A2ATaskStore()
    handler.agent_executor = Executor()
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    stream = handler.on_message_send_stream(params, call_context)
    assert await anext(stream) is terminal
    expected_error = TimeoutError if finalization_failure == "timeout" else RuntimeError
    with pytest.raises(expected_error):
        await anext(stream)


@pytest.mark.asyncio
async def test_message_stream_disconnect_does_not_naturally_finalize_execution(monkeypatch) -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    first = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    finalized = False

    async def sdk_stream(_handler, _params, _context):
        yield first
        NaturalCompletionGenerationCarrier.attach(SimpleNamespace(call_context=_context), 7)
        yield TaskStatusUpdateEvent(
            task_id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        )

    class Executor:
        async def finalize_natural_execution(self, **_kwargs) -> None:
            nonlocal finalized
            finalized = True

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = Executor()
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    stream = handler.on_message_send_stream(params, call_context)
    assert await anext(stream) is first
    await stream.aclose()

    assert finalized is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_COMPLETED])
async def test_message_stream_boundary_aclose_finalizes_after_executor_detaches(monkeypatch, state) -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    boundary = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=state),
    )
    detach_entered = asyncio.Event()
    allow_detach = asyncio.Event()
    finalized: list[int] = []

    class Executor:
        async def finalize_natural_execution(self, **kwargs) -> dict:
            finalized.append(kwargs["completion_generation"])
            return _durable_natural_finalization(kwargs["completion_generation"])

    executor = Executor()

    async def sdk_stream(_handler, _params, _context):
        try:
            yield boundary
        finally:
            detach_entered.set()
            await allow_detach.wait()
            NaturalCompletionGenerationCarrier.attach(SimpleNamespace(call_context=_context), 7)

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = executor
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    stream = handler.on_message_send_stream(params, call_context)
    assert await anext(stream) is boundary
    closing = asyncio.create_task(stream.aclose())
    await detach_entered.wait()
    assert finalized == []
    allow_detach.set()
    await closing

    assert finalized == [7]


@pytest.mark.asyncio
async def test_message_stream_boundary_aclose_finalizes_generation_attached_before_delivery(monkeypatch) -> None:
    call_context = ServerCallContext()
    boundary = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    finalized: list[int] = []

    async def sdk_stream(_handler, _params, _context):
        NaturalCompletionGenerationCarrier.attach(SimpleNamespace(call_context=_context), 7)
        yield boundary

    class Executor:
        async def finalize_natural_execution(self, **kwargs) -> dict:
            finalized.append(kwargs["completion_generation"])
            return _durable_natural_finalization(kwargs["completion_generation"])

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = A2ATaskStore()
    handler.agent_executor = Executor()
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    stream = handler.on_message_send_stream(params, call_context)
    assert await anext(stream) is boundary
    await stream.aclose()

    assert finalized == [7]


@pytest.mark.asyncio
async def test_message_stream_pending_permission_boundary_aclose_without_generation_does_not_finalize(
    monkeypatch,
) -> None:
    call_context = ServerCallContext()
    boundary = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    finalized = False

    async def sdk_stream(_handler, _params, _context):
        yield boundary

    class Executor:
        async def finalize_natural_execution(self, **_kwargs) -> None:
            nonlocal finalized
            finalized = True

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = A2ATaskStore()
    handler.agent_executor = Executor()
    handler._active_task_registry = None
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    stream = handler.on_message_send_stream(params, call_context)
    assert await anext(stream) is boundary
    await stream.aclose()

    assert finalized is False


@pytest.mark.asyncio
async def test_detached_permission_response_finalizes_after_terminal_producer_drains() -> None:
    call_context = ServerCallContext()
    request_context = SimpleNamespace(call_context=call_context)
    NaturalCompletionGenerationCarrier.prepare(request_context)
    producer_can_finish = asyncio.Event()
    finalized = asyncio.Event()

    class RequestContextBuilder:
        async def build(self, **_kwargs):
            return request_context

    class Executor:
        async def execute(self, _request_context, event_queue) -> None:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="ctx-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                )
            )
            await producer_can_finish.wait()
            NaturalCompletionGenerationCarrier.attach(request_context, 7)

        async def finalize_natural_execution(self, **kwargs) -> dict:
            assert kwargs["completion_generation"] == 7
            finalized.set()
            return _durable_natural_finalization(kwargs["completion_generation"])

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = A2ATaskStore()
    handler.agent_executor = Executor()
    handler._request_context_builder = RequestContextBuilder()
    handler._detached_message_producers = set()
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )

    stream = handler._on_inactive_permission_send_stream(params, call_context, task=task)
    event = await anext(stream)
    assert event.status.state == TaskState.TASK_STATE_COMPLETED
    await stream.aclose()
    producer_can_finish.set()

    await asyncio.wait_for(finalized.wait(), timeout=1)
    await asyncio.gather(*handler._detached_message_producers)


@pytest.mark.asyncio
async def test_inactive_pipeline_input_retries_attach_race_after_release() -> None:
    call_context = ServerCallContext()
    request_context = SimpleNamespace(call_context=call_context)

    class RequestContextBuilder:
        async def build(self, **_kwargs):
            return request_context

    class Executor:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.wait_calls = 0

        async def wait_until_recoverable_pipeline_input(self, *, context_id: str, task_id: str) -> None:
            assert (context_id, task_id) == ("ctx-1", "task-1")
            self.wait_calls += 1

        async def execute(self, _request_context, event_queue) -> None:
            self.execute_calls += 1
            if self.execute_calls == 1:
                raise InputResponseExecutionControlConflictError("Execution is terminating")
            assert RecoverableInputAdmissionCarrier.read(_request_context) == "recovery-1"
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="ctx-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )

    store = A2ATaskStore()
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    await store.save(task, call_context)
    message = Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text="continue")],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline"}}, message.metadata)
    params = SimpleNamespace(message=message, configuration=None)
    executor = Executor()
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = executor
    handler._request_context_builder = RequestContextBuilder()
    handler._detached_message_producers = set()

    async def reconcile(_params, _context) -> str:
        return "recovery-1"

    handler._reconcile_and_replace_recovered_sdk_task = reconcile

    events = await _collect_async(handler._on_inactive_input_response_send_stream(params, call_context, task=task))

    assert len(events) == 1
    assert events[0].status.state == TaskState.TASK_STATE_WORKING
    assert executor.execute_calls == 2
    assert executor.wait_calls == 2


@pytest.mark.asyncio
async def test_pipeline_ask_user_question_base_stream_retries_attach_race(monkeypatch) -> None:
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
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    stream_calls = 0

    async def sdk_stream(_handler, _params, observed_context):
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls == 1:
            raise InputResponseExecutionControlConflictError("Execution is terminating")
        assert IacCodeRequestHandler._take_recoverable_input_admission(observed_context) == "recovery-1"
        yield update

    class Executor:
        def __init__(self) -> None:
            self.wait_calls = 0

        async def wait_until_recoverable_pipeline_input(self, *, context_id: str, task_id: str) -> None:
            assert (context_id, task_id) == ("ctx-1", "task-1")
            self.wait_calls += 1

    executor = Executor()
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = executor
    handler._active_task_registry = SimpleNamespace(get=lambda _task_id: None)
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None

    async def hydrate(_params) -> None:
        return None

    reconcile_calls = 0

    async def reconcile(_params, _context) -> str | None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        return None if reconcile_calls == 1 else "recovery-1"

    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_and_replace_recovered_sdk_task = reconcile
    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    message = Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text='{"answer":"continue"}')],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline"}}, message.metadata)

    events = await _collect_async(
        handler.on_message_send_stream(SimpleNamespace(message=message, configuration=None), call_context)
    )

    assert events == [update]
    assert stream_calls == 2
    assert executor.wait_calls == 1


@pytest.mark.asyncio
async def test_nonstream_pipeline_input_retries_attach_race(monkeypatch) -> None:
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
    expected = Message(message_id="result", role=Role.ROLE_AGENT, parts=[Part(text="continued")])
    send_calls = 0

    async def sdk_send(_handler, _params, observed_context):
        nonlocal send_calls
        send_calls += 1
        if send_calls == 1:
            raise InputResponseExecutionControlConflictError("Execution is terminating")
        assert IacCodeRequestHandler._take_recoverable_input_admission(observed_context) == "recovery-1"
        return expected

    class Executor:
        def __init__(self) -> None:
            self.wait_calls = 0

        async def wait_until_recoverable_pipeline_input(self, *, context_id: str, task_id: str) -> None:
            assert (context_id, task_id) == ("ctx-1", "task-1")
            self.wait_calls += 1

    executor = Executor()
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = executor
    handler._active_task_registry = SimpleNamespace()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None

    async def hydrate(_params) -> None:
        return None

    reconcile_calls = 0

    async def reconcile(_params, _context) -> str | None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        return None if reconcile_calls == 1 else "recovery-1"

    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_and_replace_recovered_sdk_task = reconcile
    monkeypatch.setattr(DefaultRequestHandler, "on_message_send", sdk_send)
    message = Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text='{"answer":"continue"}')],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline"}}, message.metadata)

    result = await handler.on_message_send(SimpleNamespace(message=message, configuration=None), call_context)

    assert result is expected
    assert send_calls == 2
    assert executor.wait_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resumed_state",
    [
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_INPUT_REQUIRED,
    ],
)
async def test_nonstream_inactive_resource_selection_finalizes_natural_execution_once(resumed_state) -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    await store.save(task, call_context)
    await store.get_or_create_task(task_id=task.id, context_id=task.context_id)
    finalized = 0

    response = {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "status": "selected",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "resource-" + "a" * 32,
        "toolUseId": "tool-1",
        "selectorId": "ecs.instance",
        "value": "i-test123",
    }
    message = Message(
        message_id="resource-response",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text=RESOURCE_SELECTION_QUERY_PREFIX + json.dumps(response, separators=(",", ":")))],
    )

    async def resume_stream(_params, context, *, task):
        await store.save(
            Task(
                id=task.id,
                context_id=task.context_id,
                status=TaskStatus(state=resumed_state),
            ),
            context,
        )
        if False:
            yield None

    async def finalize(_params, _context) -> None:
        nonlocal finalized
        finalized += 1

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._on_inactive_input_response_send_stream = resume_stream
    handler._finalize_natural_execution = finalize

    result = await handler.on_message_send(
        SimpleNamespace(message=message, configuration=None),
        call_context,
    )

    assert result.status.state == resumed_state
    assert finalized == 1


@pytest.mark.asyncio
async def test_active_structured_pipeline_input_recovers_attach_race_in_same_request() -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    await store.save(task, call_context)
    record = await store.get_or_create_task(task_id=task.id, context_id=task.context_id)
    record.active_task = asyncio.current_task()
    subscribers = EventQueueSource(create_default_sink=False)

    class ForwardingAgentQueue:
        async def enqueue_event(self, event) -> None:
            await subscribers.enqueue_event((event, task))

        async def test_only_join_incoming_queue(self) -> None:
            await subscribers.test_only_join_incoming_queue()

    class ActiveTask:
        def __init__(self) -> None:
            self.task_id = task.id
            self.direct_message_lock = asyncio.Lock()
            self._lock = asyncio.Lock()
            self._is_finished = asyncio.Event()
            self._reference_count = 0
            self._event_queue_agent = ForwardingAgentQueue()
            self._event_queue_subscribers = subscribers

        async def _maybe_cleanup(self) -> None:
            return None

    active_task = ActiveTask()

    class ActiveTaskRegistry:
        async def get(self, _task_id):
            return active_task

        async def cancel_recovery_reservation(self, _task_id: str, _admission: str) -> None:
            return None

    class RequestContextBuilder:
        async def build(self, **kwargs):
            return SimpleNamespace(call_context=kwargs["context"])

    update = TaskStatusUpdateEvent(
        task_id=task.id,
        context_id=task.context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )

    class Executor:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.wait_calls = 0

        async def wait_until_recoverable_pipeline_input(self, *, context_id: str, task_id: str) -> None:
            assert (context_id, task_id) == ("ctx-1", "task-1")
            self.wait_calls += 1
            record.active_task = None

        async def execute(self, request_context, event_queue) -> None:
            self.execute_calls += 1
            if self.execute_calls == 1:
                raise InputResponseExecutionControlConflictError("Execution is terminating")
            assert RecoverableInputAdmissionCarrier.read(request_context) == "recovery-1"
            try:
                await event_queue.enqueue_event(update)
            finally:
                await RecoverableInputAdmissionCarrier.release(request_context)

    executor = Executor()
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = executor
    handler._request_context_builder = RequestContextBuilder()
    handler._active_task_registry = ActiveTaskRegistry()
    handler._detached_message_producers = set()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None

    async def reconcile(_params, _context) -> str:
        return "recovery-1"

    handler._reconcile_and_replace_recovered_sdk_task = reconcile
    response = {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "status": "selected",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "resource-" + "a" * 32,
        "toolUseId": "tool-1",
        "selectorId": "ecs.instance",
        "value": "i-test123",
    }
    message = Message(
        message_id="message-resource-selection",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text=RESOURCE_SELECTION_QUERY_PREFIX + json.dumps(response, separators=(",", ":")))],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline"}}, message.metadata)

    try:
        events = await _collect_async(
            handler.on_message_send_stream(SimpleNamespace(message=message, configuration=None), call_context)
        )
    finally:
        await subscribers.close(immediate=True)

    assert events == [update]
    assert executor.execute_calls == 2
    assert executor.wait_calls == 2
    assert active_task._reference_count == 0
    assert not active_task.direct_message_lock.locked()


@pytest.mark.asyncio
async def test_detached_permission_disconnect_without_terminal_generation_does_not_finalize() -> None:
    call_context = ServerCallContext()
    request_context = SimpleNamespace(call_context=call_context)
    NaturalCompletionGenerationCarrier.prepare(request_context)
    finalized = False

    class Executor:
        async def finalize_natural_execution(self, **_kwargs) -> None:
            nonlocal finalized
            finalized = True

    async def completed_producer() -> None:
        return None

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = A2ATaskStore()
    handler.agent_executor = Executor()
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))
    completed = object()
    queue: asyncio.Queue[object] = asyncio.Queue()
    await queue.put(completed)

    await handler._drain_inactive_permission_response(
        queue,
        asyncio.create_task(completed_producer()),
        completed,
        params=params,
        context=call_context,
    )

    assert finalized is False


@pytest.mark.asyncio
async def test_detached_permission_failed_producer_does_not_finalize() -> None:
    finalized = False

    class Executor:
        async def finalize_natural_execution(self, **_kwargs) -> None:
            nonlocal finalized
            finalized = True

    async def failed_producer() -> None:
        raise RuntimeError("continuation failed")

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = A2ATaskStore()
    handler.agent_executor = Executor()
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))
    completed = object()
    queue: asyncio.Queue[object] = asyncio.Queue()
    await queue.put(completed)

    await handler._drain_inactive_permission_response(
        queue,
        asyncio.create_task(failed_producer()),
        completed,
        params=params,
        context=ServerCallContext(),
    )

    assert finalized is False


@pytest.mark.asyncio
async def test_cancelled_detached_permission_drain_cancels_producer_without_finalizing() -> None:
    finalized = False
    producer_cancelled = asyncio.Event()

    class Executor:
        async def finalize_natural_execution(self, **_kwargs) -> None:
            nonlocal finalized
            finalized = True

    async def blocked_producer() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            producer_cancelled.set()
            raise

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = A2ATaskStore()
    handler.agent_executor = Executor()
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))
    completed = object()
    queue: asyncio.Queue[object] = asyncio.Queue()
    producer = asyncio.create_task(blocked_producer())
    drain = asyncio.create_task(
        handler._drain_inactive_permission_response(
            queue,
            producer,
            completed,
            params=params,
            context=ServerCallContext(),
        )
    )
    await asyncio.sleep(0)
    drain.cancel()

    with pytest.raises(asyncio.CancelledError):
        await drain
    assert producer_cancelled.is_set()
    assert finalized is False


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
async def test_message_stream_retires_stale_sdk_lifecycle_after_durable_recovery(monkeypatch) -> None:
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
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    retired: list[str] = []

    async def sdk_stream(_handler, _params, _context):
        yield update

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> str:
        return "recovery-1"

    class RecoveryAwareRegistry:
        async def reconcile_and_replace_for_recovery(self, task_id: str, *, acquire_admission, **_kwargs) -> str | None:
            admission = await acquire_admission()
            if admission is not None:
                retired.append(task_id)
            return admission

        async def cancel_recovery_reservation(self, _task_id: str, _admission: str) -> None:
            return None

        async def get(self, _task_id: str):
            return None

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._active_task_registry = RecoveryAwareRegistry()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    params = SimpleNamespace(message=SimpleNamespace(task_id="task-1", context_id="ctx-1"))

    events = await _collect_async(handler.on_message_send_stream(params, call_context))

    assert events == [update]
    assert retired == ["task-1"]


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
async def test_input_required_base_stream_rebinds_publisher_after_sdk_lifecycle_finished() -> None:
    from iac_code.a2a import pipeline_executor as pipeline_executor_module

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
    owner_release = asyncio.Event()
    domain_owner = asyncio.create_task(owner_release.wait())
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = domain_owner
    stale_queue = FakeEventQueue()
    runtime = pipeline_executor_module.A2APipelineRuntime(
        agent_runtime=SimpleNamespace(),
        publisher=SimpleNamespace(event_queue=stale_queue),
    )
    update = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    observed: dict[str, object] = {}

    class Executor:
        async def execute(self, request_context, event_queue) -> None:
            gate = DirectPipelineRouteGateCarrier.read(request_context)
            observed["gate"] = gate
            observed["registered"] = await pipeline_executor_module._register_active_interrupt(
                runtime,
                event_queue=event_queue,
                direct_route_gate=gate,
                bind_publisher_event_queue=PipelineLifecycleEventQueueCarrier.read(request_context),
            )
            try:
                await runtime.publisher.event_queue.enqueue_event(update)
            finally:
                await pipeline_executor_module._settle_active_interrupt_safely(runtime)

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    handler = IacCodeRequestHandler(
        agent_executor=Executor(),
        task_store=store,
        agent_card=SimpleNamespace(capabilities=SimpleNamespace(streaming=True, extensions=[])),
    )

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    assert await handler._active_task_registry.get("task-1") is None
    message = Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text='{"selected_candidate_index": 0}')],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline"}}, message.metadata)

    try:
        events = await asyncio.wait_for(
            _collect_async(
                handler.on_message_send_stream(
                    SendMessageRequest(message=message),
                    call_context,
                )
            ),
            timeout=_STREAM_TEST_TIMEOUT,
        )
    finally:
        owner_release.set()
        await domain_owner
        await handler._active_task_registry.retire_for_recovery("task-1")

    assert observed == {"gate": None, "registered": True}
    assert events == [update]
    assert stale_queue.events == []


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
async def test_message_stream_routes_resource_selection_text_to_active_input_required_task(monkeypatch) -> None:
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
        "kind": "cloud_resource_selection",
        "status": "selected",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "resource-" + "a" * 32,
        "toolUseId": "tool-1",
        "selectorId": "ecs.instance",
        "value": "i-test123",
    }
    message = Message(
        message_id="message-resource-selection",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text=RESOURCE_SELECTION_QUERY_PREFIX + json.dumps(response, separators=(",", ":")))],
    )
    active_stream_called = False

    async def fail_recovery(*_args, **_kwargs) -> None:
        raise AssertionError("resource selection input must not enter ordinary follow-up recovery")

    class ActiveTaskRegistry:
        async def get(self, _task_id):
            return object()

    async def active_stream(*_args, **_kwargs):
        nonlocal active_stream_called
        active_stream_called = True
        yield update

    async def fail_sdk_stream(*_args, **_kwargs):
        raise AssertionError("resource selection response must not wait in the SDK task queue")
        yield  # pragma: no cover

    monkeypatch.setattr(DefaultRequestHandler, "on_message_send_stream", fail_sdk_stream)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._active_task_registry = ActiveTaskRegistry()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = fail_recovery
    handler._reconcile_and_replace_recovered_sdk_task = fail_recovery
    handler._on_active_message_send_stream = active_stream

    events = await _collect_async(handler.on_message_send_stream(SimpleNamespace(message=message), call_context))

    assert events == [update]
    assert active_stream_called is True
    assert message.task_id == "task-1"


@pytest.mark.asyncio
async def test_active_message_route_ignores_old_terminal_events_around_its_request_boundary() -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    await store.save(task, call_context)
    record = await store.get_or_create_task(task_id=task.id, context_id=task.context_id)
    record.active_task = asyncio.current_task()

    old_terminal = Task(
        id=task.id,
        context_id=task.context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )
    current_update = TaskStatusUpdateEvent(
        task_id=task.id,
        context_id=task.context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    subscribers = EventQueueSource(create_default_sink=False)

    class ForwardingAgentQueue:
        def __init__(self) -> None:
            self.boundary_enqueued = False

        async def enqueue_event(self, event) -> None:
            if not self.boundary_enqueued:
                self.boundary_enqueued = True
                await subscribers.enqueue_event((old_terminal, task))
                await subscribers.enqueue_event((event, None))
                await subscribers.enqueue_event((old_terminal, task))
                return
            await subscribers.enqueue_event((event, task))

        async def test_only_join_incoming_queue(self) -> None:
            await subscribers.test_only_join_incoming_queue()

    class ActiveTask:
        def __init__(self) -> None:
            self.task_id = task.id
            self.direct_message_lock = asyncio.Lock()
            self._lock = asyncio.Lock()
            self._is_finished = asyncio.Event()
            self._reference_count = 0
            self._event_queue_agent = ForwardingAgentQueue()
            self._event_queue_subscribers = subscribers

        async def _maybe_cleanup(self) -> None:
            return None

    active_task = ActiveTask()

    class ActiveTaskRegistry:
        async def get(self, _task_id):
            return active_task

    class RequestContextBuilder:
        async def build(self, **_kwargs):
            return SimpleNamespace()

    class AgentExecutor:
        async def execute(self, _request_context, event_queue) -> None:
            await event_queue.enqueue_event(current_update)

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = AgentExecutor()
    handler._request_context_builder = RequestContextBuilder()
    handler._active_task_registry = ActiveTaskRegistry()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    message = Message(
        message_id="message-1",
        task_id=task.id,
        context_id=task.context_id,
        role=Role.ROLE_USER,
        parts=[Part(text="continue")],
    )
    params = SimpleNamespace(message=message, configuration=None)

    try:
        events = await asyncio.wait_for(
            _collect_async(handler.on_message_send_stream(params, call_context)),
            timeout=_STREAM_TEST_TIMEOUT,
        )
    finally:
        await subscribers.close(immediate=True)

    assert events == [current_update]
    assert active_task._reference_count == 0
    assert not active_task.direct_message_lock.locked()


@pytest.mark.asyncio
async def test_active_pipeline_reentry_delivers_events_after_sdk_lifecycle_replacement() -> None:
    from iac_code.a2a import pipeline_executor as pipeline_executor_module

    call_context = ServerCallContext()
    store = A2ATaskStore()
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    await store.save(task, call_context)
    record = await store.get_or_create_task(task_id=task.id, context_id=task.context_id)
    record.active_task = asyncio.current_task()
    update = TaskStatusUpdateEvent(
        task_id=task.id,
        context_id=task.context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    subscribers = EventQueueSource(create_default_sink=False)
    stale_queue = FakeEventQueue()

    class ForwardingAgentQueue:
        async def enqueue_event(self, event) -> None:
            await subscribers.enqueue_event((event, task))

        async def test_only_join_incoming_queue(self) -> None:
            await subscribers.test_only_join_incoming_queue()

    class ActiveTask:
        def __init__(self) -> None:
            self.task_id = task.id
            self.direct_message_lock = asyncio.Lock()
            self._lock = asyncio.Lock()
            self._is_finished = asyncio.Event()
            self._reference_count = 0
            self._event_queue_agent = ForwardingAgentQueue()
            self._event_queue_subscribers = subscribers

        async def _maybe_cleanup(self) -> None:
            return None

    active_task = ActiveTask()

    class ActiveTaskRegistry:
        async def get(self, _task_id):
            return active_task

    class RequestContextBuilder:
        async def build(self, **_kwargs):
            return SimpleNamespace()

    runtime = pipeline_executor_module.A2APipelineRuntime(
        agent_runtime=SimpleNamespace(),
        publisher=SimpleNamespace(event_queue=stale_queue),
    )

    class AgentExecutor:
        async def execute(self, request_context, event_queue) -> None:
            gate = DirectPipelineRouteGateCarrier.read(request_context)
            assert gate is not None
            assert await pipeline_executor_module._register_active_interrupt(
                runtime,
                event_queue=event_queue,
                direct_route_gate=gate,
            )
            try:
                await runtime.publisher.event_queue.enqueue_event(update)
            finally:
                await pipeline_executor_module._settle_active_interrupt_safely(runtime)

    async def hydrate(_params) -> None:
        return None

    async def reconcile(_params, _context) -> None:
        return None

    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler.agent_executor = AgentExecutor()
    handler._request_context_builder = RequestContextBuilder()
    handler._active_task_registry = ActiveTaskRegistry()
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None
    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    message = Message(
        message_id="message-1",
        task_id=task.id,
        context_id=task.context_id,
        role=Role.ROLE_USER,
        parts=[Part(text='{"selected_candidate_index": 0}')],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline"}}, message.metadata)

    try:
        events = await asyncio.wait_for(
            _collect_async(
                handler.on_message_send_stream(
                    SimpleNamespace(message=message, configuration=None),
                    call_context,
                )
            ),
            timeout=_STREAM_TEST_TIMEOUT,
        )
    finally:
        await subscribers.close(immediate=True)

    assert events == [update]
    assert stale_queue.events == []
    assert active_task._reference_count == 0
    assert not active_task.direct_message_lock.locked()


@pytest.mark.asyncio
async def test_terminal_winning_direct_route_recovers_same_request_without_old_terminal() -> None:
    def status(state: TaskState.ValueType, seconds: int) -> TaskStatus:
        value = TaskStatus(state=state)
        value.timestamp.seconds = seconds
        return value

    class RoutingExecutor:
        def __init__(self, owner_release: asyncio.Event) -> None:
            self.calls = 0
            self.waited = False
            self._owner_release = owner_release

        async def execute(self, request_context, event_queue) -> None:
            self.calls += 1
            gate = DirectPipelineRouteGateCarrier.read(request_context)
            if gate is not None:
                gate.require_recovery()
                await event_queue.enqueue_event(
                    TaskStatusUpdateEvent(
                        task_id="task-1",
                        context_id="ctx-1",
                        status=status(TaskState.TASK_STATE_CANCELED, 150),
                    )
                )
                return
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="ctx-1",
                    status=status(TaskState.TASK_STATE_WORKING, 300),
                )
            )

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

        async def wait_until_recoverable_pipeline_input(self, *, context_id: str, task_id: str) -> None:
            assert (context_id, task_id) == ("ctx-1", "task-1")
            self.waited = True
            self._owner_release.set()

    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_INPUT_REQUIRED, 200)),
        call_context,
    )
    owner_release = asyncio.Event()
    owner = asyncio.create_task(owner_release.wait())
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = owner
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_WORKING, 250)),
        call_context,
    )
    executor = RoutingExecutor(owner_release)
    handler = IacCodeRequestHandler(
        agent_executor=executor,
        task_store=store,
        agent_card=SimpleNamespace(capabilities=SimpleNamespace(streaming=True)),
    )
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None

    async def hydrate(_params) -> None:
        return None

    reconcile_calls = 0

    async def reconcile(_params, _context) -> str | None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        if reconcile_calls == 1:
            return None
        owner_release.set()
        await owner
        record.active_task = None
        await store.save(
            Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_INPUT_REQUIRED, 260)),
            call_context,
        )
        return "recovery-1"

    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    old_lifecycle = await handler._active_task_registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    message = Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text="select candidate 0")],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline"}}, message.metadata)

    try:
        events = await asyncio.wait_for(
            _collect_async(
                handler.on_message_send_stream(
                    SendMessageRequest(message=message),
                    call_context,
                )
            ),
            timeout=_STREAM_TEST_TIMEOUT,
        )
        replacement = await handler._active_task_registry.get("task-1")
    finally:
        owner_release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await handler._active_task_registry.retire_for_recovery("task-1")

    assert [event.status.state for event in events] == [TaskState.TASK_STATE_WORKING]
    assert executor.calls == 2
    assert executor.waited is True
    assert replacement is not None and replacement is not old_lifecycle
    assert old_lifecycle._is_finished.is_set()


@pytest.mark.asyncio
async def test_cancelled_direct_stream_finishes_recovery_for_the_same_pipeline_request() -> None:
    def status(state: TaskState.ValueType, seconds: int) -> TaskStatus:
        value = TaskStatus(state=state)
        value.timestamp.seconds = seconds
        return value

    direct_started = asyncio.Event()
    release_direct = asyncio.Event()
    recovered = asyncio.Event()
    owner_release = asyncio.Event()
    outer_cleanup_started = asyncio.Event()
    outer_cleanup_finished = asyncio.Event()
    detached_admission_staged = asyncio.Event()
    admission_released = asyncio.Event()

    class RoutingExecutor:
        async def execute(self, request_context, event_queue) -> None:
            gate = DirectPipelineRouteGateCarrier.read(request_context)
            if gate is not None:
                direct_started.set()
                await release_direct.wait()
                gate.require_recovery()
                return
            recovered.set()
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="ctx-1",
                    status=status(TaskState.TASK_STATE_WORKING, 300),
                )
            )

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

        async def wait_until_recoverable_pipeline_input(self, *, context_id: str, task_id: str) -> None:
            assert (context_id, task_id) == ("ctx-1", "task-1")
            owner_release.set()

    call_context = ServerCallContext(
        state={"ordinary": {"value": 1}},
        tenant="tenant-1",
        requested_extensions={"urn:test:extension"},
    )
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_INPUT_REQUIRED, 200)),
        call_context,
    )
    owner = asyncio.create_task(owner_release.wait())
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.active_task = owner
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_WORKING, 250)),
        call_context,
    )
    handler = IacCodeRequestHandler(
        agent_executor=RoutingExecutor(),
        task_store=store,
        agent_card=SimpleNamespace(capabilities=SimpleNamespace(streaming=True)),
    )
    handler._validate_extensions = lambda _context: None
    handler._validate_pipeline_message_request = lambda _params: None

    async def hydrate(_params) -> None:
        return None

    reconcile_calls = 0

    async def reconcile(_params, _context) -> str | None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        if reconcile_calls == 1:
            return None
        owner_release.set()
        await owner
        record.active_task = None
        await store.save(
            Task(id="task-1", context_id="ctx-1", status=status(TaskState.TASK_STATE_INPUT_REQUIRED, 260)),
            call_context,
        )
        return "recovery-1"

    handler._hydrate_recoverable_pipeline_task_id = hydrate
    handler._reconcile_recoverable_pipeline_task = reconcile
    release_contexts: list[object] = []
    release_recovery = handler._release_untransferred_recovery
    staged_contexts: list[ServerCallContext] = []
    stage_recovery = handler._stage_recoverable_input_admission
    acknowledged: list[tuple[object, str, bool]] = []
    acknowledge_enqueue = handler._acknowledge_recoverable_input_enqueue
    setup_active_task = handler._setup_active_task
    released_admissions: list[str] = []

    def track_stage(stage_context, admission) -> None:
        stage_recovery(stage_context, admission)
        if admission == "recovery-1":
            staged_contexts.append(stage_context)
            detached_admission_staged.set()

    def track_acknowledge(ack_context, admission) -> bool:
        result = acknowledge_enqueue(ack_context, admission)
        acknowledged.append((ack_context, admission, result))
        return result

    async def synchronize_setup(setup_params, setup_context):
        await outer_cleanup_finished.wait()
        return await setup_active_task(setup_params, setup_context)

    async def track_admission_release(admission: str | None) -> None:
        if admission is not None:
            released_admissions.append(admission)
            admission_released.set()

    async def track_release(release_params, release_context) -> None:
        release_contexts.append(release_context)
        if release_context is call_context:
            outer_cleanup_started.set()
            await detached_admission_staged.wait()
            await release_recovery(release_params, release_context)
            outer_cleanup_finished.set()
            return
        await release_recovery(release_params, release_context)

    handler._stage_recoverable_input_admission = track_stage
    handler._acknowledge_recoverable_input_enqueue = track_acknowledge
    handler._setup_active_task = synchronize_setup
    handler._release_untransferred_recovery = track_release
    store.release_recoverable_input_admission = track_admission_release
    old_lifecycle = await handler._active_task_registry.get_or_create(
        "task-1",
        call_context=call_context,
        context_id="ctx-1",
        create_task_if_missing=True,
    )
    message = Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text="select candidate 0")],
    )
    ParseDict({"iac_code": {"run_mode": "pipeline"}}, message.metadata)
    stream_task = asyncio.create_task(
        _collect_async(handler.on_message_send_stream(SendMessageRequest(message=message), call_context))
    )

    try:
        await asyncio.wait_for(direct_started.wait(), timeout=_STREAM_TEST_TIMEOUT)
        stream_task.cancel()
        await asyncio.wait_for(outer_cleanup_started.wait(), timeout=_STREAM_TEST_TIMEOUT)
        release_direct.set()
        await asyncio.wait_for(detached_admission_staged.wait(), timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.wait_for(outer_cleanup_finished.wait(), timeout=_STREAM_TEST_TIMEOUT)
        with pytest.raises(asyncio.CancelledError):
            await stream_task
        await asyncio.wait_for(recovered.wait(), timeout=_STREAM_TEST_TIMEOUT)
        replacement = await handler._active_task_registry.get("task-1")
        assert replacement is not None and replacement is not old_lifecycle
    finally:
        release_direct.set()
        owner_release.set()
        await asyncio.gather(owner, return_exceptions=True)
        detached = tuple(handler._detached_message_producers)
        if detached:
            await asyncio.wait_for(asyncio.gather(*detached), timeout=_STREAM_TEST_TIMEOUT)
        await handler._active_task_registry.retire_for_recovery("task-1")
        await asyncio.wait_for(admission_released.wait(), timeout=_STREAM_TEST_TIMEOUT)

    assert call_context in release_contexts
    assert len(staged_contexts) == 1
    detached_context = staged_contexts[0]
    assert detached_context is not call_context
    assert detached_context.state is not call_context.state
    assert detached_context.state["ordinary"] == {"value": 1}
    assert detached_context.user is call_context.user
    assert detached_context.tenant == "tenant-1"
    assert detached_context.requested_extensions == {"urn:test:extension"}
    assert detached_context.requested_extensions is not call_context.requested_extensions
    assert acknowledged == [(detached_context, "recovery-1", True)]
    assert released_admissions == ["recovery-1"]


@pytest.mark.asyncio
async def test_active_message_stream_serializes_direct_requests() -> None:
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    updates = [
        TaskStatusUpdateEvent(
            task_id=task.id,
            context_id=task.context_id,
            status=TaskStatus(state=state),
        )
        for state in (TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_INPUT_REQUIRED)
    ]
    started = [asyncio.Event(), asyncio.Event()]
    releases = [asyncio.Event(), asyncio.Event()]
    subscribers = EventQueueSource(create_default_sink=False)

    class ForwardingAgentQueue:
        async def enqueue_event(self, event) -> None:
            await subscribers.enqueue_event((event, task))

        async def test_only_join_incoming_queue(self) -> None:
            await subscribers.test_only_join_incoming_queue()

    class ActiveTask:
        def __init__(self) -> None:
            self.task_id = task.id
            self.direct_message_lock = asyncio.Lock()
            self._lock = asyncio.Lock()
            self._is_finished = asyncio.Event()
            self._reference_count = 0
            self._event_queue_agent = ForwardingAgentQueue()
            self._event_queue_subscribers = subscribers

        async def _maybe_cleanup(self) -> None:
            return None

    class RequestContextBuilder:
        async def build(self, *, params, **_kwargs):
            return SimpleNamespace(index=int(params.message.message_id[-1]))

    class AgentExecutor:
        def __init__(self) -> None:
            self.running = 0
            self.max_running = 0

        async def execute(self, request_context, event_queue) -> None:
            index = request_context.index
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            started[index].set()
            try:
                await releases[index].wait()
                await event_queue.enqueue_event(updates[index])
            finally:
                self.running -= 1

    executor = AgentExecutor()
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.agent_executor = executor
    handler._request_context_builder = RequestContextBuilder()
    active_task = ActiveTask()
    params = [
        SimpleNamespace(
            message=SimpleNamespace(message_id=f"message-{index}", context_id=task.context_id),
            configuration=None,
        )
        for index in range(2)
    ]
    first_consumer = asyncio.create_task(
        _collect_async(handler._on_active_message_send_stream(params[0], object(), task=task, active_task=active_task))
    )
    consumers = [first_consumer]

    try:
        await asyncio.wait_for(started[0].wait(), timeout=_STREAM_TEST_TIMEOUT)
        second_consumer = asyncio.create_task(
            _collect_async(
                handler._on_active_message_send_stream(params[1], object(), task=task, active_task=active_task)
            )
        )
        consumers.append(second_consumer)
        await asyncio.sleep(0)
        assert not started[1].is_set()
        releases[0].set()
        await asyncio.wait_for(started[1].wait(), timeout=_STREAM_TEST_TIMEOUT)
        releases[1].set()
        events = await asyncio.wait_for(asyncio.gather(*consumers), timeout=_STREAM_TEST_TIMEOUT)
    finally:
        for release in releases:
            release.set()
        for consumer in consumers:
            if not consumer.done():
                consumer.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        await subscribers.close(immediate=True)

    assert events == [[updates[0]], [updates[1]]]
    assert executor.max_running == 1
    assert active_task._reference_count == 0
    assert not active_task.direct_message_lock.locked()


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
        async def resolve_sideband_permission(self, _response, *, metadata=None):
            assert metadata is message
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
            self.interrupts.append(normalize_pipeline_user_input(message).display_text)
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
        await asyncio.wait_for(first_task, timeout=15)
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
            self.resume_prompts.append(normalize_pipeline_user_input(prompt).display_text)
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
        timeout=_STREAM_TEST_TIMEOUT,
    )

    assert [event.status.state for event in events] == [
        TaskState.TASK_STATE_WORKING,
        TaskState.TASK_STATE_INPUT_REQUIRED,
    ]


@pytest.mark.asyncio
async def test_subscribe_to_task_stops_after_terminal_status(monkeypatch) -> None:
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
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
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

    assert len(events) == 1
    assert events[0].status.state == TaskState.TASK_STATE_COMPLETED


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
async def test_active_message_stream_cancellation_detaches_producer() -> None:
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
            self.direct_message_lock = asyncio.Lock()
            self._lock = asyncio.Lock()
            self._is_finished = asyncio.Event()
            self._reference_count = 0
            self._event_queue_agent = SimpleNamespace(enqueue_event=self._enqueue_event)
            self._event_queue_subscribers = FakeSubscribers(FakeTappedQueue())

        @staticmethod
        async def _enqueue_event(_event) -> None:
            return None

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
    assert not producer_cancelled.is_set()
    cleanup_tasks = tuple(handler._detached_message_producers)
    assert len(cleanup_tasks) == 1
    cleanup_tasks[0].cancel()
    await asyncio.wait_for(producer_cancelled.wait(), timeout=_STREAM_TEST_TIMEOUT)
    await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    assert active_task._reference_count == 0
    assert not active_task.direct_message_lock.locked()


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


class _AcceptedCleanupBarrierRegistry(RequestScopedActiveTaskRegistry):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.release_cleanup = asyncio.Event()
        self.replacement_reserved = asyncio.Event()

    async def _reserve_recovery_replacement(self, task_id):
        active_task = await super()._reserve_recovery_replacement(task_id)
        self.replacement_reserved.set()
        return active_task

    async def _remove_task_if_same(self, active_task) -> None:
        await self.release_cleanup.wait()
        await super()._remove_task_if_same(active_task)


class _AcceptedTerminalSettlementExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release_terminal = asyncio.Event()
        self.terminal_enqueued = asyncio.Event()
        self.release_execute = asyncio.Event()
        self.executed_message_ids: list[str] = []

    async def execute(self, request_context, event_queue) -> None:
        self.executed_message_ids.append(request_context.message.message_id)
        self.started.set()
        await self.release_terminal.wait()
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id="task-1",
                context_id="ctx-1",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        )
        self.terminal_enqueued.set()
        await self.release_execute.wait()

    async def cancel(self, _request_context, _event_queue) -> None:
        return None


class _AcceptedProjectionJoinBarrier:
    def __init__(self, source) -> None:
        self._join = source.test_only_join_incoming_queue
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def join(self) -> None:
        self.entered.set()
        await self.release.wait()
        await self._join()


@pytest.mark.asyncio
async def test_recovery_drain_waits_for_real_projection_after_normal_consumer_completion(monkeypatch) -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED)),
        call_context,
    )
    executor = _AcceptedTerminalSettlementExecutor()
    registry = _AcceptedCleanupBarrierRegistry(agent_executor=executor, task_store=store)
    old = await registry.get_or_create(
        "task-1", call_context=call_context, context_id="ctx-1", create_task_if_missing=True
    )
    request = RequestContext(
        call_context=call_context,
        task_id="task-1",
        context_id="ctx-1",
        request=SendMessageRequest(
            message=Message(message_id="old-accepted-1", role=Role.ROLE_USER, parts=[Part(text="first")])
        ),
    )
    accepted_id = await old.enqueue_request(request)
    await asyncio.wait_for(executor.started.wait(), timeout=_STREAM_TEST_TIMEOUT)
    executor.release_terminal.set()
    await asyncio.wait_for(old._is_finished.wait(), timeout=_STREAM_TEST_TIMEOUT)
    assert old._request_lock.locked()
    barrier = _AcceptedProjectionJoinBarrier(old._event_queue_agent)
    monkeypatch.setattr(old._event_queue_agent, "test_only_join_incoming_queue", barrier.join)
    admissions: list[str] = []

    async def acquire_admission() -> str | None:
        admissions.append("checked")
        assert old._consumer_task is not None and old._consumer_task.done()
        assert not old._request_lock.locked()
        return None

    recovery = asyncio.create_task(
        registry.reconcile_and_replace_for_recovery(
            "task-1", call_context=call_context, context_id="ctx-1", acquire_admission=acquire_admission
        )
    )
    try:
        # The old executor return causes the real SDK producer to enqueue its
        # exact _RequestCompleted before task_done; no receipt is synthesized.
        await asyncio.wait_for(registry.replacement_reserved.wait(), timeout=_STREAM_TEST_TIMEOUT)
        assert old._recovery_replacement_pending
        executor.release_execute.set()
        await asyncio.wait_for(barrier.entered.wait(), timeout=_STREAM_TEST_TIMEOUT)
        assert old._consumer_task is not None
        await asyncio.wait_for(asyncio.shield(old._consumer_task), timeout=_STREAM_TEST_TIMEOUT)
        assert not old._request_lock.locked()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not recovery.done()
        assert admissions == []
        barrier.release.set()
        assert await asyncio.wait_for(recovery, timeout=_STREAM_TEST_TIMEOUT) is None
        assert admissions == ["checked"]
        assert accepted_id is not None
        assert executor.executed_message_ids == ["old-accepted-1"]
    finally:
        barrier.release.set()
        executor.release_terminal.set()
        executor.release_execute.set()
        if not recovery.done():
            recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
        registry.release_cleanup.set()
        await registry.retire_for_recovery("task-1")
        await asyncio.gather(*tuple(registry._cleanup_tasks), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_cleanup", [True, False])
@pytest.mark.parametrize("task_projection", ["terminal", "missing", "working"])
async def test_recovery_drain_rejects_a_second_accepted_request_dropped_by_terminal_shutdown(
    hold_cleanup, task_projection
) -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED)),
        call_context,
    )
    executor = _AcceptedTerminalSettlementExecutor()
    registry = _AcceptedCleanupBarrierRegistry(agent_executor=executor, task_store=store)
    if not hold_cleanup:
        registry.release_cleanup.set()
    old = await registry.get_or_create(
        "task-1", call_context=call_context, context_id="ctx-1", create_task_if_missing=True
    )
    first = await old.enqueue_request(
        RequestContext(
            call_context=call_context,
            task_id="task-1",
            context_id="ctx-1",
            request=SendMessageRequest(
                message=Message(message_id="old-accepted-1", role=Role.ROLE_USER, parts=[Part(text="first")])
            ),
        )
    )
    await asyncio.wait_for(executor.started.wait(), timeout=_STREAM_TEST_TIMEOUT)
    second = await old.enqueue_request(
        RequestContext(
            call_context=call_context,
            task_id="task-1",
            context_id="ctx-1",
            request=SendMessageRequest(
                message=Message(message_id="old-accepted-2", role=Role.ROLE_USER, parts=[Part(text="second")])
            ),
        )
    )
    admissions: list[str] = []

    async def acquire_admission() -> str | None:
        admissions.append("checked")
        return None

    try:
        assert first != second
        assert old._request_queue.qsize() == 1
        executor.release_terminal.set()
        await asyncio.wait_for(old._is_finished.wait(), timeout=_STREAM_TEST_TIMEOUT)
        assert old._request_queue.qsize() == 0
        executor.release_execute.set()
        assert old._consumer_task is not None and old._producer_task is not None
        await asyncio.wait_for(asyncio.shield(old._consumer_task), timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.wait_for(asyncio.shield(old._producer_task), timeout=_STREAM_TEST_TIMEOUT)
        assert not old._request_lock.locked()
        assert not old.has_unfinished_requests()
        if not hold_cleanup:
            await asyncio.gather(*tuple(registry._cleanup_tasks), return_exceptions=True)
        assert registry._active_tasks["task-1"] is old
        assert old._pending_accepted_requests == {second}
        assert executor.executed_message_ids == ["old-accepted-1"]
        if task_projection != "terminal":
            await store.delete("task-1", call_context)
            if task_projection == "working":
                await store.save(
                    Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING)),
                    call_context,
                )
        with pytest.raises(InvalidParamsError, match="ended before the accepted request settled"):
            await registry.reconcile_and_replace_for_recovery(
                "task-1", call_context=call_context, context_id="ctx-1", acquire_admission=acquire_admission
            )
        assert admissions == []
        assert registry._active_tasks["task-1"] is old
        with pytest.raises(InvalidParamsError, match="ended before the accepted request settled"):
            await registry.get_or_create("task-1", call_context=call_context, context_id="ctx-1")
        assert executor.executed_message_ids == ["old-accepted-1"]
    finally:
        executor.release_terminal.set()
        executor.release_execute.set()
        registry.release_cleanup.set()
        await registry.retire_for_recovery("task-1")
        await asyncio.gather(*tuple(registry._cleanup_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_recovery_drain_fails_closed_when_real_consumer_is_cancelled_before_completion() -> None:
    save_started = asyncio.Event()
    release_save = asyncio.Event()

    class WaitingTaskStore(A2ATaskStore):
        async def save(self, task, context=None) -> None:
            if task.status.state == TaskState.TASK_STATE_WORKING:
                save_started.set()
                await release_save.wait()
            await super().save(task, context)

    class WorkingExecutor:
        async def execute(self, _request_context, event_queue) -> None:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING)
                )
            )

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    call_context = ServerCallContext()
    store = WaitingTaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED)),
        call_context,
    )
    registry = _AcceptedCleanupBarrierRegistry(agent_executor=WorkingExecutor(), task_store=store)
    old = await registry.get_or_create(
        "task-1", call_context=call_context, context_id="ctx-1", create_task_if_missing=True
    )
    await old.enqueue_request(RequestContext(call_context=call_context, task_id="task-1", context_id="ctx-1"))
    await asyncio.wait_for(save_started.wait(), timeout=_STREAM_TEST_TIMEOUT)
    assert old._consumer_task is not None
    old._consumer_task.cancel()
    await asyncio.gather(old._consumer_task, return_exceptions=True)
    release_save.set()
    admissions: list[str] = []

    async def acquire_admission() -> str | None:
        admissions.append("checked")
        return None

    try:
        assert old._request_lock.locked()
        with pytest.raises(InvalidParamsError, match="ended before the accepted request settled"):
            await asyncio.wait_for(
                registry.reconcile_and_replace_for_recovery(
                    "task-1", call_context=call_context, context_id="ctx-1", acquire_admission=acquire_admission
                ),
                timeout=_STREAM_TEST_TIMEOUT,
            )
        assert admissions == []
    finally:
        release_save.set()
        registry.release_cleanup.set()
        await registry.retire_for_recovery("task-1")
        await asyncio.gather(*tuple(registry._cleanup_tasks), return_exceptions=True)


class _AcceptedInputRequiredExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.executed_message_ids: list[str] = []

    async def execute(self, request_context, event_queue) -> None:
        self.executed_message_ids.append(request_context.message.message_id)
        if len(self.executed_message_ids) == 1:
            self.started.set()
            await self.release_first.wait()
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id="task-1",
                context_id="ctx-1",
                status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
            )
        )

    async def cancel(self, _request_context, _event_queue) -> None:
        return None


@pytest.mark.asyncio
async def test_recovery_drain_settles_two_real_input_required_request_ids() -> None:
    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED)),
        call_context,
    )
    executor = _AcceptedInputRequiredExecutor()
    registry = RequestScopedActiveTaskRegistry(agent_executor=executor, task_store=store)
    old = await registry.get_or_create(
        "task-1", call_context=call_context, context_id="ctx-1", create_task_if_missing=True
    )
    accepted_ids = []
    for message_id in ("accepted-first", "accepted-second"):
        accepted_ids.append(
            await old.enqueue_request(
                RequestContext(
                    call_context=call_context,
                    task_id="task-1",
                    context_id="ctx-1",
                    request=SendMessageRequest(
                        message=Message(message_id=message_id, role=Role.ROLE_USER, parts=[Part(text="input")])
                    ),
                )
            )
        )
        if message_id == "accepted-first":
            await asyncio.wait_for(executor.started.wait(), timeout=_STREAM_TEST_TIMEOUT)
    try:
        assert accepted_ids[0] != accepted_ids[1]
        assert old._pending_accepted_requests == set(accepted_ids)
        executor.release_first.set()
        await asyncio.wait_for(old.wait_for_accepted_requests(), timeout=_STREAM_TEST_TIMEOUT)
        assert executor.executed_message_ids == ["accepted-first", "accepted-second"]
        assert old._pending_accepted_requests == set()
        assert not old.has_unsettled_requests()
        assert not old._consumer_task.done()
        assert (await store.get("task-1", call_context)).status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    finally:
        executor.release_first.set()
        await registry.retire_for_recovery("task-1")


@pytest.mark.asyncio
async def test_recovery_drain_preserves_sdk_swallowed_consumer_failure_after_cleanup() -> None:
    class FailingExecutor:
        async def execute(self, _request_context, _event_queue) -> None:
            raise RuntimeError("accepted request failed")

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    call_context = ServerCallContext()
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED)),
        call_context,
    )
    registry = RequestScopedActiveTaskRegistry(agent_executor=FailingExecutor(), task_store=store)
    old = await registry.get_or_create(
        "task-1", call_context=call_context, context_id="ctx-1", create_task_if_missing=True
    )
    accepted_id = await old.enqueue_request(
        RequestContext(call_context=call_context, task_id="task-1", context_id="ctx-1")
    )
    admissions: list[str] = []

    async def acquire_admission() -> str | None:
        admissions.append("checked")
        return None

    try:
        await asyncio.wait_for(asyncio.shield(old._consumer_task), timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.wait_for(asyncio.shield(old._producer_task), timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.gather(*tuple(registry._cleanup_tasks), return_exceptions=True)
        assert old._consumer_task.exception() is None
        assert old._accepted_consumer_failed
        assert old._pending_accepted_requests == {accepted_id}
        assert registry._active_tasks["task-1"] is old
        assert (await store.get("task-1", call_context)).status.state == TaskState.TASK_STATE_FAILED
        with pytest.raises(InvalidParamsError, match="ended before the accepted request settled"):
            await registry.reconcile_and_replace_for_recovery(
                "task-1", call_context=call_context, context_id="ctx-1", acquire_admission=acquire_admission
            )
        assert admissions == []
        assert registry._active_tasks["task-1"] is old
        with pytest.raises(InvalidParamsError, match="ended before the accepted request settled"):
            await registry.get_or_create("task-1", call_context=call_context, context_id="ctx-1")
    finally:
        await registry.retire_for_recovery("task-1")


@pytest.mark.asyncio
async def test_recovery_enqueue_shutdown_rolls_back_unaccepted_uuid() -> None:
    active_task = RequestScopedActiveTask(
        agent_executor=SimpleNamespace(), task_id="task-1", task_manager=SimpleNamespace(), recovery_admission="r-1"
    )
    request = RequestContext(call_context=ServerCallContext(), task_id="task-1", context_id="ctx-1")
    RecoverableInputAdmissionCarrier.attach(request, "r-1")
    active_task._request_queue.shutdown(immediate=True)
    try:
        with pytest.raises(QueueShutDown):
            await active_task.enqueue_request(request)
        assert active_task._pending_accepted_requests == set()
        assert active_task._recovery_admission == "r-1"
        assert not active_task.has_unsettled_requests()
    finally:
        await active_task.retire_for_recovery()


@pytest.mark.asyncio
@pytest.mark.parametrize("task_projection", ["missing", "working"])
async def test_recovery_drain_rechecks_consumer_after_async_task_lookup(task_projection) -> None:
    class LookupBarrierTaskStore(A2ATaskStore):
        def __init__(self) -> None:
            super().__init__()
            self.pause_next_get = False
            self.lookup_entered = asyncio.Event()
            self.release_lookup = asyncio.Event()

        async def get(self, task_id, context=None):
            if self.pause_next_get:
                self.pause_next_get = False
                self.lookup_entered.set()
                await self.release_lookup.wait()
            return await super().get(task_id, context)

    call_context = ServerCallContext()
    store = LookupBarrierTaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED)),
        call_context,
    )
    executor = _AcceptedTerminalSettlementExecutor()
    registry = RequestScopedActiveTaskRegistry(agent_executor=executor, task_store=store)
    old = await registry.get_or_create(
        "task-1", call_context=call_context, context_id="ctx-1", create_task_if_missing=True
    )
    for message_id in ("accepted-first", "accepted-second"):
        await old.enqueue_request(
            RequestContext(
                call_context=call_context,
                task_id="task-1",
                context_id="ctx-1",
                request=SendMessageRequest(
                    message=Message(message_id=message_id, role=Role.ROLE_USER, parts=[Part(text="input")])
                ),
            )
        )
        if message_id == "accepted-first":
            await asyncio.wait_for(executor.started.wait(), timeout=_STREAM_TEST_TIMEOUT)
    admissions: list[str] = []

    async def acquire_admission() -> str | None:
        admissions.append("checked")
        return None

    store.pause_next_get = True
    recovery = asyncio.create_task(
        registry.reconcile_and_replace_for_recovery(
            "task-1", call_context=call_context, context_id="ctx-1", acquire_admission=acquire_admission
        )
    )
    try:
        await asyncio.wait_for(store.lookup_entered.wait(), timeout=_STREAM_TEST_TIMEOUT)
        assert old._recovery_replacement_pending
        assert not old._consumer_task.done()
        executor.release_terminal.set()
        await asyncio.wait_for(old._is_finished.wait(), timeout=_STREAM_TEST_TIMEOUT)
        executor.release_execute.set()
        await asyncio.wait_for(asyncio.shield(old._consumer_task), timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.wait_for(asyncio.shield(old._producer_task), timeout=_STREAM_TEST_TIMEOUT)
        assert len(old._pending_accepted_requests) == 1
        assert executor.executed_message_ids == ["accepted-first"]
        await store.delete("task-1", call_context)
        if task_projection == "working":
            await store.save(
                Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING)),
                call_context,
            )
        store.release_lookup.set()
        with pytest.raises(InvalidParamsError, match="ended before the accepted request settled"):
            await asyncio.wait_for(recovery, timeout=_STREAM_TEST_TIMEOUT)
        assert admissions == []
        assert registry._active_tasks["task-1"] is old
    finally:
        store.release_lookup.set()
        executor.release_terminal.set()
        executor.release_execute.set()
        if not recovery.done():
            recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
        await registry.retire_for_recovery("task-1")


@pytest.mark.asyncio
async def test_recovery_drain_rechecks_failure_during_async_lookup_before_consumer_exit() -> None:
    class FailedProjectionBarrierStore(A2ATaskStore):
        def __init__(self) -> None:
            super().__init__()
            self.pause_next_get = False
            self.lookup_entered = asyncio.Event()
            self.release_lookup = asyncio.Event()
            self.failed_projection_entered = asyncio.Event()
            self.release_failed_projection = asyncio.Event()

        async def get(self, task_id, context=None):
            if self.pause_next_get:
                self.pause_next_get = False
                self.lookup_entered.set()
                await self.release_lookup.wait()
            return await super().get(task_id, context)

        async def save(self, task, context=None) -> None:
            if task.status.state == TaskState.TASK_STATE_FAILED:
                self.failed_projection_entered.set()
                await self.release_failed_projection.wait()
            await super().save(task, context)

    class FailingExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release_failure = asyncio.Event()

        async def execute(self, _request_context, _event_queue) -> None:
            self.started.set()
            await self.release_failure.wait()
            raise RuntimeError("accepted request failed")

        async def cancel(self, _request_context, _event_queue) -> None:
            return None

    call_context = ServerCallContext()
    store = FailedProjectionBarrierStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED)),
        call_context,
    )
    executor = FailingExecutor()
    registry = RequestScopedActiveTaskRegistry(agent_executor=executor, task_store=store)
    old = await registry.get_or_create(
        "task-1", call_context=call_context, context_id="ctx-1", create_task_if_missing=True
    )
    await old.enqueue_request(RequestContext(call_context=call_context, task_id="task-1", context_id="ctx-1"))
    await asyncio.wait_for(executor.started.wait(), timeout=_STREAM_TEST_TIMEOUT)
    admissions: list[str] = []

    async def acquire_admission() -> str | None:
        admissions.append("checked")
        return None

    store.pause_next_get = True
    recovery = asyncio.create_task(
        registry.reconcile_and_replace_for_recovery(
            "task-1", call_context=call_context, context_id="ctx-1", acquire_admission=acquire_admission
        )
    )
    try:
        await asyncio.wait_for(store.lookup_entered.wait(), timeout=_STREAM_TEST_TIMEOUT)
        executor.release_failure.set()
        await asyncio.wait_for(store.failed_projection_entered.wait(), timeout=_STREAM_TEST_TIMEOUT)
        assert old._accepted_consumer_failed
        assert not old._consumer_task.done()
        store.release_lookup.set()
        with pytest.raises(InvalidParamsError, match="ended before the accepted request settled"):
            await asyncio.wait_for(recovery, timeout=_STREAM_TEST_TIMEOUT)
        assert not old._consumer_task.done()
        assert admissions == []
        assert registry._active_tasks["task-1"] is old
    finally:
        executor.release_failure.set()
        store.release_lookup.set()
        store.release_failed_projection.set()
        if not recovery.done():
            recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
        await registry.retire_for_recovery("task-1")


class _OrdinaryNormalHandoffSdkFixture:
    """Keep the actual SDK INPUT_REQUIRED task after a durable natural release."""

    task_id = "task-normal-handoff"
    context_id = "ctx-normal-handoff"

    def __init__(self, tmp_path, *, backup_service=None) -> None:
        self.cwd = tmp_path / "workspace"
        self.cwd.mkdir()
        self.call_context = ServerCallContext()
        self.store = A2ATaskStore(persistence=A2APersistenceStore(tmp_path / "a2a"))
        self.service = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=backup_service)
        self.loop = FakeAgentLoop([TextDeltaEvent(text="normal followup accepted")])
        self.factory_session_ids: list[str] = []

    def create_runtime(self, options):
        self.factory_session_ids.append(options.session_id)
        return FakeRuntime(agent_loop=self.loop, session_id=options.session_id)

    async def prepare(self, *, finalize=True, with_ack=True) -> None:
        ctx = await self.store.get_or_create_context(
            context_id=self.context_id,
            cwd=str(self.cwd),
            runtime_factory=lambda session_id: FakeRuntime(agent_loop=self.loop, session_id=session_id),
        )
        self.session_id = ctx.session_id
        SessionStorage().ensure_v2_session_dir_for_new_session(str(self.cwd), self.session_id)
        owner = self.store.owner_for_context(self.call_context)
        await self.store.get_or_create_task(task_id=self.task_id, context_id=self.context_id, owner=owner)
        await self.store.save(
            Task(
                id=self.task_id,
                context_id=self.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
            ),
            self.call_context,
        )
        self.control = await self.service.begin_execution(
            context_id=self.context_id,
            task_id=self.task_id,
            owner=owner,
            cwd=str(self.cwd),
            execution_mode="pipeline",
        )
        self.control.bind_session(self.session_id)
        await self.control.checkpoint()
        current = asyncio.current_task()
        assert current is not None
        self.completion_generation = await self.control.detach_task(
            current, execution_status="input-required", natural_completion=True
        )
        assert self.completion_generation is not None
        if finalize:
            await self.finish_natural_handoff()
            assert self.control.phase == "terminated"
            assert self.control.natural_handoff_admits_replacement()
        else:
            # Pipeline business has completed while its SDK wait projection
            # still remains INPUT_REQUIRED. The real executor's natural cleanup
            # therefore closes a completed business, not an absent ask checkpoint.
            record = await self.store.get_or_create_task(task_id=self.task_id, context_id=self.context_id, owner=owner)
            record.state = "completed"
            self.store.mirror_task(record)
            assert (await self.store.get_task_record(self.task_id)).state == "completed"
            assert (
                await self.store.get(self.task_id, self.call_context)
            ).status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        self.write_handoff(with_ack=with_ack)
        self.executor = IacCodeA2AExecutor(
            task_store=self.store, model="qwen3.6-plus", execution_control_service=self.service
        )
        self.store.set_execution_control_provider(
            self.service.snapshot_for_context,
            self.service.has_active_work,
            self.service.reserve_recoverable_input_continuation,
            self.service.release_recoverable_input_continuation,
        )
        self.handler = IacCodeRequestHandler(
            agent_executor=self.executor,
            task_store=self.store,
            agent_card=SimpleNamespace(capabilities=SimpleNamespace(streaming=True, extensions=[])),
        )

    async def finish_natural_handoff(self) -> None:
        await self.control.finalize_natural_completion(
            task_id=self.task_id, completion_generation=self.completion_generation
        )

    async def restore_sdk_wait_projection(self) -> None:
        """Model the SDK wait projection lagging the completed business record."""

        record = await self.store.get_or_create_task(
            task_id=self.task_id,
            context_id=self.context_id,
            owner=self.store.owner_for_context(self.call_context),
        )
        record.state = "input-required"
        await self.store.save(
            Task(
                id=self.task_id,
                context_id=self.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
            ),
            self.call_context,
        )
        record.state = "completed"
        self.store.mirror_task(record)
        assert (await self.store.get_task_record(self.task_id)).state == "completed"
        assert (
            await self.store.get(self.task_id, self.call_context)
        ).status.state == TaskState.TASK_STATE_INPUT_REQUIRED

    def write_handoff(self, *, with_ack=True) -> None:
        handoff = {
            "schemaVersion": "1.0",
            "eventId": "handoff-business-1",
            "sequence": 1,
            "createdAt": "2026-10-10T07:00:00Z",
            "eventType": "pipeline_handoff_ready",
            "scope": "pipeline",
            "pipelineRunId": self.context_id,
            "taskId": self.task_id,
            "contextId": self.context_id,
            "pipelineName": "selling",
            "status": "completed",
            "visibility": "committed",
            "data": {"action": "switch_to_normal", "targetMode": "normal", "summary": "[Pipeline Handoff Context]"},
        }
        ack = {
            **handoff,
            "eventId": "handoff-backup-ack-2",
            "sequence": 2,
            "eventType": "backup_committed",
            "data": {
                "committedEventId": handoff["eventId"],
                "committedSequence": handoff["sequence"],
                "committedEventType": handoff["eventType"],
            },
        }
        pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(self.cwd), session_id=self.session_id)
        self.handoff_events = (handoff, ack)
        events = self.handoff_events if with_ack else (handoff,)
        A2APipelineJournal(pipeline_dir).append_many(events, durable=True)
        A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events(events))

    def commit_handoff_ack(self) -> None:
        pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(self.cwd), session_id=self.session_id)
        A2APipelineJournal(pipeline_dir).append(self.handoff_events[1], durable=True)
        A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events(self.handoff_events))

    async def send(self):
        message = Message(
            message_id="ordinary-followup-1",
            task_id=self.task_id,
            context_id=self.context_id,
            role=Role.ROLE_USER,
            parts=[Part(text="Explain cleanup of the existing resources.")],
        )
        ParseDict({"iac_code": {"cwd": str(self.cwd), "run_mode": "pipeline"}}, message.metadata)
        return await _collect_async(
            self.handler.on_message_send_stream(SendMessageRequest(message=message), self.call_context)
        )

    async def close(self) -> None:
        if hasattr(self, "handler"):
            await self.handler._active_task_registry.retire_for_recovery(self.task_id)
        await self.service.close()


@pytest.mark.asyncio
async def test_public_sdk_ordinary_followup_replaces_a_proven_closed_pipeline_handoff(monkeypatch, tmp_path) -> None:
    fixture = _OrdinaryNormalHandoffSdkFixture(tmp_path)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fixture.create_runtime)
    try:
        await fixture.prepare()
        assert await fixture.executor._should_route_pipeline_handoff_to_normal(
            context_id=fixture.context_id, cwd=str(fixture.cwd)
        )
        events = await asyncio.wait_for(fixture.send(), timeout=_STREAM_TEST_TIMEOUT)
        active = await fixture.handler._active_task_registry.get(fixture.task_id)
        assert active is not None
        await asyncio.wait_for(active.wait_for_accepted_requests(), timeout=_STREAM_TEST_TIMEOUT)
        assert not active._accepted_consumer_failed
        assert active._pending_accepted_requests == set()
        assert fixture.loop.prompts == ["Explain cleanup of the existing resources."]
        assert fixture.factory_session_ids == []  # the original session runtime is reused
        replacement = fixture.service.get_for_context(fixture.context_id)
        assert replacement is not None and replacement is not fixture.control
        assert replacement.execution_mode == "normal"
        assert replacement.task_id == fixture.task_id
        assert (await fixture.store.get_context_record(fixture.context_id)).session_id == fixture.session_id
        assert events and all(
            event.task_id == fixture.task_id and event.context_id == fixture.context_id
            for event in events
            if isinstance(event, TaskStatusUpdateEvent)
        )
        assert any(
            isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
            for event in events
        )
    finally:
        await fixture.close()


class _OrdinaryAttachBarrier:
    def __init__(self, control) -> None:
        self._attach = control.attach_task
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.conflicts = 0

    async def attach(self, task, *, mark_working=True):
        from iac_code.a2a.execution_control import ExecutionControlConflictError

        self.entered.set()
        await self.release.wait()
        try:
            return await self._attach(task, mark_working=mark_working)
        except ExecutionControlConflictError:
            self.conflicts += 1
            raise


@pytest.mark.asyncio
async def test_public_sdk_ordinary_followup_rechecks_ack_after_real_attach_conflict(monkeypatch, tmp_path) -> None:
    fixture = _OrdinaryNormalHandoffSdkFixture(tmp_path)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fixture.create_runtime)
    sending = None
    barrier = None
    try:
        await fixture.prepare(finalize=False, with_ack=False)
        assert not await fixture.executor._should_route_pipeline_handoff_to_normal(
            context_id=fixture.context_id, cwd=str(fixture.cwd)
        )
        assert fixture.control.phase == "running"
        barrier = _OrdinaryAttachBarrier(fixture.control)
        monkeypatch.setattr(fixture.control, "attach_task", barrier.attach)
        sending = asyncio.create_task(fixture.send())
        await asyncio.wait_for(barrier.entered.wait(), timeout=_STREAM_TEST_TIMEOUT)
        # The SDK accepted the request while the original controller was live.
        # The gate delays only entry to the real attach; its guard is not mocked.
        active = await fixture.handler._active_task_registry.get(fixture.task_id)
        assert active is not None and len(active._pending_accepted_requests) == 1
        original_uuid = next(iter(active._pending_accepted_requests))
        await fixture.finish_natural_handoff()
        fixture.commit_handoff_ack()
        assert fixture.control.phase == "terminated"
        assert fixture.control.snapshot()["commitError"] is None
        assert fixture.control.natural_handoff_admits_replacement()
        barrier.release.set()
        await asyncio.wait_for(sending, timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.wait_for(active.wait_for_accepted_requests(), timeout=_STREAM_TEST_TIMEOUT)
        assert barrier.conflicts == 1
        assert original_uuid not in active._pending_accepted_requests
        assert not active._accepted_consumer_failed
        assert fixture.loop.prompts == ["Explain cleanup of the existing resources."]
        assert await fixture.handler._active_task_registry.get(fixture.task_id) is active
        replacement = fixture.service.get_for_context(fixture.context_id)
        assert replacement is not None and replacement is not fixture.control
        assert replacement.execution_mode == "normal"
    finally:
        if barrier is not None:
            barrier.release.set()
        if sending is not None:
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
        await fixture.close()


class _OrdinaryNaturalBackupBarrier(SessionBackupService):
    def __init__(self) -> None:
        super().__init__(retry_delays=())
        self.entered = threading.Event()
        self.release = threading.Event()

    def backup_session(self, *args, **kwargs):
        self.entered.set()
        if not self.release.wait(timeout=_STREAM_TEST_TIMEOUT):
            raise TimeoutError("test backup barrier was not released")
        return super().backup_session(*args, **kwargs)


@pytest.mark.asyncio
async def test_public_sdk_ordinary_followup_waits_for_same_task_natural_backup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared-backup"))
    backup = _OrdinaryNaturalBackupBarrier()
    fixture = _OrdinaryNormalHandoffSdkFixture(tmp_path, backup_service=backup)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fixture.create_runtime)
    finalizing = None
    sending = None
    try:
        await fixture.prepare(finalize=False)
        backup.initialize_session(str(fixture.cwd), fixture.session_id)
        finalizing = asyncio.create_task(fixture.finish_natural_handoff())
        assert await asyncio.to_thread(backup.entered.wait, _STREAM_TEST_TIMEOUT)
        assert fixture.control.phase == "terminated"
        assert not fixture.control.natural_handoff_admits_replacement()
        await fixture.restore_sdk_wait_projection()
        sending = asyncio.create_task(fixture.send())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(sending), timeout=0.05)
        assert fixture.loop.prompts == []
        active = await fixture.handler._active_task_registry.get(fixture.task_id)
        assert active is not None and len(active._pending_accepted_requests) == 1
        original_uuid = next(iter(active._pending_accepted_requests))
        backup.release.set()
        await asyncio.wait_for(finalizing, timeout=_STREAM_TEST_TIMEOUT)
        assert fixture.control.snapshot()["commitError"] is None
        assert fixture.control.natural_handoff_admits_replacement()
        await asyncio.wait_for(sending, timeout=_STREAM_TEST_TIMEOUT)
        await asyncio.wait_for(active.wait_for_accepted_requests(), timeout=_STREAM_TEST_TIMEOUT)
        assert original_uuid not in active._pending_accepted_requests
        assert not active._accepted_consumer_failed
        assert fixture.loop.prompts == ["Explain cleanup of the existing resources."]
        assert fixture.control.natural_handoff_receipt() is not None
        replacement = fixture.service.get_for_context(fixture.context_id)
        assert replacement is not None and replacement is not fixture.control
        assert replacement.execution_mode == "normal"
    finally:
        backup.release.set()
        for task in (sending, finalizing):
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await fixture.close()


class _OrdinaryRejectedHandoffBarrier:
    """Pause only after the real helper rejects, before SDK error publication."""

    def __init__(self, executor) -> None:
        self._begin = executor._begin_proven_normal_handoff_input
        self.rejected = asyncio.Event()
        self.release = asyncio.Event()

    async def begin(self, **kwargs):
        from iac_code.a2a.execution_control import ExecutionControlConflictError

        try:
            return await self._begin(**kwargs)
        except ExecutionControlConflictError:
            self.rejected.set()
            await self.release.wait()
            raise


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_failure", ["missing-ack", "wrong-ack", "commit-error", "blocked", "wrong-task"])
async def test_public_sdk_ordinary_followup_does_not_bypass_invalid_handoff(
    monkeypatch, tmp_path, proof_failure
) -> None:
    from iac_code.a2a import execution_control as execution_control_module
    from iac_code.a2a.execution_control import ExecutionControlConflictError

    fixture = _OrdinaryNormalHandoffSdkFixture(tmp_path)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fixture.create_runtime)
    sending = None
    rejection = None
    original_write = None
    try:
        await fixture.prepare(with_ack=proof_failure not in {"missing-ack", "wrong-ack"})
        if proof_failure == "wrong-ack":
            fixture.handoff_events[1]["data"] = {"committedEventId": "different-event", "committedSequence": 99}
            fixture.commit_handoff_ack()
        elif proof_failure == "commit-error":
            original_write = execution_control_module.atomic_write_json

            def fail_old_control_commit(path, document):
                if document.get("executionId") == fixture.control.execution_id:
                    raise OSError("injected persistent control write failure")
                return original_write(path, document)

            monkeypatch.setattr(execution_control_module, "atomic_write_json", fail_old_control_commit)
            fixture.control.revision += 1
            await fixture.control._persist_natural_completion_claim(fixture.control.snapshot())
            assert fixture.control._commit_error == "state_commit_failed"
            rejection = _OrdinaryRejectedHandoffBarrier(fixture.executor)
            monkeypatch.setattr(fixture.executor, "_begin_proven_normal_handoff_input", rejection.begin)
        elif proof_failure == "blocked":
            fixture.control.backup = {"status": "blocked"}
            fixture.control._backup_state_committed = False
        elif proof_failure == "wrong-task":
            fixture.control.task_id = "task-another-owner"
        expected_error = (
            ExecutionControlConflictError
            if proof_failure == "wrong-task"
            else (InputResponseExecutionControlConflictError, InvalidParamsError)
        )
        sending = asyncio.create_task(fixture.send())
        if rejection is not None:
            await asyncio.wait_for(rejection.rejected.wait(), timeout=_STREAM_TEST_TIMEOUT)
            assert fixture.control._commit_error == "state_commit_failed"
            assert fixture.loop.prompts == []
            active = await fixture.handler._active_task_registry.get(fixture.task_id)
            assert active is not None and len(active._pending_accepted_requests) == 1
            # The actual admission already rejected. Restore storage only so
            # SDK failure projection/cleanup can finish without a permanent
            # unrelated TaskStore write outage masking that rejection.
            monkeypatch.setattr(execution_control_module, "atomic_write_json", original_write)
            await fixture.control.observe_state()
            await fixture.control.await_natural_handoff_settlement(timeout=_STREAM_TEST_TIMEOUT)
            await fixture.control.wait_until_recoverable_input_continuation(
                fixture.task_id, timeout=_STREAM_TEST_TIMEOUT
            )
            assert fixture.control._commit_error is None
            rejection.release.set()
        with pytest.raises(expected_error):
            await asyncio.wait_for(sending, timeout=_STREAM_TEST_TIMEOUT)
        assert fixture.loop.prompts == []
        assert fixture.service.get_for_context(fixture.context_id) is fixture.control
        active = await fixture.handler._active_task_registry.get(fixture.task_id)
        assert active is not None and active._accepted_consumer_failed
        assert len(active._pending_accepted_requests) == 1
    finally:
        if original_write is not None:
            monkeypatch.setattr(execution_control_module, "atomic_write_json", original_write)
        if rejection is not None:
            rejection.release.set()
        if sending is not None:
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
        if proof_failure == "wrong-task":
            fixture.control.task_id = fixture.task_id
        await fixture.close()


class _OrdinaryBeginLockBarrier:
    """Delay the real context-start lock after the helper's final proof read."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        await self.lock.acquire()
        return self

    async def __aexit__(self, *_args):
        self.lock.release()


@pytest.mark.asyncio
async def test_public_sdk_ordinary_followup_rejects_explicit_stop_winning_begin_lock(monkeypatch, tmp_path) -> None:
    fixture = _OrdinaryNormalHandoffSdkFixture(tmp_path)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fixture.create_runtime)
    barrier = _OrdinaryBeginLockBarrier()
    sending = None
    try:
        await fixture.prepare()
        await barrier.lock.acquire()
        fixture.service._context_start_locks[fixture.context_id] = barrier
        sending = asyncio.create_task(fixture.send())
        await asyncio.wait_for(barrier.entered.wait(), timeout=_STREAM_TEST_TIMEOUT)
        # A real explicit Stop invalidates the old natural receipt while begin
        # is waiting for its existing lock. No phase/proof value is fabricated.
        await fixture.control.terminate(
            execution_id=fixture.control.execution_id,
            request_id="explicit-stop-wins",
            connection_epoch=1,
            reason="explicit_terminate",
        )
        await fixture.control.wait_until_recoverable_input_continuation(fixture.task_id, timeout=_STREAM_TEST_TIMEOUT)
        assert fixture.control.termination_reason == "explicit_terminate"
        assert not fixture.control.natural_handoff_admits_replacement()
        barrier.lock.release()
        with pytest.raises((InputResponseExecutionControlConflictError, InvalidParamsError)):
            await asyncio.wait_for(sending, timeout=_STREAM_TEST_TIMEOUT)
        assert fixture.loop.prompts == []
        assert fixture.service.get_for_context(fixture.context_id) is fixture.control
    finally:
        if barrier.lock.locked():
            barrier.lock.release()
        if sending is not None:
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
        await fixture.close()


class _OrdinaryDiskClaimBarrier:
    def __init__(self, admissions) -> None:
        self._claim = admissions.claim_begin_without_admission
        self.entered = threading.Event()
        self.release = threading.Event()

    def claim(self, *args, **kwargs):
        self.entered.set()
        if not self.release.wait(timeout=_STREAM_TEST_TIMEOUT):
            raise TimeoutError("test disk claim barrier was not released")
        return self._claim(*args, **kwargs)


class _OrdinaryCompetingDiskStop:
    @classmethod
    def run(cls, control_path, cwd, results) -> None:
        results.put(asyncio.run(cls.stop(control_path, cwd)))

    @staticmethod
    async def stop(control_path, cwd) -> bool:
        from pathlib import Path

        from iac_code.a2a.execution_control import ExecutionController

        path = Path(control_path)
        previous = json.loads(path.read_text(encoding="utf-8"))
        # An independently scheduled writer uses the exact persisted identity
        # and the production controller's termination + owner-write fence.
        control = ExecutionController(
            context_id=previous["contextId"],
            task_id=previous["taskId"],
            execution_id=previous["executionId"],
            owner=previous["owner"],
            owner_generation=previous["ownerGeneration"],
            server_instance_id=previous["serverInstanceId"],
            cwd=cwd,
            persistence_path=path,
            backup_service=None,
            execution_mode="pipeline",
        )
        control.revision = previous["revision"]
        control.persisted_revision = previous["persistedRevision"]
        try:
            await control.terminate(
                execution_id=control.execution_id,
                request_id="other-process-explicit-stop",
                connection_epoch=1,
                reason="explicit_terminate",
            )
            await control.wait_until_recoverable_input_continuation(control.task_id, timeout=_STREAM_TEST_TIMEOUT)
            committed = json.loads(path.read_text(encoding="utf-8"))
            return bool(
                committed["executionId"] == previous["executionId"]
                and committed["terminationReason"] == "explicit_terminate"
                and committed["phase"] == "terminated"
                and committed["releaseReady"] is True
                and committed.get("naturalHandoff") is None
            )
        finally:
            await control.close()


@pytest.mark.asyncio
async def test_public_sdk_ordinary_followup_rejects_a_competing_process_stop_at_disk_claim(
    monkeypatch, tmp_path
) -> None:
    import multiprocessing

    fixture = _OrdinaryNormalHandoffSdkFixture(tmp_path)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fixture.create_runtime)
    barrier = None
    sending = None
    process = None
    process_context = multiprocessing.get_context("spawn")
    results = process_context.Queue()
    try:
        await fixture.prepare()
        barrier = _OrdinaryDiskClaimBarrier(fixture.service._recoverable_input_admissions)
        monkeypatch.setattr(
            fixture.service._recoverable_input_admissions, "claim_begin_without_admission", barrier.claim
        )
        sending = asyncio.create_task(fixture.send())
        assert await asyncio.to_thread(barrier.entered.wait, _STREAM_TEST_TIMEOUT)
        path = fixture.service._persistence_root / "execution-control" / (fixture.context_id + ".json")
        process = process_context.Process(
            target=_OrdinaryCompetingDiskStop.run, args=(str(path), str(fixture.cwd), results)
        )
        process.start()
        await asyncio.to_thread(process.join, _STREAM_TEST_TIMEOUT)
        assert not process.is_alive()
        assert process.exitcode == 0
        assert results.get(timeout=_STREAM_TEST_TIMEOUT) is True
        # Local state remains a positive natural receipt: only the original
        # file-lock/CAS authority can detect the other process's newer Stop.
        assert fixture.control.natural_handoff_admits_replacement()
        barrier.release.set()
        with pytest.raises((InputResponseExecutionControlConflictError, InvalidParamsError)):
            await asyncio.wait_for(sending, timeout=_STREAM_TEST_TIMEOUT)
        assert fixture.loop.prompts == []
        assert fixture.service.get_for_context(fixture.context_id) is fixture.control
        committed = json.loads(path.read_text(encoding="utf-8"))
        assert committed["terminationReason"] == "explicit_terminate"
        assert committed["executionId"] == fixture.control.execution_id
    finally:
        if barrier is not None:
            barrier.release.set()
        if process is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, _STREAM_TEST_TIMEOUT)
        if sending is not None:
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
        results.close()
        results.join_thread()
        await fixture.close()


class _CanceledSdkHintFixture:
    """Keep the real SDK completion pending after its canceled Task was saved."""

    task_id = "task-canceled-hint"
    context_id = "ctx-canceled-hint"

    def __init__(self, tmp_path) -> None:
        self.cwd = tmp_path / "workspace"
        self.cwd.mkdir()
        self.call_context = ServerCallContext()
        self.store = A2ATaskStore(persistence=A2APersistenceStore(tmp_path / "a2a"), owner_resolver=self.request_owner)
        self.release_execute = asyncio.Event()
        self.executed_message_ids: list[str] = []
        self.control_message_ids: list[str] = []
        self.handler = IacCodeRequestHandler(
            agent_executor=self,
            task_store=self.store,
            agent_card=SimpleNamespace(capabilities=SimpleNamespace(streaming=True, extensions=[])),
        )

    @staticmethod
    def request_owner(context) -> str:
        return context.state.get("test_owner", "")

    def request(self, message_id: str) -> SendMessageRequest:
        message = Message(
            message_id=message_id,
            task_id=self.task_id,
            context_id=self.context_id,
            role=Role.ROLE_USER,
            parts=[Part(text="Continue the interrupted planning request.")],
        )
        ParseDict({"iac_code": {"cwd": str(self.cwd), "run_mode": "pipeline"}}, message.metadata)
        return SendMessageRequest(message=message)

    async def prepare(self) -> None:
        await self.store.get_or_create_context(
            context_id=self.context_id,
            cwd=str(self.cwd),
            runtime_factory=lambda session_id: SimpleNamespace(session_id=session_id),
        )
        await self.store.save(
            Task(
                id=self.task_id,
                context_id=self.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            ),
            self.call_context,
        )
        stream = self.handler.on_message_send_stream(self.request("old-accepted-stop"), self.call_context)
        try:
            event = await asyncio.wait_for(anext(stream), timeout=_STREAM_TEST_TIMEOUT)
            assert isinstance(event, TaskStatusUpdateEvent)
            assert event.status.state == TaskState.TASK_STATE_CANCELED
        finally:
            # Observing a Task terminal is not an SDK execute completion. Close
            # only this subscriber while the real producer stays at its barrier.
            await stream.aclose()
        self.old = await self.handler._active_task_registry.get(self.task_id)
        assert isinstance(self.old, RequestScopedActiveTask)
        await asyncio.wait_for(self.old._is_finished.wait(), timeout=_STREAM_TEST_TIMEOUT)
        # Exercise the SDK's real canceled-producer boundary: execute has not
        # returned, so cancellation cannot invent an exact _RequestCompleted.
        assert self.old._producer_task is not None
        self.old._producer_task.cancel()
        await asyncio.wait_for(asyncio.shield(self.old._producer_task), timeout=_STREAM_TEST_TIMEOUT)
        assert self.old._consumer_task is not None
        await asyncio.wait_for(asyncio.shield(self.old._consumer_task), timeout=_STREAM_TEST_TIMEOUT)
        assert len(self.old._pending_accepted_requests) == 1
        assert self.old.has_unsettled_requests()
        assert (await self.store.get(self.task_id, self.call_context)).status.state == TaskState.TASK_STATE_CANCELED
        assert (await self.store.get_task_record(self.task_id)).state == "canceled"

    async def execute(self, request_context, event_queue) -> None:
        from iac_code.a2a.resource_selector import parse_resource_selection_response

        if parse_resource_selection_response(request_context.message) is not None:
            self.control_message_ids.append(request_context.message.message_id)
            await event_queue.enqueue_event(self.control_ack(request_context.message))
            return
        self.executed_message_ids.append(request_context.message.message_id)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=request_context.task_id,
                context_id=request_context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )
        )
        # The SDK owns _RequestCompleted and can produce it only after this
        # real execute returns. No ledger receipt or TaskManager is fabricated.
        await self.release_execute.wait()

    async def cancel(self, _request_context, _event_queue) -> None:
        return None

    def control_ack(self, message) -> Message:
        return Message(
            message_id="ack-" + message.message_id,
            task_id=self.task_id,
            context_id=self.context_id,
            role=Role.ROLE_AGENT,
            parts=[Part(text="Correlated control acknowledged.")],
        )

    async def resolve_sideband_permission(self, _response, *, metadata):
        self.control_message_ids.append(metadata.message_id)
        return self.control_ack(metadata)

    async def close(self) -> None:
        self.release_execute.set()
        if hasattr(self, "old"):
            producer = self.old._producer_task
            if producer is not None:
                await asyncio.wait_for(asyncio.shield(producer), timeout=_STREAM_TEST_TIMEOUT)
            await self.handler._active_task_registry.retire_for_recovery(self.task_id)
        cleanup = tuple(self.handler._active_task_registry._cleanup_tasks)
        if cleanup:
            await asyncio.gather(*cleanup, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_public_sdk_pipeline_stop_reentry_explicit_canceled_hint_rejects_before_new_request_enqueue(
    tmp_path, streaming
) -> None:
    fixture = _CanceledSdkHintFixture(tmp_path)
    try:
        await fixture.prepare()
        old = fixture.old
        pending = set(old._pending_accepted_requests)
        request = fixture.request("new-ordinary-reentry")
        with pytest.raises(InvalidParamsError) as rejected:
            if streaming:
                await asyncio.wait_for(
                    _collect_async(fixture.handler.on_message_send_stream(request, fixture.call_context)),
                    timeout=_STREAM_TEST_TIMEOUT,
                )
            else:
                await asyncio.wait_for(
                    fixture.handler.on_message_send(request, fixture.call_context), timeout=_STREAM_TEST_TIMEOUT
                )
        # Rejecting the new hint cannot settle, erase, or replay the old request.
        assert old._pending_accepted_requests == pending
        assert old.has_unsettled_requests()
        assert await fixture.handler._active_task_registry.get(fixture.task_id) is old
        assert fixture.executed_message_ids == ["old-accepted-stop"]
        assert request.message.task_id == fixture.task_id
        assert request.message.context_id == fixture.context_id
        assert str(rejected.value) == (f"Task {fixture.task_id} is in terminal state: {TaskState.TASK_STATE_CANCELED}")
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["context", "business-state", "sdk-state", "owner", "foreign-caller", "normal-mode"]
)
async def test_public_sdk_pipeline_stop_reentry_requires_matching_canceled_authorities(tmp_path, boundary) -> None:
    fixture = _CanceledSdkHintFixture(tmp_path)
    try:
        await fixture.prepare()
        request = fixture.request("mismatched-reentry")
        call_context = fixture.call_context
        pending = set(fixture.old._pending_accepted_requests)
        owned = await fixture.store.get_or_create_task(task_id=fixture.task_id, context_id=fixture.context_id)
        if boundary == "context":
            request.message.context_id = "ctx-other"
        elif boundary == "business-state":
            owned.state = "completed"
            fixture.store.mirror_task(owned)
        elif boundary == "sdk-state":
            status = TaskStatus(state=TaskState.TASK_STATE_WORKING)
            status.timestamp.GetCurrentTime()
            await fixture.store.save(
                Task(
                    id=fixture.task_id,
                    context_id=fixture.context_id,
                    status=status,
                ),
                fixture.call_context,
            )
            owned.state = "canceled"
            fixture.store.mirror_task(owned)
        elif boundary == "owner":
            owned.owner = "other-owner"
            fixture.store.mirror_task(owned)
        elif boundary == "foreign-caller":
            call_context = ServerCallContext()
            call_context.state["test_owner"] = "other-owner"
        else:
            ParseDict({"iac_code": {"cwd": str(fixture.cwd), "run_mode": "normal"}}, request.message.metadata)
        with pytest.raises(InvalidParamsError) as rejected:
            await asyncio.wait_for(
                _collect_async(fixture.handler.on_message_send_stream(request, call_context)),
                timeout=_STREAM_TEST_TIMEOUT,
            )
        assert "is in terminal state:" not in str(rejected.value)
        assert "lifecycle ended before the accepted request settled" in str(rejected.value)
        assert fixture.executed_message_ids == ["old-accepted-stop"]
        assert fixture.old._pending_accepted_requests == pending
        assert await fixture.handler._active_task_registry.get(fixture.task_id) is fixture.old
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_public_sdk_pipeline_stop_reentry_does_not_hide_task_storage_read_failure(tmp_path, monkeypatch) -> None:
    fixture = _CanceledSdkHintFixture(tmp_path)
    try:
        await fixture.prepare()
        pending = set(fixture.old._pending_accepted_requests)

        async def unavailable(task_id, context=None):
            assert task_id == fixture.task_id
            raise OSError("offline task storage failure")

        monkeypatch.setattr(fixture.store, "get", unavailable)
        with pytest.raises(OSError, match="offline task storage failure"):
            await _collect_async(
                fixture.handler.on_message_send_stream(fixture.request("read-failure"), fixture.call_context)
            )
        assert fixture.old._pending_accepted_requests == pending
        assert fixture.executed_message_ids == ["old-accepted-stop"]
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("kind", ["permission", "cloud_resource_selection"])
async def test_public_sdk_pipeline_stop_reentry_preserves_correlated_control_route(tmp_path, streaming, kind) -> None:
    fixture = _CanceledSdkHintFixture(tmp_path)
    try:
        await fixture.prepare()
        pending = set(fixture.old._pending_accepted_requests)
        response = {
            "schemaVersion": 1,
            "kind": kind,
            "requestTaskId": fixture.task_id,
            "contextId": fixture.context_id,
            "inputId": "resource-" + "a" * 32 if kind == "cloud_resource_selection" else "permission-1",
            "toolUseId": "tool-1",
        }
        response.update({"status": "canceled"} if kind == "cloud_resource_selection" else {"decision": "deny"})
        prefix = RESOURCE_SELECTION_QUERY_PREFIX if kind == "cloud_resource_selection" else PERMISSION_QUERY_PREFIX
        request = fixture.request("control-" + kind)
        request.message.parts[0].text = prefix + json.dumps(response)
        if streaming:
            events = await asyncio.wait_for(
                _collect_async(fixture.handler.on_message_send_stream(request, fixture.call_context)),
                timeout=_STREAM_TEST_TIMEOUT,
            )
            assert len(events) == 1 and isinstance(events[0], Message)
        else:
            result = await asyncio.wait_for(
                fixture.handler.on_message_send(request, fixture.call_context), timeout=_STREAM_TEST_TIMEOUT
            )
            if kind == "cloud_resource_selection":
                assert isinstance(result, Task)
                assert result.id == fixture.task_id and result.context_id == fixture.context_id
                assert result.status.state == TaskState.TASK_STATE_CANCELED
            else:
                assert isinstance(result, Message)
                assert result.task_id == fixture.task_id and result.context_id == fixture.context_id
        assert fixture.control_message_ids == [request.message.message_id]
        assert fixture.executed_message_ids == ["old-accepted-stop"]
        assert fixture.old._pending_accepted_requests == pending
        assert await fixture.handler._active_task_registry.get(fixture.task_id) is fixture.old
    finally:
        await fixture.close()


class _PipelineStopModelHttp:
    """Block the actual standard SDK's first and resumed logical model calls."""

    def __init__(self) -> None:
        self.first_model = asyncio.Event()
        self.resumed_model = asyncio.Event()
        self.requests: list[dict] = []
        self.business_calls = 0

    async def handle(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        if not payload.get("stream", False):
            return httpx.Response(
                200,
                json={
                    "id": "continue-judge",
                    "object": "chat.completion",
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"action": "continue", "reason": "Continue the same planning step."}
                                ),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        self.business_calls += 1
        (self.first_model if self.business_calls == 1 else self.resumed_model).set()
        await asyncio.Event().wait()
        return httpx.Response(200)  # pragma: no cover


class _PublicStoppedPipelineFixture:
    """Exercise full SDK dispatch and real Runner/Stop/durable successor claim."""

    task_id = "task-public-stop"
    context_id = "ctx-public-stop"
    original_text = "Remember planning marker stopped-sdk and network 10.242.0.0/16."
    reentry_text = "Continue the interrupted plan and preserve the original network."

    def __init__(self, tmp_path) -> None:
        from iac_code.services.session_backup_staging import StagedSessionBackupService

        self.cwd = tmp_path / "workspace"
        self.cwd.mkdir()
        self.config = tmp_path / "pipeline-config"
        self.config.mkdir()
        (self.config / "model.md").write_text("Plan only; do not execute external operations.", encoding="utf-8")
        (self.config / "pipeline.yaml").write_text(
            json.dumps(
                {
                    "name": "selling",
                    "context_dependencies": {"plan": []},
                    "steps": [
                        {
                            "id": "solution_planning_and_selection",
                            "conclusion_field": "plan",
                            "forward": None,
                            "prompt": "model.md",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.model = _PipelineStopModelHttp()
        self.clients = []
        self.runners = []
        self.storage = SessionStorage()
        self.backup = StagedSessionBackupService(tmp_path / "staging", self.storage, retry_delays=())
        self.persistence = A2APersistenceStore(tmp_path / "a2a")
        self.store = A2ATaskStore(persistence=self.persistence)
        self.service = ExecutionControlService(persistence_root=self.persistence.root, backup_service=self.backup)
        self.executor = IacCodeA2AExecutor(
            task_store=self.store, model="gpt-4o", backup_service=self.backup, execution_control_service=self.service
        )
        self.store.set_execution_control_provider(
            self.service.snapshot_for_context,
            self.service.has_active_work,
            self.service.reserve_recoverable_input_continuation,
            self.service.release_recoverable_input_continuation,
        )
        self.handler = IacCodeRequestHandler(
            agent_executor=self.executor,
            task_store=self.store,
            agent_card=SimpleNamespace(capabilities=SimpleNamespace(streaming=True, extensions=[])),
        )
        self.sending = []

    def create_runtime(self, options):
        from iac_code.tools.base import ToolRegistry

        # The public factory's runtime does not configure a provider. Every
        # actual old/new step uses its own exact standard manager below.
        return SimpleNamespace(provider_manager=object(), tool_registry=ToolRegistry(), session_id=options.session_id)

    def create_model_manager(self):
        from openai import AsyncOpenAI

        from iac_code.providers.manager import ProviderManager
        from iac_code.providers.retry import RetryConfig

        manager = ProviderManager(
            "gpt-4o",
            {"openai": "offline-test"},
            provider_key_override="openai",
            ignore_llm_source=True,
            provider_config_override={},
            retry_config=RetryConfig(max_retries=0),
        )
        self.clients.append(manager._provider._client)
        sdk = AsyncOpenAI(
            api_key="offline-test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.model.handle)),
            max_retries=0,
        )
        self.clients.append(sdk)
        manager._provider._client = sdk
        return manager

    def create_pipeline(self, _executor, *, session_id, cwd, runtime, session_storage, **_kwargs):
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner(
            self.config,
            self.create_model_manager(),
            runtime.tool_registry,
            session_storage,
            session_id,
            cwd=cwd,
            resume_from_sidecar=True,
            surface="a2a",
            backup_service=self.backup,
        )
        self.runners.append(runner)
        return runner

    def request(self, *, task_id, message_id, text):
        message = Message(
            task_id=task_id,
            context_id=self.context_id,
            message_id=message_id,
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
        )
        ParseDict({"iac_code": {"cwd": str(self.cwd), "run_mode": "pipeline"}}, message.metadata)
        return SendMessageRequest(message=message)

    async def collect_until_canceled(self, stream):
        events = []
        try:
            async for event in stream:
                events.append(event)
                if isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_CANCELED:
                    assert event.task_id == self.task_id and event.context_id == self.context_id
                    return events
            raise AssertionError("The stopped subscription ended before its canceled terminal was observed")
        finally:
            # A stopped client closes this subscriber at the delivered canceled
            # boundary; it does not claim successful natural stream exhaustion.
            await stream.aclose()

    def send(self, request, *, close_at_canceled=False):
        # Carrier state belongs to one SDK request, including rejected hints.
        stream = self.handler.on_message_send_stream(request, ServerCallContext())
        collecting = self.collect_until_canceled(stream) if close_at_canceled else _collect_async(stream)
        sending = asyncio.create_task(collecting)
        self.sending.append(sending)
        return sending

    async def prepare_stopped(self) -> None:
        ctx = await self.store.get_or_create_context(
            context_id=self.context_id,
            cwd=str(self.cwd),
            runtime_factory=lambda session_id: self.create_runtime(SimpleNamespace(session_id=session_id)),
        )
        self.storage.ensure_v2_session_dir_for_new_session(str(self.cwd), ctx.session_id)
        self.backup.initialize_session(str(self.cwd), ctx.session_id)
        await self.store.save(
            Task(id=self.task_id, context_id=self.context_id, status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)),
            ServerCallContext(),
        )
        old_request = self.request(task_id=self.task_id, message_id="public-stop-first", text=self.original_text)
        first = self.send(old_request, close_at_canceled=True)
        await asyncio.wait_for(self.model.first_model.wait(), timeout=_STREAM_TEST_TIMEOUT)
        self.control = self.service.get_for_context(self.context_id)
        assert self.control is not None and self.control.task_id == self.task_id
        ctx = await self.store.get_context_record(self.context_id)
        self.session_id = ctx.session_id
        await self.control.terminate(
            execution_id=self.control.execution_id,
            request_id="public-stop",
            connection_epoch=1,
            reason="explicit_terminate",
        )
        first_events = await asyncio.wait_for(first, timeout=_STREAM_TEST_TIMEOUT)
        canceled_events = [
            event
            for event in first_events
            if isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_CANCELED
        ]
        assert len(canceled_events) == 1
        await self.control.wait_until_recoverable_input_continuation(self.task_id, timeout=_STREAM_TEST_TIMEOUT)
        self.proof = await self.store.canceled_task_release_proof(
            context_id=self.context_id, task_id=self.task_id, include_model_boundary=True
        )
        assert self.proof is not None
        assert self.control.backup["status"] == "staged_committed"
        assert self.proof["backupCommitId"] == self.control.backup["commitId"]
        assert self.runners[0]._attempts["items"]["att_0001"]["status"] == "failed"

    async def close(self) -> None:
        current = self.service.get_for_context(self.context_id)
        if current is not None and current.phase not in {"terminating", "terminated"}:
            await current.terminate(
                execution_id=current.execution_id,
                request_id="fixture-stop",
                connection_epoch=1,
                reason="explicit_terminate",
            )
        for sending in self.sending:
            if not sending.done():
                sending.cancel()
        await asyncio.gather(*self.sending, return_exceptions=True)
        for task_id in tuple(self.handler._active_task_registry._active_tasks):
            await self.handler._active_task_registry.retire_for_recovery(task_id)
        await self.service.close()
        for client in self.clients:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_proof", [False, True])
async def test_public_sdk_pipeline_stop_reentry_omitted_task_uses_real_stop_proof_runner_and_disk_claim(
    tmp_path, monkeypatch, missing_proof
) -> None:
    from iac_code.a2a.pipeline_continuation import PipelineContinuationStore

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    fixture = _PublicStoppedPipelineFixture(tmp_path)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", fixture.create_runtime)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fixture.create_runtime)
    monkeypatch.setattr(
        "iac_code.a2a.executor.IacCodeA2APipelineExecutor._create_pipeline",
        lambda executor, **kwargs: fixture.create_pipeline(executor, **kwargs),
    )
    original_control_bytes = None
    try:
        await fixture.prepare_stopped()
        pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(fixture.cwd), session_id=fixture.session_id)
        intent_store = PipelineContinuationStore(pipeline_dir, session_dir=pipeline_dir.parent.parent)
        assert intent_store.load(context_id=fixture.context_id) is None
        old = await fixture.handler._active_task_registry.get(fixture.task_id)
        old_pending = set(old._pending_accepted_requests) if old is not None else None
        hint = fixture.request(task_id=fixture.task_id, message_id="public-stop-reentry", text=fixture.reentry_text)
        with pytest.raises(InvalidParamsError) as refused:
            await asyncio.wait_for(fixture.send(hint), timeout=_STREAM_TEST_TIMEOUT)
        assert str(refused.value) == (f"Task {fixture.task_id} is in terminal state: {TaskState.TASK_STATE_CANCELED}")
        assert fixture.model.business_calls == 1
        assert intent_store.load(context_id=fixture.context_id) is None
        assert await fixture.handler._active_task_registry.get(fixture.task_id) is old
        if old is not None:
            assert old._pending_accepted_requests == old_pending
        # The refused hint never consumed this SDK message identity. The same
        # request then follows the separately authorized omitted-task path.
        request = fixture.request(task_id="", message_id="public-stop-reentry", text=fixture.reentry_text)
        if missing_proof:
            control_path = fixture.persistence.root / "execution-control" / (fixture.context_id + ".json")
            original_control_bytes = control_path.read_bytes()
            control_path.unlink()
            with pytest.raises(InvalidParamsError, match="cancellation release proof is unavailable"):
                await asyncio.wait_for(fixture.send(request), timeout=_STREAM_TEST_TIMEOUT)
            assert request.message.task_id == ""
            assert intent_store.load(context_id=fixture.context_id) is None
            assert fixture.model.business_calls == 1
        else:
            sending = fixture.send(request)
            await asyncio.wait_for(fixture.model.resumed_model.wait(), timeout=_STREAM_TEST_TIMEOUT)
            successor = request.message.task_id
            assert successor and successor != fixture.task_id
            intent = intent_store.load(context_id=fixture.context_id)
            assert intent is not None and intent.successor_task_id == successor and intent.phase == "running"
            assert intent.cancellation_execution_id == fixture.proof["executionId"]
            assert intent.cancellation_backup_commit_id == fixture.proof["backupCommitId"]
            new_control = fixture.service.get_for_context(fixture.context_id)
            assert new_control is not fixture.control and new_control.task_id == successor
            assert new_control.owner_generation > fixture.control.owner_generation
            canonical = json.loads(
                (fixture.persistence.root / "execution-control" / (fixture.context_id + ".json")).read_text(
                    encoding="utf-8"
                )
            )
            assert canonical["taskId"] == successor and canonical["executionId"] == new_control.execution_id
            active = await fixture.handler._active_task_registry.get(successor)
            assert active is not None and len(active._pending_accepted_requests) == 1
            assert not sending.done() and len(fixture.runners) == 2
            assert fixture.runners[-1]._execution["active_attempt_id"] == "att_0002"
            business = [payload for payload in fixture.model.requests if payload.get("stream")]
            assert [row["content"] for row in business[-1]["messages"] if row["role"] == "user"] == [
                fixture.original_text,
                fixture.reentry_text,
            ]
        if old is not None:
            assert old._pending_accepted_requests == old_pending
    finally:
        if original_control_bytes is not None:
            (fixture.persistence.root / "execution-control" / (fixture.context_id + ".json")).write_bytes(
                original_control_bytes
            )
        await fixture.close()
