from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from iac_code.a2a.transport import A2ATransportBinding, UnsupportedA2ATransportError
from iac_code.i18n import _

SUPPORTED_TRANSPORTS = frozenset({"http", "stdio", "unix", "websocket", "grpc", "grpc-jsonrpc", "redis-streams"})


class A2ATransportConfigError(ValueError):
    """Raised when a runnable transport is missing required runtime configuration."""


class A2ATransportDependencyError(RuntimeError):
    """Raised when an optional transport dependency is not installed."""


class A2AFrameError(ValueError):
    """Raised when a transport frame cannot be decoded as an A2A JSON-RPC message."""


@dataclass(frozen=True)
class TransportStreamEvent:
    request_id: str | int | None
    payload: dict[str, Any]
    final: bool = False


@dataclass(frozen=True)
class TransportServerOptions:
    transport: str
    model: str
    host: str = "127.0.0.1"
    port: int = 41242
    token: str | None = None
    basic_username: str | None = None
    basic_password: str | None = None
    api_key: str | None = None
    api_key_header: str = "X-API-Key"
    persistence_dir: str | None = None
    artifact_dir: str | None = None
    signing_secret: str | None = None
    signing_key_id: str = "default"
    push_notifications: bool = False
    socket_path: str | None = None
    ws_path: str = "/a2a"
    grpc_host: str | None = None
    grpc_port: int | None = None
    redis_url: str | None = None
    request_stream: str = "iac-code:a2a:requests"
    response_stream: str = "iac-code:a2a:responses"
    consumer_group: str = "iac-code"


@dataclass(frozen=True)
class TransportClientOptions:
    binding: A2ATransportBinding
    token: str | None = None
    basic_username: str | None = None
    basic_password: str | None = None
    api_key: str | None = None
    api_key_header: str = "X-API-Key"
    command: list[str] | None = None
    redis_url: str | None = None
    request_stream: str = "iac-code:a2a:requests"
    response_stream: str = "iac-code:a2a:responses"
    timeout_seconds: float = 30.0


class A2ATransportServer(Protocol):
    async def serve(self) -> None:
        """Run the transport server until cancelled or shut down."""

    async def aclose(self) -> None:
        """Close listener resources."""


class A2ATransportClient(Protocol):
    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one unary JSON-RPC request."""

    def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Send one streaming JSON-RPC request and yield response/event payloads."""

    async def aclose(self) -> None:
        """Close client resources."""


class A2ARuntimeTransport(Protocol):
    name: str

    def create_server(self, options: TransportServerOptions) -> A2ATransportServer:
        """Create a server for this transport."""

    def create_client(self, options: TransportClientOptions) -> A2ATransportClient:
        """Create a client for this transport."""


def normalize_transport_name(value: str | None) -> str:
    normalized = (value or "http").strip().lower().replace("_", "-")
    aliases = {
        "jsonrpc": "http",
        "json-rpc": "http",
        "http+jsonrpc": "http",
        "http-jsonrpc": "http",
        "https": "http",
        "grpcs": "grpc",
        "grpc-jsonrpc": "grpc-jsonrpc",
        "grpc+jsonrpc": "grpc-jsonrpc",
        "ws": "websocket",
        "wss": "websocket",
        "redis": "redis-streams",
        "redis-stream": "redis-streams",
    }
    return aliases.get(normalized, normalized)


def binding_from_url(url: str, *, protocol_version: str | None = None) -> A2ATransportBinding:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    transport = normalize_transport_name(scheme)
    return A2ATransportBinding(url=url, protocol_binding=transport, protocol_version=protocol_version)


def is_runnable_binding(binding: A2ATransportBinding) -> bool:
    transport = normalize_transport_name(binding.protocol_binding)
    if transport == "http":
        return binding.url.startswith(("http://", "https://"))
    if transport == "websocket":
        return binding.url.startswith(("ws://", "wss://"))
    if transport == "grpc":
        return binding.url.startswith(("grpc://", "grpcs://"))
    if transport == "grpc-jsonrpc":
        return binding.url.startswith(("grpc-jsonrpc://", "grpc+jsonrpc://"))
    if transport == "redis-streams":
        return binding.url.startswith("redis-streams://")
    if transport == "unix":
        return binding.url.startswith("unix://")
    if transport == "stdio":
        return binding.url.startswith("stdio://")
    return False


def select_binding(bindings: Sequence[A2ATransportBinding]) -> A2ATransportBinding:
    for binding in bindings:
        if is_runnable_binding(binding):
            transport = normalize_transport_name(binding.protocol_binding)
            return A2ATransportBinding(
                url=binding.url,
                protocol_binding=transport,
                protocol_version=binding.protocol_version,
            )
    names = ", ".join(binding.protocol_binding for binding in bindings) or "none"
    raise UnsupportedA2ATransportError(f"No runnable A2A transport found. Candidate bindings: {names}")


def validate_transport_supported(transport: str) -> None:
    """Raise ValueError if the transport name is not recognised."""
    if transport not in SUPPORTED_TRANSPORTS:
        supported = ", ".join(sorted(SUPPORTED_TRANSPORTS))
        raise ValueError(f"Unsupported transport '{transport}'. Supported values: {supported}")


def validate_transport_for_platform(transport: str) -> None:
    """Raise an error if the transport is unsupported or unavailable on the current platform."""
    validate_transport_supported(transport)
    if transport == "unix" and sys.platform == "win32":
        raise RuntimeError(
            _(
                "Unix domain socket transport is not supported on Windows. "
                "Use --transport http or --transport stdio instead."
            )
        )
