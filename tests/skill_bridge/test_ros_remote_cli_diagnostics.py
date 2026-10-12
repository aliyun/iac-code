from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "skills/alicloud-ros-agent/scripts/ros_agent.py"
spec = importlib.util.spec_from_file_location("ros_remote_diagnostics_bridge", BRIDGE_PATH)
assert spec is not None and spec.loader is not None
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class Process:
    def __init__(self, output, return_code=0):
        self.stdout = io.StringIO(output)
        self.return_code = return_code

    def wait(self, timeout=None):
        return self.return_code

    def poll(self):
        return self.return_code

    def terminate(self):
        pass

    def kill(self):
        pass


def args(mode="remote"):
    return argparse.Namespace(
        aliyun_path="aliyun",
        endpoint="ros.aliyuncs.com",
        connect_timeout=10,
        read_timeout=600,
        profile=None,
        region_id="cn-hangzhou",
        no_thinking=False,
        mode="normal",
        session_id=None,
        transport="aliyun_cli",
        aliyun_cli_execution_mode=mode,
        debug=True,
    )


def payload(state):
    return {"result": {"statusUpdate": {"taskId": "task-1", "contextId": "session-1", "status": {"state": state}}}}


def records(root):
    return [
        json.loads(line)
        for line in (root / ".ros-agent-debug/remote-aliyun-results.jsonl").read_text(encoding="utf-8").splitlines()
    ]


SSE_LOST = "Aliyun CLI call failed: failed to call tool: SSE connection lost"


@pytest.mark.parametrize("operation", ["read", "replace"])
@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("transient", [False, True])
def test_state_sharing_violation_retries_only_on_windows(monkeypatch, tmp_path, operation, platform, transient):
    path = tmp_path / "state.json"
    path.write_text('{"old":true}', encoding="utf-8")
    platform_os = SimpleNamespace(**dict(vars(os), name=platform))
    monkeypatch.setattr(bridge, "os", platform_os)
    calls = []
    sleeps = []
    monkeypatch.setattr(bridge.time, "sleep", sleeps.append)

    def denied():
        calls.append(None)
        if not transient or len(calls) == 1:
            raise PermissionError("sharing violation")

    if operation == "read":
        original = Path.open

        def open_file(target, *args, **kwargs):
            if target == path:
                denied()
            return original(target, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_file)

        def invoke():
            return bridge._load_state_json(path)
    else:
        original = os.replace

        def replace(source, target):
            denied()
            return original(source, target)

        monkeypatch.setattr(platform_os, "replace", replace)

        def invoke():
            return bridge._atomic_json(path, {"new": True})

    if platform == "nt" and transient:
        invoke()
        assert len(calls) == 2
        assert sleeps == [0.02]
    else:
        error = bridge.BridgeError if operation == "read" else PermissionError
        with pytest.raises(error):
            invoke()
        assert len(calls) == (10 if platform == "nt" else 1)
        assert len(sleeps) == (9 if platform == "nt" else 0)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("wrapper", ["aliyun.cmd", "aliyun.BAT"])
def test_stop_windows_cli_wrapper_retires_child_tree(monkeypatch, wrapper):
    monkeypatch.setattr(bridge, "os", SimpleNamespace(**dict(vars(os), name="nt")))
    stopped = []
    process = SimpleNamespace(args=[wrapper, "ros", "start-chat"], pid=123, poll=lambda: 1 if stopped else None)

    def run(command, **kwargs):
        assert command == ["taskkill", "/PID", "123", "/T", "/F"]
        assert kwargs["timeout"] == 5
        stopped.append(True)

    monkeypatch.setattr(bridge.subprocess, "run", run)
    bridge._stop_process(process)
    assert stopped == [True]


