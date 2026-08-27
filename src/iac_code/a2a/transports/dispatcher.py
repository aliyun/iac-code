from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import AsyncExitStack, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator, cast

import httpx
from a2a.server.agent_execution.active_task import INTERRUPTED_TASK_STATES, TERMINAL_TASK_STATES
from a2a.server.events.event_queue import EventQueue
from a2a.server.events.event_queue_v2 import QueueShutDown
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import DEFAULT_LIST_TASKS_PAGE_SIZE, decode_page_token, encode_page_token
from a2a.types import (
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    Message,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.errors import (
    ExtensionSupportRequiredError,
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from a2a.utils.task import apply_history_length
from starlette.applications import Starlette
from starlette.routing import Route

from iac_code.a2a.agent_card import build_agent_card, build_extended_agent_card
from iac_code.a2a.app import normalize_v03_jsonrpc_version
from iac_code.a2a.artifacts import A2AArtifactStore
from iac_code.a2a.events import make_text_part
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.exposure import normalize_a2a_exposure_types
from iac_code.a2a.input_required import parse_permission_response
from iac_code.a2a.jsonrpc_passthrough import (
    install_jsonrpc_error_data_passthrough,
    install_v03_jsonrpc_error_data_passthrough,
)
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.pipeline_executor import (
    _CANCEL_WAITING_INPUT_BACKUP_BLOCKED,
    WaitingInputCancelResult,
    cancel_waiting_input_task_from_sidecar,
    recoverable_task_id_from_sidecar,
    terminal_task_state_from_sidecar,
    waiting_input_task_id_from_sidecar,
)
from iac_code.a2a.pipeline_transport_delivery import (
    acknowledge_pipeline_transport_delivery,
    bind_pipeline_transport_delivery_route,
    bind_pipeline_transport_delivery_tracker,
    close_pipeline_transport_delivery_tracker,
    create_pipeline_transport_delivery_tracker,
    mark_pipeline_transport_delivery_dequeued,
)
from iac_code.a2a.projection import (
    project_a2a_data,
    resolve_a2a_public_path_roots,
    resolve_a2a_public_path_roots_for_data,
)
from iac_code.a2a.push import (
    A2APushConfigStore,
    A2APushSender,
    InvalidPushNotificationConfigError,
    validate_push_callback_url,
)
from iac_code.a2a.push_queue import LocalFileA2APushQueue, RedisStreamsA2APushQueue, require_redis_asyncio
from iac_code.a2a.push_secrets import A2APushSecretKeyring
from iac_code.a2a.push_worker import A2APushDeliveryWorker
from iac_code.a2a.request_mode import resolve_request_run_mode
from iac_code.a2a.runtime_registry import A2ARuntimeOwner, A2ARuntimeRegistration, register_runtime_owner
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.i18n import _
from iac_code.pipeline.config import RunMode
from iac_code.services.permission_wait import PermissionWaitCheckpointStore
from iac_code.services.session_backup import SessionBackupService
from iac_code.services.session_backup_staging import (
    SessionBackupStagingProcess,
    create_a2a_session_backup_runtime,
)
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.public_errors import sanitize_strict_text

logger = logging.getLogger(__name__)
_ACTIVE_MESSAGE_STREAM_COMPLETED = object()
_ASGI_STREAM_END = object()


@dataclass
class _ASGIResponseChunk:
    body: bytes
    more_body: bool
    consumed: asyncio.Future[None]


class _DetachedPermissionEventQueue(EventQueue):
    """Minimal producer queue for resuming an already-persisted A2A task."""

    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue

    async def enqueue_event(self, event: Any) -> None:
        await self._queue.put(event)


class _StreamingASGIResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        queue: asyncio.Queue[_ASGIResponseChunk | object],
        app_task: asyncio.Task[None],
        app_error: list[BaseException],
        disconnect: asyncio.Event,
    ) -> None:
        self._queue = queue
        self._app_task = app_task
        self._app_error = app_error
        self._disconnect = disconnect
        self._pending: _ASGIResponseChunk | None = None
        self._finished = False
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            while True:
                entry = await self._queue.get()
                if entry is _ASGI_STREAM_END:
                    await self._finish()
                    return
                assert isinstance(entry, _ASGIResponseChunk)
                self._pending = entry
                try:
                    if entry.body:
                        yield entry.body
                finally:
                    self._acknowledge_pending()
                if not entry.more_body:
                    await self._finish()
                    return
        finally:
            self._acknowledge_pending()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._acknowledge_pending()
        self._disconnect.set()
        if not self._finished and not self._app_task.done():
            self._app_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._app_task

    def _acknowledge_pending(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.consumed.done():
            pending.consumed.set_result(None)

    async def _finish(self) -> None:
        await self._app_task
        self._finished = True
        if self._app_error:
            raise self._app_error[0]


class _StreamingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, app: Any) -> None:
        self._app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 0),
            "root_path": "",
        }
        request_chunks = request.stream.__aiter__()
        request_complete = False
        response_started = asyncio.Event()
        response_complete = False
        response_status: int | None = None
        response_headers: list[tuple[bytes, bytes]] | None = None
        response_queue: asyncio.Queue[_ASGIResponseChunk | object] = asyncio.Queue()
        disconnect = asyncio.Event()
        app_error: list[BaseException] = []

        async def receive() -> dict[str, Any]:
            nonlocal request_complete
            if request_complete:
                await disconnect.wait()
                return {"type": "http.disconnect"}
            try:
                body = await anext(request_chunks)
            except StopAsyncIteration:
                request_complete = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": body, "more_body": True}

        async def send(message: dict[str, Any]) -> None:
            nonlocal response_complete, response_headers, response_status
            message_type = message["type"]
            if message_type == "http.response.start":
                if response_started.is_set():
                    raise RuntimeError("ASGI application sent duplicate response start")
                response_status = int(message["status"])
                response_headers = list(message.get("headers", []))
                response_started.set()
                return
            if message_type != "http.response.body":
                return
            if response_status is None or response_complete:
                raise RuntimeError("ASGI application sent an invalid response body")
            more_body = bool(message.get("more_body", False))
            chunk = _ASGIResponseChunk(
                body=bytes(message.get("body", b"")),
                more_body=more_body,
                consumed=asyncio.get_running_loop().create_future(),
            )
            await response_queue.put(chunk)
            await chunk.consumed
            response_complete = not more_body

        async def run_app() -> None:
            try:
                await self._app(scope, receive, send)
            except BaseException as exc:
                app_error.append(exc)
            finally:
                response_started.set()
                if not response_complete:
                    await response_queue.put(_ASGI_STREAM_END)

        app_task = asyncio.create_task(run_app(), name="a2a-streaming-asgi-dispatch")
        try:
            await response_started.wait()
            if response_status is None or response_headers is None:
                await app_task
                if app_error:
                    raise app_error[0]
                raise RuntimeError("ASGI application did not start a response")
        except BaseException:
            disconnect.set()
            if not app_task.done():
                app_task.cancel()
            with suppress(asyncio.CancelledError):
                await app_task
            raise
        stream = _StreamingASGIResponseStream(response_queue, app_task, app_error, disconnect)
        return httpx.Response(response_status, headers=response_headers, stream=stream)


