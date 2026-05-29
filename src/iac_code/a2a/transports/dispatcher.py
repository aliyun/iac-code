from __future__ import annotations

import inspect
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
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
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
)
from a2a.utils.errors import (
    ExtensionSupportRequiredError,
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from starlette.applications import Starlette
from starlette.routing import Route

from iac_code.a2a.agent_card import build_agent_card, build_extended_agent_card
from iac_code.a2a.app import normalize_v03_jsonrpc_version
from iac_code.a2a.artifacts import A2AArtifactStore
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.exposure import normalize_a2a_exposure_types
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2APersistenceStore
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
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params: SendMessageRequest, context):
        self._validate_extensions(context)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def on_cancel_task(self, params: CancelTaskRequest, context) -> Task | None:
        self._validate_extensions(context)
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError(f"Task {params.id} not found")
        if isinstance(self.task_store, A2ATaskStore) and not await self.task_store.is_task_active(params.id):
            raise TaskNotCancelableError
        return await super().on_cancel_task(params, context)

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

    def _validate_extensions(self, context) -> None:
        requested = set(getattr(context, "requested_extensions", set()) or set())
        required = sorted(extension.uri for extension in self._agent_card.capabilities.extensions if extension.required)
        missing = [uri for uri in required if uri not in requested]
        if missing:
            raise ExtensionSupportRequiredError(f"Required A2A extensions were not requested: {', '.join(missing)}")


def _create_dispatch_app(handler: DefaultRequestHandler) -> Starlette:
    jsonrpc_endpoint = create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True)[0].endpoint

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