def test_foreground_close_releases_suspended_output_reader(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    process = bridge._ForegroundCLIProcess("a" * 32, ["aliyun", "ros", "start-chat"], {}, io.BytesIO(), "primary")
    process.output.write_bytes(b"one complete line\n")
    bridge._atomic_json(process.result_path, {"returnCode": 0})
    stream = iter(process)
    try:
        assert next(stream) == "one complete line\n"
        reader = process.output_handle
        process.close()
        assert reader.closed
        assert not any(path.name.startswith("remote-cli-") for path in process.path.parent.iterdir())
    finally:
        stream.close()


def test_foreground_stream_mailbox_rejects_other_api_operations():
    job = {"aliyunPath": "/fake/aliyun", "endpoint": "ros.aliyuncs.com", "sessionId": "session-1"}
    assert bridge._valid_foreground_command(["/fake/aliyun", "ros", "start-chat", "--query", "hello"], job)
    assert bridge._valid_foreground_command([
        "/fake/aliyun", "ros", "start-chat", "--body",
        json.dumps({"StreamOptions.Action": "Reconnect", "StreamOptions.Cursor": "v1.current.1"}),
    ], job)
    for operation in ("ListChatMessages", "StartChat", "StopChat", "DeleteStack"):
        command = ["/fake/aliyun", "ros", operation, "--AgentVersion", "V2",
                   "--SessionId", "session-1", "--RecoveryOnly", "true", "--endpoint", "ros.aliyuncs.com"]
        assert not bridge._valid_foreground_command(command, job)
    for command in (None, [], ["/fake/aliyun", "ros", None], ["/other/aliyun", "ros", "start-chat"]):
        assert not bridge._valid_foreground_command(command, job)


@pytest.mark.parametrize("diagnostic", [
    SSE_LOST,
    "Error: failed to call tool: SSE connection lost\n",
    json.dumps({"summary": "Failed to call tool", "detail": "SSE connection lost"}),
    json.dumps({"error": "failed to call tool: SSE connection lost"}),
    json.dumps({"error": {"message": SSE_LOST}}),
])
def test_remote_stream_interruption_recognizes_only_transport_envelopes(diagnostic):
    assert bridge.is_remote_cli_stream_interruption(diagnostic)


@pytest.mark.parametrize("diagnostic", [
    "HIL 上下文已过期（父命令已结束）",
    "permission denied",
    "some other connection lost",
    "The prompt says SSE connection lost, please explain",
    json.dumps({"Code": "StreamExpired", "Message": "SSE connection lost"}),
    json.dumps({"code": "InvalidStreamCursor", "message": "SSE connection lost"}),
    json.dumps({"statusCode": 403, "message": "SSE connection lost"}),
    json.dumps({"error": {"code": "Unauthorized", "message": SSE_LOST}}),
    json.dumps({"id": "v1.business.1", "message": SSE_LOST}),
    json.dumps({"result": {"message": SSE_LOST}}),
])
def test_remote_stream_interruption_does_not_reclassify_business_or_fatal_errors(diagnostic):
    assert not bridge.is_remote_cli_stream_interruption(diagnostic)


def test_remote_stream_interruption_does_not_override_structured_expiry_in_same_attempt():
    assert not bridge.is_remote_cli_stream_interruption(
        SSE_LOST, json.dumps({"error": {"code": "StreamExpired", "message": "replay window expired"}}),
    )


@pytest.mark.parametrize("failure", [
    {"summary": "Failed to call tool", "detail": "HIL context expired"},
    {"Code": "StreamExpired", "Message": "replay window expired", "statusCode": 410},
    {"code": "InvalidStreamCursor", "message": "invalid cursor"},
    {"code": "Unauthorized", "message": "permission denied", "statusCode": 403},
    {"summary": "Failed to call tool", "detail": "an unknown failure"},
])
def test_remote_reconnect_fatal_error_is_not_masked_by_previous_sse_diagnostic(monkeypatch, tmp_path, failure):
    calls = []

    def launch(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return Process(json.dumps({"id": "v1.fatal.1", "data": payload("TASK_STATE_WORKING")})
                           + "\n" + json.dumps({"error": SSE_LOST}), 1)
        assert len(calls) == 2
        return Process(json.dumps(failure), 1)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert result["state"] == "failed"
    assert result["error"]["code"] == "aliyun_cli_failed"
    assert len(calls) == 2


@pytest.mark.parametrize("channel,exit_code", [("stderr", 1), ("stdout", 1), ("stdout", 0), ("json-error", 0)])
@pytest.mark.parametrize("debug", [False, True])
def test_remote_sse_loss_reconnects_from_durable_anchor_without_query(
    monkeypatch, tmp_path, buffered_job, channel, exit_code, debug,
):
    buffer, _append, job_path, spool = buffered_job
    calls = []
    first = [{"id": "v1.batch.{}".format(i), "data": payload("TASK_STATE_WORKING")} for i in range(1, 4)]
    error = json.dumps({"summary": "Failed to call tool", "detail": SSE_LOST})
    if channel == "json-error":
        error = json.dumps({"error": SSE_LOST})

    def launch(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            if channel == "stderr":
                kwargs["stderr"].write(SSE_LOST.encode())
            return Process(json.dumps(first) + ("\n" + error if channel != "stderr" else ""), exit_code)
        assert len(calls) == 2
        assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.3"
        assert "--query" not in command
        assert "--session-id" in command and command[command.index("--session-id") + 1] == "session-1"
        assert json.loads(command[command.index("--body") + 1]) == {
            "StreamOptions.Action": "Reconnect", "StreamOptions.Cursor": "v1.batch.3",
        }
        # Replayed events do not duplicate the durable event spool.
        return Process(json.dumps(first[-1:] + [{"id": "v1.batch.4", "data": payload("TASK_STATE_COMPLETED")}]))

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    options = args()
    options.debug = debug
    result = bridge._consume_start_chat(options, tmp_path, "original request", None, [], on_payload=buffer)
    assert result["state"] == "turn-completed"
    assert len(calls) == 2
    assert buffer.committed_count == 4
    assert [json.loads(line)["streamEventId"] for line in spool.read_text(encoding="utf-8").splitlines()] == [
        "v1.batch.1", "v1.batch.4",
    ]  # Identical working projections are folded; replay adds no duplicate.
    if debug:
        decision = [item for item in records(tmp_path) if item["kind"] == "recovery-decision"]
        assert len(decision) == 1
        assert decision[0]["hasRecoveryAnchor"] and decision[0]["reconnectAllowed"]
        assert decision[0]["requestResubmitted"] is False
    else:
        assert not (tmp_path / ".ros-agent-debug").exists()


@pytest.mark.parametrize("known_session", [None, "session-1"])
def test_remote_sse_loss_before_current_request_anchor_reports_unknown_without_resubmission(
    monkeypatch, tmp_path, known_session,
):
    calls = []

    def launch(command, **kwargs):
        calls.append(command)
        kwargs["stderr"].write(SSE_LOST.encode())
        return Process("", 1)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    options = args()
    options.session_id = known_session
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._consume_start_chat(options, tmp_path, "an answer already accepted by the cloud", None, [])
    assert failure.value.code == "missing_recovery_anchor"
    assert "outcome is unknown" in failure.value.message
    assert "will not replay Query" in failure.value.message
    assert failure.value.retryable is False
    assert len(calls) == 1
    assert "--query" in calls[0]
    decision = [item for item in records(tmp_path) if item["kind"] == "recovery-decision"][0]
    assert decision["hasRecoveryAnchor"] is False
    assert decision["reconnectAllowed"] is False


@pytest.mark.parametrize("advance", [False, True])
def test_remote_sse_loss_bounds_no_progress_and_resets_budget_after_committed_events(monkeypatch, tmp_path, advance):
    calls = []
    cursors = []

    def launch(command, **kwargs):
        calls.append(command)
        attempt = len(calls)
        if attempt > 1:
            assert "--query" not in command
            cursors.append(json.loads(command[command.index("--body") + 1])["StreamOptions.Cursor"])
        kwargs["stderr"].write(SSE_LOST.encode())
        if attempt == 1 or (advance and attempt == 4):
            seq = 1 if attempt == 1 else 2
            return Process(json.dumps({"id": "v1.retry.{}".format(seq), "data": payload("TASK_STATE_WORKING")}), 1)
        return Process("", 1)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert failure.value.code == "remote_cli_stream_interrupted"
    assert failure.value.retryable is False
    assert len(calls) == (7 if advance else 4)
    assert cursors == (["v1.retry.1"] * 3 + ["v1.retry.2"] * 3 if advance else ["v1.retry.1"] * 3)
    decisions = [item for item in records(tmp_path) if item["kind"] == "recovery-decision"]
    assert decisions[-1]["consecutiveNoProgress"] == 3
    assert decisions[-1]["reconnectAllowed"] is False


@pytest.mark.parametrize("state", ["TASK_STATE_COMPLETED", "TASK_STATE_FAILED"])
def test_remote_sse_loss_after_terminal_event_preserves_authoritative_outcome(monkeypatch, tmp_path, state):
    calls = []

    def launch(command, **kwargs):
        calls.append(command)
        kwargs["stderr"].write(SSE_LOST.encode())
        return Process(json.dumps({"id": "v1.final.1", "data": payload(state)}), 1)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert result["state"] == ("turn-completed" if state == "TASK_STATE_COMPLETED" else "failed")
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["normal", "pipeline"])
@pytest.mark.parametrize(
    "state", ["TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"],
)
@pytest.mark.parametrize("tail", ["", ',{"id":"v1.final.2","data":'])
def test_remote_array_truncation_after_committed_terminal_never_reconnects(
    monkeypatch, tmp_path, buffered_job, mode, state, tail,
):
    buffer, _append, job_path, _spool = buffered_job
    buffer.mode = mode
    calls = []

    def launch(command, **_kwargs):
        calls.append(command)
        assert len(calls) == 1, "A committed terminal event must not trigger a reconnect."
        return Process("[" + json.dumps({"id": "v1.final.1", "data": payload(state)}) + tail)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    options = args()
    options.mode = mode
    options.debug = False
    result = bridge._consume_start_chat(options, tmp_path, "hello", None, [], on_payload=buffer)
    expected = state.removeprefix("TASK_STATE_").lower()
    if mode == "normal" and expected == "completed":
        expected = "turn-completed"
    assert result["state"] == expected
    assert result["ok"] is (state != "TASK_STATE_FAILED")
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.final.1"
    assert buffer.committed_count == 1
    assert len(calls) == 1


def test_remote_terminal_does_not_mask_invalid_cli_array_separator(monkeypatch, tmp_path, buffered_job):
    buffer, _append, _job_path, _spool = buffered_job
    output = "[" + json.dumps({"id": "v1.final.1", "data": payload("TASK_STATE_COMPLETED")}) + ":]"
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kwargs: Process(output))
    options = args()
    options.debug = False
    with pytest.raises(bridge.BridgeError) as failure:
        bridge._consume_start_chat(options, tmp_path, "hello", None, [], on_payload=buffer)
    assert failure.value.code == "invalid_cli_output"


def test_local_cli_sse_loss_keeps_existing_non_retryable_behavior(monkeypatch, tmp_path):
    calls = []

    def launch(command, **kwargs):
        calls.append(command)
        kwargs["stderr"].write(SSE_LOST.encode())
        return Process(json.dumps({"id": "v1.local.1", "data": payload("TASK_STATE_WORKING")}), 1)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    result = bridge._consume_start_chat(args("local"), tmp_path, "hello", None, [])
    assert result["state"] == "failed"
    assert result["error"]["code"] == "aliyun_cli_failed"
    assert len(calls) == 1


@pytest.fixture
def buffered_job(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "durable"))
    job_id = "a" * 32
    _root, job_path, spool = bridge._job_paths(job_id)
    bridge._atomic_json(job_path, {"mode": "normal", "state": "working", "activeRequestSeq": 1})
    buffer = bridge._RemoteProjectionBuffer(job_id, "normal", 1, "primary", None)
    summary = bridge.StreamSummary(None, mode="normal")

    def append(sequence, event=None):
        event = event or payload("TASK_STATE_WORKING")
        summary.apply(event)
        return buffer("v1.batch.{}".format(sequence), event, summary, False)

    return buffer, append, job_path, spool


@pytest.mark.parametrize("repeated_controls", [False, True])
def test_remote_projection_batches_525_events_into_four_durable_commits(monkeypatch, buffered_job, repeated_controls):
    buffer, append, job_path, _spool = buffered_job
    writes = []
    atomic = bridge._atomic_json

    def write(path, value, *args):
        if path == job_path:
            writes.append(value["streamCursor"])
        return atomic(path, value, *args)

    monkeypatch.setattr(bridge, "_atomic_json", write)
    if repeated_controls:
        project = bridge._project_managed_stream_event

        def with_controls(*arguments):
            return dict(project(*arguments), pendingPermissions=[])

        monkeypatch.setattr(bridge, "_project_managed_stream_event", with_controls)
    for sequence in range(1, 525):
        assert append(sequence)
    assert append(525, payload("TASK_STATE_COMPLETED"))
    assert writes == ["v1.batch.1", "v1.batch.257", "v1.batch.513", "v1.batch.525"]
    assert buffer.committed_count == 525
    assert buffer.flush_count == 4
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.525"
    assert buffer.pending == []