@dataclass
class A2ARuntimeComponents:
    handler: DefaultRequestHandler
    task_store: A2ATaskStore
    card: Any
    app: Starlette
    _exit_stack: AsyncExitStack
    backup_service: Any | None = None
    push_worker: Any | None = None
    push_queue: Any | None = None
    runtime_registration: A2ARuntimeRegistration | None = None
    backup_staging_process: SessionBackupStagingProcess | None = None

    def start_background_services(self) -> None:
        if self.backup_staging_process is not None:
            self.backup_staging_process.start()

    async def aclose(self) -> None:
        if self.runtime_registration is not None:
            self.runtime_registration.unregister()
            self.runtime_registration = None
        await self.task_store.stop_cleanup_loop()
        executor = getattr(self.handler, "agent_executor", None)
        if executor is not None:
            artifact_store = getattr(executor, "artifact_store", None)
            if artifact_store is not None:
                close = getattr(artifact_store, "aclose", None)
                if close is not None:
                    await close()
        push_sender = getattr(self.handler, "_push_sender", None)
        if push_sender is not None:
            close = getattr(push_sender, "aclose", None)
            if close is not None:
                await close()
        if self.push_worker is not None:
            close = getattr(self.push_worker, "aclose", None)
            if close is not None:
                await close()
        if self.push_queue is not None:
            close = getattr(self.push_queue, "aclose", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        await self._exit_stack.aclose()
        if self.backup_staging_process is not None:
            self.backup_staging_process.close()


def create_runtime_components(
    *,
    model: str,
    host: str,
    port: int,
    token: str | None = None,
    basic_username: str | None = None,
    basic_password: str | None = None,
    api_key: str | None = None,
    api_key_header: str = "X-API-Key",
    persistence_dir: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    signing_secret: str | None = None,
    signing_key_id: str = "default",
    push_notifications: bool = False,
    push_queue: str = "local-file",
    push_redis_url: str | None = None,
    push_stream: str = "iac-code:a2a:push",
    push_retry_key: str = "iac-code:a2a:push:retry",
    push_dead_stream: str = "iac-code:a2a:push:dead",
    push_consumer_group: str = "iac-code-push",
    push_consumer_name: str | None = None,
    push_lease_timeout_ms: int = 300_000,
    supported_interfaces: list[dict[str, str]] | None = None,
    agent_extensions: object | None = None,
    auto_approve_permissions: bool = False,
    permission_wait: object | None = None,
    thinking_exposure: object | None = None,
    backup_service: Any | None = None,
) -> A2ARuntimeComponents:
    from iac_code.services.permission_wait import PermissionWaitPolicy

    metrics = NoOpA2AMetrics()
    permission_wait_policy = PermissionWaitPolicy.from_config(permission_wait)
    thinking_exposure_types = normalize_a2a_exposure_types(thinking_exposure)
    backup_staging_process = None
    if backup_service is None:
        backup_runtime = create_a2a_session_backup_runtime()
        backup_service = backup_runtime.service
        backup_staging_process = backup_runtime.staging_process
    persistence = A2APersistenceStore(persistence_dir) if persistence_dir is not None else None
    artifact_store = A2AArtifactStore(artifact_dir) if artifact_dir is not None else None
    push_config_store = None
    push_sender = None
    push_worker = None
    push_queue_instance = None
    push_secret_keyring = None
    if push_notifications and persistence is None:
        from iac_code.config import get_config_dir

        persistence = A2APersistenceStore(get_config_dir() / "a2a")
    task_store = A2ATaskStore(metrics=metrics, persistence=persistence, backup_service=backup_service)
    if push_notifications:
        assert persistence is not None
        push_secret_keyring = A2APushSecretKeyring(Path(persistence.root) / "push_keys.json")
        push_config_store = A2APushConfigStore(persistence=persistence, secret_keyring=push_secret_keyring)
        if push_queue == "redis-streams":
            if not push_redis_url:
                raise RuntimeError("--push-redis-url is required for --push-queue redis-streams.")
            redis_module = require_redis_asyncio()
            redis_client = redis_module.from_url(push_redis_url)
            push_queue_instance = RedisStreamsA2APushQueue(
                redis=redis_client,
                stream=push_stream,
                retry_key=push_retry_key,
                dead_stream=push_dead_stream,
                consumer_group=push_consumer_group,
                consumer_name=push_consumer_name or "",
                lease_timeout_ms=push_lease_timeout_ms,
                owns_redis=True,
                secret_keyring=push_secret_keyring,
            )
        elif push_queue == "local-file":
            push_queue_instance = LocalFileA2APushQueue(
                Path(persistence.root) / "push_queue",
                secret_keyring=push_secret_keyring,
            )
        else:
            raise RuntimeError("--push-queue must be local-file or redis-streams.")
        push_sender = A2APushSender(config_store=push_config_store, queue=push_queue_instance, metrics=metrics)
        push_worker = A2APushDeliveryWorker(
            queue=push_queue_instance,
            metrics=metrics,
            header_resolver=push_config_store.resolve_headers_for_dispatch,
            path_roots_resolver=lambda task_id: resolve_a2a_public_path_roots(task_store, task_id=task_id),
        )
    executor = IacCodeA2AExecutor(
        task_store=task_store,
        model=model,
        metrics=metrics,
        artifact_store=artifact_store,
        auto_approve_permissions=auto_approve_permissions,
        permission_wait_policy=permission_wait_policy,
        thinking_exposure_types=thinking_exposure_types,
        backup_service=backup_service,
    )
    card = build_agent_card(
        host=host,
        port=port,
        token_enabled=bool(token),
        basic_enabled=bool(basic_username and basic_password),
        api_key_enabled=bool(api_key),
        api_key_header=api_key_header,
        signing_secret=signing_secret,
        signing_key_id=signing_key_id,
        push_notifications=push_notifications,
        supported_interfaces=supported_interfaces,
        agent_extensions=agent_extensions,
        thinking_exposure_types=thinking_exposure_types,
    )
    handler = IacCodeRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=card,
        push_config_store=push_config_store,
        push_sender=push_sender,
        extended_agent_card=build_extended_agent_card(card),
        backup_service=backup_service,
        metrics=metrics,
    )
    app = _create_dispatch_app(handler)
    runtime_registration = register_runtime_owner(
        A2ARuntimeOwner(
            task_store=task_store,
            model=model,
            metrics=metrics,
            persistence_root=persistence.root if persistence is not None else None,
            artifact_store=artifact_store,
            auto_approve_permissions=auto_approve_permissions,
            thinking_exposure_types=thinking_exposure_types,
        )
    )
    return A2ARuntimeComponents(
        handler=handler,
        task_store=task_store,
        card=card,
        app=app,
        _exit_stack=AsyncExitStack(),
        backup_service=backup_service,
        push_worker=push_worker,
        push_queue=push_queue_instance,
        runtime_registration=runtime_registration,
        backup_staging_process=backup_staging_process,
    )


