from __future__ import annotations

import importlib
from typing import Any


def test_importing_pipeline_executor_does_not_install_jsonrpc_passthrough(monkeypatch) -> None:
    from a2a.server.request_handlers import response_helpers
    from a2a.server.routes import jsonrpc_dispatcher

    def sentinel_build_error_response(request_id: str | int | None, error: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": getattr(error, "code", -32603)}}

    monkeypatch.setattr(response_helpers, "build_error_response", sentinel_build_error_response)
    monkeypatch.setattr(jsonrpc_dispatcher, "build_error_response", sentinel_build_error_response)

    import iac_code.a2a.pipeline_executor as pipeline_executor

    importlib.reload(pipeline_executor)

    assert response_helpers.build_error_response is sentinel_build_error_response
    assert jsonrpc_dispatcher.build_error_response is sentinel_build_error_response


def test_jsonrpc_passthrough_explicit_install_is_idempotent(monkeypatch) -> None:
    from a2a.server.request_handlers import response_helpers
    from a2a.server.routes import jsonrpc_dispatcher

    from iac_code.a2a.jsonrpc_passthrough import install_jsonrpc_error_data_passthrough

    def original_build_error_response(request_id: str | int | None, error: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": getattr(error, "code", -32603)}}

    class RecoverableError(Exception):
        code = -32602
        jsonrpc_error_data_passthrough = True
        data = {"recoverableTaskId": "task-owner"}

    monkeypatch.setattr(response_helpers, "build_error_response", original_build_error_response)
    monkeypatch.setattr(jsonrpc_dispatcher, "build_error_response", original_build_error_response)

    install_jsonrpc_error_data_passthrough()
    installed = response_helpers.build_error_response
    install_jsonrpc_error_data_passthrough()

    assert response_helpers.build_error_response is installed
    assert jsonrpc_dispatcher.build_error_response is installed
    response = installed("req-1", RecoverableError("Pipeline already running"))
    assert response["error"]["code"] == -32602
    assert response["error"]["data"] == {"recoverableTaskId": "task-owner"}