def test_remote_projection_time_bound_flushes_even_without_another_event(monkeypatch, buffered_job):
    buffer, append, job_path, _spool = buffered_job
    clock = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    assert append(1)
    assert append(2)
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.1"
    clock[0] = bridge.REMOTE_PROJECTION_BATCH_SECONDS
    assert buffer.flush_if_due()
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.2"


@pytest.mark.parametrize("fence", ["stopRequestedAt", "activeRequestSeq", "sidebandWorkerToken"])
def test_remote_projection_batch_rechecks_cancel_and_worker_ownership(buffered_job, fence):
    buffer, append, job_path, _spool = buffered_job
    if fence == "sidebandWorkerToken":
        buffer.worker_role = "sideband"
        buffer.worker_token = "owner-1"
        job = bridge._load_state_json(job_path)
        job[fence] = "owner-1"
        bridge._atomic_json(job_path, job)
    assert append(1)
    assert append(2)
    job = bridge._load_state_json(job_path)
    cursor_key = "sidebandStreamCursor" if fence == "sidebandWorkerToken" else "streamCursor"
    job[fence] = {"stopRequestedAt": 123, "activeRequestSeq": 2, "sidebandWorkerToken": "owner-2"}[fence]
    bridge._atomic_json(job_path, job)
    assert buffer.flush() is False
    assert buffer.flush() is False
    assert bridge._load_state_json(job_path)[cursor_key] == "v1.batch.1"


def test_remote_projection_batch_can_replay_after_spool_fsync_before_job_commit(monkeypatch, buffered_job):
    buffer, append, job_path, spool = buffered_job
    assert append(1)
    # An actionable event makes this batch visible in the event spool.
    buffer.pending.append(({"type": "artifact", "requestSeq": 1, "artifact": {"name": "template"}}, "v1.batch.2"))
    atomic = bridge._atomic_json

    def crash(path, value, *args):
        if path == job_path:
            raise OSError("simulated crash before cursor commit")
        return atomic(path, value, *args)

    with monkeypatch.context() as patch:
        patch.setattr(bridge, "_atomic_json", crash)
        with pytest.raises(OSError, match="simulated crash"):
            buffer.flush()
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.1"
    assert [event["streamEventId"] for event in bridge._read_spool(spool)].count("v1.batch.2") == 1
    assert buffer.flush()
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.2"
    assert [event["streamEventId"] for event in bridge._read_spool(spool)].count("v1.batch.2") == 1


@pytest.mark.parametrize("kind", ["input-required", "permission-ack", "artifact", "terminal", "failed"])
def test_remote_projection_actionable_boundaries_flush_all_prior_deltas(monkeypatch, buffered_job, kind):
    buffer, append, job_path, _spool = buffered_job
    assert append(1)
    assert append(2)
    monkeypatch.setattr(bridge, "_project_managed_stream_event", lambda *_args: {"type": kind, "requestSeq": 1})
    assert append(3)
    assert buffer.pending == []
    assert buffer.committed_count == 3
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.3"


def test_remote_projection_permission_clear_flushes_but_repeated_metadata_does_not(monkeypatch, buffered_job):
    buffer, append, job_path, _spool = buffered_job
    assert append(1)
    pending = [{"inputId": "permission-1", "permissionClass": "sub_pipeline"}]
    monkeypatch.setattr(bridge, "_project_managed_stream_event", lambda *_args: {
        "type": "status", "requestSeq": 1, "pendingPermissions": list(pending),
    })
    assert append(2)
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.2"
    assert append(3)
    assert len(buffer.pending) == 1
    pending.clear()
    assert append(4)
    job = bridge._load_state_json(job_path)
    assert job["streamCursor"] == "v1.batch.4"
    assert "inputRequired" not in job
    assert "pendingPermissions" not in job
    assert buffer.pending == []


def test_remote_follow_heartbeat_does_not_claim_cloud_progress_before_first_event(buffered_job):
    _buffer, _append, job_path, _spool = buffered_job
    job = bridge._load_state_json(job_path)
    job.update(transport="aliyun_cli", aliyunCLIExecutionMode="remote", preferredLanguage="zh")
    bridge._atomic_json(job_path, job)
    result = bridge._follow_timeout_result("a" * 32, 0)
    assert "尚未收到云端事件" in result["heartbeat"]
    assert "仍在处理中" not in result["heartbeat"]


def test_remote_truncated_batch_flushes_durable_cursor_before_reconnect(monkeypatch, tmp_path, buffered_job):
    buffer, _append, job_path, _spool = buffered_job
    first = [{"id": "v1.batch.{}".format(index), "data": payload("TASK_STATE_WORKING")} for index in range(1, 21)]
    output = json.dumps(first)[:-1] + ',{"id":"v1.batch.21","data":'
    final = json.dumps([{"id": "v1.batch.21", "data": payload("TASK_STATE_COMPLETED")}])
    calls = []

    def popen(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return Process(output)
        assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.20"
        body = json.loads(command[command.index("--body") + 1])
        assert body["StreamOptions.Cursor"] == "v1.batch.20"
        assert "--query" not in command
        return Process(final)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [], on_payload=buffer)
    assert result["state"] == "turn-completed"
    assert buffer.committed_count == 21
    assert buffer.flush_count == 3  # First anchor, truncation/EOF, terminal.
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.batch.21"
    batches = [record for record in records(tmp_path) if record["kind"] == "projection-batch"]
    assert [record["flushCount"] for record in batches] == [2, 1]
    assert [record["committedEventCount"] for record in records(tmp_path) if record["kind"] == "end"] == [20, 1]


@pytest.mark.parametrize('strategy', ['per-event', 'batched'])
def test_remote_real_subprocess_truncation_with_slow_durable_writes(
    monkeypatch, tmp_path, buffered_job, strategy, record_property,
):
    buffer, _append, job_path, _spool = buffered_job
    # A real subprocess returns 524 complete events, half of event 525 and a
    # successful exit status. Its reconnect returns event 525 authoritatively.
    fake_cli = tmp_path / 'truncating_aliyun.py'
    fake_cli.write_text('''import json, sys
def event(sequence, state):
    return {'id': 'v1.batch.%d' % sequence, 'data': {'result': {'statusUpdate': {
        'taskId': 'task-1', 'contextId': 'session-1', 'status': {'state': state}}}}}
if '--query' in sys.argv:
    print(json.dumps([event(i, 'TASK_STATE_WORKING') for i in range(1, 525)])[:-1]
          + ',{"id":"v1.batch.525","data":', end='', flush=True)
else:
    print(json.dumps([event(525, 'TASK_STATE_COMPLETED')]), flush=True)
''', encoding='utf-8')
    atomic = bridge._atomic_json
    real_popen = subprocess.Popen
    delay = float(os.environ.get('ROS_AGENT_TEST_DURABLE_WRITE_SECONDS', '0.005'))
    writes = []
    calls = []

    def slow_write(path, value, *arguments):
        if path == job_path:
            time.sleep(delay)
            writes.append(value['streamCursor'])
        return atomic(path, value, *arguments)

    def launch(command, **kwargs):
        calls.append(command)
        if len(calls) == 2:
            assert bridge._load_state_json(job_path)['streamCursor'] == 'v1.batch.524'
            assert json.loads(command[command.index('--body') + 1])['StreamOptions.Cursor'] == 'v1.batch.524'
            assert '--query' not in command
        assert len(calls) <= 2, 'Unexpected extra reconnect or repeated original Query.'
        return real_popen([sys.executable, str(fake_cli), *command[1:]], **kwargs)

    def per_event(event_id, event, summary, _already_applied):
        projection = bridge._project_managed_stream_event(event, summary, 'normal', 1, 'primary', None)
        return bridge._append_projection('a' * 32, projection, event_id)

    monkeypatch.setattr(bridge, 'resolve_aliyun', lambda _path: sys.executable)
    monkeypatch.setattr(bridge, '_atomic_json', slow_write)
    monkeypatch.setattr(bridge.subprocess, 'Popen', launch)
    started_at = time.monotonic()
    result = bridge._consume_start_chat(
        args(), tmp_path, 'offline-only', None, [], on_payload=buffer if strategy == 'batched' else per_event,
    )
    elapsed = time.monotonic() - started_at
    assert result['state'] == 'turn-completed', result
    assert result['eventCount'] == 525
    assert len(calls) == 2
    assert ['--query' in command for command in calls] == [True, False]
    assert bridge._load_state_json(job_path)['streamCursor'] == 'v1.batch.525'
    if strategy == 'batched':
        assert writes == ['v1.batch.1', 'v1.batch.257', 'v1.batch.513', 'v1.batch.524', 'v1.batch.525']
        assert buffer.committed_count == 525
        assert buffer.flush_count == 5
    else:
        assert writes == ['v1.batch.{}'.format(index) for index in range(1, 526)]
    ends = [record for record in records(tmp_path) if record['kind'] == 'end']
    assert [record['errorCode'] for record in ends] == ['cli_output_truncated', None]
    record_property('durable_write_delay_seconds', delay)
    record_property('durable_commits', len(writes))
    record_property('elapsed_seconds', round(elapsed, 3))


