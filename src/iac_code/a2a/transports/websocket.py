from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any

from starlette.applications import Starlette
from starlette.endpoints import WebSocketEndpoint
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from iac_code.a2a.transports.dispatcher import A2AJsonRpcDispatcher, A2ARuntimeComponents
from iac_code.a2a.transports.stdio import is_streaming_request

logger = logging.getLogger(__name__)


def websocket_event_frame(payload: dict[str, Any], *, final: bool) -> dict[str, Any]:
    return {"id": payload.get("id"), "payload": payload, "final": final}


def websocket_error_frame(request_id: Any, *, code: int, message: str) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    return websocket_event_frame(payload, final=True)


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    try:
        await websocket.send_json(payload)
    except (RuntimeError, WebSocketDisconnect):
        logger.debug("A2A WebSocket send failed; client disconnected", exc_info=True)
        return False
    return True


class WebSocketA2AServerApp:
    def __init__(self, *, components: A2ARuntimeComponents, path: str = "/a2a") -> None:
        self._components = components
        self._path = path

    def create_app(self) -> Starlette:
        components = self._components

        class A2AEndpoint(WebSocketEndpoint):
            encoding = "text"

            async def on_connect(self, websocket: WebSocket) -> None:
                await websocket.accept()
                self.dispatcher = A2AJsonRpcDispatcher(components)

            async def on_disconnect(self, websocket: WebSocket, close_code: int) -> None:
                await self.dispatcher.aclose()

            async def on_receive(self, websocket: WebSocket, data: str) -> None:
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    await _send_json(websocket, websocket_error_frame(None, code=-32700, message="Parse error"))
                    return
                if not isinstance(payload, dict):
                    await _send_json(websocket, websocket_error_frame(None, code=-32600, message="Invalid Request"))
                    return
                if is_streaming_request(payload):
                    async for event in self.dispatcher.dispatch_stream(payload):
                        if not await _send_json(websocket, websocket_event_frame(event, final=False)):
                            return
                    final_payload = {"jsonrpc": "2.0", "id": payload.get("id")}
                    await _send_json(websocket, websocket_event_frame(final_payload, final=True))
                    return

                response = await self.dispatcher.dispatch(payload)
                await _send_json(websocket, websocket_event_frame(response, final=True))

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

        return Starlette(routes=[WebSocketRoute(self._path, A2AEndpoint)], lifespan=lifespan)
