from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from google.protobuf.json_format import MessageToDict

from iac_code.a2a.projection import (
    project_a2a_data,
    project_a2a_proto,
    resolve_a2a_public_path_roots_for_data,
)
from iac_code.a2a.transports.base import A2ATransportDependencyError
from iac_code.a2a.transports.dispatcher import A2ARuntimeComponents

_GRPC_REQUEST_DATA: ContextVar[Any] = ContextVar("iac_code_a2a_grpc_request_data", default=None)


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

    async def serve(self) -> None:
        grpc = require_grpc()
        try:
            from a2a.server.request_handlers.grpc_handler import GrpcHandler
            from a2a.types import a2a_pb2_grpc
        except ImportError as exc:
            raise A2ATransportDependencyError(
                "Official gRPC A2A transport requires optional dependencies. Install iac-code[a2a-grpc]."
            ) from exc

        if self._components is None:
            raise ValueError("gRPC server requires runtime components.")

        self._server = grpc.aio.server()
        servicer = _projecting_grpc_handler(GrpcHandler, self._components)
        a2a_pb2_grpc.add_A2AServiceServicer_to_server(servicer, self._server)
        self._server.add_insecure_port(f"{self._host}:{self._port}")
        await self._server.start()
        await self._server.wait_for_termination()

    async def aclose(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=1)
        if self._components is not None:
            await self._components.aclose()


def _projecting_grpc_handler(grpc_handler_type: type[Any], components: A2ARuntimeComponents) -> Any:
    class ProjectingGrpcHandler(grpc_handler_type):
        async def _handle_unary(
            self,
            request: Any,
            context: Any,
            handler_func: Any,
            default_response: Any,
        ) -> Any:
            request_data = MessageToDict(request, preserving_proto_field_name=False)
            token = _GRPC_REQUEST_DATA.set(request_data)
            try:
                response = await super()._handle_unary(request, context, handler_func, default_response)
                roots = await _grpc_roots(components, request_data, response)
                return project_a2a_proto(response, public_path_roots=roots)
            finally:
                _GRPC_REQUEST_DATA.reset(token)

        async def _handle_stream(self, request: Any, context: Any, handler_func: Any):
            request_data = MessageToDict(request, preserving_proto_field_name=False)
            token = _GRPC_REQUEST_DATA.set(request_data)
            try:
                async for response in super()._handle_stream(request, context, handler_func):
                    roots = await _grpc_roots(components, request_data, response)
                    yield project_a2a_proto(response, public_path_roots=roots)
            finally:
                _GRPC_REQUEST_DATA.reset(token)

        async def abort_context(self, error: Any, context: Any) -> None:
            request_data = _GRPC_REQUEST_DATA.get()
            roots = await _grpc_roots(components, request_data, None)
            projected = project_a2a_data(
                {"message": getattr(error, "message", str(error)), "data": getattr(error, "data", None)},
                public_path_roots=roots,
            )
            projected_error = type(error)(message=projected["message"], data=projected.get("data"))
            await super().abort_context(projected_error, context)

    return ProjectingGrpcHandler(components.handler)


async def _grpc_roots(components: A2ARuntimeComponents, request_data: Any, response: Any) -> list[dict[str, str]]:
    response_data = MessageToDict(response, preserving_proto_field_name=False) if response is not None else None
    return await resolve_a2a_public_path_roots_for_data(
        components.task_store,
        response_data=response_data,
        request_data=request_data,
        request_bare_id_is_task_id=True,
    )