def test_remote_bootstrap_ack_never_precedes_its_batched_durable_checkpoint(monkeypatch, tmp_path, buffered_job):
    buffer, _append, job_path, _spool = buffered_job
    atomic = bridge._atomic_json
    acknowledgements = []

    def write(path, value, *arguments):
        if Path(path).name == "bootstrap-ack.json":
            assert bridge._load_state_json(job_path)["streamCursor"] == value["eventId"]
            acknowledgements.append(value)
        return atomic(path, value, *arguments)

    def popen(_command, **kwargs):
        invocation_id = kwargs["env"][bridge.REMOTE_BOOTSTRAP_INVOCATION_ENV]
        events = [
            {"id": "v1.batch.1", "data": payload("TASK_STATE_WORKING")},
            {"invocationId": invocation_id, "sessionId": "session-1",
             "event": {"id": "v1.batch.2", "data": payload("TASK_STATE_WORKING")}},
            {"id": "v1.batch.3", "data": payload("TASK_STATE_COMPLETED")},
        ]
        return Process("\n".join(json.dumps(event) for event in events))

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge, "_atomic_json", write)
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [], on_payload=buffer)
    assert result["state"] == "turn-completed"
    assert len(acknowledgements) == 1
    assert acknowledgements[0]["eventId"] == "v1.batch.2"


def test_remote_cli_mailbox_is_private_local_temp_while_job_state_stays_durable(monkeypatch, tmp_path):
    local_temp = tmp_path / "local-temp"
    local_temp.mkdir()
    durable = tmp_path / "oss-home" / "state"
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(durable))
    monkeypatch.setattr(bridge.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(local_temp))
    job_id = "a" * 32
    job_root, job_path, _spool = bridge._job_paths(job_id)
    bridge._atomic_json(job_path, {"sessionId": "durable-session", "streamEventId": "durable-cursor"})
    process = bridge._ForegroundCLIProcess(
        job_id, ["/fake/aliyun", "ros", "start-chat"], {"STAROPS_HIL": "never-persist-hil"}, io.BytesIO(), "primary",
    )
    ipc_root = process.path.parent
    assert local_temp in ipc_root.parents
    assert durable not in ipc_root.parents
    assert job_path.parent == job_root == durable / "jobs" / job_id
    assert bridge._load_state_json(job_path)["streamEventId"] == "durable-cursor"
    assert "never-persist-hil" not in process.path.read_text(encoding="utf-8")
    assert bridge._remote_cli_root("b" * 32) != ipc_root
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "another-state"))
    assert bridge._remote_cli_root(job_id) != ipc_root
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(durable))
    if os.name != "nt":
        assert ipc_root.stat().st_mode & 0o777 == 0o700
        assert ipc_root.parent.stat().st_mode & 0o777 == 0o700
        assert process.path.stat().st_mode & 0o777 == 0o600
        assert process.output.stat().st_mode & 0o777 == 0o600
    bridge._atomic_json(process.result_path, {"returnCode": 0})
    process.close()
    assert not list(ipc_root.glob("remote-cli-*"))
    assert job_path.exists()  # Cleaning IPC must not remove the recovery anchor.


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks requires Windows privileges.")
def test_remote_cli_refuses_a_symlinked_temporary_mailbox(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    root = bridge._remote_cli_root("a" * 32)
    target = tmp_path / "foreign-mailbox"
    target.mkdir()
    root.rmdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(bridge.BridgeError, match="not private"):
        bridge._remote_cli_root("a" * 32)


def test_remote_cli_drains_stdout_if_result_is_published_just_after_eof(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    process = bridge._ForegroundCLIProcess(
        "a" * 32, ["/fake/aliyun", "ros", "start-chat"], {}, io.BytesIO(), "primary",
    )
    output = json.dumps([{"id": "v1.debug.1", "data": payload("TASK_STATE_COMPLETED")}])
    published = []

    def result():
        if not published:
            # The first read saw EOF before the writer completed the batch.
            process.output.write_bytes(output.encode("utf-8"))
            published.append(True)
        return {"returnCode": 0}

    monkeypatch.setattr(process, "_result", result)
    assert "".join(process) == output
    process.close()


def test_remote_cli_mailbox_accepts_aggregate_json_larger_than_sse_line_limit(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(bridge, "MAX_SSE_LINE_BYTES", 256)
    monkeypatch.setattr(bridge, "MAX_CLI_BATCH_BYTES", 4096)
    batch = [
        {"id": "v1.large.{}".format(i), "data": dict(payload("TASK_STATE_WORKING"), note="中文" * 100)}
        for i in range(1, 3)
    ]
    output = json.dumps(batch, ensure_ascii=False).encode("utf-8")
    assert bridge.MAX_SSE_LINE_BYTES < len(output) < bridge.MAX_CLI_BATCH_BYTES
    process = bridge._ForegroundCLIProcess(
        "a" * 32, ["/fake/aliyun", "ros", "start-chat"], {}, io.BytesIO(), "primary",
    )
    try:
        process.output.write_bytes(output)
        bridge._atomic_json(process.result_path, {"returnCode": 0})
        events = list(bridge.iter_cli_plugin_payloads(process))
        assert [event["id"] for event in events] == ["v1.large.1", "v1.large.2"]
    finally:
        process.close()


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_remote_cli_mailbox_still_enforces_cli_batch_limit(monkeypatch, tmp_path, extra_bytes):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(bridge, "MAX_CLI_BATCH_BYTES", 256)
    process = bridge._ForegroundCLIProcess(
        "a" * 32, ["/fake/aliyun", "ros", "start-chat"], {}, io.BytesIO(), "primary",
    )
    try:
        process.output.write_bytes(b"x" * (bridge.MAX_CLI_BATCH_BYTES + extra_bytes))
        bridge._atomic_json(process.result_path, {"returnCode": 0})
        if extra_bytes:
            with pytest.raises(bridge.BridgeError) as failure:
                list(process)
            assert failure.value.code == "stream_failed"
        else:
            assert "".join(process) == "x" * bridge.MAX_CLI_BATCH_BYTES
    finally:
        process.close()


def test_foreground_cli_does_not_poll_durable_state_at_mailbox_frequency(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "a" * 32
    _root, job_path, _spool = bridge._job_paths(job_id)
    job = {"transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote"}
    bridge._atomic_json(job_path, job)
    clock = [0.0]
    checks = []
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def follow_ready(*_args):
        checks.append(clock[0])
        return None, job

    monkeypatch.setattr(bridge, "_follow_ready_result", follow_ready)
    bridge._drive_foreground_cli(job_id, 0, 1)
    assert 3 <= len(checks) <= 5  # Not the 50 reads/second used for local IPC.
    assert all(right - left >= 0.25 for left, right in zip(checks, checks[1:]))


def test_remote_debug_reads_a_fresh_descriptor_instead_of_the_writer_view(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "a" * 32
    _root, job_path, _spool = bridge._job_paths(job_id)
    bridge._atomic_json(job_path, {
        "transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote", "aliyunPath": "/fake/aliyun",
        "workspace": str(tmp_path), "debug": True, "activeRequestSeq": 1,
    })
    process = bridge._ForegroundCLIProcess(
        job_id, ["/fake/aliyun", "ros", "start-chat"],
        {bridge.REMOTE_BOOTSTRAP_INVOCATION_ENV: "fake-invocation"}, io.BytesIO(), "primary", True,
    )
    request = bridge._load_state_json(process.path)
    request["ownerPid"] = os.getpid()
    bridge._atomic_json(process.path, request)
    original_open = Path.open

    class StaleWriter:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, key):
            return getattr(self.handle, key)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def read(self, *_args):
            return b""  # Mirrors the measured OSS writer-handle read view.

    def stale_open(path, mode="r", *arguments, **kwargs):
        handle = original_open(path, mode, *arguments, **kwargs)
        return StaleWriter(handle) if path in (process.output, process.errors) and mode == "a+b" else handle

    output = json.dumps([{"id": "v1.debug.1", "data": payload("TASK_STATE_COMPLETED")}])

    def popen(_command, **kwargs):
        kwargs["stdout"].write(output.encode("utf-8"))
        cli = Process("")
        cli.pid = os.getpid()
        return cli

    monkeypatch.setattr(Path, "open", stale_open)
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    invocation_id = process.path.name.split(".")[0][len("remote-cli-"):]
    assert bridge.run_remote_cli(job_id, invocation_id) == 0
    assert "".join(process) == output
    process.close()
    saved = records(tmp_path)
    assert json.loads(next(row["text"] for row in saved if row["kind"] == "stdout")) == json.loads(output)
    assert next(row for row in saved if row["kind"] == "cli-result")["ipcStorage"] == "local-temp"


def test_missing_id_is_logged_before_the_same_error(monkeypatch, tmp_path):
    output = json.dumps({"data": payload("TASK_STATE_WORKING")})
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kw: Process(output))
    with pytest.raises(bridge.BridgeError) as error:
        bridge._consume_start_chat(args(), tmp_path, "never-log-the-input-prompt", None, [])
    assert error.value.code == "missing_stream_event_id"
    log = records(tmp_path)
    assert json.loads(next(item["text"] for item in log if item["kind"] == "stdout")) == json.loads(output)
    event = next(item for item in log if item["kind"] == "parsed-event")
    assert event["idPresent"] is False
    assert event["envelopeKeys"] == ["data"]
    assert log[-1]["errorCode"] == "missing_stream_event_id"
    assert "never-log-the-input-prompt" not in json.dumps(log)
    if os.name != "nt":
        assert (tmp_path / ".ros-agent-debug").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / ".ros-agent-debug/remote-aliyun-results.jsonl").stat().st_mode & 0o777 == 0o600


def test_normal_stream_and_reconnect_still_complete(monkeypatch, tmp_path):
    outputs = iter(
        [
            json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")}),
            json.dumps({"id": "v1.debug.2", "data": payload("TASK_STATE_COMPLETED")}),
        ]
    )
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kw: Process(next(outputs)))
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert result["ok"] is True
    assert result["state"] == "turn-completed"
    starts = [item for item in records(tmp_path) if item["kind"] == "start"]
    assert [item["reconnect"] for item in starts] == [False, True]
    assert len({item["invocationId"] for item in starts}) == 2
    assert [item["id"] for item in records(tmp_path) if item["kind"] == "parsed-event"] == ["v1.debug.1", "v1.debug.2"]


def test_array_parser_yields_complete_event_before_reading_the_next_chunk():
    first = {"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")}
    second = {"id": "v1.debug.2", "data": payload("TASK_STATE_COMPLETED")}
    chunks_read = []

    def chunks():
        chunks_read.append(1)
        yield "[\n" + json.dumps(first) + ",\n"
        chunks_read.append(2)
        yield json.dumps(second) + "\n]"

    events = bridge.iter_cli_plugin_payloads(chunks())
    assert next(events)["id"] == "v1.debug.1"
    assert chunks_read == [1]
    assert [event["id"] for event in events] == ["v1.debug.2"]


