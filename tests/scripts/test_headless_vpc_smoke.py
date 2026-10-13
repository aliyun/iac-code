"""Offline failures retain the smoke acceptance and explain output shape."""

import json
import subprocess

import pytest

from scripts.ci import run_e2e
from scripts.headless.smoke import test_headless_vpc as smoke


@pytest.fixture(autouse=True)
def clear_diagnostics():
    smoke.OUTPUT_DIAGNOSTICS.clear()
    smoke.TEXT_DIAGNOSTICS.clear()
    yield
    smoke.OUTPUT_DIAGNOSTICS.clear()
    smoke.TEXT_DIAGNOSTICS.clear()


@pytest.mark.parametrize(("stdout", "exit_code", "stderr", "expected"), [
    (json.dumps({"text": "private-template", "usage": {"input_tokens": 5}}), 0, "",
     {"json_valid": True, "text_length": 16, "input_tokens_integer": True, "error_category": "none"}),
    (json.dumps({"text": "", "usage": {"input_tokens": 5}}), 0, "",
     {"json_valid": True, "text_length": 0, "input_tokens_integer": True}),
    (json.dumps({"text": "private-template", "usage": None}), 0, "",
     {"json_valid": True, "usage_object": False, "input_tokens_integer": False}),
    ("private-invalid-output", 0, "", {"json_valid": False, "parse_error_line": 1}),
    (json.dumps({"error": "RateLimitError: private-secret"}), 1, "",
     {"error_present": True, "error_category": "rate_limit"}),
])
def test_json_failure_diagnostics_preserve_acceptance(monkeypatch, stdout, exit_code, stderr, expected):
    monkeypatch.setattr(smoke, "run_headless", lambda _: subprocess.CompletedProcess([], exit_code, stdout, stderr))
    passed = smoke.test_json_output()
    detail = smoke.OUTPUT_DIAGNOSTICS["json"]
    assert detail["exit_code"] == exit_code
    for key, value in expected.items():
        assert detail[key] == value
    assert passed is (expected.get("text_length") == 16 and expected.get("input_tokens_integer") is True)
    exported = run_e2e._public_live_summary({"diagnostics": {"headless_output": smoke.OUTPUT_DIAGNOSTICS}})
    assert exported["diagnostics"]["headless_output"]["json"] == detail
    assert "private" not in json.dumps(exported)


@pytest.mark.parametrize(("events", "expected_pass", "failed_check"), [
    ([{"type": "thinking_delta", "text": "private-thought"},
      {"type": "text_delta", "text": "private-template"},
      {"type": "message_end", "stop_reason": "end_turn"}], True, None),
    ([{"type": "thinking_delta", "text": "private-thought"},
      {"type": "tool_result", "result": "private-result"},
      {"type": "message_end", "stop_reason": "end_turn"}], False, "contains text_delta events"),
    ([{"type": "text_delta", "text": "private-template"},
      {"type": "text_delta", "text": "private-template"},
      {"type": "text_delta", "text": "private-template"}], False, "contains message_end event"),
])
def test_stream_diagnostics_identify_missing_event_without_relaxing_acceptance(
    monkeypatch, events, expected_pass, failed_check,
):
    output = "\n".join(json.dumps(event) for event in events)
    monkeypatch.setattr(smoke, "run_headless", lambda _: subprocess.CompletedProcess([], 0, output, ""))
    assert smoke.test_stream_json_output() is expected_pass
    detail = smoke.OUTPUT_DIAGNOSTICS["stream-json"]
    if failed_check:
        assert detail["checks"][failed_check] is False
    assert detail["parse_error_count"] == 0
    exported = run_e2e._public_live_summary({"diagnostics": {"headless_output": smoke.OUTPUT_DIAGNOSTICS}})
    assert exported["diagnostics"]["headless_output"]["stream-json"] == detail
    assert "private" not in json.dumps(exported)


def test_public_output_diagnostics_reject_unstructured_content():
    public = run_e2e._public_live_summary({"diagnostics": {"headless_output": {
        "json": {"exit_code": -9, "stderr": "private-secret", "error_category": "private-error",
                 "text_length": "private-text", "json_valid": True,
                 "checks": {"text is non-empty": False, "private-check": True},
                 "event_counts": {"error": 1, "private-event": 1}},
        "private-format": {"private-data": "private-secret"},
    }}})
    assert public["diagnostics"]["headless_output"] == {
        "json": {"exit_code": -9, "json_valid": True,
                 "checks": {"text is non-empty": False}, "event_counts": {"error": 1}},
    }
    assert "private" not in json.dumps(public)