class IacCodeRequestHandler(DefaultRequestHandler):
    def __init__(
        self,
        *args: Any,
        backup_service: Any | None = None,
        metrics: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._backup_service = backup_service or SessionBackupService()
        self._metrics = metrics or NoOpA2AMetrics()

    async def on_get_task(self, params: GetTaskRequest, context):
        self._validate_extensions(context)
        return await super().on_get_task(params, context)

    async def on_list_tasks(self, params: ListTasksRequest, context):
        self._validate_extensions(context)
        return await super().on_list_tasks(params, context)

    async def on_message_send(self, params: SendMessageRequest, context):
        self._validate_extensions(context)
        self._validate_pipeline_message_request(params)
        permission_response = parse_permission_response(params.message)
        if permission_response is not None:
            if not params.message.task_id:
                params.message.task_id = permission_response.task_id
            resolve = getattr(getattr(self, "agent_executor", None), "resolve_sideband_permission", None)
            if callable(resolve):
                ack = await resolve(permission_response, metadata=params.message.metadata)
                if ack is not None:
                    return ack
            if isinstance(self.task_store, A2ATaskStore) and not await self.task_store.is_task_active(
                permission_response.task_id
            ):
                task = await self.task_store.get(permission_response.task_id, context)
                if task is not None:
                    async for _event in self._on_inactive_permission_send_stream(
                        params,
                        context,
                        task=task,
                    ):
                        pass
                    refreshed = await self.task_store.get(permission_response.task_id, context)
                    return refreshed or task
        await self._hydrate_recoverable_pipeline_task_id(params)
        await self._reconcile_recoverable_pipeline_task(params, context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params: SendMessageRequest, context):
        self._validate_extensions(context)
        self._validate_pipeline_message_request(params)
        permission_response = parse_permission_response(params.message)
        if permission_response is not None:
            if not params.message.task_id:
                params.message.task_id = permission_response.task_id
            resolve = getattr(getattr(self, "agent_executor", None), "resolve_sideband_permission", None)
            if callable(resolve):
                ack = await resolve(permission_response, metadata=params.message.metadata)
                if ack is not None:
                    yield ack
                    return
        if permission_response is None:
            await self._hydrate_recoverable_pipeline_task_id(params)
            await self._reconcile_recoverable_pipeline_task(params, context)
        task_id = params.message.task_id or None
        if task_id and isinstance(self.task_store, A2ATaskStore) and await self.task_store.is_task_active(task_id):
            task = await self.task_store.get(task_id, context)
            active_task = await self._active_task_registry.get(task_id)
            if (
                task is not None
                and active_task is not None
                and task.status.state not in TERMINAL_TASK_STATES
                and (task.status.state not in INTERRUPTED_TASK_STATES or permission_response is not None)
            ):
                active_stream = self._on_active_message_send_stream(
                    params,
                    context,
                    task=task,
                    active_task=active_task,
                )
                try:
                    async for event in active_stream:
                        yield event
                finally:
                    await active_stream.aclose()
                return
        if permission_response is not None and isinstance(self.task_store, A2ATaskStore):
            task = await self.task_store.get(permission_response.task_id, context)
            if task is not None:
                direct_stream = self._on_inactive_permission_send_stream(params, context, task=task)
                try:
                    async for event in direct_stream:
                        yield event
                finally:
                    await direct_stream.aclose()
                return
        base_stream = super().on_message_send_stream(params, context)
        tracked_stream = (
            base_stream
            if resolve_request_run_mode(params.message) is RunMode.PIPELINE
            else _iterate_with_pipeline_transport_tracking(
                base_stream,
                task_id=getattr(params.message, "task_id", None) or None,
                context_id=getattr(params.message, "context_id", None) or None,
            )
        )
        try:
            async for event in tracked_stream:
                mark_pipeline_transport_delivery_dequeued(event)
                yield event
                acknowledge_pipeline_transport_delivery(event)
        finally:
            await tracked_stream.aclose()

    async def _on_inactive_permission_send_stream(self, params: SendMessageRequest, context, *, task: Task):
        """Resume an existing input boundary without asking the SDK to recreate its task."""

        request_context = await self._request_context_builder.build(
            params=params,
            task_id=task.id,
            context_id=params.message.context_id,
            task=task,
            context=context,
        )
        completed = object()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1024)

        async def run_permission_response() -> None:
            try:
                await self.agent_executor.execute(request_context, _DetachedPermissionEventQueue(queue))
            except BaseException as exc:
                await queue.put(exc)
            finally:
                await queue.put(completed)

        producer = asyncio.create_task(run_permission_response())
        handed_off = False
        try:
            while True:
                event = await queue.get()
                if event is completed:
                    break
                if isinstance(event, BaseException):
                    raise event
                if isinstance(event, Task):
                    self._validate_task_id_match(task.id, event.id)
                    yield apply_history_length(event, params.configuration)
                else:
                    yield event
            await producer
        except (asyncio.CancelledError, GeneratorExit):
            # The decision may already be committed. Let the same continuation
            # finish exactly once even if the response transport disappears.
            handed_off = True
            asyncio.create_task(self._drain_inactive_permission_response(queue, producer, completed))
            raise
        finally:
            if not handed_off and not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer

    @staticmethod
    async def _drain_inactive_permission_response(
        queue: asyncio.Queue[Any],
        producer: asyncio.Task[None],
        completed: object,
    ) -> None:
        try:
            while True:
                value = await queue.get()
                if value is completed:
                    break
            await producer
        except BaseException:
            logger.debug("Detached permission response continuation failed", exc_info=True)

    async def _on_active_message_send_stream(self, params: SendMessageRequest, context, *, task: Task, active_task):
        request_context = await self._request_context_builder.build(
            params=params,
            task_id=task.id,
            context_id=params.message.context_id,
            task=task,
            context=context,
        )
        async with active_task._lock:
            if active_task._is_finished.is_set():
                raise InvalidParamsError(_("Task {task_id} is already completed.").format(task_id=active_task.task_id))
            active_task._reference_count += 1
        tapped_queue = await active_task._event_queue_subscribers.tap()

        async def run_active_message() -> None:
            try:
                await self.agent_executor.execute(request_context, active_task._event_queue_agent)
            finally:
                await self._wait_for_active_message_events(active_task)
                with suppress(QueueShutDown):
                    await tapped_queue._put_internal((_ACTIVE_MESSAGE_STREAM_COMPLETED, None))

        delivery_tracker = create_pipeline_transport_delivery_tracker()
        with bind_pipeline_transport_delivery_route(
            delivery_tracker,
            task_id=task.id,
            context_id=getattr(task, "context_id", None) or params.message.context_id,
        ):
            with bind_pipeline_transport_delivery_tracker(delivery_tracker):
                producer_task = asyncio.create_task(run_active_message())

            try:
                while True:
                    try:
                        dequeued = await tapped_queue.dequeue_event()
                    except QueueShutDown:
                        break
                    event, _updated_task = cast(Any, dequeued)
                    if event is _ACTIVE_MESSAGE_STREAM_COMPLETED:
                        tapped_queue.task_done()
                        break
                    if isinstance(event, BaseException):
                        raise event
                    try:
                        mark_pipeline_transport_delivery_dequeued(event)
                        if isinstance(event, Task):
                            self._validate_task_id_match(task.id, event.id)
                            yield apply_history_length(event, params.configuration)
                        else:
                            yield event
                    finally:
                        tapped_queue.task_done()
                    acknowledge_pipeline_transport_delivery(event)
            except (asyncio.CancelledError, GeneratorExit):
                producer_task.cancel()
                raise
            finally:
                close_pipeline_transport_delivery_tracker(delivery_tracker)
                await tapped_queue.close(immediate=True)
                async with active_task._lock:
                    active_task._reference_count -= 1
                await active_task._maybe_cleanup()
                await self._cleanup_active_message_producer(producer_task, task.id)

    async def _hydrate_recoverable_pipeline_task_id(self, params: SendMessageRequest) -> None:
        if resolve_request_run_mode(params.message) is not RunMode.PIPELINE or not isinstance(
            self.task_store, A2ATaskStore
        ):
            return
        message = getattr(params, "message", None)
        if message is None:
            return
        if getattr(message, "task_id", None):
            return
        context_id = getattr(message, "context_id", None)
        if not isinstance(context_id, str) or not context_id:
            return
        try:
            context_record = await self.task_store.get_context_record(context_id)
            if not SessionStorage().exists(context_record.cwd, context_record.session_id):
                reconcile = getattr(self.agent_executor, "_reconcile_session_before_route", None)
                if callable(reconcile):
                    await reconcile(context_id=context_id, cwd=context_record.cwd)
            task_id = recoverable_task_id_from_sidecar(
                cwd=context_record.cwd,
                session_id=context_record.session_id,
                context_id=context_id,
            )
        except Exception:
            logger.debug("Failed to hydrate A2A pipeline task id for context %s", context_id, exc_info=True)
            return
        if task_id:
            message.task_id = task_id

    async def _reconcile_recoverable_pipeline_task(self, params: SendMessageRequest, context) -> None:
        if resolve_request_run_mode(params.message) is not RunMode.PIPELINE or not isinstance(
            self.task_store, A2ATaskStore
        ):
            return
        message = getattr(params, "message", None)
        if message is None:
            return
        task_id = getattr(message, "task_id", None)
        if not isinstance(task_id, str) or not task_id:
            return
        try:
            task = await self.task_store.get(task_id, context)
        except Exception:
            logger.debug("Failed to load A2A task %s before pipeline reconciliation", task_id, exc_info=True)
            return
        if task is None or task.status.state not in TERMINAL_TASK_STATES:
            return

        context_id = getattr(message, "context_id", None) or getattr(task, "context_id", None)
        if not isinstance(context_id, str) or not context_id:
            return
        try:
            context_record = await self.task_store.get_context_record(context_id)
            recoverable_task_id = recoverable_task_id_from_sidecar(
                cwd=context_record.cwd,
                session_id=context_record.session_id,
                context_id=context_id,
            )
        except Exception:
            logger.debug("Failed to inspect recoverable A2A pipeline task %s", task_id, exc_info=True)
            return
        if recoverable_task_id != task_id:
            return

        task.status.CopyFrom(TaskStatus(state=TaskState.Name(TaskState.TASK_STATE_INPUT_REQUIRED)))
        task.status.timestamp.GetCurrentTime()
        await self.task_store.save(task, context)
        if context_record.active_task_id == task_id:
            context_record.active_task_id = None
            context_record.touch()
            self.task_store.mirror_context(context_record)

    async def _wait_for_active_message_events(self, active_task) -> None:
        event_queue_agent = getattr(active_task, "_event_queue_agent", None)
        if event_queue_agent is not None:
            join_incoming = getattr(event_queue_agent, "test_only_join_incoming_queue", None)
            if callable(join_incoming):
                await join_incoming()
            agent_queue = getattr(event_queue_agent, "queue", None)
            join_agent_queue = getattr(agent_queue, "join", None)
            if callable(join_agent_queue):
                await join_agent_queue()

        event_queue_subscribers = getattr(active_task, "_event_queue_subscribers", None)
        join_subscribers = getattr(event_queue_subscribers, "test_only_join_incoming_queue", None)
        if callable(join_subscribers):
            await join_subscribers()

    async def _cleanup_active_message_producer(self, producer_task: asyncio.Task, task_id: str) -> None:
        try:
            await producer_task
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(
                "Active task message producer task_id=%s failed: %s",
                sanitize_strict_text(task_id),
                sanitize_strict_text(str(exc)),
            )

    async def on_cancel_task(self, params: CancelTaskRequest, context) -> Task | None:
        self._validate_extensions(context)
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError(f"Task {params.id} not found")
        if isinstance(self.task_store, A2ATaskStore) and not await self.task_store.is_task_active(params.id):
            durable_cancel = await self._claim_inactive_durable_permission_cancel(task)
            if durable_cancel == "lost":
                return await self._reconcile_inactive_pipeline_input_required_task(task, context)
            if durable_cancel == "normal":
                canceled = await self._reconcile_inactive_terminal_task(task, context, "canceled")
                await self.task_store.discard_context_runtime(task.context_id)
                return canceled
            canceled_task = await self._cancel_inactive_pipeline_waiting_input_task(task, context)
            if canceled_task is not None:
                return canceled_task
            canceled_task = await self._cancel_inactive_normal_input_required_task(task, context)
            if canceled_task is not None:
                return canceled_task
            raise TaskNotCancelableError
        return await super().on_cancel_task(params, context)

    async def _claim_inactive_durable_permission_cancel(self, task: Task) -> str | None:
        """Let the checkpoint lock decide a restart-time answer/cancel race."""

        if not isinstance(self.task_store, A2ATaskStore) or not _task_is_input_required(task):
            return None
        try:
            context_record = await self.task_store.get_context_record(task.context_id)
            store = PermissionWaitCheckpointStore(context_record.cwd, context_record.session_id)
            record = next(
                (
                    value
                    for value in store.list_active()
                    if value.get("taskId") == task.id and value.get("contextId") == task.context_id
                ),
                None,
            )
        except Exception:
            logger.debug("Failed to inspect durable permission wait during cancellation", exc_info=True)
            return None
        if record is None:
            return None
        boundary_id = str(record.get("boundaryId") or "")
        try:
            canceled = await asyncio.to_thread(store.cancel, boundary_id)
        except ValueError:
            # The same checkpoint file lock serializes this with a permission
            # decision. A claimed decision wins and cancellation must not
            # terminalize the task underneath its recovery.
            try:
                current = await asyncio.to_thread(store.load, boundary_id)
            except ValueError:
                current = None
            if isinstance(current, dict) and current.get("phase") == "CANCELED":
                canceled = current
            else:
                return "lost"
        return "normal" if canceled.get("permissionClass") == "normal" else "pipeline"

    async def _cancel_inactive_pipeline_waiting_input_task(self, task: Task, context) -> Task | None:
        if not isinstance(self.task_store, A2ATaskStore) or not _task_is_input_required(task):
            return None
        try:
            context_record = await self.task_store.get_context_record(task.context_id)
        except Exception:
            return None
        try:
            task_record = await self.task_store.get_task_record(task.id)
        except Exception:
            task_record = None
        cancel_result = await asyncio.to_thread(
            cancel_waiting_input_task_from_sidecar,
            cwd=context_record.cwd,
            session_id=context_record.session_id,
            context_id=task.context_id,
            task_id=task.id,
            reason=_("Task canceled while waiting for input."),
            backup_service=self._backup_service,
            task_store=self.task_store,
            task_record=task_record,
            context_record=context_record,
            metrics=self._metrics,
        )
        if cancel_result in {
            _CANCEL_WAITING_INPUT_BACKUP_BLOCKED,
            WaitingInputCancelResult.BACKUP_BLOCKED_PERSIST_FAILED,
        }:
            return await self._reconcile_inactive_pipeline_input_required_task(task, context)
        if cancel_result == WaitingInputCancelResult.PERSIST_FAILED:
            return None
        if cancel_result == WaitingInputCancelResult.NOT_OWNER:
            terminal_state = terminal_task_state_from_sidecar(
                cwd=context_record.cwd,
                session_id=context_record.session_id,
                context_id=task.context_id,
                task_id=task.id,
            )
            if terminal_state is None:
                return None
            return await self._reconcile_inactive_terminal_task(task, context, terminal_state)

        if cancel_result != WaitingInputCancelResult.CANCELED:
            return None
        return await self._reconcile_inactive_terminal_task(task, context, "canceled")

    async def _cancel_inactive_normal_input_required_task(self, task: Task, context) -> Task | None:
        if not isinstance(self.task_store, A2ATaskStore) or not _task_is_cancelable_normal_input_required(task):
            return None
        try:
            context_record = await self.task_store.get_context_record(task.context_id)
        except Exception:
            return None
        try:
            waiting_task_id = await asyncio.to_thread(
                waiting_input_task_id_from_sidecar,
                cwd=context_record.cwd,
                session_id=context_record.session_id,
                context_id=task.context_id,
            )
        except Exception:
            logger.debug("Failed to inspect A2A pipeline waiting-input sidecar", exc_info=True)
            return None
        if waiting_task_id is not None:
            return None
        canceled = await self._reconcile_inactive_terminal_task(task, context, "canceled")
        await self.task_store.discard_context_runtime(task.context_id)
        return canceled

    async def _reconcile_inactive_pipeline_input_required_task(self, task: Task, context) -> Task:
        task.status.CopyFrom(TaskStatus(state=TaskState.Name(TaskState.TASK_STATE_INPUT_REQUIRED)))
        task.status.timestamp.GetCurrentTime()
        await self.task_store.save(task, context)
        return task

    async def _reconcile_inactive_terminal_task(self, task: Task, context, terminal_state: str) -> Task:
        proto_state = {
            "completed": TaskState.TASK_STATE_COMPLETED,
            "failed": TaskState.TASK_STATE_FAILED,
            "canceled": TaskState.TASK_STATE_CANCELED,
        }.get(terminal_state, TaskState.TASK_STATE_CANCELED)
        message_text = {
            TaskState.TASK_STATE_COMPLETED: _("Task completed."),
            TaskState.TASK_STATE_FAILED: _("Task failed."),
            TaskState.TASK_STATE_CANCELED: _("Task canceled."),
        }[proto_state]
        task.status.CopyFrom(
            TaskStatus(
                state=TaskState.Name(proto_state),
                message=Message(
                    message_id=f"{task.id}-{proto_state}",
                    task_id=task.id,
                    context_id=task.context_id,
                    role=Role.ROLE_AGENT,
                    parts=[make_text_part(message_text)],
                ),
            )
        )
        task.status.timestamp.GetCurrentTime()
        await self.task_store.save(task, context)
        if self._push_sender is not None:
            try:
                await self._push_sender.send_notification(
                    task.id,
                    TaskStatusUpdateEvent(task_id=task.id, context_id=task.context_id, status=task.status),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to enqueue A2A push notification for terminal task %s: %s",
                    task.id,
                    sanitize_strict_text(str(exc))[:500],
                )
        return task

    async def on_subscribe_to_task(self, params: SubscribeToTaskRequest, context):
        self._validate_extensions(context)
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError(f"Task {params.id} not found")
        if isinstance(self.task_store, A2ATaskStore) and not await self.task_store.is_task_active(params.id):
            raise TaskNotFoundError(f"Task {params.id} is not active")
        terminal_state_seen = False
        async for event in super().on_subscribe_to_task(params, context):
            event_state = _task_event_state(event)
            terminal_state_seen = terminal_state_seen or event_state in TERMINAL_TASK_STATES
            yield event
            if event_state in INTERRUPTED_TASK_STATES:
                return
        if terminal_state_seen:
            return

        # a2a-sdk 1.1 can close its subscriber queue after the producer finishes but
        # before the consumer publishes the last update. Recover the persisted terminal
        # snapshot so subscribers do not observe a silent, non-terminal stream ending.
        final_task = await self.task_store.get(params.id, context)
        if final_task is not None and final_task.status.state in TERMINAL_TASK_STATES:
            yield final_task

    async def on_create_task_push_notification_config(
        self, params: TaskPushNotificationConfig, context
    ) -> TaskPushNotificationConfig:
        self._validate_extensions(context)
        try:
            validate_push_callback_url(params.url)
        except InvalidPushNotificationConfigError as exc:
            raise InvalidParamsError(str(exc)) from exc
        return await super().on_create_task_push_notification_config(params, context)

    async def on_get_task_push_notification_config(self, params: GetTaskPushNotificationConfigRequest, context):
        self._validate_extensions(context)
        return await super().on_get_task_push_notification_config(params, context)

    async def on_list_task_push_notification_configs(
        self, params: ListTaskPushNotificationConfigsRequest, context
    ) -> ListTaskPushNotificationConfigsResponse:
        self._validate_extensions(context)
        task = await self.task_store.get(params.task_id, context)
        if task is None:
            raise TaskNotFoundError(f"Task {params.task_id} not found")
        if self._push_config_store is None:
            return await super().on_list_task_push_notification_configs(params, context)
        configs = await self._push_config_store.get_info(params.task_id, context)
        configs.sort(key=lambda config: config.id)
        start_idx = 0
        if params.page_token:
            start_config_id = decode_page_token(params.page_token)
            for idx, config in enumerate(configs):
                if config.id == start_config_id:
                    start_idx = idx
                    break
            else:
                raise InvalidParamsError(f"Invalid page token: {params.page_token}")
        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        end_idx = start_idx + page_size
        next_page_token = encode_page_token(configs[end_idx].id) if end_idx < len(configs) else None
        return ListTaskPushNotificationConfigsResponse(
            configs=configs[start_idx:end_idx],
            next_page_token=next_page_token or "",
        )

    async def on_delete_task_push_notification_config(
        self, params: DeleteTaskPushNotificationConfigRequest, context
    ) -> None:
        self._validate_extensions(context)
        await super().on_delete_task_push_notification_config(params, context)

    def _validate_pipeline_message_request(self, params: SendMessageRequest) -> None:
        if resolve_request_run_mode(params.message) != RunMode.PIPELINE:
            return
        executor = getattr(self, "agent_executor", None)
        if isinstance(executor, IacCodeA2AExecutor):
            executor.validate_pipeline_message_request(params.message)

    def _validate_extensions(self, context) -> None:
        requested = set(getattr(context, "requested_extensions", set()) or set())
        required = sorted(extension.uri for extension in self._agent_card.capabilities.extensions if extension.required)
        missing = [uri for uri in required if uri not in requested]
        if missing:
            raise ExtensionSupportRequiredError(f"Required A2A extensions were not requested: {', '.join(missing)}")