@pytest.mark.parametrize("pretty", [False, True])
@pytest.mark.parametrize("cut", ["after-member", "after-comma", "inside-member"])
def test_truncated_array_preserves_only_complete_events(pretty, cut):
    first = {"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")}
    # Embedded array-like characters must not be used to locate JSON boundaries.
    first["data"]["note"] = '中文 "]},[" and \\ escapes'
    output = "[" + json.dumps(first, ensure_ascii=False, indent=2 if pretty else None)
    if cut != "after-member":
        output += ",\n"
    if cut == "inside-member":
        output += '{"id":"v1.debug.2","data":{"contextId":"wrong-session","text":"unfinished'
    events = bridge.iter_cli_plugin_payloads(output.splitlines(keepends=True))
    event = next(events)
    assert event["id"] == "v1.debug.1"
    assert event["payload"] == first["data"]
    with pytest.raises(bridge.BridgeError) as error:
        next(events)
    assert error.value.code == "cli_output_truncated"
    assert error.value.retryable is True


@pytest.mark.parametrize("chunk_size", [1, 7, 128])
def test_array_parser_keeps_jsonl_and_bootstrap_output_compatible(chunk_size):
    first = {"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")}
    second = {"id": "v1.debug.2", "data": payload("TASK_STATE_COMPLETED")}
    bootstrap = {"invocationId": "invocation-1", "event": first, "sessionId": "session-1"}
    output = json.dumps(bootstrap) + "\n[]\n" + json.dumps([first, second], indent=2) + "\n" + json.dumps(second)
    chunks = [output[start:start + chunk_size] for start in range(0, len(output), chunk_size)]
    events = list(bridge.iter_cli_plugin_payloads(chunks))
    assert [event["id"] for event in events] == ["v1.debug.1", "v1.debug.1", "v1.debug.2", "v1.debug.2"]
    assert events[0]["bootstrap"] == bootstrap


def test_array_parser_waits_for_numeric_tokens_and_nested_values_to_complete():
    output = '[1e2,-12.3,true,null,"quoted ] , [",[{"id":"v1.debug.1","data":{"contextId":"session-1"}}]]'
    events = list(bridge.iter_cli_plugin_payloads(output))
    assert len(events) == 6
    assert events[-1]["id"] == "v1.debug.1"
    assert [event["payload"] for event in events[:-1]] == [None] * 5


@pytest.mark.parametrize("bad_separator", [" ", ":", ",]"])
def test_array_parser_does_not_accept_invalid_separators(bad_separator):
    first = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")})
    tail = bad_separator if bad_separator == ",]" else bad_separator + first + "]"
    events = bridge.iter_cli_plugin_payloads("[" + first + tail)
    assert next(events)["id"] == "v1.debug.1"
    with pytest.raises(bridge.BridgeError) as error:
        list(events)
    assert error.value.code == "invalid_cli_output"
    assert error.value.retryable is False


def test_80k_truncated_arrays_reconnect_from_last_complete_event_without_repeating_query(monkeypatch, tmp_path):
    first = {"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")}
    second = {"id": "v1.debug.2", "data": payload("TASK_STATE_WORKING")}
    third = {"id": "v1.debug.3", "data": payload("TASK_STATE_WORKING")}
    for event in (first, second, third):
        event["data"]["padding"] = "x" * (40 * 1024)
    completed = {"id": "v1.debug.4", "data": payload("TASK_STATE_COMPLETED")}
    outputs = iter([
        json.dumps([first, second, third, completed], indent=2)[:80 * 1024],
        json.dumps([second, third, completed], indent=2)[:80 * 1024],
        json.dumps([third, completed], indent=2)[:80 * 1024],
    ])
    commands = []
    applied = []

    def popen(command, **_kwargs):
        commands.append(command)
        return Process(next(outputs))

    def on_payload(event_id, _payload, _summary, _already_applied):
        applied.append(event_id)
        return True

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge.time, "sleep", lambda _: None)
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [], on_payload=on_payload)
    assert result["state"] == "turn-completed"
    assert result["eventCount"] == 4
    assert applied == ["v1.debug.1", "v1.debug.2", "v1.debug.3", "v1.debug.4"]
    assert len(commands) == 3
    assert "--query" in commands[0]
    for sequence, command in enumerate(commands[1:], start=1):
        assert "--query" not in command
        assert json.loads(command[command.index("--body") + 1]) == {
            "StreamOptions.Action": "Reconnect", "StreamOptions.Cursor": "v1.debug.{}".format(sequence),
        }
        assert command[command.index("--session-id") + 1] == "session-1"
    ends = [record for record in records(tmp_path) if record["kind"] == "end"]
    assert [record["errorCode"] for record in ends] == ["cli_output_truncated", "cli_output_truncated", None]


