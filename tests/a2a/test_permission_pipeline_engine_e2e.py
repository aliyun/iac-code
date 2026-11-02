"""HTTP cold permission recovery with the production PipelineRunner and StepExecutor."""

import json
from pathlib import Path

import pytest

from tests.a2a.test_permission_handoff_process_e2e import assert_success, count_lines, wait_for
from tests.a2a.test_permission_prompt_e2e import Server, _permission, _reply

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]
PIPELINE = Path(__file__).resolve().parents[2] / "scripts/a2a/e2e/permission_wait/real_pipeline"


@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "cold-recovery"])
def test_pipeline_handoff_folds_original_request_without_losing_history(tmp_path, restart):
    (tmp_path / "workspace").mkdir()
    original = "请让我选择已有 VPC。\n只选择，不创建、修改或删除资源；收到选择后结束本轮。"
    server = Server(tmp_path, pipeline_dir=str(PIPELINE), tool_count=1, resident_timeout=0.1)
    try:
        server.start()
        permission = _permission(server.send(original))
        if restart:
            wait_for(
                lambda: (
                    (
                        json.loads((tmp_path / "a2a/execution-control/context-1.json").read_text(encoding="utf-8")).get(
                            "inputHandoff"
                        )
                        or {}
                    ).get("phase")
                    == "COMMITTED"
                )
            )
            server.stop()
            server.start()
        completed = server.send(_reply(permission), task_id=permission["requestTaskId"])
        assert "pipeline_handoff_ready" in json.dumps(completed), completed
        if restart:
            server.stop()
            server.start()
        assert_success(server.send("new prompt"), "handled new prompt")
        inputs_path = tmp_path / "model-inputs.jsonl"
        inputs = [json.loads(line) for line in inputs_path.read_text(encoding="utf-8").splitlines()]
        messages = inputs[-1]
        assert [message["role"] for message in messages] == ["user", "user"]
        assert messages[-1]["content"] == "new prompt"
        summary = messages[0]["content"]
        assert summary.startswith("[Pipeline Handoff Context]\n")
        included, _ = json.JSONDecoder().raw_decode(summary.split("Included context:\n", 1)[1])
        assert included["original_user_request"] == original
        assert included["protected"] == {"status": "ready"}
        assert "This is injected context for the assistant, not a user request." in summary
        assert "Safety requirements for normal chat:" in summary
        assert "obtain a fresh, explicit confirmation from the user in normal chat" in summary
        assert "pipeline-managed automatic cleanup" in summary

        # AgentLoop writes its full history after a turn. Folding must affect
        # only the provider view, including after another cold Normal resume.
        root_sessions = [
            p for p in (tmp_path / "config/projects").rglob("session.jsonl") if "transcripts" not in p.parts
        ]
        assert len(root_sessions) == 1
        history = [json.loads(line) for line in root_sessions[0].read_text(encoding="utf-8").splitlines()]
        assert sum(message.get("content") == original for message in history) == 1
        assert count_lines(tmp_path / "executions.jsonl") == 1
        calls = len(inputs)
        server.send(_reply(permission), task_id=permission["requestTaskId"])
        server.stop()
        server.start()
        server.send(_reply(permission), task_id=permission["requestTaskId"])
        assert count_lines(inputs_path) == calls
        assert count_lines(tmp_path / "executions.jsonl") == 1
        assert_success(server.send("another prompt"), "handled new prompt")
        next_messages = json.loads(inputs_path.read_text(encoding="utf-8").splitlines()[-1])
        assert not any(message["content"] == original for message in next_messages)
        assert next_messages[-1]["content"] == "another prompt"
        history = [json.loads(line) for line in root_sessions[0].read_text(encoding="utf-8").splitlines()]
        assert sum(message.get("content") == original for message in history) == 1
    finally:
        server.stop()
        server.client.close()


@pytest.mark.parametrize("decision", ["allow_once", "deny"])
def test_real_pipeline_permission_recovery_completes_step_and_accepts_next_prompt(tmp_path, decision):
    (tmp_path / "workspace").mkdir()
    server = Server(tmp_path, pipeline_dir=str(PIPELINE), tool_count=1, resident_timeout=0.1)
    try:
        server.start()
        first = server.send("original")
        permission = _permission(first)
        assert count_lines(tmp_path / "executions.jsonl") == 0
        wait_for(
            lambda: (
                (
                    json.loads((tmp_path / "a2a/execution-control/context-1.json").read_text(encoding="utf-8")).get(
                        "inputHandoff"
                    )
                    or {}
                ).get("phase")
                == "COMMITTED"
            )
        )
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
