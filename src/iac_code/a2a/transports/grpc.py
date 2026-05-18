from __future__ import annotations

from typing import Any

from iac_code.a2a.transports.base import A2ATransportDependencyError
from iac_code.a2a.transports.dispatcher import A2ARuntimeComponents


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
        servicer = GrpcHandler(self._components.handler)
        a2a_pb2_grpc.add_A2AServiceServicer_to_server(servicer, self._server)
        self._server.add_insecure_port(f"{self._host}:{self._port}")
        await self._server.start()
        await self._server.wait_for_termination()

    async def aclose(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=1)
        if self._components is not None:
            await self._components.aclose()
