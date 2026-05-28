from __future__ import annotations

from unittest.mock import patch

import pytest

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


class TestValidateTransportForPlatform:
    @patch("iac_code.a2a.transports.base.sys")
    def test_unix_on_windows_raises(self, mock_sys):
        from iac_code.a2a.transports.base import validate_transport_for_platform

        mock_sys.platform = "win32"
        with pytest.raises(RuntimeError, match="Unix domain socket transport is not supported on Windows"):
            validate_transport_for_platform("unix")

    @patch("iac_code.a2a.transports.base.sys")
    def test_unix_on_linux_passes(self, mock_sys):
        from iac_code.a2a.transports.base import validate_transport_for_platform

        mock_sys.platform = "linux"
        validate_transport_for_platform("unix")

    @patch("iac_code.a2a.transports.base.sys")
    def test_http_on_windows_passes(self, mock_sys):
        from iac_code.a2a.transports.base import validate_transport_for_platform

        mock_sys.platform = "win32"
        validate_transport_for_platform("http")
        validate_transport_for_platform("stdio")
