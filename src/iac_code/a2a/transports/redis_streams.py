from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from iac_code.a2a.transports.base import A2ATransportDependencyError
from iac_code.a2a.transports.dispatcher import A2AJsonRpcDispatcher, A2ARuntimeComponents
from iac_code.a2a.transports.stdio import is_streaming_request


@dataclass(frozen=True)
class RedisStreamsMessage:
    entry_id: str
    correlation_id: str
    payload: dict[str, Any]
    final: bool


def require_redis() -> Any:
    try:
        return import_module("redis.asyncio")
    except ModuleNotFoundError as exc:
        raise A2ATransportDependencyError(
            "Redis Streams A2A transport requires optional dependencies. Install iac-code[a2a-redis]."
        ) from exc


def parse_redis_entry(entry_id: str, fields: Mapping[Any, Any]) -> RedisStreamsMessage:
    payload = _field_value(fields, "payload")
    correlation_id = _field_value(fields, "correlation_id")
    data = json.loads(_decode_field(payload))
    if not isinstance(data, dict):
        raise ValueError("Redis Streams A2A payload must be a JSON object")

    return RedisStreamsMessage(
        entry_id=entry_id,
        correlation_id=_decode_field(correlation_id),
        payload=data,
        final=_field_value(fields, "final") in {True, "true", "1", b"true", b"1"},
    )


class RedisStreamsA2AClient:
    def __init__(
        self,
        *,
        redis: Any,
        request_stream: str,
        response_stream: str,
        timeout_seconds: float,
        redis_factory: Callable[[], Any | Awaitable[Any]] | None = None,
    ) -> None:
        self._redis = redis
        self._redis_factory = redis_factory
        self._request_stream = request_stream
        self._response_stream = response_stream
        self._timeout_seconds = timeout_seconds

    async def send(self, payload: dict[str, Any], *, correlation_id: str | None = None) -> dict[str, Any]:
        correlation_id = correlation_id or str(uuid.uuid4())
        await self._write_request(payload, correlation_id=correlation_id)
        async for item in self._read_responses(correlation_id):
            return item.payload
        raise TimeoutError("Redis Streams A2A request timed out")

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        correlation_id = str(uuid.uuid4())
        await self._write_request(payload, correlation_id=correlation_id)
        async for item in self._read_responses(correlation_id):
            yield item.payload
            if item.final:
                break

    async def aclose(self) -> None:
        close = getattr(self._redis, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def _write_request(self, payload: dict[str, Any], *, correlation_id: str) -> None:
        await self._redis.xadd(
            self._request_stream,
            {
                "correlation_id": correlation_id,
                "reply_stream": self._response_stream,
                "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        )

    async def _read_responses(self, correlation_id: str) -> AsyncIterator[RedisStreamsMessage]:
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        # Start from "0-0" instead of "$" to avoid a race where the server
        # responds before the first xread call.  After each batch we advance
        # the cursor to the last entry ID we saw, which is the standard
        # Redis Streams consumer pattern.
        last_id = "0-0"
        consecutive_errors = 0
        max_consecutive_errors = 3
        while asyncio.get_running_loop().time() < deadline:
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                rows = await asyncio.wait_for(
                    self._redis.xread({self._response_stream: last_id}, count=10, block=100),
                    timeout=max(0.001, min(0.2, remaining)),
                )
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                if self._redis_factory is not None:
                    await self._reconnect()
                    consecutive_errors = 0
                    continue
                if consecutive_errors >= max_consecutive_errors:
                    raise TimeoutError(
                        f"Redis Streams connection failed after {max_consecutive_errors} consecutive errors"
                    ) from exc
                await asyncio.sleep(min(1.0, 0.5 * consecutive_errors))
                continue
            for _stream, entries in rows:
                for entry_id, fields in entries:
                    # Advance cursor past this entry so we don't re-read it.
                    last_id = entry_id if isinstance(entry_id, str) else entry_id.decode("utf-8")
                    message = parse_redis_entry(entry_id, fields)
                    if message.correlation_id == correlation_id:
                        yield message
                        if message.final:
                            return
            await asyncio.sleep(0)

    async def _reconnect(self) -> None:
        close = getattr(self._redis, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        if self._redis_factory is None:
            return
        redis = self._redis_factory()
        if inspect.isawaitable(redis):
            redis = await redis
        self._redis = redis


class RedisStreamsA2AServer:
    def __init__(
        self,
        *,
        redis: Any,
        components: A2ARuntimeComponents,
        request_stream: str,
        response_stream: str,
        consumer_group: str,
        consumer_name: str = "",
    ) -> None:
        self._redis = redis
        self._components = components
        self._dispatcher = A2AJsonRpcDispatcher(components)
        self._request_stream = request_stream
        self._response_stream = response_stream
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name or f"consumer-{uuid.uuid4().hex[:12]}"
        self._group_ready = False
        self._closed = False

    async def serve(self) -> None:
        while not self._closed:
            processed = await self.serve_once()
            if not processed:
                await asyncio.sleep(0)

    async def serve_once(self) -> bool:
        await self._ensure_group()
        rows = await self._redis.xreadgroup(
            self._consumer_group,
            self._consumer_name,
            {self._request_stream: ">"},
            count=1,
            block=100,
        )
        if not rows:
            return False

        processed = False
        for _stream, entries in rows:
            for entry_id, fields in entries:
                await self._process_entry(entry_id, fields)
                processed = True
        return processed

    async def aclose(self) -> None:
        self._closed = True
        dispatcher_close = getattr(self._dispatcher, "aclose", None)
        if dispatcher_close is not None:
            await dispatcher_close()
        close = getattr(self._redis, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        await self._components.aclose()

    async def _process_entry(self, entry_id: str, fields: Mapping[Any, Any]) -> None:
        try:
            message = parse_redis_entry(entry_id, fields)
            reply_stream = self._reply_stream(fields)

            if is_streaming_request(message.payload):
                async for event in self._dispatcher.dispatch_stream(message.payload):
                    await self._write_response(
                        reply_stream,
                        correlation_id=message.correlation_id,
                        payload=event,
                        final=False,
                    )
                await self._write_response(
                    reply_stream,
                    correlation_id=message.correlation_id,
                    payload={"jsonrpc": "2.0", "id": message.payload.get("id")},
                    final=True,
                )
            else:
                response = await self._dispatcher.dispatch(message.payload)
                await self._write_response(
                    reply_stream,
                    correlation_id=message.correlation_id,
                    payload=response,
                    final=True,
                )
        finally:
            await self._ack(entry_id)

    def _reply_stream(self, fields: Mapping[Any, Any]) -> str:
        value = _field_value(fields, "reply_stream")
        return self._response_stream if value is None else _decode_field(value)

    async def _write_response(
        self,
        stream: str,
        *,
        correlation_id: str,
        payload: dict[str, Any],
        final: bool,
    ) -> None:
        await self._redis.xadd(
            stream,
            {
                "correlation_id": correlation_id,
                "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "final": "true" if final else "false",
            },
        )

    async def _ack(self, entry_id: str) -> None:
        xack = getattr(self._redis, "xack", None)
        if xack is None:
            return
        result = xack(self._request_stream, self._consumer_group, entry_id)
        if inspect.isawaitable(result):
            await result

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._redis.xgroup_create(self._request_stream, self._consumer_group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True


def _decode_field(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _field_value(fields: Mapping[Any, Any], name: str) -> Any:
    return fields.get(name, fields.get(name.encode("utf-8")))
