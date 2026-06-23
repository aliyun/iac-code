from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, cast

import httpx
from a2a.server.agent_execution.active_task import TERMINAL_TASK_STATES
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
from iac_code.a2a.app import install_v03_jsonrpc_error_data_passthrough, normalize_v03_jsonrpc_version
from iac_code.a2a.artifacts import A2AArtifactStore
from iac_code.a2a.events import make_text_part
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.exposure import normalize_a2a_exposure_types
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.pipeline_executor import (
    cancel_waiting_input_task_from_sidecar,
    recoverable_task_id_from_sidecar,
    terminal_task_state_from_sidecar,
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
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.i18n import _
from iac_code.pipeline.config import RunMode, get_run_mode
from iac_code.utils.public_errors import public_exception_summary

logger = logging.getLogger(__name__)
_ACTIVE_MESSAGE_STREAM_COMPLETED = object()


@dataclass
class A2ARuntimeComponents:
    handler: DefaultRequestHandler
    task_store: A2ATaskStore
    card: Any
    app: Starlette
    _exit_stack: AsyncExitStack
    push_worker: Any | None = None
    push_queue: Any | None = None

    async def aclose(self) -> None:
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
    thinking_exposure: object | None = None,
) -> A2ARuntimeComponents:
    metrics = NoOpA2AMetrics()
    thinking_exposure_types = normalize_a2a_exposure_types(thinking_exposure)
    persistence = A2APersistenceStore(persistence_dir) if persistence_dir is not None else None
    artifact_store = A2AArtifactStore(artifact_dir) if artifact_dir is not None else None
    push_config_store = None
    push_sender = None
    push_worker = None
    push_queue_instance = None
    push_secret_keyring = None
    if push_notifications:
        if persistence is None:
            from iac_code.config import get_config_dir

            persistence = A2APersistenceStore(get_config_dir() / "a2a")
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
        )
    task_store = A2ATaskStore(metrics=metrics, persistence=persistence)
    executor = IacCodeA2AExecutor(
        task_store=task_store,
        model=model,
        metrics=metrics,
        artifact_store=artifact_store,
        auto_approve_permissions=auto_approve_permissions,
        thinking_exposure_types=thinking_exposure_types,
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
    )
    return A2ARuntimeComponents(
        handler=handler,
        task_store=task_store,
        card=card,
        app=_create_dispatch_app(handler),
        _exit_stack=AsyncExitStack(),
        push_worker=push_worker,
        push_queue=push_queue_instance,
    )


class IacCodeRequestHandler(DefaultRequestHandler):
    async def on_get_task(self, params: GetTaskRequest, context):
        self._validate_extensions(context)
        return await super().on_get_task(params, context)

    async def on_list_tasks(self, params: ListTasksRequest, context):
        self._validate_extensions(context)
        return await super().on_list_tasks(params, context)

    async def on_message_send(self, params: SendMessageRequest, context):
        self._validate_extensions(context)
        self._validate_pipeline_message_request(params)
        await self._hydrate_recoverable_pipeline_task_id(params)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params: SendMessageRequest, context):
        self._validate_extensions(context)
        self._validate_pipeline_message_request(params)
        await self._hydrate_recoverable_pipeline_task_id(params)
        task_id = params.message.task_id or None
        if task_id and isinstance(self.task_store, A2ATaskStore) and await self.task_store.is_task_active(task_id):
            task = await self.task_store.get(task_id, context)
            active_task = await self._active_task_registry.get(task_id)
            if task is not None and active_task is not None and task.status.state not in TERMINAL_TASK_STATES:
                async for event in self._on_active_message_send_stream(
                    params,
                    context,
                    task=task,
                    active_task=active_task,
                ):
                    yield event
                return
        async for event in super().on_message_send_stream(params, context):
            yield event

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
                if isinstance(event, Task):
                    self._validate_task_id_match(task.id, event.id)
                    yield apply_history_length(event, params.configuration)
                else:
                    yield event
                tapped_queue.task_done()
        except (asyncio.CancelledError, GeneratorExit):
            producer_task.cancel()
            raise
        finally:
            await tapped_queue.close(immediate=True)
            async with active_task._lock:
                active_task._reference_count -= 1
            await active_task._maybe_cleanup()
            await self._cleanup_active_message_producer(producer_task, task.id)

    async def _hydrate_recoverable_pipeline_task_id(self, params: SendMessageRequest) -> None:
        if get_run_mode() is not RunMode.PIPELINE or not isinstance(self.task_store, A2ATaskStore):
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
        except Exception:
            logger.exception("Active task message producer %s failed", task_id)

    async def on_cancel_task(self, params: CancelTaskRequest, context) -> Task | None:
        self._validate_extensions(context)
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError(f"Task {params.id} not found")
        if isinstance(self.task_store, A2ATaskStore) and not await self.task_store.is_task_active(params.id):
            canceled_task = await self._cancel_inactive_pipeline_waiting_input_task(task, context)
            if canceled_task is not None:
                return canceled_task
            raise TaskNotCancelableError
        return await super().on_cancel_task(params, context)

    async def _cancel_inactive_pipeline_waiting_input_task(self, task: Task, context) -> Task | None:
        if not isinstance(self.task_store, A2ATaskStore) or not _task_is_input_required(task):
            return None
        try:
            context_record = await self.task_store.get_context_record(task.context_id)
        except Exception:
            return None
        if not cancel_waiting_input_task_from_sidecar(
            cwd=context_record.cwd,
            session_id=context_record.session_id,
            context_id=task.context_id,
            task_id=task.id,
            reason=_("Task canceled while waiting for input."),
        ):
            terminal_state = terminal_task_state_from_sidecar(
                cwd=context_record.cwd,
                session_id=context_record.session_id,
                context_id=task.context_id,
                task_id=task.id,
            )
            if terminal_state is None:
                return None
            return await self._reconcile_inactive_pipeline_terminal_task(task, context, terminal_state)

        return await self._reconcile_inactive_pipeline_terminal_task(task, context, "canceled")

    async def _reconcile_inactive_pipeline_terminal_task(self, task: Task, context, terminal_state: str) -> Task:
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
                    public_exception_summary(exc, max_chars=500),
                    exc_info=True,
                )
        return task

    async def on_subscribe_to_task(self, params: SubscribeToTaskRequest, context):
        self._validate_extensions(context)
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError(f"Task {params.id} not found")
        if isinstance(self.task_store, A2ATaskStore) and not await self.task_store.is_task_active(params.id):
            raise TaskNotFoundError(f"Task {params.id} is not active")
        async for event in super().on_subscribe_to_task(params, context):
            yield event

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
        if get_run_mode() != RunMode.PIPELINE:
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


def _task_is_input_required(task: Task) -> bool:
    try:
        return TaskState.Name(task.status.state) == TaskState.Name(TaskState.TASK_STATE_INPUT_REQUIRED)
    except Exception:
        return str(task.status.state) in {"TASK_STATE_INPUT_REQUIRED", "input-required"}


def _create_dispatch_app(handler: DefaultRequestHandler) -> Starlette:
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
            transport=httpx.ASGITransport(app=self._components.app),
            base_url="http://transport.local",
        )

    async def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._http_client.post("/", json=payload, headers={"A2A-Version": "1.0"})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("A2A dispatcher response must be a JSON object")
        return data

    async def dispatch_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async with self._http_client.stream("POST", "/", json=payload, headers={"A2A-Version": "1.0"}) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    yield json.loads(line.removeprefix("data:").strip())

    async def aclose(self) -> None:
        await self._http_client.aclose()
