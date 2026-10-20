from __future__ import annotations

import argparse
import errno
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ros_first_cursor_bridge", ROOT / "skills/alicloud-ros-agent/scripts/ros_agent.py",
)
assert spec is not None and spec.loader is not None
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

SSE_LOST = "Aliyun CLI call failed: failed to call tool: SSE connection lost"
MCP_POLL_TIMEOUT = "MCP Returned Error Result " + json.dumps({
    "summary": "Failed to call tool", "popRequestId": "fake-request-id",
    "detail": "Async task polling timeout after 600 seconds",
})


def args(**overrides):
    values = dict(
        transport="aliyun_cli", aliyun_cli_execution_mode="remote", aliyun_path="aliyun",
        endpoint="ros.aliyuncs.com", connect_timeout=10, read_timeout=1800,
        profile=None, region_id="cn-hangzhou", no_thinking=False, mode="normal",
        session_id=None, client_reference_id="reference-1", debug=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class Process:
    def __init__(self, output, return_code=0):
        self.stdout = io.StringIO(output)
        self.return_code = return_code

    def poll(self):
        return self.return_code

    def wait(self):
        return self.return_code


def page(*items, **overrides):
    return json.dumps(dict(Sessions=list(items), **overrides))


def session(**overrides):
    return dict(SessionId="session-1", ClientReferenceId="reference-1", FirstCursor="v1.recovered.0", **overrides)


def event(sequence, terminal=False):
    status = {"state": "TASK_STATE_COMPLETED" if terminal else "TASK_STATE_WORKING"}
    if terminal:
        status["message"] = {"role": "ROLE_AGENT", "parts": [{"text": "final answer"}]}
    return json.dumps({"id": "v1.recovered." + str(sequence), "data": {"result": {"statusUpdate": {
        "contextId": "session-1", "taskId": "task-1", "status": status,
        "metadata": {"iac_code": {"assistantFinal": {"complete": terminal}}},
    }}}})


@pytest.fixture
def executor(monkeypatch):
    commands = []
    outputs = []

    def popen(command, **kwargs):
        commands.append(command)
        assert outputs, "Unexpected extra CLI request"
        output, code, stderr = outputs.pop(0)
        kwargs["stderr"].write(stderr.encode("utf-8"))
        return Process(output, code)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge.time, "sleep", lambda _: None)
    return commands, outputs


@pytest.mark.parametrize("failure", [("", 1, SSE_LOST), ("", 1, MCP_POLL_TIMEOUT), ("[", 0, ""), ("", 0, "")])
@pytest.mark.parametrize("known_session", [False, True])
def test_first_event_loss_recovers_without_resending_query(executor, tmp_path, failure, known_session):
    commands, outputs = executor
    outputs.extend([failure, (page(session()), 0, ""), (event(1) + "\n" + event(2, True), 0, "")])
    applied = []
    configured = args(session_id="session-1" if known_session else None)
    result = bridge._consume_start_chat(
        configured, tmp_path, "original answer", None, [],
        on_payload=lambda event_id, *_: applied.append(event_id) or True,
    )
    assert result["state"] == "turn-completed"
    assert result["finalText"] == "final answer"
    assert applied == [None, "v1.recovered.1", "v1.recovered.2"]
    assert [command[2] for command in commands] == ["start-chat", "list-chat-sessions", "start-chat"]
    assert sum("--query" in command for command in commands) == 1
    assert commands[0][commands[0].index("--query") + 1] == "original answer"
    if known_session:
        assert "--body" not in commands[0]  # Never rebind an existing session's reference.
    else:
        assert json.loads(commands[0][commands[0].index("--body") + 1]) == {"ClientReferenceId": "reference-1"}
    assert json.loads(commands[2][commands[2].index("--body") + 1]) == {
        "StreamOptions.Action": "Reconnect", "StreamOptions.Cursor": "v1.recovered.0",
    }
    assert commands[2][commands[2].index("--session-id") + 1] == "session-1"


