from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager, suppress
from email.utils import formatdate
from pathlib import Path
from time import time
from typing import Awaitable, Callable

from a2a.server.routes import create_jsonrpc_routes, create_rest_routes
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, SimpleUser
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from iac_code.a2a.agent_card import agent_card_to_client_dict

_V03_JSONRPC_METHODS = frozenset(
    {
        "message/send",
        "message/stream",
        "tasks/get",
        "tasks/cancel",
        "tasks/pushNotificationConfig/set",
        "tasks/pushNotificationConfig/get",
        "tasks/pushNotificationConfig/list",
        "tasks/pushNotificationConfig/delete",
        "tasks/resubscribe",
        "agent/getAuthenticatedExtendedCard",
    }
)


def resolve_token(cli_token: str | None) -> str | None:
    return cli_token or os.environ.get("IACCODE_A2A_HTTP_TOKEN")


def resolve_basic_credentials(cli_username: str | None, cli_password: str | None) -> tuple[str, str] | None:
    username = cli_username or os.environ.get("IACCODE_A2A_BASIC_USERNAME")
    password = cli_password or os.environ.get("IACCODE_A2A_BASIC_PASSWORD")
    if username and password:
        return username, password
    return None


def resolve_api_key(cli_api_key: str | None) -> str | None:
    return cli_api_key or os.environ.get("IACCODE_A2A_API_KEY")


def resolve_api_key_header(cli_api_key_header: str | None) -> str:
    return cli_api_key_header or os.environ.get("IACCODE_A2A_API_KEY_HEADER") or "X-API-Key"


class A2AAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        token: str | None,
        basic_username: str | None,
        basic_password: str | None,
        api_key: str | None,
        api_key_header: str,
    ) -> None:
        super().__init__(app)
        self._token = token
        self._basic_username = basic_username
        self._basic_password = basic_password
        self._api_key = api_key
        self._api_key_header = api_key_header

    @property
    def _auth_enabled(self) -> bool:
        return bool(self._token or (self._basic_username and self._basic_password) or self._api_key)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        principal = self._authorized_principal(request)
        if self._auth_enabled and principal is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        if principal is not None:
            request.scope["auth"] = AuthCredentials([principal.partition(":")[0]])
            request.scope["user"] = SimpleUser(principal)
        return await call_next(request)

    def _authorized_principal(self, request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if self._token and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self._token):
            return "bearer"
        if self._basic_username and self._basic_password and self._valid_basic_auth(auth):
            return f"basic:{self._basic_username}"
        api_key = request.headers.get(self._api_key_header)
        if self._api_key and api_key and hmac.compare_digest(api_key, self._api_key):
            return f"api-key:{self._api_key_header}"
        if not self._auth_enabled:
            return None
        return None

    def _valid_basic_auth(self, auth: str) -> bool:
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        username, separator, password = decoded.partition(":")
        if not separator:
            return False
        if not username or not password:
            return False
        return hmac.compare_digest(username, self._basic_username or "") and hmac.compare_digest(
            password, self._basic_password or ""
        )


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})


async def normalize_v03_jsonrpc_version(request: Request) -> None:
    try:
        body = await request.json()
    except Exception:
        return
    if not isinstance(body, dict) or body.get("method") not in _V03_JSONRPC_METHODS:
        return

    headers = [(name, value) for name, value in request.scope["headers"] if name.lower() != b"a2a-version"]
    headers.append((b"a2a-version", b"0.3"))
    request.scope["headers"] = headers
    if hasattr(request, "_headers"):
        delattr(request, "_headers")


