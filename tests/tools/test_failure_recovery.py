"""Tests for the terminal tool-failure ledger."""

from __future__ import annotations

from iac_code.tools.base import ToolResult
from iac_code.tools.failure_recovery import (
    TERMINAL_FAILURE_METADATA_KEY,
    TerminalFailureLedger,
    mark_terminal_failure,
    take_terminal_failure,
    terminal_failure_signature,
)


def test_signature_includes_the_business_error_code_when_present():
    assert terminal_failure_signature(status=400, code="ResourceTypeNotFound") == "http_400:ResourceTypeNotFound"
    assert terminal_failure_signature(status=404) == "http_404"


def test_marking_preserves_existing_metadata():
    result = ToolResult.error("rejected")
    result.metadata = {"aliyun_http": {"status": 400}}

    mark_terminal_failure(result, "http_400")

    assert result.metadata == {"aliyun_http": {"status": 400}, TERMINAL_FAILURE_METADATA_KEY: "http_400"}


def test_taking_the_marker_removes_it_and_drops_empty_metadata():
    result = mark_terminal_failure(ToolResult.error("rejected"), "http_400")

    assert take_terminal_failure(result) == "http_400"
    assert result.metadata is None
    assert take_terminal_failure(result) is None


def test_taking_the_marker_keeps_other_metadata():
    result = ToolResult.error("rejected")
    result.metadata = {"aliyun_http": {"status": 400}}
    mark_terminal_failure(result, "http_400")

    assert take_terminal_failure(result) == "http_400"
    assert result.metadata == {"aliyun_http": {"status": 400}}


def test_ledger_records_and_looks_up_per_tool_and_input():
    ledger = TerminalFailureLedger()
    ledger.record(tool_name="aliyun_api", canonical_input_sha256="a" * 64, signature="http_400")

    assert ledger.lookup(tool_name="aliyun_api", canonical_input_sha256="a" * 64) == "http_400"
    assert ledger.lookup(tool_name="aliyun_api", canonical_input_sha256="b" * 64) is None
    assert ledger.lookup(tool_name="ros_stack", canonical_input_sha256="a" * 64) is None


def test_ledger_evicts_the_least_recently_used_entry_when_full():
    ledger = TerminalFailureLedger(max_entries=2)
    ledger.record(tool_name="aliyun_api", canonical_input_sha256="a" * 64, signature="http_400")
    ledger.record(tool_name="aliyun_api", canonical_input_sha256="b" * 64, signature="http_403")
    assert ledger.lookup(tool_name="aliyun_api", canonical_input_sha256="a" * 64) == "http_400"

    ledger.record(tool_name="aliyun_api", canonical_input_sha256="c" * 64, signature="http_404")

    assert ledger.lookup(tool_name="aliyun_api", canonical_input_sha256="b" * 64) is None
    assert ledger.lookup(tool_name="aliyun_api", canonical_input_sha256="a" * 64) == "http_400"
    assert ledger.lookup(tool_name="aliyun_api", canonical_input_sha256="c" * 64) == "http_404"