def test_reference_lookup_finishes_pagination_and_preserves_next_token(executor, tmp_path):
    commands, outputs = executor
    outputs.extend([
        (page(session(), NextToken="opaque / token=="), 0, ""),
        (page({"SessionId": "other", "ClientReferenceId": "other"}), 0, ""),
    ])
    assert bridge._lookup_first_cursor(args(), tmp_path, None, "primary", None) == ("session-1", "v1.recovered.0")
    assert len(commands) == 2
    parameters = json.loads(commands[1][commands[1].index("--body") + 1])
    assert parameters == {"SessionScopeOption": "CurrentCaller"}
    assert commands[1][2] == "list-chat-sessions"
    assert commands[1][commands[1].index("--agent-version") + 1] == "V2"
    assert commands[1][commands[1].index("--biz-mode") + 1] == "IaCCodeNormal"
    assert commands[1][commands[1].index("--max-results") + 1] == "20"
    assert commands[1][commands[1].index("--next-token") + 1] == "opaque / token=="
    assert "--next-token" not in commands[0]


@pytest.mark.parametrize("mode", ["normal", "pipeline"])
def test_known_session_uses_latest_cursor_without_round_matching(executor, tmp_path, mode):
    commands, outputs = executor
    item = session()
    item.update(ClientReferenceId="different-reference", FirstCursor="v1.previous.0")
    outputs.append((page(item, NextToken="more-sessions"), 0, ""))
    assert bridge._lookup_first_cursor(args(mode=mode), tmp_path, "session-1", "primary", None) == (
        "session-1", "v1.previous.0",
    )
    assert len(commands) == 1
    assert commands[0][commands[0].index("--biz-mode") + 1] == (
        "IaCCodeNormal" if mode == "normal" else "IaCCodePipeline"
    )


def test_cancelled_lookup_does_not_launch_another_request(executor, tmp_path):
    commands, _outputs = executor
    assert bridge._lookup_first_cursor(args(), tmp_path, "session-1", "primary", lambda: False) is None
    assert commands == []


def test_oversized_session_list_is_not_used(executor, monkeypatch, tmp_path):
    commands, outputs = executor
    monkeypatch.setattr(bridge, "MAX_SESSION_LIST_BYTES", 64)
    outputs.append((page(session()), 0, ""))
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._lookup_first_cursor(args(), tmp_path, "session-1", "primary", None)
    assert failure.value.code == "session_lookup_failed"
    assert len(commands) == 1


def test_expired_first_cursor_does_not_create_another_session(executor, tmp_path):
    commands, outputs = executor
    outputs.extend([
        ("", 1, SSE_LOST), (page(session()), 0, ""),
        ('{"Code":"StreamExpired","Message":"expired","statusCode":410}', 1, ""),
    ])
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert result["state"] == "failed"
    assert sum("--query" in command for command in commands) == 1
    assert len(commands) == 3


def test_duplicate_reference_across_pages_is_not_bound(executor, tmp_path):
    commands, outputs = executor
    other = session()
    other["SessionId"] = "session-2"
    outputs.extend([(page(session(), NextToken="next"), 0, ""), (page(other), 0, "")])
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._lookup_first_cursor(args(), tmp_path, None, "primary", None)
    assert failure.value.code == "ambiguous_session_reference"
    assert len(commands) == 2


@pytest.mark.parametrize("last_page", ["truncated{", page(session(), NextToken="next")])
def test_incomplete_scan_does_not_bind_a_partial_match(executor, monkeypatch, tmp_path, last_page):
    commands, outputs = executor
    monkeypatch.setattr(bridge, "MAX_SESSION_LOOKUP_PAGES", 2)
    outputs.extend([(page(session(), NextToken="next"), 0, ""), (last_page, 0, "")])
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._lookup_first_cursor(args(), tmp_path, None, "primary", None)
    assert failure.value.code == "session_lookup_failed"
    assert len(commands) == 2


@pytest.mark.parametrize("missing", [None, ""])
def test_first_cursor_can_appear_on_a_later_scan(executor, tmp_path, missing):
    commands, outputs = executor
    pending = session()
    pending["FirstCursor"] = missing
    outputs.extend([(page(pending), 0, ""), (page(session()), 0, "")])
    assert bridge._lookup_first_cursor(args(), tmp_path, "session-1", "primary", None) == (
        "session-1", "v1.recovered.0",
    )
    assert len(commands) == 2


