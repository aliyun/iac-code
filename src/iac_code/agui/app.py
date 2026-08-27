"""Starlette application exposing an A2A-backed AG-UI POST/SSE endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import math
import os
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

from ag_ui.encoder import EventEncoder
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from iac_code import __version__
from iac_code.agui.adapter import AguiA2AAdapter, RunTicket
from iac_code.agui.errors import AdmissionError, normalize_agui_language, translate_agui_error
from iac_code.agui.inputs import MAX_REQUEST_BYTES, canonical_digest, parse_run_input
from iac_code.i18n import resolve_ui_language, translate_message

_HEARTBEAT_SECONDS = 15.0


def create_app(
    *,
    adapter: AguiA2AAdapter | None = None,
    a2a_url: str = "http://127.0.0.1:41242/",
    a2a_client: Any | None = None,
    auth_token: str | None = None,
    interrupt_ttl: int = 540,
    state_dir: str | Path | None = None,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    idle_shutdown: float = 0,
    request_shutdown: Callable[[], None] | None = None,
) -> Starlette:
    if idle_shutdown > 0 and request_shutdown is None:
        raise ValueError("idle_shutdown requires a server shutdown callback")
    run_adapter = adapter or AguiA2AAdapter(
        a2a_url=a2a_url,
        client=a2a_client,
        interrupt_ttl=interrupt_ttl,
        state_dir=state_dir,
    )
    owns_adapter = adapter is None
    expected_token = auth_token if auth_token is not None else os.environ.get("IAC_CODE_AGUI_AUTH_TOKEN")

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "protocol": "ag-ui",
                "protocolPackageVersion": "0.1.20",
                "executionKernel": "a2a-1.0",
                "serverVersion": __version__,
            }
        )

    async def run(request: Request) -> Response:
        language = _request_language(request)
        auth_error = _authorize(request, expected_token, language=language)
        if auth_error is not None:
            return auth_error
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _json_error(
                415,
                "UNSUPPORTED_MEDIA_TYPE",
                translate_message("Content-Type must be application/json.", language=language),
            )
        try:
            body = await _bounded_body(request, max_request_bytes)
        except _BodyTooLargeError:
            return _json_error(
                413,
                "REQUEST_TOO_LARGE",
                translate_message("The AG-UI request body is too large.", language=language),
            )
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_error(
                400,
                "INVALID_JSON",
                translate_message("The request body is not valid JSON.", language=language),
            )
        if not isinstance(payload, dict):
            return _json_error(
                400,
                "INVALID_INPUT",
                translate_message("RunAgentInput must be a JSON object.", language=language),
            )
        language = _payload_language(payload, fallback=language)
        try:
            run_input = parse_run_input(payload)
        except ValueError:
            return _json_error(
                400,
                "INVALID_INPUT",
                translate_message("Invalid AG-UI RunAgentInput envelope.", language=language),
            )
        try:
            ticket = await run_adapter.admit(
                run_input,
                canonical_digest(payload),
                preferred_language=language,
            )
        except AdmissionError as exc:
            return _json_error(
                exc.status_code,
                exc.code,
                translate_agui_error(exc.message, language=language),
            )
        except Exception:
            return _json_error(
                400,
                "INVALID_INPUT",
                translate_message("Invalid iac-code forwarded properties.", language=language),
            )
        return StreamingResponse(
            _event_stream(run_adapter, ticket),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async def cancel(request: Request) -> Response:
        language = _request_language(request)
        auth_error = _authorize(request, expected_token, language=language)
        if auth_error is not None:
            return auth_error
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _json_error(
                415,
                "UNSUPPORTED_MEDIA_TYPE",
                translate_message("Content-Type must be application/json.", language=language),
            )
        try:
            body = await _bounded_body(request, min(max_request_bytes, 64 * 1024))
            payload = json.loads(body)
        except _BodyTooLargeError:
            return _json_error(
                413,
                "REQUEST_TOO_LARGE",
                translate_message("The cancel request body is too large.", language=language),
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_error(
                400,
                "INVALID_JSON",
                translate_message("The cancel request body is not valid JSON.", language=language),
            )
        if not isinstance(payload, dict):
            return _json_error(
                400,
                "INVALID_INPUT",
                translate_message("The cancel request must be a JSON object.", language=language),
            )
        thread_id = payload.get("threadId")
        ros_invocation_id = payload.get("rosInvocationId")
        if not all(isinstance(value, str) and value for value in (thread_id, ros_invocation_id)):
            return _json_error(
                400,
                "INVALID_INPUT",
                translate_message("threadId and rosInvocationId are required.", language=language),
            )
        status = await run_adapter.cancel(
            request.path_params["execution_id"],
            thread_id=thread_id,
            ros_invocation_id=ros_invocation_id,
        )
        if status == "not_found":
            return _json_error(
                404,
                "EXECUTION_NOT_FOUND",
                translate_message("The execution was not found.", language=language),
            )
        return JSONResponse({"executionId": request.path_params["execution_id"], "status": status})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> Any:
        monitor: asyncio.Task[None] | None = None
        await run_adapter.start()
        if idle_shutdown > 0:
            assert request_shutdown is not None
            monitor = asyncio.create_task(
                _monitor_idle(run_adapter, idle_shutdown, request_shutdown),
                name="agui-idle-shutdown",
            )
        try:
            yield
        finally:
            if monitor is not None:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
            if owns_adapter:
                await run_adapter.aclose()

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/", run, methods=["POST"]),
            Route(
                "/extensions/iac-code/v1/executions/{execution_id:str}/cancel",
                cancel,
                methods=["POST"],
            ),
        ],
        lifespan=lifespan,
    )
    app.state.run_adapter = run_adapter
    return app


async def _event_stream(adapter: AguiA2AAdapter, ticket: RunTicket) -> AsyncIterator[str]:
    encoder = EventEncoder()
    iterator = adapter.stream(ticket).__aiter__()
    next_item: asyncio.Task[Any] | None = None

    async def next_event() -> Any:
        return await anext(iterator)

    try:
        while True:
            if next_item is None:
                next_item = asyncio.create_task(next_event())
            done, _ = await asyncio.wait({next_item}, timeout=_HEARTBEAT_SECONDS)
            if not done:
                yield ": heartbeat\n\n"
                continue
            try:
                item = next_item.result()
            except StopAsyncIteration:
                return
            next_item = None
            yield encoder.encode(item)
    finally:
        if next_item is not None and not next_item.done():
            next_item.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await next_item
        close_iterator = getattr(iterator, "aclose", None)
        if close_iterator is not None:
            with contextlib.suppress(Exception):
                await close_iterator()
        if not ticket.completed:
            await adapter.disconnect(ticket)


def _authorize(request: Request, expected_token: str | None, *, language: str) -> JSONResponse | None:
    if not expected_token:
        return None
    header = request.headers.get("authorization", "")
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not hmac.compare_digest(token, expected_token):
        return _json_error(
            401,
            "UNAUTHORIZED",
            translate_message("A valid bearer token is required.", language=language),
        )
    return None


class _BodyTooLargeError(Exception):
    pass


async def _bounded_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise _BodyTooLargeError
        except ValueError:
            pass
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _BodyTooLargeError
    return bytes(body)


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def _request_language(request: Request) -> str:
    fallback = resolve_ui_language(None)
    candidates: list[tuple[float, int, str]] = []
    for index, preference in enumerate(request.headers.get("accept-language", "").split(",")):
        segments = [segment.strip() for segment in preference.split(";")]
        value = segments[0]
        quality = 1.0
        for parameter in segments[1:]:
            name, separator, raw_quality = parameter.partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    quality = float(raw_quality.strip())
                except ValueError:
                    quality = 0.0
                break
        if not math.isfinite(quality) or quality <= 0 or quality > 1:
            continue
        language = fallback if value == "*" else normalize_agui_language(value, fallback="")
        if language:
            candidates.append((quality, -index, language))
    if candidates:
        return max(candidates)[2]
    return fallback


def _payload_language(payload: Mapping[str, Any], *, fallback: str) -> str:
    forwarded_props = payload.get("forwardedProps")
    if not isinstance(forwarded_props, Mapping):
        return fallback
    iac_code = forwarded_props.get("iacCode")
    if not isinstance(iac_code, Mapping):
        return fallback
    return normalize_agui_language(iac_code.get("preferredLanguage"), fallback=fallback)


async def _monitor_idle(
    adapter: AguiA2AAdapter,
    idle_seconds: float,
    request_shutdown: Callable[[], None],
) -> None:
    interval = min(5.0, max(0.1, idle_seconds / 4))
    while True:
        await asyncio.sleep(interval)
        if adapter.is_idle and asyncio.get_running_loop().time() - adapter.last_activity >= idle_seconds:
            request_shutdown()
            return
