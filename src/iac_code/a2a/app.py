from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from email.utils import formatdate
from pathlib import Path
from time import time
from typing import Any, AsyncIterator, Awaitable, Callable, cast

from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.routes import create_jsonrpc_routes, create_rest_routes
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, SimpleUser
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from iac_code.a2a.agent_card import agent_card_to_client_dict
from iac_code.a2a.jsonrpc_passthrough import (
    install_jsonrpc_error_data_passthrough,
    install_v03_jsonrpc_error_data_passthrough,
)
from iac_code.a2a.projection import (
    project_a2a_data,
    project_a2a_text,
    resolve_a2a_public_path_roots,
    resolve_a2a_public_path_roots_for_data,
)
from iac_code.i18n import _

logger = logging.getLogger(__name__)
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
_MAX_AFTER_SEQUENCE_DIGITS = 20


class _PrincipalUser(User):
    def __init__(self, principal: str) -> None:
        self._principal = principal

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._principal


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


class A2AProjectionMiddleware(BaseHTTPMiddleware):
    """Apply the current A2A delivery policy at the HTTP wire boundary."""

    def __init__(self, app: Any, *, task_store: Any) -> None:
        super().__init__(app)
        self._task_store = task_store

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_data = await _a2a_request_data(request)
        response = await call_next(request)
        request_data = _with_a2a_path_parameters(request, request_data)
        if request.url.path in {"/health", AGENT_CARD_WELL_KNOWN_PATH}:
            return response

        content_type = response.headers.get("content-type", "").lower()
        streaming_response = cast(Any, response)
        if content_type.startswith("text/event-stream"):
            streaming_response.body_iterator = self._project_sse(
                streaming_response.body_iterator,
                request_data=request_data,
            )
            return response
        if "json" not in content_type:
            return response

        body = b"".join([_response_chunk_bytes(chunk) async for chunk in streaming_response.body_iterator])
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response(
                body,
                status_code=response.status_code,
                headers=_response_headers_without_length(response),
                background=response.background,
            )
        roots = await self._resolve_roots(data, request_data)
        projected = project_a2a_data(data, public_path_roots=roots)
        return Response(
            json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            status_code=response.status_code,
            headers=_response_headers_without_length(response),
            background=response.background,
        )

    async def _project_sse(self, body_iterator: Any, *, request_data: Any) -> AsyncIterator[bytes]:
        pending = b""
        async for chunk in body_iterator:
            pending += _response_chunk_bytes(chunk)
            while True:
                frame, separator, pending = _take_sse_frame(pending)
                if separator is None:
                    pending = frame
                    break
                yield await self._project_sse_frame(frame, request_data=request_data) + separator
        if pending:
            yield await self._project_sse_frame(pending, request_data=request_data)

    async def _project_sse_frame(self, frame: bytes, *, request_data: Any) -> bytes:
        output: list[bytes] = []
        for line in frame.splitlines(keepends=True):
            stripped = line.lstrip()
            if not stripped.startswith(b"data:"):
                output.append(line)
                continue
            prefix_length = len(line) - len(stripped)
            payload_with_ending = stripped.removeprefix(b"data:")
            ending = b""
            if payload_with_ending.endswith(b"\r\n"):
                payload_with_ending, ending = payload_with_ending[:-2], b"\r\n"
            elif payload_with_ending.endswith(b"\n"):
                payload_with_ending, ending = payload_with_ending[:-1], b"\n"
            try:
                data = json.loads(payload_with_ending.strip())
            except (UnicodeDecodeError, json.JSONDecodeError):
                output.append(line)
                continue
            roots = await self._resolve_roots(data, request_data)
            projected = project_a2a_data(data, public_path_roots=roots)
            encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            output.append(line[:prefix_length] + b"data: " + encoded + ending)
        return b"".join(output)

    async def _resolve_roots(self, response_data: Any, request_data: Any) -> list[dict[str, str]]:
        return await resolve_a2a_public_path_roots_for_data(
            self._task_store,
            response_data=response_data,
            request_data=request_data,
        )


async def _a2a_request_data(request: Request) -> Any:
    data: dict[str, Any] = dict(request.query_params)
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            data.update(body)
        elif body is not None:
            data["body"] = body
    return data


def _with_a2a_path_parameters(request: Request, request_data: Any) -> dict[str, Any]:
    """Add REST route identity after routing has populated ``path_params``."""

    data = dict(request_data) if isinstance(request_data, dict) else {"body": request_data}
    path_params = request.scope.get("path_params")
    if not isinstance(path_params, dict):
        return data
    task_id = path_params.get("id")
    if isinstance(task_id, str) and task_id and "/tasks/" in request.url.path:
        data.setdefault("taskId", task_id)
    context_id = path_params.get("context_id") or path_params.get("contextId")
    if isinstance(context_id, str) and context_id:
        data.setdefault("contextId", context_id)
    return data