def test_missing_session_lookup_is_bounded_and_never_replays_query(executor, tmp_path):
    commands, outputs = executor
    outputs.extend([("", 1, SSE_LOST)] + [(page(), 0, "")] * bridge.MAX_SESSION_LOOKUP_SCANS)
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert failure.value.code == "missing_recovery_anchor"
    assert len(commands) == 1 + bridge.MAX_SESSION_LOOKUP_SCANS
    assert sum("--query" in command for command in commands) == 1


@pytest.mark.parametrize("diagnostic", [SSE_LOST, MCP_POLL_TIMEOUT])
def test_recovered_session_is_published_even_when_first_cursor_is_not_ready(executor, tmp_path, diagnostic):
    commands, outputs = executor
    pending = session()
    pending.pop("FirstCursor")
    outputs.extend([("", 1, diagnostic)] + [(page(pending), 0, "")] * bridge.MAX_SESSION_LOOKUP_SCANS)
    identities = []
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._consume_start_chat(
            args(), tmp_path, "hello", None, [],
            on_payload=lambda event_id, payload, *_: identities.append((event_id, payload)) or True,
        )
    assert failure.value.code == "missing_recovery_anchor"
    assert identities == [(None, {"SessionId": "session-1"})]
    assert sum("--query" in command for command in commands) == 1


@pytest.mark.parametrize("output,code", [("not-json", 0), ('{"Code":"Forbidden"}', 1), ('{"Code":"Forbidden"}', 0)])
def test_failed_list_request_never_resubmits_business_input(executor, tmp_path, output, code):
    commands, outputs = executor
    outputs.extend([("", 1, SSE_LOST), (output, code, "")])
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert failure.value.code == "session_lookup_failed"
    assert len(commands) == 2


def test_first_cursor_empty_replay_is_bounded(executor, tmp_path):
    commands, outputs = executor
    outputs.extend([("", 1, SSE_LOST), (page(session()), 0, "")] + [("", 0, "")] * 3)
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert failure.value.code == "stream_recovery_exhausted"
    assert len(commands) == 5
    assert sum("--query" in command for command in commands) == 1


def test_replay_uses_committed_event_cursor_and_deduplicates(executor, tmp_path):
    commands, outputs = executor
    outputs.extend([
        ("", 1, SSE_LOST), (page(session()), 0, ""),
        (event(1), 1, SSE_LOST), (event(1) + "\n" + event(2, True), 0, ""),
    ])
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert result["eventCount"] == 2
    assert result["state"] == "turn-completed"
    assert json.loads(commands[-1][commands[-1].index("--body") + 1])["StreamOptions.Cursor"] == "v1.recovered.1"
    assert [command[2] for command in commands].count("list-chat-sessions") == 1


@pytest.mark.parametrize("value", [None, "", "v1.recovered.1", "v1..0", "v2.recovered.0", 1])
def test_invalid_first_cursor_is_not_used(value):
    with pytest.raises(bridge.BridgeError):
        bridge.parse_first_cursor(value)


def test_first_cursor_is_not_a_committed_event_id():
    assert bridge.parse_first_cursor("v1.recovered.0") == "v1.recovered"
    with pytest.raises(bridge.BridgeError):
        bridge.parse_stream_event_id("v1.recovered.0")


def test_lookup_mailbox_is_restricted_to_the_configured_read_only_request(executor):
    job = {"aliyunPath": "/fake/aliyun", "endpoint": "ros.aliyuncs.com", "mode": "normal", "regionId": "cn-hangzhou"}
    command = bridge.build_session_list_command(args(), "opaque token")
    assert bridge._valid_foreground_command(command, job)
    for field, value in (("SessionScopeOption", "Account"), ("Mode", "IaCCodePipeline"), ("Query", "hello")):
        modified = list(command)
        parameters = json.loads(modified[modified.index("--body") + 1])
        parameters[field] = value
        modified[modified.index("--body") + 1] = json.dumps(parameters)
        assert not bridge._valid_foreground_command(modified, job)
    assert not bridge._valid_foreground_command(command + ["--profile", "other"], job)
    for flag, value in (("--agent-version", "V1"), ("--biz-mode", "IaCCodePipeline"), ("--max-results", "100")):
        modified = list(command)
        modified[modified.index(flag) + 1] = value
        assert not bridge._valid_foreground_command(modified, job)
    legacy = list(command)
    legacy[2] = "ListChatSessions"
    assert not bridge._valid_foreground_command(legacy, job)