async def _iterate_with_pipeline_transport_tracking(
    events,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
) -> AsyncGenerator[Any, None]:
    iterator = events.__aiter__()
    delivery_tracker = create_pipeline_transport_delivery_tracker()
    route = (
        bind_pipeline_transport_delivery_route(delivery_tracker, task_id=task_id, context_id=context_id)
        if task_id and context_id
        else nullcontext()
    )
    with route:
        try:
            while True:
                with bind_pipeline_transport_delivery_tracker(delivery_tracker):
                    try:
                        event = await anext(iterator)
                    except StopAsyncIteration:
                        return
                yield event
        finally:
            close_pipeline_transport_delivery_tracker(delivery_tracker)
            close = getattr(iterator, "aclose", None)
            if callable(close):
                await close()


def _task_is_input_required(task: Task) -> bool:
    try:
        return TaskState.Name(task.status.state) == TaskState.Name(TaskState.TASK_STATE_INPUT_REQUIRED)
    except Exception:
        return str(task.status.state) in {"TASK_STATE_INPUT_REQUIRED", "input-required"}


def _task_is_cancelable_normal_input_required(task: Task) -> bool:
    if not _task_is_input_required(task):
        return False
    message = getattr(getattr(task, "status", None), "message", None)
    parts = getattr(message, "parts", None) or []
    retryable_messages = {
        _("A temporary error occurred. Please retry."),
        _("Authentication required. Configure credentials and retry."),
    }
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text in retryable_messages:
            return True
    return False


