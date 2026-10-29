"""Process E2E for output-limit continuation through real A2A and provider adapters."""

import json
from pathlib import Path

import pytest

from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.services.session_storage import SessionStorage
from scripts.ci.output_limit_model import OutputLimitModel
from tests.a2a.test_permission_handoff_process_e2e import assert_success
from tests.a2a.test_permission_prompt_e2e import Server

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]
PIPELINE = Path(__file__).resolve().parents[2] / "scripts/a2a/e2e/permission_wait/real_pipeline"
CONTINUATION = "Continue from where you stopped. Do not repeat completed work."


def _run(tmp_path, monkeypatch, *, protocol: str, scenario: str, pipeline: bool = False):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    with OutputLimitModel(protocol, scenario) as model:
        server = Server(
            tmp_path,
            protocol=protocol,
            model_url=model.url,
            max_turns=6,
            **({"pipeline_dir": str(PIPELINE)} if pipeline else {}),
        )
        try:
            server.start()
            events = server.send("original")
        finally:
            server.stop()
            server.client.close()
        assert not model.errors, model.errors
        return events, model.requests


def _saved(tmp_path):
    context = A2APersistenceStore(tmp_path / "a2a").load_context("context-1")
    return SessionStorage().load(context.cwd, context.session_id)


def _assert_final_failure(events):
    updates = [event["result"]["statusUpdate"] for event in events if "statusUpdate" in event.get("result", {})]
    assert updates[-1]["status"]["state"] == "TASK_STATE_FAILED"
    assert not any(update.get("metadata", {}).get("iac_code", {}).get("assistantFinal") for update in updates)


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_text_continues_after_output_limit(tmp_path, monkeypatch, protocol):
    events, requests = _run(tmp_path, monkeypatch, protocol=protocol, scenario="text")

    assert_success(events, "second fragment")
    assert len(requests) == 2
    second_request = json.dumps(requests[1])
    assert "first fragment" in second_request
    assert CONTINUATION in second_request
    final_updates = [
        event.get("result", {}).get("statusUpdate", {})
        for event in events
        if event.get("result", {})
        .get("statusUpdate", {})
        .get("metadata", {})
        .get("iac_code", {})
        .get("assistantFinal", {})
        .get("complete")
    ]
    assert len(final_updates) == 1
    assert final_updates[0]["status"]["message"]["parts"][0]["text"] == "first fragmentsecond fragment"
    saved = _saved(tmp_path)
    assert sum(message.get_text() == "first fragment" for message in saved) == 1
    assert sum(message.get_text() == "second fragment" for message in saved) == 1
    assert sum(message.get_text() == CONTINUATION for message in saved) == 1


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_truncated_tool_turn_is_discarded(tmp_path, monkeypatch, protocol):
    events, requests = _run(tmp_path, monkeypatch, protocol=protocol, scenario="tool")

    assert_success(events, "second fragment")
    assert len(requests) == 2
    assert not (tmp_path / "executions.jsonl").exists()
    output = json.dumps(events)
    assert '"status": "discarded"' in output and "discarded-call" in output
    assert '"status": "completed", "toolUseId": "discarded-call"' not in output
    assert "discarded-call" not in json.dumps(requests[1])
    assert "discarded-call" not in json.dumps([message.model_dump(mode="json") for message in _saved(tmp_path)])


def test_repeated_output_limit_fails_with_bounded_requests(tmp_path, monkeypatch):
    events, requests = _run(tmp_path, monkeypatch, protocol="chat", scenario="exhausted")

    output = json.dumps(events)
    assert len(requests) == 4
    assert "TASK_STATE_FAILED" in output
    assert "output token limit" in output
    assert not (tmp_path / "executions.jsonl").exists()
    _assert_final_failure(events)


def test_output_limit_without_visible_text_fails_immediately(tmp_path, monkeypatch):
    events, requests = _run(tmp_path, monkeypatch, protocol="chat", scenario="empty")

    output = json.dumps(events)
    assert len(requests) == 1
    assert "TASK_STATE_FAILED" in output
    assert "output token limit" in output
    _assert_final_failure(events)


def test_pipeline_does_not_complete_after_output_limit(tmp_path, monkeypatch):
    events, requests = _run(tmp_path, monkeypatch, protocol="chat", scenario="pipeline", pipeline=True)

    output = json.dumps(events)
    # The fixture pipeline retries its failed step three times; each agent
    # attempt must still stop after the fourth output-limited response.
    assert len(requests) == 12
    assert "TASK_STATE_FAILED" in output
    assert "pipeline_completed" not in output
    assert "complete_step" not in json.dumps([message.model_dump(mode="json") for message in _saved(tmp_path)])
    _assert_final_failure(events)
