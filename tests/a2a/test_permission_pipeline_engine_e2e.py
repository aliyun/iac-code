"""HTTP cold permission recovery with the production PipelineRunner and StepExecutor."""

import json
from pathlib import Path

import pytest

from tests.a2a.test_permission_handoff_process_e2e import assert_success, count_lines
from tests.a2a.test_permission_prompt_e2e import Server, _permission, _reply

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]
PIPELINE = Path(__file__).resolve().parents[2] / "scripts/a2a/e2e/permission_wait/real_pipeline"


@pytest.mark.parametrize("decision", ["allow_once", "deny"])
def test_real_pipeline_permission_recovery_completes_step_and_accepts_next_prompt(tmp_path, decision):
    (tmp_path / "workspace").mkdir()
    server = Server(tmp_path, pipeline_dir=str(PIPELINE), tool_count=1)
    try:
        server.start()
        first = server.send("original")
        permission = _permission(first)
        assert count_lines(tmp_path / "executions.jsonl") == 0
        server.stop()
        server.start()
        events = server.send(_reply(permission, decision), task_id=permission["requestTaskId"])
        output = json.dumps(events)
        assert "TASK_STATE_FAILED" not in output, events
        assert not any("error" in event for event in events), events
        assert "pipeline_completed" in output and "pipeline_handoff_ready" in output, events
        assert count_lines(tmp_path / "executions.jsonl") == int(decision == "allow_once")
        journals = list((tmp_path / "config").rglob("a2a-events.jsonl"))
        assert len(journals) == 1
        journal = journals[0].read_text(encoding="utf-8")
        assert "step_completed" in journal and "protected_write" in journal
        assert "pipeline_completed" in journal and "rollback_started" not in journal
        assert_success(server.send("new prompt"), "handled new prompt")
        calls = count_lines(tmp_path / "model-inputs.jsonl")
        server.send(_reply(permission, decision), task_id=permission["requestTaskId"])
        server.stop()
        server.start()
        server.send(_reply(permission, decision), task_id=permission["requestTaskId"])
        assert count_lines(tmp_path / "model-inputs.jsonl") == calls
        assert count_lines(tmp_path / "executions.jsonl") == int(decision == "allow_once")
    finally:
        server.stop()
        server.client.close()
