import pytest

from iac_code.a2a.transport import A2ATransportBinding, UnsupportedA2ATransportError, ensure_supported_transport
from iac_code.a2a.transports.base import (
    A2ARuntimeTransport,
    TransportClientOptions,
    TransportServerOptions,
    TransportStreamEvent,
    binding_from_url,
    normalize_transport_name,
    select_binding,
)


def test_normalize_transport_name_accepts_all_runnable_bindings() -> None:
    assert normalize_transport_name("HTTP+JSONRPC") == "http"
    assert normalize_transport_name("JSONRPC") == "http"
    assert normalize_transport_name("stdio") == "stdio"
    assert normalize_transport_name("unix") == "unix"
    assert normalize_transport_name("websocket") == "websocket"
    assert normalize_transport_name("ws") == "websocket"
    assert normalize_transport_name("grpc") == "grpc"
    assert normalize_transport_name("grpc-jsonrpc") == "grpc-jsonrpc"
    assert normalize_transport_name("redis-streams") == "redis-streams"


def test_binding_from_url_derives_transport_name() -> None:
    assert binding_from_url("https://127.0.0.1:41242/").transport == "http"
    assert binding_from_url("stdio://iac-code").transport == "stdio"
    assert binding_from_url("unix:///tmp/iac-code.sock").transport == "unix"
    assert binding_from_url("wss://agent.example/a2a").transport == "websocket"
    assert binding_from_url("grpc://127.0.0.1:50051").transport == "grpc"
    assert binding_from_url("grpc-jsonrpc://127.0.0.1:50052").transport == "grpc-jsonrpc"
    assert binding_from_url("redis-streams://localhost/0/iac-code").transport == "redis-streams"


def test_select_binding_prefers_first_supported_binding() -> None:
    bindings = [
        A2ATransportBinding(url="nats://broker/iac-code", protocol_binding="nats"),
        A2ATransportBinding(url="unix:///tmp/iac-code.sock", protocol_binding="unix"),
    ]

    selected = select_binding(bindings)

    assert selected.url == "unix:///tmp/iac-code.sock"
    assert selected.transport == "unix"


def test_select_binding_fails_when_no_runnable_binding_exists() -> None:
    with pytest.raises(UnsupportedA2ATransportError, match="No runnable A2A transport"):
        select_binding([A2ATransportBinding(url="nats://broker/iac-code", protocol_binding="nats")])


def test_ensure_supported_transport_accepts_non_http_runtimes() -> None:
    binding = A2ATransportBinding(url="ws://127.0.0.1:41243/a2a", protocol_binding="websocket")

    assert ensure_supported_transport(binding).url == binding.url


def test_runtime_options_are_plain_data() -> None:
    server = TransportServerOptions(transport="unix", model="qwen3.6-plus", socket_path="/tmp/iac-code.sock")
    client = TransportClientOptions(binding=binding_from_url("unix:///tmp/iac-code.sock"))
    event = TransportStreamEvent(request_id="1", payload={"result": {"ok": True}}, final=True)

    assert server.transport == "unix"
    assert client.binding.transport == "unix"
    assert event.payload["result"]["ok"] is True


def test_runtime_transport_protocol_shape() -> None:
    assert hasattr(A2ARuntimeTransport, "create_server")
    assert hasattr(A2ARuntimeTransport, "create_client")
