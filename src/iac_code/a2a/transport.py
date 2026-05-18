from __future__ import annotations

import base64
from dataclasses import dataclass


class UnsupportedA2ATransportError(ValueError):
    """Raised when a discovered protocol binding is not runnable by this client."""


@dataclass(frozen=True)
class A2ATransportBinding:
    url: str
    protocol_binding: str
    protocol_version: str | None = None

    @property
    def transport(self) -> str:
        from iac_code.a2a.transports.base import normalize_transport_name

        return normalize_transport_name(self.protocol_binding)


@dataclass(frozen=True)
class A2AAuthConfig:
    bearer_token: str | None = None
    api_key: str | None = None
    api_key_header: str = "X-API-Key"
    basic_username: str | None = None
    basic_password: str | None = None


def normalize_protocol_binding(value: str | None) -> str:
    from iac_code.a2a.transports.base import normalize_transport_name

    normalized = normalize_transport_name(value)
    return "jsonrpc" if normalized == "http" else normalized


def ensure_supported_transport(binding: A2ATransportBinding) -> A2ATransportBinding:
    from iac_code.a2a.transports.base import is_runnable_binding

    if is_runnable_binding(binding):
        return binding
    raise UnsupportedA2ATransportError(
        f"A2A protocol binding {binding.protocol_binding!r} at {binding.url!r} is not runnable."
    )


def headers_for_auth(config: A2AAuthConfig | None) -> dict[str, str]:
    if config is None:
        return {}
    headers: dict[str, str] = {}
    if config.bearer_token:
        headers["Authorization"] = f"Bearer {config.bearer_token}"
    elif config.basic_username and config.basic_password:
        raw = f"{config.basic_username}:{config.basic_password}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    if config.api_key:
        headers[config.api_key_header or "X-API-Key"] = config.api_key
    return headers