def _task_event_state(event: Any) -> int | None:
    status = getattr(event, "status", None)
    state = getattr(status, "state", None)
    return state if isinstance(state, int) else None


def _create_dispatch_app(handler: DefaultRequestHandler) -> Starlette:
    install_jsonrpc_error_data_passthrough()
    jsonrpc_endpoint = create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True)[0].endpoint
    install_v03_jsonrpc_error_data_passthrough(jsonrpc_endpoint)

    async def handle_jsonrpc(request):
        await normalize_v03_jsonrpc_version(request)
        return await jsonrpc_endpoint(request)

    return Starlette(routes=[Route("/", handle_jsonrpc, methods=["POST"])])


class A2AJsonRpcDispatcher:
    def __init__(self, components: A2ARuntimeComponents) -> None:
        self._components = components
        self._http_client = httpx.AsyncClient(
            transport=_StreamingASGITransport(self._components.app),
            base_url="http://transport.local",
        )

    async def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._http_client.post("/", json=payload, headers={"A2A-Version": "1.0"})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("A2A dispatcher response must be a JSON object")
        roots = await resolve_a2a_public_path_roots_for_data(
            getattr(self._components, "task_store", None),
            response_data=data,
            request_data=payload,
        )
        return project_a2a_data(data, public_path_roots=roots)

    async def dispatch_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async with self._http_client.stream("POST", "/", json=payload, headers={"A2A-Version": "1.0"}) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data = json.loads(line.removeprefix("data:").strip())
                    roots = await resolve_a2a_public_path_roots_for_data(
                        getattr(self._components, "task_store", None),
                        response_data=data,
                        request_data=payload,
                    )
                    yield project_a2a_data(data, public_path_roots=roots)

    async def aclose(self) -> None:
        await self._http_client.aclose()
