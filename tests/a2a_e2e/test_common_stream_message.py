from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path

import pytest

from scripts.a2a.e2e import common


@pytest.mark.parametrize("return_code", [0, 1])
def test_preflight_records_process_exit_without_changing_success(monkeypatch, tmp_path, return_code):
    monkeypatch.setattr(common.subprocess, "run", lambda *args, **kwargs: common.subprocess.CompletedProcess(
        args[0], return_code, stdout="OK", stderr="",
    ))
    result = common.run_llm_preflight(python_cmd=["python"], cwd=str(tmp_path), env={}, timeout=60, run_dir=tmp_path)
    assert result["ok"] is (return_code == 0)
    assert result["timedOut"] is False
    assert result["returnCode"] == return_code
    assert json.loads((tmp_path / "preflight.json").read_text(encoding="utf-8"))["timedOut"] is False


def test_preflight_timeout_remains_failure_and_retains_explicit_timeout(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise common.subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"partial response")
    monkeypatch.setattr(common.subprocess, "run", timeout)
    result = common.run_llm_preflight(python_cmd=["python"], cwd=str(tmp_path), env={}, timeout=60, run_dir=tmp_path)
    assert result["ok"] is False
    assert result["timedOut"] is True
    assert result["returnCode"] is None
    assert result["summary"].startswith("timed out after 60s")


def test_capture_scrubs_explicit_config_credentials_inside_strings_without_mutating_response(tmp_path):
    config = tmp_path / 'isolated-config'
    config.mkdir()
    (config / '.credentials.yml').write_text('dashscope: fake-llm-key-for-capture\n', encoding='utf-8')
    (config / '.cloud-credentials.yml').write_text(
        'aliyun:\n  access_key_id: fake-cloud-key-id\n  access_key_secret: fake-cloud-key-secret\n'
        '  sts_token: fake-cloud-sts-token\n  region_id: cn-hangzhou\n', encoding='utf-8',
    )
    response = {'snapshot': {'display': {'text': (
        'SDK error: fake-cloud-key-id fake-cloud-key-secret fake-cloud-sts-token fake-llm-key-for-capture'
    )}}, 'VpcId': 'vpc-test-fixture', 'region': 'cn-hangzhou'}
    captured = common._redact_json_value(response, {'IAC_CODE_CONFIG_DIR': str(config)})
    assert captured['snapshot']['display']['text'] == 'SDK error: <redacted> <redacted> <redacted> <redacted>'
    assert captured['VpcId'] == 'vpc-test-fixture' and captured['region'] == 'cn-hangzhou'
    assert 'fake-cloud-key-id' in response['snapshot']['display']['text']
    # No default or global credential lookup when a caller supplies no config.
    assert common._redact_sensitive_text('fake-cloud-key-id', {}) == 'fake-cloud-key-id'


def test_capture_scrubs_current_and_recent_pre_refresh_keys(tmp_path):
    path = tmp_path / '.cloud-credentials.yml'
    path.write_text('access_key_secret: fake-before-refresh-secret\n', encoding='utf-8')
    env = {'IAC_CODE_CONFIG_DIR': str(tmp_path)}
    assert common._redact_sensitive_text('fake-before-refresh-secret', env) == '<redacted>'
    path.write_text('access_key_secret: fake-after-refresh-secret\n', encoding='utf-8')
    assert common._redact_sensitive_text('fake-before-refresh-secret fake-after-refresh-secret', env) == (
        '<redacted> <redacted>'
    )


def test_capture_does_not_read_credential_symlink_outside_isolated_config(tmp_path):
    config = tmp_path / 'isolated'
    config.mkdir()
    external = tmp_path / 'external-secret.yml'
    external.write_text('access_key_secret: fake-external-secret\n', encoding='utf-8')
    try:
        (config / '.cloud-credentials.yml').symlink_to(external)
    except OSError:
        pytest.skip('symlinks unavailable on this host')
    assert common._capture_credential_values({'IAC_CODE_CONFIG_DIR': str(config)}) == ()


@pytest.mark.parametrize(("states", "finished"), [
    (["TASK_STATE_WORKING", "TASK_STATE_COMPLETED"], True),
    (["TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"], True),
    (["TASK_STATE_INPUT_REQUIRED", "TASK_STATE_FAILED"], False),
    (["TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"], False),
    (["TASK_STATE_INPUT_REQUIRED", "TASK_STATE_WORKING"], False),
    ([], False),
])
def test_normal_turn_completion_is_decided_by_final_status(states, finished):
    summary = common.StreamSummary(name="normal", prompt="question", status_states=states)
    assert common._normal_turn_finished(summary) is finished


def _json_response(payload: dict[str, object]) -> io.BytesIO:
    response = io.BytesIO(json.dumps(payload).encode("utf-8"))
    headers = Message()
    headers["Content-Type"] = "application/json"
    response.headers = headers
    return response