@pytest.mark.parametrize("output", ["[", '[{"id":"v1.debug.1","data":'])
def test_array_truncated_before_first_complete_event_never_resends_query(monkeypatch, tmp_path, output):
    commands = []

    def popen(command, **_kwargs):
        commands.append(command)
        return Process(output)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    with pytest.raises(bridge.BridgeError) as error:
        bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert error.value.code == "missing_recovery_anchor"
    assert len(commands) == 1
    assert records(tmp_path)[-1]["errorCode"] == "cli_output_truncated"


def test_truncation_that_never_yields_a_complete_new_event_does_not_retry_forever(monkeypatch, tmp_path):
    first = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")})
    incomplete = '{"id":"v1.debug.2","data":{"padding":"unfinished'
    commands = []

    def popen(command, **_kwargs):
        assert len(commands) < 4, "Truncated output without progress was retried indefinitely."
        commands.append(command)
        return Process("[" + (first + "," if len(commands) == 1 else "") + incomplete)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge.time, "sleep", lambda _: None)
    with pytest.raises(bridge.BridgeError) as error:
        bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert error.value.code == "cli_output_truncated"
    assert error.value.retryable is False
    assert "output limit" in str(error.value)
    assert len(commands) == 4
    assert ["--query" in command for command in commands] == [True, False, False, False]


def test_truncation_stall_budget_resets_when_a_complete_batch_advances_the_cursor(monkeypatch, tmp_path):
    first = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")})
    second = json.dumps({"id": "v1.debug.2", "data": payload("TASK_STATE_WORKING")})
    completed = json.dumps({"id": "v1.debug.3", "data": payload("TASK_STATE_COMPLETED")})
    incomplete = '{"id":"v1.debug.9","data":'
    outputs = iter([
        "[" + first + "," + incomplete, "[" + incomplete, "[" + incomplete,
        "[" + second + "]", "[" + incomplete, "[" + incomplete, "[" + completed + "]",
    ])
    commands = []

    def popen(command, **_kwargs):
        commands.append(command)
        return Process(next(outputs))

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge.time, "sleep", lambda _: None)
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert result["state"] == "turn-completed"
    assert result["eventCount"] == 3
    assert [json.loads(command[command.index("--body") + 1])["StreamOptions.Cursor"]
            for command in commands[1:]] == ["v1.debug.1"] * 3 + ["v1.debug.2"] * 3


def test_credentials_and_starops_hil_are_redacted(monkeypatch, tmp_path):
    secret = "fake-starops-auth-value with spaces"
    monkeypatch.setenv("STAROPS_HIL", secret)
    log = bridge._RemoteCLIDebugLog(tmp_path, "invocation", False, "primary", args())
    log.output(
        "stdout",
        json.dumps(
            {
                "id": "v1.debug.1",
                "data": {
                    "password": "fake password with spaces",
                    "AccessKeySecret": "fake-ak-secret",
                    "Cookie": "fake-cookie",
                    "text": secret,
                    "url": urllib.parse.quote(secret, safe=""),
                    "nested": json.dumps({"security_token": "fake-security-token", "safe": "keep"}),
                },
            }
        ),
    )
    log.output("stderr", "password=fake password with spaces\nAuthorization: Bearer fake-auth-token\n")
    serialized = (tmp_path / ".ros-agent-debug/remote-aliyun-results.jsonl").read_text(encoding="utf-8")
    for value in (
        secret,
        urllib.parse.quote(secret, safe=""),
        "fake password with spaces",
        "fake-ak-secret",
        "fake-cookie",
        "fake-security-token",
        "fake-auth-token",
    ):
        assert value not in serialized
    data = json.loads(next(item["text"] for item in records(tmp_path) if item["kind"] == "stdout"))["data"]
    assert data["password"] == "[REDACTED]"
    assert json.loads(data["nested"])["safe"] == "keep"


def test_size_limits_do_not_change_stdout(monkeypatch, tmp_path):
    log = bridge._RemoteCLIDebugLog(tmp_path, "invocation", False, "primary", args())
    monkeypatch.setattr(log, "MAX_RAW_LINE_BYTES", 4)
    monkeypatch.setattr(log, "MAX_STDOUT_BYTES", 20)
    source = ["123456", "x" * 21]
    assert list(log.lines(source)) == source
    assert any(item.get("textOmitted") == "line-size-limit" for item in records(tmp_path))
    assert any(item["kind"] == "stdout-limit" for item in records(tmp_path))
    monkeypatch.setattr(log, "MAX_FILE_BYTES", 1024)
    log.record("extra", text="x" * 2048)
    assert log.file_limited is True
    assert (tmp_path / ".ros-agent-debug/remote-aliyun-results.jsonl").stat().st_size <= 1024


def test_log_write_failure_does_not_change_error(monkeypatch, tmp_path):
    (tmp_path / ".ros-agent-debug").write_text("not-a-directory", encoding="utf-8")
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(
        bridge.subprocess, "Popen", lambda *_args, **_kw: Process(json.dumps({"data": payload("TASK_STATE_WORKING")}))
    )
    with pytest.raises(bridge.BridgeError) as error:
        bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert error.value.code == "missing_stream_event_id"


def test_local_mode_does_not_create_debug_log(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    output = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_COMPLETED")})
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kw: Process(output))
    assert bridge._consume_start_chat(args("local"), tmp_path, "hello", None, [])["ok"] is True
    assert not (tmp_path / ".ros-agent-debug").exists()


@pytest.mark.parametrize("setting", [None, False, True])
def test_debug_config_defaults_off_and_accepts_only_booleans(tmp_path, setting):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({} if setting is None else {"debug": setting}), encoding="utf-8")
    config = bridge.load_skill_config(config_path)
    configured = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(configured, config)
    assert configured.debug is (setting is True)


@pytest.mark.parametrize("setting", ["true", 1, 0, None, [], {}])
def test_debug_config_rejects_non_booleans(tmp_path, setting):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"debug": setting}), encoding="utf-8")
    with pytest.raises(bridge.BridgeError) as error:
        bridge.load_skill_config(config_path)
    assert error.value.code == "invalid_config"


@pytest.mark.parametrize("setting", [None, False])
def test_remote_cli_debug_disabled_does_not_create_log(monkeypatch, tmp_path, setting):
    configured = args()
    if setting is None:
        del configured.debug
    else:
        configured.debug = setting
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    output = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_COMPLETED")})
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kw: Process(output))
    assert bridge._consume_start_chat(configured, tmp_path, "hello", None, [])["ok"] is True
    assert not (tmp_path / ".ros-agent-debug").exists()


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_ros_plugin_commands_do_not_pass_yes(monkeypatch, mode):
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    configured = args(mode)
    commands = [
        bridge.build_command(configured, "ping", None, []),
        bridge.build_reconnect_command(configured, "session-1", "v1.debug.1"),
        bridge.build_stop_command({"endpoint": configured.endpoint, "aliyunCLIExecutionMode": mode}, "session-1"),
    ]
    assert all("--yes" not in command for command in commands)


@pytest.mark.parametrize("mode", ["remote", "local", "code"])
@pytest.mark.parametrize(
    ("prompt", "remote_query"),
    [
        ("帮我生成 VPC", "帮我生成 VPC"),
        ("帮我生成 VPC\n", "帮我生成 VPC"),
        ("帮我生成 VPC\r\n\r\n", "帮我生成 VPC"),
        ("first\r\nsecond\nthird\rfourth\r\n", "first second third fourth"),
        ("first\n\nsecond\n", "first  second"),
        ("Resources:\n  VPC:\n    Type: ALIYUN::ECS::VPC\n", "Resources:   VPC:     Type: ALIYUN::ECS::VPC"),
        ('{"text":"first\\nsecond"}\n', '{"text":"first\\nsecond"}'),
        ("  first\tsecond; 'quoted'\n", "  first\tsecond; 'quoted'"),
    ],
)
def test_only_remote_cli_normalizes_query_line_breaks(monkeypatch, mode, prompt, remote_query):
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    configured = args(mode)
    if mode == "code":
        configured.transport = "code"
        query = bridge.build_start_chat_parameters(configured, prompt, None, [])["Query"]
    else:
        command = bridge.build_command(configured, prompt, None, [])
        query = command[command.index("--query") + 1]
    assert query == (remote_query if mode == "remote" else prompt)
    if mode == "remote":
        assert "\r" not in query and "\n" not in query


