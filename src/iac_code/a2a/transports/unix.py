from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from iac_code.a2a.transports.dispatcher import A2ARuntimeComponents
from iac_code.a2a.transports.stdio import StdioA2AClient, StdioA2AServer


def validate_socket_path(socket_path: str) -> Path:
    path = Path(socket_path)
    if not path.parent.exists():
        raise ValueError(f"Unix socket parent does not exist: {path.parent}")
    return path


class UnixA2AServer:
    def __init__(self, *, components: A2ARuntimeComponents, socket_path: str) -> None:
        self._components = components
        self._socket_path = validate_socket_path(socket_path)
        self._server: asyncio.AbstractServer | None = None

    async def serve(self) -> None:
        if self._socket_path.exists():
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self._socket_path))
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server = StdioA2AServer(components=self._components, reader=reader, writer=writer)
        try:
            await server.serve()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        await self._components.aclose()


class UnixA2AClient:
    def __init__(self, *, socket_path: str) -> None:
        self._socket_path = validate_socket_path(socket_path)
        self._client: StdioA2AClient | None = None

    async def _connect(self) -> StdioA2AClient:
        if self._client is None:
            reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
            self._client = StdioA2AClient(reader=reader, writer=writer)
        return self._client

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await (await self._connect()).send(payload)

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async for item in (await self._connect()).stream(payload):
            yield item

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
