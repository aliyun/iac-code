from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from types import MethodType
from typing import Any, AsyncIterable, AsyncIterator

from a2a.server.context import ServerCallContext
from jsonrpc.jsonrpc2 import JSONRPC20Response
from sse_starlette.sse import EventSourceResponse
from starlette.responses import Response

logger = logging.getLogger(__name__)


def install_jsonrpc_error_data_passthrough() -> None:
    try:
        from a2a.server.request_handlers import response_helpers
        from a2a.server.routes import jsonrpc_dispatcher
    except Exception:
        return
    current = response_helpers.build_error_response
    if getattr(current, "_iac_code_recoverable_data_passthrough", False):
        return
    original = current

    def build_error_response_with_passthrough(request_id: str | int | None, error: Any) -> dict[str, Any]:
        if getattr(error, "jsonrpc_error_data_passthrough", False):
            payload = {
                "code": int(getattr(error, "code", -32603)),
                "message": str(error),
            }
            data = getattr(error, "data", None)
            if data is not None:
                payload["data"] = data
            return JSONRPC20Response(error=payload, _id=request_id).data
        return original(request_id, error)

    setattr(build_error_response_with_passthrough, "_iac_code_recoverable_data_passthrough", True)
    setattr(response_helpers, "build_error_response", build_error_response_with_passthrough)
    setattr(jsonrpc_dispatcher, "build_error_response", build_error_response_with_passthrough)


def install_v03_jsonrpc_error_data_passthrough(jsonrpc_endpoint: Callable[..., Awaitable[Response]]) -> None:
    dispatcher = getattr(jsonrpc_endpoint, "__self__", None)
    adapter = getattr(dispatcher, "_v03_adapter", None)
    if adapter is None or getattr(adapter, "_iac_code_recoverable_error_passthrough", False):
        return

    try:
        from a2a.compat.v0_3 import types as types_v03
    except Exception:
        logger.debug("A2A v0.3 compatibility types are unavailable", exc_info=True)
        return

    async def _process_streaming_request_with_passthrough(
        self: Any,
        request_id: str | int | None,
        request_obj: Any,
        context: ServerCallContext,
    ) -> EventSourceResponse:
        method = request_obj.method
        if method == "message/stream":
            stream_gen = self.handler.on_message_send_stream(request_obj, context)
        elif method == "tasks/resubscribe":
            stream_gen = self.handler.on_subscribe_to_task(request_obj, context)
        else:
            raise ValueError(f"Unsupported streaming method {method}")

        async def event_generator(stream: AsyncIterable[Any]) -> AsyncIterator[dict[str, str]]:
            try:
                async for item in stream:
                    yield {"data": item.model_dump_json(by_alias=True, exclude_none=True)}
            except Exception as exc:
                logger.exception("Error during stream generation in v0.3 JSONRPCAdapter")
                if getattr(exc, "jsonrpc_error_data_passthrough", False):
                    error = types_v03.InvalidParamsError(message=str(exc), data=getattr(exc, "data", None))
                else:
                    error = types_v03.InternalError(message=str(exc))
                err_resp = types_v03.SendStreamingMessageResponse(
                    root=types_v03.JSONRPCErrorResponse(id=request_id, error=error)
                )
                yield {"data": err_resp.model_dump_json(by_alias=True, exclude_none=True)}

        return EventSourceResponse(event_generator(stream_gen))

    adapter._process_streaming_request = MethodType(_process_streaming_request_with_passthrough, adapter)
    adapter._iac_code_recoverable_error_passthrough = True