def create_app(
    *,
    host: str,
    port: int,
    token: str | None,
    model: str,
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
) -> Starlette:
    from iac_code.a2a.transports.dispatcher import create_runtime_components

    components = create_runtime_components(
        model=model,
        host=host,
        port=port,
        token=token,
        basic_username=basic_username,
        basic_password=basic_password,
        api_key=api_key,
        api_key_header=api_key_header,
        persistence_dir=persistence_dir,
        artifact_dir=artifact_dir,
        signing_secret=signing_secret,
        signing_key_id=signing_key_id,
        push_notifications=push_notifications,
        push_queue=push_queue,
        push_redis_url=push_redis_url,
        push_stream=push_stream,
        push_retry_key=push_retry_key,
        push_dead_stream=push_dead_stream,
        push_consumer_group=push_consumer_group,
        push_consumer_name=push_consumer_name,
        push_lease_timeout_ms=push_lease_timeout_ms,
        supported_interfaces=supported_interfaces,
        agent_extensions=agent_extensions,
        auto_approve_permissions=auto_approve_permissions,
        thinking_exposure=thinking_exposure,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await components.task_store.start_cleanup_loop()
        push_worker_task: asyncio.Task[None] | None = None
        if components.push_worker is not None:
            push_worker_task = asyncio.create_task(components.push_worker.serve_forever())
        try:
            yield
        finally:
            if push_worker_task is not None:
                push_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await push_worker_task
            await components.aclose()

    card_data = agent_card_to_client_dict(components.card)
    card_etag = _agent_card_etag(card_data)
    card_last_modified = formatdate(time(), usegmt=True)
    card_cache_headers = {
        "Cache-Control": "public, max-age=60",
        "ETag": card_etag,
        "Last-Modified": card_last_modified,
    }

    async def get_agent_card(request: Request) -> Response:
        if request.headers.get("if-none-match") == card_etag:
            return Response(status_code=304, headers=card_cache_headers)
        return JSONResponse(card_data, headers=card_cache_headers)

    routes: list[BaseRoute] = [
        Route("/health", health, methods=["GET"]),
        Route(AGENT_CARD_WELL_KNOWN_PATH, get_agent_card, methods=["GET"]),
    ]
    jsonrpc_endpoint = create_jsonrpc_routes(components.handler, rpc_url="/", enable_v0_3_compat=True)[0].endpoint

    async def handle_jsonrpc(request: Request) -> Response:
        await normalize_v03_jsonrpc_version(request)
        return await jsonrpc_endpoint(request)

    routes.append(Route("/", handle_jsonrpc, methods=["POST"]))
    routes.extend(create_rest_routes(components.handler, enable_v0_3_compat=True))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(
        A2AAuthMiddleware,
        token=token,
        basic_username=basic_username,
        basic_password=basic_password,
        api_key=api_key,
        api_key_header=api_key_header,
    )
    return app


def _agent_card_etag(card: dict[str, object]) -> str:
    body = json.dumps(card, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f'"sha256-{hashlib.sha256(body).hexdigest()}"'


def run_server(
    *,
    host: str,
    port: int,
    token: str | None,
    model: str,
    basic_username: str | None,
    basic_password: str | None,
    api_key: str | None,
    api_key_header: str,
    persistence_dir: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    signing_secret: str | None = None,
    signing_key_id: str = "default",
    push_notifications: bool = False,
    transport: str = "http",
    socket_path: str | None = None,
    ws_path: str = "/a2a",
    grpc_host: str | None = None,
    grpc_port: int | None = None,
    redis_url: str | None = None,
    request_stream: str = "iac-code:a2a:requests",
    response_stream: str = "iac-code:a2a:responses",
    consumer_group: str = "iac-code",
    push_queue: str = "local-file",
    push_redis_url: str | None = None,
    push_stream: str = "iac-code:a2a:push",
    push_retry_key: str = "iac-code:a2a:push:retry",
    push_dead_stream: str = "iac-code:a2a:push:dead",
    push_consumer_group: str = "iac-code-push",
    push_consumer_name: str | None = None,
    push_lease_timeout_ms: int = 300_000,
    auto_approve_permissions: bool = False,
    thinking_exposure: object | None = None,
) -> None:
    from iac_code.a2a.transports.base import normalize_transport_name, validate_transport_for_platform

    normalized_transport = normalize_transport_name(transport)
    if persistence_dir is None:
        from iac_code.config import get_config_dir

        persistence_dir = get_config_dir() / "a2a"
    if artifact_dir is None:
        artifact_dir = Path(persistence_dir) / "artifacts"

    if normalized_transport == "unix" and not socket_path:
        raise RuntimeError("--socket-path is required for --transport unix.")
    if normalized_transport == "redis-streams" and not redis_url:
        raise RuntimeError("--redis-url is required for --transport redis-streams.")
    if push_queue == "redis-streams" and not push_redis_url:
        raise RuntimeError("--push-redis-url is required for --push-queue redis-streams.")

    validate_transport_for_platform(normalized_transport)

    supported_interfaces = _supported_interfaces(
        transport=normalized_transport,
        host=host,
        port=port,
        socket_path=socket_path,
        ws_path=ws_path,
        grpc_host=grpc_host,
        grpc_port=grpc_port,
        redis_url=redis_url,
        request_stream=request_stream,
        response_stream=response_stream,
        consumer_group=consumer_group,
    )

    from iac_code.a2a.transports.dispatcher import create_runtime_components

    common_kwargs = {
        "model": model,
        "host": host,
        "port": port,
        "token": token,
        "basic_username": basic_username,
        "basic_password": basic_password,
        "api_key": api_key,
        "api_key_header": api_key_header,
        "persistence_dir": persistence_dir,
        "artifact_dir": artifact_dir,
        "signing_secret": signing_secret,
        "signing_key_id": signing_key_id,
        "push_notifications": push_notifications,
        "push_queue": push_queue,
        "push_redis_url": push_redis_url,
        "push_stream": push_stream,
        "push_retry_key": push_retry_key,
        "push_dead_stream": push_dead_stream,
        "push_consumer_group": push_consumer_group,
        "push_consumer_name": push_consumer_name,
        "push_lease_timeout_ms": push_lease_timeout_ms,
        "supported_interfaces": supported_interfaces,
        "auto_approve_permissions": auto_approve_permissions,
        "thinking_exposure": thinking_exposure,
    }

    if normalized_transport == "stdio":
        from iac_code.a2a.transports.stdio import StdioA2AServer

        components = create_runtime_components(**common_kwargs)
        asyncio.run(_serve_async_transport(StdioA2AServer(components=components), components=components))
        return

    if normalized_transport == "unix":
        from iac_code.a2a.transports.unix import UnixA2AServer

        components = create_runtime_components(**common_kwargs)
        asyncio.run(
            _serve_async_transport(
                UnixA2AServer(components=components, socket_path=socket_path or ""),
                components=components,
            )
        )
        return

    if normalized_transport == "grpc":
        from iac_code.a2a.transports.grpc import GrpcA2AServer

        components = create_runtime_components(**common_kwargs)
        resolved_grpc_port = port if grpc_port is None else grpc_port
        asyncio.run(
            _serve_async_transport(
                GrpcA2AServer(components=components, host=grpc_host or host, port=resolved_grpc_port),
                components=components,
            )
        )
        return

    if normalized_transport == "grpc-jsonrpc":
        from iac_code.a2a.transports.grpc_jsonrpc import GrpcJsonRpcA2AServer

        components = create_runtime_components(**common_kwargs)
        resolved_grpc_port = port if grpc_port is None else grpc_port
        asyncio.run(
            _serve_async_transport(
                GrpcJsonRpcA2AServer(components=components, host=grpc_host or host, port=resolved_grpc_port),
                components=components,
            )
        )
        return

    if normalized_transport == "redis-streams":
        from iac_code.a2a.transports.redis_streams import RedisStreamsA2AServer, require_redis

        redis_module = require_redis()
        components = create_runtime_components(**common_kwargs)
        redis = redis_module.from_url(redis_url)
        asyncio.run(
            _serve_async_transport(
                RedisStreamsA2AServer(
                    redis=redis,
                    components=components,
                    request_stream=request_stream,
                    response_stream=response_stream,
                    consumer_group=consumer_group,
                ),
                components=components,
            )
        )
        return

    if normalized_transport == "websocket":
        from iac_code.a2a.transports.websocket import WebSocketA2AServerApp

        components = create_runtime_components(**common_kwargs)
        app = WebSocketA2AServerApp(components=components, path=ws_path).create_app()
    else:
        app = create_app(
            host=host,
            port=port,
            token=token,
            model=model,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
            persistence_dir=persistence_dir,
            artifact_dir=artifact_dir,
            signing_secret=signing_secret,
            signing_key_id=signing_key_id,
            push_notifications=push_notifications,
            push_queue=push_queue,
            push_redis_url=push_redis_url,
            push_stream=push_stream,
            push_retry_key=push_retry_key,
            push_dead_stream=push_dead_stream,
            push_consumer_group=push_consumer_group,
            push_consumer_name=push_consumer_name,
            push_lease_timeout_ms=push_lease_timeout_ms,
            supported_interfaces=supported_interfaces,
            auto_approve_permissions=auto_approve_permissions,
            thinking_exposure=thinking_exposure,
        )

    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("A2A server dependencies are missing. Install with: pip install 'iac-code[a2a]'") from exc

    uvicorn.run(
        app,
        host=host,
        port=port,
    )


async def _serve_async_transport(server, *, components) -> None:
    await components.task_store.start_cleanup_loop()
    push_worker_task: asyncio.Task[None] | None = None
    if components.push_worker is not None:
        push_worker_task = asyncio.create_task(components.push_worker.serve_forever())
        await asyncio.sleep(0)
    try:
        await server.serve()
    finally:
        if push_worker_task is not None:
            push_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await push_worker_task
        try:
            await server.aclose()
        finally:
            await components.aclose()


def _supported_interfaces(
    *,
    transport: str,
    host: str,
    port: int,
    socket_path: str | None,
    ws_path: str,
    grpc_host: str | None,
    grpc_port: int | None,
    redis_url: str | None,
    request_stream: str,
    response_stream: str,
    consumer_group: str,
) -> list[dict[str, str]] | None:
    if transport == "http":
        return [
            {"url": f"http://{host}:{port}/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": f"http://{host}:{port}", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
        ]
    if transport == "stdio":
        return [{"url": "stdio://iac-code", "protocolBinding": "stdio", "protocolVersion": "1.0"}]
    if transport == "unix" and socket_path:
        return [{"url": f"unix://{socket_path}", "protocolBinding": "unix", "protocolVersion": "1.0"}]
    if transport == "websocket":
        return [{"url": f"ws://{host}:{port}{ws_path}", "protocolBinding": "websocket", "protocolVersion": "1.0"}]
    if transport == "grpc":
        return [
            {
                "url": f"grpc://{grpc_host or host}:{port if grpc_port is None else grpc_port}",
                "protocolBinding": "grpc",
                "protocolVersion": "1.0",
            }
        ]
    if transport == "grpc-jsonrpc":
        return [
            {
                "url": f"grpc-jsonrpc://{grpc_host or host}:{port if grpc_port is None else grpc_port}",
                "protocolBinding": "grpc-jsonrpc",
                "protocolVersion": "1.0",
            }
        ]
    if transport == "redis-streams" and redis_url:
        return [
            {
                "url": f"redis-streams://{redis_url}/{request_stream}/{response_stream}/{consumer_group}",
                "protocolBinding": "redis-streams",
                "protocolVersion": "1.0",
            }
        ]
    return None