@pytest.mark.parametrize("mode", ["remote", "local"])
def test_query_normalization_is_only_at_cli_boundary(monkeypatch, tmp_path, mode):
    prompt_path = tmp_path / "prompt.txt"
    prompt = "Resources:\r\n  VPC:\n    Type: ALIYUN::ECS::VPC\r\n"
    prompt_path.write_bytes(prompt.encode("utf-8"))
    assert bridge.read_prompt(tmp_path, str(prompt_path)) == prompt
    configured = args(mode)
    configured.session_id = "session-1"
    calls = []
    output = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_COMPLETED")})

    def popen(command, **kwargs):
        calls.append(command)
        return Process(output)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    assert bridge._consume_start_chat(configured, tmp_path, prompt, None, [])["ok"] is True
    assert len(calls) == 1
    query = calls[0][calls[0].index("--query") + 1]
    assert query == ("Resources:   VPC:     Type: ALIYUN::ECS::VPC" if mode == "remote" else prompt)
    assert calls[0][calls[0].index("--session-id") + 1] == "session-1"
    assert prompt_path.read_bytes() == prompt.encode("utf-8")


@pytest.mark.parametrize("return_code", [0, 1])
@pytest.mark.parametrize("stderr_copy", [False, True])
def test_mcp_error_wrapper_preserves_cli_failure_without_reconnect(monkeypatch, tmp_path, return_code, stderr_copy):
    wrapper = {"summary": "Failed to call tool", "popRequestId": "request-1", "detail": "unknown flag --yes"}
    output = "MCP Returned Error Result\n" + json.dumps(wrapper) + "\n"
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        if stderr_copy:
            kwargs["stderr"].write(output.encode("utf-8"))
        return Process(output, return_code)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    if return_code:
        result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
        error = result["error"]
        assert result["ok"] is False
        assert result["state"] == "failed"
    else:
        with pytest.raises(bridge.BridgeError) as failure:
            bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
        error = {"code": failure.value.code, "message": str(failure.value)}
    assert error["code"] == "aliyun_cli_failed"
    assert "unknown flag --yes" in error["message"]
    assert len(calls) == 1
    events = [item for item in records(tmp_path) if item["kind"] == "parsed-event"]
    assert events[-1]["cliFailure"] is True
    assert records(tmp_path)[-1]["errorCode"] == "aliyun_cli_failed"


@pytest.mark.parametrize("with_anchor", [False, True])
def test_expired_hil_is_reported_without_blind_retry(monkeypatch, tmp_path, with_anchor):
    message = "HIL 上下文已过期(父命令已结束),请在新的前台命令中重新执行,不要按服务不可用重试"
    prefix = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")}) + "\n" if with_anchor else ""
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        kwargs["stderr"].write(message.encode("utf-8"))
        return Process(prefix + message + "\n", 1)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    result = bridge._consume_start_chat(args(), tmp_path, "hello", None, [])
    assert result["error"]["code"] == "aliyun_cli_failed"
    assert message in result["error"]["message"]
    assert len(calls) == 1


def test_valid_sse_envelope_is_not_mistaken_for_mcp_error():
    wrapper = {"summary": "Failed to call tool", "detail": "business data"}
    event = next(bridge.iter_cli_plugin_payloads([json.dumps({"id": "v1.debug.1", "data": wrapper})]))
    assert event["id"] == "v1.debug.1"
    assert event["payload"] == wrapper
    assert not event.get("cliFailure")


@pytest.mark.parametrize("pretty", [False, True])
def test_debug_redacts_echoed_prompt_and_returned_history(tmp_path, pretty):
    log = bridge._RemoteCLIDebugLog(tmp_path, "invocation", False, "primary", args())
    output = json.dumps(
        {
            "data": {
                "history": [{"parts": [{"text": "fake-user-prompt"}]}],
                "Query": "fake-query",
                "contextId": "session-1",
            }
        },
        indent=2 if pretty else None,
    )
    assert list(log.lines(output.splitlines(keepends=True))) == output.splitlines(keepends=True)
    serialized = json.dumps(records(tmp_path))
    assert "fake-user-prompt" not in serialized
    assert "fake-query" not in serialized
    assert "session-1" in serialized


def test_debug_batch_keeps_redaction_and_records_with_one_append(monkeypatch, tmp_path):
    monkeypatch.setenv("STAROPS_HIL", "fake-hil-never-log")
    log = bridge._RemoteCLIDebugLog(tmp_path, "invocation", False, "primary", args(), record_start=False)
    original_open = bridge.os.open
    opened = []

    def counted_open(path, *arguments):
        opened.append(path)
        return original_open(path, *arguments)

    monkeypatch.setattr(bridge.os, "open", counted_open)
    with log.batch():
        for sequence in range(200):
            log.output("stdout", json.dumps({"id": sequence, "password": "fake-password", "hil": "fake-hil-never-log"}))
        assert not log.path.exists()
    assert len(opened) == 1
    saved = records(tmp_path)
    assert len(saved) == 200
    assert [json.loads(row["text"])["id"] for row in saved] == list(range(200))
    assert "fake-password" not in json.dumps(saved)
    assert "fake-hil-never-log" not in json.dumps(saved)


def test_debug_batch_bounds_memory_and_file(monkeypatch, tmp_path):
    log = bridge._RemoteCLIDebugLog(tmp_path, "invocation", False, "primary", args(), record_start=False)
    monkeypatch.setattr(log, "MAX_FILE_BYTES", 2048)
    with log.batch():
        for _ in range(100):
            log.record("stdout", text="x" * 200)
        assert log.pending_bytes <= log.MAX_FILE_BYTES
        assert log.file_limited is True
    assert log.path.stat().st_size <= log.MAX_FILE_BYTES
    assert records(tmp_path)[-1]["kind"] == "file-limit"


@pytest.mark.parametrize("truncated", [False, True])
def test_debug_parser_timing_excludes_input_wait_and_consumer_work(monkeypatch, tmp_path, truncated):
    clock = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    log = bridge._RemoteCLIDebugLog(tmp_path, "invocation", False, "primary", args(), record_start=False)

    def lines():
        clock[0] += 5
        yield "fake input"
        clock[0] += 7

    def parser(source):
        source = iter(source)
        assert next(source) == "fake input"
        clock[0] += 2
        yield {"id": "v1.debug.1", "payload": payload("TASK_STATE_WORKING")}
        clock[0] += 3
        yield {"id": "v1.debug.2", "payload": payload("TASK_STATE_COMPLETED")}
        assert list(source) == []
        clock[0] += 4
        if truncated:
            raise bridge.BridgeError("cli_output_truncated", "fake truncation", True)

    monkeypatch.setattr(bridge, "iter_cli_plugin_payloads", parser)
    events = log.parse_events(lines())
    assert next(events)["id"] == "v1.debug.1"
    clock[0] += 100  # Time spent by the consumer must not count as parsing.
    assert next(events)["id"] == "v1.debug.2"
    clock[0] += 100
    if truncated:
        with pytest.raises(bridge.BridgeError, match="fake truncation"):
            next(events)
    else:
        assert list(events) == []
    assert log.metrics()["outputReadElapsedMs"] == 12000
    assert log.metrics()["parseElapsedMs"] == 9000
    assert log.metrics()["completeEventCount"] == 2


def test_debug_attempt_metrics_reset_and_distinguish_replay_from_cursor_progress(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    first = {"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")}
    completed = {"id": "v1.debug.3", "data": payload("TASK_STATE_COMPLETED")}
    outputs = iter([
        json.dumps([first, first, {"data": {"object": "heartbeat"}}, "malformed"])[:-1] + ',{"unfinished',
        json.dumps([first, completed]),
    ])
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kw: Process(next(outputs)))
    original_parsed_event = bridge._RemoteCLIDebugLog.parsed_event

    def parsed_event(log, event):
        original_parsed_event(log, event)
        clock[0] += 0.01

    projected = []

    def project(event_id, *_args):
        projected.append(event_id)
        clock[0] += 0.025  # Includes projection and simulated state persistence.
        return True

    monkeypatch.setattr(bridge._RemoteCLIDebugLog, "parsed_event", parsed_event)
    result = bridge._consume_start_chat(args(), tmp_path, "never-log-the-input-prompt", None, [], on_payload=project)
    assert result["state"] == "turn-completed"
    assert projected == ["v1.debug.1", "v1.debug.3"]
    ends = [row for row in records(tmp_path) if row["kind"] == "end"]
    assert len(ends) == 2
    assert [row["startCursor"] for row in ends] == [None, "v1.debug.1"]
    assert [row["committedCursor"] for row in ends] == ["v1.debug.1", "v1.debug.3"]
    assert [row["sequenceAdvance"] for row in ends] == [1, 2]
    assert [row["completeEventCount"] for row in ends] == [2, 2]
    assert [row["committedEventCount"] for row in ends] == [1, 1]
    assert [row["replayedEventCount"] for row in ends] == [1, 1]
    assert [row["parsedEventLogElapsedMs"] for row in ends] == [40, 20]
    assert all(row["projectionElapsedMs"] == row["eventProcessElapsedMs"] == 25 for row in ends)
    assert all(row["parseElapsedMs"] == row["outputReadElapsedMs"] == 0 for row in ends)
    assert ends[0]["errorCode"] == "cli_output_truncated"
    assert ends[1]["errorCode"] is None
    assert "never-log-the-input-prompt" not in json.dumps(records(tmp_path))