def test_stream_message_accepts_json_task_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = _json_response(
        {"result": {"id": "task-1", "contextId": "ctx-1", "status": {"state": "TASK_STATE_COMPLETED"}}}
    )
    monkeypatch.setattr(common, "urlopen", lambda request, timeout: response)

    summary = common.stream_message(
        server_url="http://example.invalid", cwd=str(tmp_path), prompt="answer", name="answer",
        run_dir=tmp_path, timeout=1,
    )

    assert summary.status_states == ["TASK_STATE_COMPLETED"]
    assert summary.event_count == 1
    assert summary.response_content_type == "application/json"


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_stream_observer_receives_original_usage_before_secret_safe_log_serialization(tmp_path, monkeypatch, transport):
    event = {"eventType": "usage", "eventId": "usage-1", "data": {"totalTokens": 123, "apiKey": "fake-secret"}}
    if transport == "json":
        response = _json_response(event)
    else:
        response = io.BytesIO(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
        response.headers = Message()
        response.headers["Content-Type"] = "text/event-stream"
    monkeypatch.setattr(common, "urlopen", lambda request, timeout: response)
    observed = []
    common.stream_message(
        server_url="http://example.invalid", cwd=str(tmp_path), prompt="answer", name="answer",
        run_dir=tmp_path, timeout=1, on_event=lambda value: observed.append(value["data"]["totalTokens"]),
    )
    assert observed == [123]
    log = (tmp_path / "answer.events.jsonl").read_text(encoding="utf-8")
    assert "fake-secret" not in log
    assert json.loads(log)["data"]["totalTokens"] == "<redacted>"


def test_stream_message_surfaces_json_rpc_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = _json_response({"error": {"code": -32602, "message": "invalid request"}})
    monkeypatch.setattr(common, "urlopen", lambda request, timeout: response)

    with pytest.raises(common.JsonRpcResponseError, match="JSON-RPC error") as raised:
        common.stream_message(
            server_url="http://example.invalid", cwd=str(tmp_path), prompt="answer", name="answer",
            run_dir=tmp_path, timeout=1,
        )
    assert raised.value.code == -32602


def test_sse_rpc_error_is_not_treated_as_finished_work(tmp_path, monkeypatch):
    response = io.BytesIO(b'data: {"result":{"taskId":"task-1","contextId":"ctx-1",'
        b'"status":{"state":"TASK_STATE_WORKING"}}}\n\n'
        b'data: {"error":{"code":-32603,"message":"private cloud transcript"}}\n\n')
    response.headers = Message()
    response.headers['Content-Type'] = 'text/event-stream'
    monkeypatch.setattr(common, 'urlopen', lambda *_args, **_kwargs: response)
    with pytest.raises(common.JsonRpcResponseError) as raised:
        common.stream_message(server_url='http://example.invalid', cwd=str(tmp_path), prompt='private prompt',
                              name='initial', run_dir=tmp_path, timeout=1)
    assert raised.value.code == -32603 and 'private' not in str(raised.value)
    diagnostic = json.loads((tmp_path / 'stream-diagnostics.jsonl').read_text(encoding='utf-8'))
    assert diagnostic['outcome'] == 'error' and diagnostic['jsonrpc_error_code'] == -32603
    assert diagnostic['last_state'] == 'TASK_STATE_WORKING'
    assert not any(word in json.dumps(diagnostic) for word in ('private', 'task-1', 'ctx-1'))


def test_clean_working_eof_is_recorded_without_claiming_completion(tmp_path, monkeypatch):
    response = _json_response({'result': {'id': 'task-1', 'status': {'state': 'TASK_STATE_WORKING'}}})
    monkeypatch.setattr(common, 'urlopen', lambda *_args, **_kwargs: response)
    summary = common.stream_message(server_url='http://example.invalid', cwd=str(tmp_path), prompt='goal',
                                  name='initial', run_dir=tmp_path, timeout=1)
    diagnostic = json.loads((tmp_path / 'stream-diagnostics.jsonl').read_text(encoding='utf-8'))
    assert diagnostic['outcome'] == 'eof' and diagnostic['last_state'] == 'TASK_STATE_WORKING'
    assert not common._normal_turn_finished(summary)


@pytest.mark.parametrize(('exception', 'category'), [
    (TimeoutError('private timeout'), 'timeout'),
    (ConnectionResetError('private host'), 'connection'),
    (common.URLError(TimeoutError('private url')), 'timeout'),
    (common.URLError('private host'), 'url'),
    (OSError('private path'), 'os'),
])
def test_stream_transport_failure_retains_category_without_message(tmp_path, monkeypatch, exception, category):
    def fail(*args, **kwargs):
        raise exception
    monkeypatch.setattr(common, 'urlopen', fail)
    with pytest.raises(RuntimeError):
        common.stream_message(server_url='http://example.invalid', cwd=str(tmp_path), prompt='goal',
                              name='initial', run_dir=tmp_path, timeout=1)
    diagnostic = json.loads((tmp_path / 'stream-diagnostics.jsonl').read_text(encoding='utf-8'))
    assert diagnostic['error_kind'] == category
    assert diagnostic['outcome'] == 'error'
    assert 'private' not in json.dumps(diagnostic)
