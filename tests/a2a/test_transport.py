from base64 import b64encode

import pytest

from iac_code.a2a.transport import (
    A2AAuthConfig,
    A2ATransportBinding,
    UnsupportedA2ATransportError,
    ensure_supported_transport,
    headers_for_auth,
    normalize_protocol_binding,
)


def test_normalize_protocol_binding_accepts_jsonrpc_aliases() -> None:
    assert normalize_protocol_binding("JSONRPC") == "jsonrpc"
    assert normalize_protocol_binding("json-rpc") == "jsonrpc"
    assert normalize_protocol_binding("HTTP+JSONRPC") == "jsonrpc"


def test_ensure_supported_transport_accepts_jsonrpc_http() -> None:
    binding = A2ATransportBinding(url="http://127.0.0.1:41242/", protocol_binding="JSONRPC")

    assert ensure_supported_transport(binding) is binding


def test_ensure_supported_transport_rejects_unknown_runtime() -> None:
    binding = A2ATransportBinding(url="nats://broker/iac-code", protocol_binding="nats")

    with pytest.raises(UnsupportedA2ATransportError, match="nats"):
        ensure_supported_transport(binding)


def test_headers_for_auth_combines_supported_http_auth() -> None:
    config = A2AAuthConfig(
        bearer_token="token-1",
        api_key="key-1",
        api_key_header="X-IAC-Code-Key",
    )

    assert headers_for_auth(config) == {
        "Authorization": "Bearer token-1",
        "X-IAC-Code-Key": "key-1",
    }


def test_headers_for_auth_supports_basic_auth_when_configured() -> None:
    config = A2AAuthConfig(basic_username="iac", basic_password="secret")

    assert headers_for_auth(config) == {"Authorization": "Basic " + b64encode(b"iac:secret").decode("ascii")}