@pytest.mark.parametrize("projection_fails", [False, True])
def test_debug_does_not_count_uncommitted_projection_as_progress(monkeypatch, tmp_path, projection_fails):
    clock = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    output = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")})
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kw: Process(output))

    def project(*_args):
        clock[0] += 0.025
        if projection_fails:
            raise bridge.BridgeError("fake_projection_error", "fake persistence failure")
        return False

    if projection_fails:
        with pytest.raises(bridge.BridgeError, match="fake persistence failure"):
            bridge._consume_start_chat(args(), tmp_path, "hello", None, [], on_payload=project)
    else:
        assert bridge._consume_start_chat(args(), tmp_path, "hello", None, [], on_payload=project)["_workerStopped"]
    end = next(row for row in records(tmp_path) if row["kind"] == "end")
    assert end["completeEventCount"] == 1
    assert end["committedEventCount"] == end["sequenceAdvance"] == 0
    assert end["committedCursor"] is None
    assert end["projectionElapsedMs"] == end["eventProcessElapsedMs"] == 25


@pytest.mark.parametrize(
    ("debug", "clock_shift", "expected_ms"), [(True, 0.125, 125), (True, -0.125, 0), (False, 0.125, None)]
)
def test_managed_debug_queue_to_actual_cli_launch(monkeypatch, tmp_path, debug, clock_shift, expected_ms):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv("STAROPS_HIL", "fake-hil-never-log")
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "time", lambda: clock[0])
    job_id = "a" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(job_path, {
        "transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote", "aliyunPath": "/fake/aliyun",
        "workspace": str(tmp_path), "debug": debug, "activeRequestSeq": 1,
    })
    process = bridge._ForegroundCLIProcess(
        job_id, ["/fake/aliyun", "ros", "start-chat", "--query", "never-log-the-input-prompt"],
        {bridge.REMOTE_BOOTSTRAP_INVOCATION_ENV: "fake-invocation"}, io.BytesIO(), "primary", debug,
    )
    request = bridge._load_state_json(process.path)
    assert ("queuedAtUnixMs" in request) is debug
    request["ownerPid"] = os.getpid()
    bridge._atomic_json(process.path, request)
    clock[0] += clock_shift

    def popen(_command, **kwargs):
        kwargs["stdout"].write(b"[]")
        cli = Process("")
        cli.pid = os.getpid()
        return cli

    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    invocation_id = process.path.name.split(".")[0][len("remote-cli-"):]
    assert bridge.run_remote_cli(job_id, invocation_id) == 0
    process.close()
    if debug:
        log = records(tmp_path)
        assert next(row for row in log if row["kind"] == "cli-result")["queueToLaunchMs"] == expected_ms
        assert "fake-hil-never-log" not in json.dumps(log)
        assert "never-log-the-input-prompt" not in json.dumps(log)
    else:
        assert not (tmp_path / ".ros-agent-debug").exists()


@pytest.mark.parametrize("log_fails", [False, True])
def test_slow_debug_does_not_block_truncation_reconnect_or_lose_cleaned_output(monkeypatch, tmp_path, log_fails):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv("STAROPS_HIL", "fake-hil-never-log")
    job_id = "a" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(job_path, {
        "transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote", "aliyunPath": "/fake/aliyun",
        "workspace": str(tmp_path), "debug": True, "activeRequestSeq": 1,
    })
    first = json.dumps({"id": "v1.debug.1", "data": payload("TASK_STATE_WORKING")}, indent=2)
    completed = json.dumps({"id": "v1.debug.2", "data": payload("TASK_STATE_COMPLETED")}, indent=2)
    outputs = iter(["[\n" + first + ',\n{"id":"unfinished', "[\n" + completed + "\n]"])
    commands = []

    def popen(command, **kwargs):
        commands.append(command)
        kwargs["stdout"].write(next(outputs).encode("utf-8"))
        kwargs["stderr"].write(b"password=fake-password\n")
        process = Process("")
        process.pid = os.getpid()
        return process

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _: "/fake/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    blocked = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    original_append = bridge._RemoteCLIDebugLog._append_records

    def slow_append(log, rows):
        if any(b'"kind":"stdout"' in row for row in rows) and not blocked.is_set():
            blocked.set()
            assert release.wait(10), "Test did not release the slow logger."
            if log_fails:
                raise OSError("fake log write failure")
        return original_append(log, rows)

    monkeypatch.setattr(bridge._RemoteCLIDebugLog, "_append_records", slow_append)
    original_process = bridge._ForegroundCLIProcess
    owners = []
    owner_errors = []
    first_path = []

    class ForegroundProcess(original_process):
        def __init__(self, *arguments):
            super().__init__(*arguments)
            request = bridge._load_state_json(self.path)
            request["ownerPid"] = os.getpid()
            bridge._atomic_json(self.path, request)
            invocation_id = self.path.name.split(".")[0][len("remote-cli-"):]
            is_first = not owners
            if is_first:
                first_path.append(self.path)

            def run():
                try:
                    bridge.run_remote_cli(job_id, invocation_id)
                except BaseException as error:
                    owner_errors.append(error)

            owner = threading.Thread(target=run)
            owners.append(owner)
            owner.start()
            if is_first:
                assert blocked.wait(5), "The first CLI did not reach the delayed debug append."
            else:
                assert blocked.is_set() and not release.is_set()
                second_started.set()

    monkeypatch.setattr(bridge, "_ForegroundCLIProcess", ForegroundProcess)
    configured = args()
    configured.managed_job_id = job_id
    finished = threading.Event()
    results = []
    parser_errors = []

    def consume():
        try:
            results.append(bridge._consume_start_chat(configured, tmp_path, "hello", None, []))
        except BaseException as error:
            parser_errors.append(error)
        finally:
            finished.set()

    parser = threading.Thread(target=consume)
    parser.start()
    try:
        assert finished.wait(5), "Recovery waited for the delayed debug writer."
        assert parser_errors == []
        assert results[0]["state"] == "turn-completed"
        assert second_started.is_set()
        assert len(commands) == 2
        assert "--query" in commands[0] and "--query" not in commands[1]
        assert json.loads(commands[1][commands[1].index("--body") + 1])["StreamOptions.Cursor"] == "v1.debug.1"
        assert not first_path[0].exists()
        assert not first_path[0].with_suffix(".stdout").exists()
    finally:
        release.set()
        parser.join(10)
        for owner in owners:
            owner.join(10)
        assert not parser.is_alive()
        assert all(not owner.is_alive() for owner in owners)
    assert owner_errors == []
    saved = records(tmp_path)
    assert "fake-password" not in json.dumps(saved)
    assert "fake-hil-never-log" not in json.dumps(saved)
    if not log_fails:
        output = "".join(row["text"] for row in saved if row["kind"] == "stdout")
        assert '"unfinished' in output  # Captured before the parser removed its mailbox.
        outcomes = [row for row in saved if row["kind"] == "cli-result"]
        assert len(outcomes) == 2
        assert all(row["cliElapsedMs"] >= 0 and row["resultReadyDelayMs"] >= 0 for row in outcomes)
        assert all(row["queueToLaunchMs"] >= 0 for row in outcomes)
        assert len([row for row in saved if row["kind"] == "debug-complete"]) == 2
    ends = [row for row in saved if row["kind"] == "end"]
    assert [row["committedCursor"] for row in ends] == ["v1.debug.1", "v1.debug.2"]
    assert [row["committedEventCount"] for row in ends] == [1, 1]
