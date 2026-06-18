from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from iac_code.a2a.transports.base import A2ATransportDependencyError
from iac_code.a2a.transports.dispatcher import A2ARuntimeComponents
from iac_code.utils.public_errors import public_error_from_exception


@dataclass(frozen=True)
class JsonRpcEnvelope:
    payload: bytes
    final: bool = False


def require_grpc() -> Any:
    try:
        import grpc  # type: ignore[import-not-found]
    except ImportError as exc:
        raise A2ATransportDependencyError(
            "gRPC A2A transport requires optional dependencies. Install iac-code[a2a-grpc]."
        ) from exc
    return grpc


class GrpcA2AServer:
    def __init__(self, *, components: A2ARuntimeComponents | None, host: str, port: int) -> None:
        if not host or port < 0:
            raise ValueError("gRPC host and port are required.")
        self._components = components
        self._host = host
        self._port = port
        self._server: Any | None = None
        self._servicer: _JsonRpcServicer | None = None

    async def serve(self) -> None:
        grpc = require_grpc()

        try:
            pb2_grpc = import_module("iac_code.a2a.transports.proto.a2a_jsonrpc_pb2_grpc")
        except ModuleNotFoundError as exc:
            raise A2ATransportDependencyError(
                "gRPC JSON-RPC A2A compatibility transport requires generated protobuf bindings."
            ) from exc

        if self._components is None:
            raise ValueError("gRPC server requires runtime components.")

        self._server = grpc.aio.server()
        self._servicer = _JsonRpcServicer(self._components)
        pb2_grpc.add_A2AJsonRpcServicer_to_server(self._servicer, self._server)
        self._server.add_insecure_port(f"{self._host}:{self._port}")
        await self._server.start()
        await self._server.wait_for_termination()

    async def aclose(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=1)
        if self._servicer is not None:
            await self._servicer.aclose()
        if self._components is not None:
            await self._components.aclose()


class _JsonRpcServicer:
    def __init__(self, components: A2ARuntimeComponents) -> None:
        from iac_code.a2a.transports.dispatcher import A2AJsonRpcDispatcher

        self._dispatcher = A2AJsonRpcDispatcher(components)

    async def Send(self, request: JsonRpcEnvelope, context: Any) -> JsonRpcEnvelope:  # noqa: N802
        payload = _from_envelope(request)
        try:
            response = await self._dispatcher.dispatch(payload)
        except Exception as exc:
            return _to_envelope(_jsonrpc_public_error(payload.get("id"), exc))
        return _to_envelope(response)

    async def Stream(  # noqa: N802
        self,
        request: JsonRpcEnvelope,
        context: Any,
    ) -> AsyncIterator[JsonRpcEnvelope]:
        payload = _from_envelope(request)
        try:
            async for event in self._dispatcher.dispatch_stream(payload):
                yield _to_envelope(event)
            yield _to_envelope({"jsonrpc": "2.0", "id": payload.get("id")}, final=True)
        except asyncio.CancelledError:
            if _context_cancelled(context):
                return
            raise
        except Exception as exc:
            yield _to_envelope(_jsonrpc_public_error(payload.get("id"), exc), final=True)

    async def aclose(self) -> None:
        await self._dispatcher.aclose()


class GrpcA2AClient:
    def __init__(self, *, stub: Any) -> None:
        self._stub = stub

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._stub.Send(_to_envelope(payload))
        return _from_envelope(response)

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async for response in self._stub.Stream(_to_envelope(payload)):
            data = _from_envelope(response)
            if getattr(response, "final", False):
                data["final"] = True
            yield data

    async def aclose(self) -> None:
        close = getattr(self._stub, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


GrpcJsonRpcA2AServer = GrpcA2AServer
GrpcJsonRpcA2AClient = GrpcA2AClient


def _to_envelope(payload: dict[str, Any], *, final: bool = False, envelope_cls: Any = JsonRpcEnvelope) -> Any:
    return envelope_cls(
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), final=final
    )


def _jsonrpc_public_error(request_id: Any, exc: BaseException) -> dict[str, Any]:
    failure = public_error_from_exception(exc)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": -32603,
            "message": failure.summary,
            "data": {"error_id": failure.error_id},
        },
    }


def _from_envelope(envelope: JsonRpcEnvelope) -> dict[str, Any]:
    data = json.loads(envelope.payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("gRPC A2A envelope must contain a JSON object")
    return data


def _context_cancelled(context: Any) -> bool:
    cancelled = getattr(context, "cancelled", None)
    if cancelled is None:
        return False
    result = cancelled()
    return bool(result)
