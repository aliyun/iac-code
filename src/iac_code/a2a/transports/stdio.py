from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from typing import Any

from iac_code.a2a.transports.base import A2AFrameError
from iac_code.a2a.transports.dispatcher import A2AJsonRpcDispatcher, A2ARuntimeComponents


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
    return payload.get("method") in {"message/stream", "StreamMessage"}


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
            try:
                payload = decode_frame(line)
                if is_streaming_request(payload):
                    async for event in self._dispatcher.dispatch_stream(payload):
                        writer.write(encode_frame(event))
                        await writer.drain()
                else:
                    writer.write(encode_frame(await self._dispatcher.dispatch(payload)))
                    await writer.drain()
            except Exception as exc:
                writer.write(encode_frame(_error_response(None, str(exc))))
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


def _error_response(request_id: str | int | None, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": message}}


async def open_stdio_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
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