def _response_chunk_bytes(chunk: Any) -> bytes:
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, memoryview):
        return chunk.tobytes()
    return str(chunk).encode("utf-8")


def _response_headers_without_length(response: Response) -> dict[str, str]:
    return {key: value for key, value in response.headers.items() if key.lower() != "content-length"}


def _take_sse_frame(data: bytes) -> tuple[bytes, bytes | None, bytes]:
    candidates = [(index, separator) for separator in (b"\r\n\r\n", b"\n\n") if (index := data.find(separator)) >= 0]
    if not candidates:
        return data, None, b""
    index, separator = min(candidates, key=lambda item: item[0])
    return data[:index], separator, data[index + len(separator) :]


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
    from iac_code.a2a.pipeline_recovery import A2APipelineRecoveryService

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

    recovery_service = A2APipelineRecoveryService(task_store=components.task_store)

    async def recovery_path_roots(*, context_id: str | None, task_id: str | None) -> list[dict[str, str]]:
        return await resolve_a2a_public_path_roots(
            components.task_store,
            task_id=task_id,
            context_id=context_id,
        )

    async def get_pipeline_state(request: Request) -> JSONResponse:
        context_id = request.query_params.get("contextId") or None
        task_id = request.query_params.get("taskId") or None
        if not context_id and not task_id:
            return JSONResponse({"error": _("contextId or taskId is required")}, status_code=400)

        after_sequence, parse_error = _parse_after_sequence(request.query_params.get("afterSequence"))
        if parse_error is not None:
            return JSONResponse({"error": parse_error}, status_code=400)

        call_context = _call_context_from_request(request)
        try:
            state = await recovery_service.get_state(
                context_id=context_id,
                task_id=task_id,
                after_sequence=after_sequence,
                call_context=call_context,
            )
        except ValueError as exc:
            roots = await recovery_path_roots(
                context_id=context_id,
                task_id=task_id,
            )
            return JSONResponse(
                {"error": project_a2a_text(str(exc), public_path_roots=roots)},
                status_code=404,
            )
        roots = await recovery_path_roots(
            context_id=context_id,
            task_id=task_id,
        )
        return JSONResponse(project_a2a_data(state, public_path_roots=roots))

    routes: list[BaseRoute] = [
        Route("/health", health, methods=["GET"]),
        Route(AGENT_CARD_WELL_KNOWN_PATH, get_agent_card, methods=["GET"]),
        Route("/iac-code/pipeline/state", get_pipeline_state, methods=["GET"]),
    ]
    install_jsonrpc_error_data_passthrough()
    jsonrpc_endpoint = create_jsonrpc_routes(components.handler, rpc_url="/", enable_v0_3_compat=True)[0].endpoint
    install_v03_jsonrpc_error_data_passthrough(jsonrpc_endpoint)

    async def handle_jsonrpc(request: Request) -> Response:
        await normalize_v03_jsonrpc_version(request)
        return await jsonrpc_endpoint(request)

    routes.append(Route("/", handle_jsonrpc, methods=["POST"]))
    routes.extend(create_rest_routes(components.handler, enable_v0_3_compat=True))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(A2AProjectionMiddleware, task_store=components.task_store)
    app.add_middleware(
        A2AAuthMiddleware,
        token=token,
        basic_username=basic_username,
        basic_password=basic_password,
        api_key=api_key,
        api_key_header=api_key_header,
    )
    return app


def _call_context_from_request(request: Request) -> ServerCallContext | None:
    user = request.scope.get("user")
    principal = getattr(user, "username", None) or getattr(user, "display_name", None)
    if not isinstance(principal, str) or not principal:
        return None
    return ServerCallContext(user=_PrincipalUser(principal))


def _agent_card_etag(card: dict[str, object]) -> str:
    body = json.dumps(card, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f'"sha256-{hashlib.sha256(body).hexdigest()}"'


def _parse_after_sequence(value: str | None) -> tuple[int | None, str | None]:
    if value is None or value == "":
        return None, None
    if len(value) > _MAX_AFTER_SEQUENCE_DIGITS:
        return None, _("afterSequence must be a non-negative integer")
    if value.isascii() and value.isdecimal():
        try:
            return int(value), None
        except ValueError:
            pass
    return None, _("afterSequence must be a non-negative integer")


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
