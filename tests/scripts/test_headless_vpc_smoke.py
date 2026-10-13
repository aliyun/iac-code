"""Offline failures retain the smoke acceptance and explain output shape."""

import asyncio
import io
import json
import subprocess
from types import SimpleNamespace

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
async def test_smoke_permission_flow_allows_local_work_and_rejects_cloud_writes(monkeypatch, tmp_path):
    from iac_code.cli.headless import HeadlessRunner
    from iac_code.cli.output_formats import OutputFormat
    from iac_code.services.permissions.loader import parse_cli_permission_mode
    from iac_code.services.permissions.pipeline import check_tool_permission
    from iac_code.tools.bash.bash_tool import BashTool
    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
    from iac_code.tools.write_file import WriteFileTool
    from iac_code.types.permissions import PermissionMode, ToolPermissionContext
    from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, Usage

    commands = []

    def capture(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(smoke.subprocess, "run", capture)
    smoke.run_headless("json")
    command = commands[0]
    mode = parse_cli_permission_mode(command[command.index("--permission-mode") + 1])
    context = ToolPermissionContext(mode=mode, cwd=str(tmp_path), allow_rules={}, deny_rules={}, ask_rules={})
    generated_file = {"file_path": str(tmp_path / "template.json"), "content": "{}"}
    old_context = ToolPermissionContext(mode=PermissionMode.DONT_ASK, cwd=str(tmp_path),
                                       allow_rules={}, deny_rules={}, ask_rules={})
    assert (await check_tool_permission(WriteFileTool(), generated_file, old_context)).behavior == "deny"
    local_file = await check_tool_permission(WriteFileTool(), generated_file, context)
    assert local_file.behavior == "ask"
    local_command = {"command": "mkdir generated"}
    assert (await check_tool_permission(BashTool(), local_command, old_context)).behavior == "deny"
    assert (await check_tool_permission(BashTool(), local_command, context)).behavior == "allow"
    cloud = await check_tool_permission(AliyunApi(), {"product": "ros", "action": "CreateStack",
                                                    "params": {"StackName": "test"}}, context)
    assert cloud.behavior == "ask" and cloud.audit.is_read_only is False
    answers = {}

    async def events(prompt):
        for name, tool_input, decision in (
            ("write_file", generated_file, local_file),
            ("aliyun_api", {"product": "ros", "action": "CreateStack"}, cloud),
        ):
            future = asyncio.get_running_loop().create_future()
            yield PermissionRequestEvent(
                tool_name=name, tool_input=tool_input, tool_use_id="offline-" + name, response_future=future,
                audit_context={"session_id": "offline-e2e", "metadata": decision.audit},
            )
            answers[name] = future.result()
        yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    runner = HeadlessRunner(model="offline-model", output_format=OutputFormat.JSON,
                            output_stream=io.StringIO(), cli_permission_mode=mode.value)
    monkeypatch.setattr(runner, "_create_agent_loop", lambda: SimpleNamespace(run_streaming=events))
    monkeypatch.setattr("iac_code.cli.headless.start_background_housekeeping", lambda: None)
    monkeypatch.setattr("iac_code.cli.headless.emit_auto_permission_audit", lambda *args, **kwargs: True)
    monkeypatch.setattr("iac_code.cli.headless.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.cli.headless.graceful_shutdown", lambda: None)
    assert await runner.run(smoke.PROMPT) == 0
    assert answers == {"write_file": True, "aliyun_api": False}


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


def test_text_output_does_not_claim_unobservable_tool_results():
    smoke._record_output_diagnostics("text", subprocess.CompletedProcess([], 0, "VPC", ""))
    assert "tool_error_count" not in smoke.OUTPUT_DIAGNOSTICS["text"]
    assert "permission_denial_count" not in smoke.OUTPUT_DIAGNOSTICS["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["json", "stream-json"])
@pytest.mark.parametrize("mode", ["dont_ask", "accept_edits"])
async def test_generation_skill_file_step_reproduces_old_smoke_failure(monkeypatch, tmp_path, output_format, mode):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.cli.headless import EXIT_MAX_TURNS, HeadlessRunner
    from iac_code.cli.output_formats import OutputFormat
    from iac_code.tools.base import ToolRegistry
    from iac_code.tools.write_file import WriteFileTool
    from iac_code.types.permissions import PermissionMode, ToolPermissionContext
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        TextDeltaEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    target = tmp_path / "template.json"
    template = '{"Resources":{"Vpc":{"Type":"ALIYUN::ECS::VPC"}}}'

    class GenerationProvider:
        calls = 0

        def get_model_name(self):
            return "offline-model"

        async def stream(self, messages, system, tools=None, **kwargs):
            self.calls += 1
            yield MessageStartEvent(message_id=str(self.calls))
            # Replay the skill's existing file-before-output workflow. The real
            # AgentLoop, permission pipeline and WriteFileTool determine success.
            if target.is_file():
                yield TextDeltaEvent(text=template)
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=1))
            else:
                call_id = "write-" + str(self.calls)
                yield ToolUseStartEvent(tool_use_id=call_id, name="write_file")
                yield ToolUseEndEvent(tool_use_id=call_id, name="write_file",
                                      input={"path": str(target), "content": template})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage(input_tokens=1))

    provider = GenerationProvider()
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    loop = AgentLoop(provider_manager=provider, system_prompt="Save the generated template before returning it.",
                     tool_registry=registry, cwd=str(tmp_path), max_turns=20,
                     permission_context=ToolPermissionContext(mode=PermissionMode(mode), cwd=str(tmp_path)))
    output = io.StringIO()
    runner = HeadlessRunner(model="offline-model", output_format=OutputFormat(output_format), output_stream=output)
    monkeypatch.setattr(runner, "_create_agent_loop", lambda: loop)
    monkeypatch.setattr("iac_code.cli.headless.start_background_housekeeping", lambda: None)
    monkeypatch.setattr("iac_code.cli.headless.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.cli.headless.graceful_shutdown", lambda: None)
    code = await runner.run(smoke.PROMPT)
    result = subprocess.CompletedProcess([], code, output.getvalue(), "")
    monkeypatch.setattr(smoke, "run_headless", lambda _: result)
    passed = smoke.test_json_output() if output_format == "json" else smoke.test_stream_json_output()
    if mode == "dont_ask":
        assert code == EXIT_MAX_TURNS and provider.calls == 20
        assert not target.exists() and passed is False
        assert smoke.OUTPUT_DIAGNOSTICS[output_format]["permission_denial_count"] == 20
    else:
        assert code == 0 and provider.calls == 2
        assert target.read_text() == template and passed is True
        assert smoke.OUTPUT_DIAGNOSTICS[output_format]["permission_denial_count"] == 0
