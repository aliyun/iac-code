from __future__ import annotations

from iac_code.a2a.transports.base import binding_from_url, normalize_transport_name


def test_grpc_binding_names_distinguish_official_and_jsonrpc_compatibility() -> None:
    assert normalize_transport_name("grpc") == "grpc"
    assert normalize_transport_name("grpcs") == "grpc"
    assert normalize_transport_name("grpc-jsonrpc") == "grpc-jsonrpc"
    assert normalize_transport_name("grpc+jsonrpc") == "grpc-jsonrpc"

    official = binding_from_url("grpc://127.0.0.1:41243")
    custom = binding_from_url("grpc-jsonrpc://127.0.0.1:41244")

    assert official.protocol_binding == "grpc"
    assert custom.protocol_binding == "grpc-jsonrpc"
