from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from collections.abc import AsyncIterator
from typing import Any

from iac_code.a2a.transports.base import A2AFrameError
from iac_code.a2a.transports.dispatcher import A2AJsonRpcDispatcher, A2ARuntimeComponents
from iac_code.utils.public_errors import public_error_from_exception

logger = logging.getLogger(__name__)


def encode_frame(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_frame(line: bytes | str) -> dict[str, Any]:
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise A2AFrameError(f"Invalid JSON-RPC frame: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise A2AFrameError("A2A frame must decode to a JSON object")
    return payload


def is_streaming_request(payload: dict[str, Any]) -> bool:
    return payload.get("method") in {"message/stream", "StreamMessage", "SendStreamingMessage"}


class StdioA2AServer:
    def __init__(
        self,
        *,
        components: A2ARuntimeComponents,
        reader: asyncio.StreamReader | None = None,
        writer: Any | None = None,
    ) -> None:
        self._components = components
        self._dispatcher = A2AJsonRpcDispatcher(components)
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def serve(self) -> None:
        reader = self._reader
        writer = self._writer
        if reader is None or writer is None:
            reader, writer = await open_stdio_streams()
        while not self._closed:
            line = await reader.readline()
            if not line:
                break
            request_id: str | int | None = None
            streaming_request = False
            try:
                payload = decode_frame(line)
                raw_request_id = payload.get("id")
                request_id = (
                    raw_request_id if isinstance(raw_request_id, (str, int)) or raw_request_id is None else None
                )
                streaming_request = is_streaming_request(payload)
                if streaming_request:
                    async for event in self._dispatcher.dispatch_stream(payload):
                        writer.write(encode_frame(event))
                        await writer.drain()
                    writer.write(encode_frame({"jsonrpc": "2.0", "id": request_id, "final": True}))
                    await writer.drain()
                else:
                    writer.write(encode_frame(await self._dispatcher.dispatch(payload)))
                    await writer.drain()
            except Exception as exc:
                logger.exception("A2A stdio transport request failed")
                failure = public_error_from_exception(exc)
                response = _error_response(request_id, failure.summary, error_id=failure.error_id)
                if streaming_request:
                    response["final"] = True
                writer.write(encode_frame(response))
                await writer.drain()

    async def aclose(self) -> None:
        self._closed = True
        await self._components.aclose()


class StdioA2AClient:
    def __init__(self, *, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._writer.write(encode_frame(payload))
        await self._writer.drain()
        return decode_frame(await self._reader.readline())

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        self._writer.write(encode_frame(payload))
        await self._writer.drain()
        while True:
            response = decode_frame(await self._reader.readline())
            yield response
            if response.get("final") is True or response.get("result", {}).get("final") is True:
                break

    async def aclose(self) -> None:
        close = getattr(self._writer, "close", None)
        if close is not None:
            close()
        wait_closed = getattr(self._writer, "wait_closed", None)
        if wait_closed is not None:
            await wait_closed()


def _error_response(request_id: str | int | None, message: str, *, error_id: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": -32603, "message": message}
    if error_id is not None:
        error["data"] = {"error_id": error_id}
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


async def open_stdio_streams() -> tuple[asyncio.StreamReader, Any]:
    if sys.platform == "win32":
        return await _open_stdio_streams_windows()
    return await _open_stdio_streams_unix()


async def _open_stdio_streams_unix() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader = asyncio.StreamReader()
    read_protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: read_protocol, sys.stdin.buffer)
    write_transport, write_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        sys.stdout.buffer,
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, reader, loop)
    return reader, writer


async def _open_stdio_streams_windows() -> tuple[asyncio.StreamReader, "_SyncStdoutWriter"]:
    """Windows ProactorEventLoop doesn't support connect_read_pipe on stdin.
    Use a daemon thread to read sys.stdin.buffer and feed an asyncio.StreamReader.
    Write side is a synchronous wrapper — Windows stdout writes are blocking
    and thread-safe so no event-loop integration is needed."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()

    def _stdin_thread() -> None:
        while True:
            try:
                chunk = sys.stdin.buffer.read(4096)
            except (OSError, ValueError):
                try:
                    loop.call_soon_threadsafe(reader.feed_eof)
                except RuntimeError:
                    pass
                return
            try:
                if not chunk:
                    loop.call_soon_threadsafe(reader.feed_eof)
                    return
                loop.call_soon_threadsafe(reader.feed_data, chunk)
            except RuntimeError:
                return

    threading.Thread(target=_stdin_thread, daemon=True, name="stdio-stdin-reader").start()
    writer = _SyncStdoutWriter(sys.stdout.buffer)
    return reader, writer


class _SyncStdoutWriter:
    """Minimal StreamWriter-compatible wrapper for Windows stdout."""

    def __init__(self, buffer: Any) -> None:
        self._buffer = buffer
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        self._buffer.write(data)
        self._buffer.flush()

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None
