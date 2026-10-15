"""Recovery conflicts preserve decision/execution state through JSON-RPC."""

from iac_code.a2a.executor import InputResponseExecutionControlConflictError
from iac_code.a2a.jsonrpc_passthrough import install_jsonrpc_error_data_passthrough


def test_permission_recovery_conflict_exposes_pending_execution_without_internal_error():
    install_jsonrpc_error_data_passthrough()
    error = InputResponseExecutionControlConflictError(
        "Permission recovery execution ownership unavailable: HANDOFF_NOT_COMMITTED",
        reason="HANDOFF_NOT_COMMITTED",
        decision_recorded=True,
    )
    # Resolve the installed helper so this exercises the wire response contract.
    from a2a.server.request_handlers import response_helpers

    response = response_helpers.build_error_response("reply-1", error)
    assert response["error"]["code"] == -32602
    assert response["error"]["data"] == {
        "kind": "permission_recovery_conflict",
        "reason": "HANDOFF_NOT_COMMITTED",
        "recoverable": True,
        "decisionRecorded": True,
        "executionResumed": False,
    }
