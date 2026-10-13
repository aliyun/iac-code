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


@pytest.mark.asyncio
async def test_smoke_command_allows_template_file_without_allowing_cloud_mutation(monkeypatch, tmp_path):
    from iac_code.services.permissions.loader import parse_cli_permission_mode
    from iac_code.services.permissions.pipeline import check_tool_permission
    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
    from iac_code.tools.write_file import WriteFileTool
    from iac_code.types.permissions import ToolPermissionContext

    commands = []

    def capture(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(smoke.subprocess, "run", capture)
    smoke.run_headless("json")
    command = commands[0]
    mode = parse_cli_permission_mode(command[command.index("--permission-mode") + 1])
    allow = command[command.index("--allowed-tools") + 1].split(",")
    context = ToolPermissionContext(mode=mode, cwd=str(tmp_path), allow_rules={"cli_arg": allow},
                                    deny_rules={}, ask_rules={})
    generated_file = {"file_path": str(tmp_path / "template.json"), "content": "{}"}
    old_context = ToolPermissionContext(mode=mode, cwd=str(tmp_path), allow_rules={}, deny_rules={}, ask_rules={})
    assert (await check_tool_permission(WriteFileTool(), generated_file, old_context)).behavior == "deny"
    assert (await check_tool_permission(WriteFileTool(), generated_file, context)).behavior == "allow"
    denied = await check_tool_permission(AliyunApi(), {"product": "ros", "action": "CreateStack",
                                                     "params": {"StackName": "test"}}, context)
    assert denied.behavior == "deny"


def test_tool_denials_are_counted_without_exporting_result_content():
    payload = {"text": "", "usage": {"input_tokens": 1}, "tool_uses": [
        {"is_error": True, "result": "Permission denied: private-path"},
    ]}
    smoke._record_output_diagnostics("json", subprocess.CompletedProcess([], 2, json.dumps(payload), ""))
    detail = smoke.OUTPUT_DIAGNOSTICS["json"]
    assert detail["tool_error_count"] == detail["permission_denial_count"] == 1
    assert detail["text_length"] == 0 and detail["exit_code"] == 2
    exported = run_e2e._public_live_summary({"diagnostics": {"headless_output": smoke.OUTPUT_DIAGNOSTICS}})
    assert exported["diagnostics"]["headless_output"]["json"] == detail
    assert "private" not in json.dumps(exported)