def test_code_and_local_cli_do_not_send_client_reference(executor):
    configured = args(aliyun_cli_execution_mode="local")
    assert "--body" not in bridge.build_command(configured, "hello", None, [])
    assert "ClientReferenceId" not in bridge.build_start_chat_parameters(configured, "hello", None, [])


def test_agenthub_lookup_allows_independent_telemetry_ids_but_not_other_arguments(executor, monkeypatch):
    template = "AlibabaCloud-Agent-Skills/alibabacloud-ros-agent/{session-id}"
    monkeypatch.setattr(bridge, "SKILL_DISTRIBUTION", "agenthub")
    monkeypatch.setattr(bridge, "USER_AGENT_TEMPLATE", template)
    monkeypatch.setattr(bridge, "USER_AGENT", template.replace("{session-id}", "a" * 32))
    command = bridge.build_session_list_command(args())
    index = command.index("--user-agent") + 1
    command[index] = template.replace("{session-id}", "b" * 32)
    job = {"aliyunPath": "/fake/aliyun", "endpoint": "ros.aliyuncs.com", "mode": "normal", "regionId": "cn-hangzhou"}
    assert bridge._valid_foreground_command(command, job)
    modified = list(command)
    modified[modified.index("--biz-mode") + 1] = "IaCCodePipeline"
    assert not bridge._valid_foreground_command(modified, job)
    for value in ("arbitrary-header", template, command[index] + "\nInjected: value", command[index][:-1]):
        modified = list(command)
        modified[index] = value
        assert not bridge._valid_foreground_command(modified, job)
    monkeypatch.setattr(bridge, "SKILL_DISTRIBUTION", "public")
    assert not bridge._valid_foreground_command(command, job)


@pytest.mark.parametrize("operation", ["projection", "finish", "heartbeat"])
def test_remote_spool_works_when_mounted_filesystem_rejects_append(monkeypatch, tmp_path, operation):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "a" * 32
    _root, job_path, spool = bridge._job_paths(job_id)
    job = dict(activeRequestSeq=1, transport="aliyun_cli", aliyunCLIExecutionMode="remote", mode="normal",
               state="working", workspace=str(tmp_path), preferredLanguage="en", turn=1)
    bridge._atomic_json(job_path, job)
    original = b'{"type":"status","requestSeq":1}\n'
    bridge._atomic_bytes(spool, original)
    path_open = Path.open

    def reject_append(path, mode="r", *args, **kwargs):
        if path == spool and "a" in mode:
            raise OSError(errno.EINVAL, "Mounted filesystem rejects append")
        return path_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_append)
    if operation == "finish":
        assert bridge._finish_job(job_id, 1, {"ok": True, "state": "turn-completed", "finalText": "done",
                                            "finalTextComplete": True}, 123)
        assert bridge._load_state_json(job_path)["state"] == "turn-completed"
    elif operation == "projection":
        assert bridge._append_projection(job_id, {
            "type": "assistant-final", "requestSeq": 1, "sessionId": "session-1",
            "finalText": "done", "finalTextComplete": True,
        }, "v1.test.1")
        assert bridge._load_state_json(job_path)["streamCursor"] == "v1.test.1"
    else:
        result = bridge._follow_timeout_result(job_id, 0)
        assert result["followTimedOut"] is True
    assert spool.read_bytes().startswith(original)
    assert len(bridge._read_spool(spool)) == 2


def test_remote_spool_replace_failure_preserves_previous_spool_and_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "b" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    job = dict(activeRequestSeq=1, transport="aliyun_cli", aliyunCLIExecutionMode="remote", state="working")
    bridge._atomic_json(job_path, job)
    original = b'{"type":"status"}\n'
    bridge._atomic_bytes(spool, original)
    replace = bridge.os.replace

    def fail_spool_replace(source, target):
        if Path(target) == spool:
            raise OSError(errno.EIO, "Injected replace failure")
        return replace(source, target)

    monkeypatch.setattr(bridge.os, "replace", fail_spool_replace)
    with pytest.raises(OSError):
        bridge._append_projection(job_id, {
            "type": "assistant-final", "requestSeq": 1, "sessionId": "session-1", "finalText": "done",
        }, "v1.test.1")
    assert spool.read_bytes() == original
    assert bridge._load_state_json(job_path) == job
    assert not list(root.glob("*.tmp"))
