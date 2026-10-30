"""Concurrent candidate permissions survive replacement HTTP workers."""

import json
from pathlib import Path

import pytest
import yaml

from .test_permission_handoff_process_e2e import WorkerPermissionProbe, count_lines, document, wait_for
from .test_permission_prompt_e2e import Server, _permission, _reply

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


class ParallelPermissionScenario:
    def __init__(self, root: Path, *, tool_count: int = 1):
        self.root = root
        self.tool_count = tool_count
        self.pipeline = root / "parallel-fixture"
        self.pipeline.mkdir()
        (root / "workspace").mkdir()
        (self.pipeline / "architecture.md").write_text("BUILD_CANDIDATES", encoding="utf-8")
        marker = "PROTECTED_CANDIDATE" if tool_count == 1 else "ORDERED_CANDIDATE"
        (self.pipeline / "protected.md").write_text(marker + " {candidate.name}", encoding="utf-8")
        (self.pipeline / "pipeline.yaml").write_text(
            """name: permission-engine-fixture
context_dependencies:
  architecture: []
  evaluated: [architecture]
sub_pipelines:
  evaluate_candidate:
    iterate_over: architecture.candidates
    context_fields_from_parent: []
    steps:
      - id: protected
        conclusion_field: result
        forward: null
        prompt: protected.md
        context_fields: [candidate]
steps:
  - id: architecture
    conclusion_field: architecture
    forward: evaluate
    prompt: architecture.md
  - id: evaluate
    type: parallel_sub_pipeline
    sub_pipeline: evaluate_candidate
    conclusion_field: evaluated
    forward: null
""",
            encoding="utf-8",
        )

    def worker(self, name: str, fault: str | None = None) -> Server:
        return Server(
            self.root,
            pipeline_dir=str(self.pipeline),
            parallel_candidates=2,
            tool_count=self.tool_count,
            resident_timeout=0.1,
            config_dir=str(self.root / name / "config"),
            persistence_dir=str(self.root / "shared/a2a"),
            backup_dir=str(self.root / "shared/backup"),
            observation_dir=str(self.root / "shared"),
            strict_permission_pause=True,
            fault=fault,
        )

    @staticmethod
    def permissions(events):
        found = {}
        for event in events:
            result = event.get("result", {})
            update = result.get("statusUpdate") or result.get("task") or result
            permission = update.get("metadata", {}).get("iac_code", {}).get("input", {})
            if permission.get("kind") == "permission":
                found[permission["inputId"]] = permission
        return list(found.values())

    def sidecar(self, worker_name):
        paths = list((self.root / worker_name / "config/projects").glob("*/*/pipeline/meta.yaml"))
        assert len(paths) == 1
        return yaml.safe_load(paths[0].read_text(encoding="utf-8"))

    def checkpoint(self, worker_name, input_id):
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / worker_name / "config").rglob("permission-waits/pwb_*.json")
        ]
        return next(record for record in records if record.get("inputId") == input_id)


@pytest.mark.parametrize("first_candidate", [0, 1])
def test_parallel_permissions_keep_original_cards_across_replacement_workers(tmp_path, first_candidate):
    scenario = ParallelPermissionScenario(tmp_path)
    old = scenario.worker("old")
    replacement = scenario.worker("replacement")
    final = scenario.worker("final")
    try:
        old.start()
        first = old.send("original")
        permissions = sorted(scenario.permissions(first), key=lambda card: card["toolUseId"])
        assert len(permissions) == 2, first
        assert "pipeline_completed" not in json.dumps(first)
        wait_for(lambda: (document(tmp_path).get("inputHandoff") or {}).get("phase") == "COMMITTED")
        before = document(tmp_path)
        assert len(before["inputHandoff"]["boundaries"]) == 2
        assert count_lines(tmp_path / "shared/executions.jsonl") == 0
        old.stop()

        replacement.start()
        selected, waiting = permissions[first_candidate], permissions[1 - first_candidate]
        events = replacement.send(_reply(selected), task_id=selected["requestTaskId"])
        assert not any("error" in event for event in events), events
        assert "pipeline_failed" not in json.dumps(events), events
        assert "pipeline_completed" not in json.dumps(events)
        assert count_lines(tmp_path / "shared/executions.jsonl") == 1
        recovered = scenario.permissions(events)
        restored = next(card for card in recovered if card["inputId"] == waiting["inputId"])
        for key in ("requestTaskId", "contextId", "toolUseId", "toolName"):
            assert restored[key] == waiting[key]
        wait_for(lambda: (document(tmp_path).get("inputHandoff") or {}).get("phase") == "COMMITTED")
        candidates = scenario.sidecar("replacement")["execution"]["candidates"]
        assert candidates[str(first_candidate)]["status"] == "completed"
        assert candidates[str(1 - first_candidate)]["status"] == "suspended"
        assert document(tmp_path)["executionId"] != before["executionId"]
        replacement.stop()

        final.start()
        events = final.send(_reply(waiting), task_id=waiting["requestTaskId"])
        assert not any("error" in event for event in events), events
        assert "pipeline_completed" in json.dumps(events), events
        assert count_lines(tmp_path / "shared/executions.jsonl") == 2
        executed = [
            json.loads(line)["value"]
            for line in (tmp_path / "shared/executions.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert sorted(executed) == ["tool-candidate-0", "tool-candidate-1"]
        final.send(_reply(permissions[0]), task_id=permissions[0]["requestTaskId"])
        final.send(_reply(permissions[1]), task_id=permissions[1]["requestTaskId"])
        assert count_lines(tmp_path / "shared/executions.jsonl") == 2
    finally:
        for server in (final, replacement, old):
            server.stop()
            server.client.close()


def test_successor_permission_keeps_other_original_candidate_card_across_cold_recovery(tmp_path):
    scenario = ParallelPermissionScenario(tmp_path, tool_count=2)
    workers = [scenario.worker("worker-" + str(index)) for index in range(5)]
    executions = tmp_path / "shared/executions.jsonl"
    try:
        workers[0].start()
        cards = scenario.permissions(workers[0].send("original"))
        assert len(cards) == 2
        first, other = cards
        assert first["toolUseId"] == other["toolUseId"] == "tool-1"
        current = first
        for index in range(4):
            wait_for(lambda: (document(tmp_path).get("inputHandoff") or {}).get("phase") == "COMMITTED")
            workers[index].stop()
            workers[index + 1].start()
            events = workers[index + 1].send(_reply(current), task_id=current["requestTaskId"])
            output = json.dumps(events)
            assert not any("error" in event for event in events), events
            assert "pipeline_failed" not in output, events
            assert count_lines(executions) == (0, 2, 2, 4)[index]
            if index == 0:
                pending = scenario.permissions(events)
                assert any(card["inputId"] == other["inputId"] for card in pending)
                current = next(card for card in pending if card["toolUseId"] == "tool-2")
                assert current["inputId"] not in {first["inputId"], other["inputId"]}
            elif index == 1:
                current = other
            elif index == 2:
                current = next(card for card in scenario.permissions(events) if card["toolUseId"] == "tool-2")
            else:
                assert "pipeline_completed" in output
        executed = [json.loads(line)["value"] for line in executions.read_text(encoding="utf-8").splitlines()]
        assert sorted(executed) == ["tool-1", "tool-1", "tool-2", "tool-2"]
    finally:
        for worker in workers:
            worker.stop()
            worker.client.close()


def test_late_candidate_completion_preserves_already_suspended_sibling_handoff(tmp_path):
    scenario = ParallelPermissionScenario(tmp_path)
    old, final = scenario.worker("old", "late-second-candidate"), scenario.worker("final")
    try:
        old.start()
        first = None
        with old.stream("original") as response:
            stream = old.events(response)
            for event in stream:
                card = _permission([event], required=False)
                if card is None:
                    continue
                if card["toolUseId"] == "tool-candidate-0":
                    first = card
                    wait_for(lambda: scenario.checkpoint("old", card["inputId"])["phase"] == "SUSPENDED")
                    (tmp_path / "shared/release-second-candidate").touch()
                else:
                    assert first is not None
                    assert scenario.checkpoint("old", first["inputId"])["phase"] == "SUSPENDED"
                    answered = old.send(_reply(card), task_id=card["requestTaskId"])
                    assert not any("error" in value for value in answered), answered
                    list(stream)
                    break
        assert first is not None
        old.wait_for_permission_handoff(first)
        candidates = scenario.sidecar("old")["execution"]["candidates"]
        assert candidates["0"]["status"] == "suspended"
        assert candidates["1"]["status"] == "completed"
        assert count_lines(tmp_path / "shared/executions.jsonl") == 1
        assert WorkerPermissionProbe(tmp_path, "old").inspect()["status"] == "ready"
        old.stop()
        final.start()
        completed = final.send(_reply(first), task_id=first["requestTaskId"])
        assert "pipeline_completed" in json.dumps(completed)
        assert count_lines(tmp_path / "shared/executions.jsonl") == 2
    finally:
        (tmp_path / "shared/release-second-candidate").touch()
        for worker in (final, old):
            worker.stop()
            worker.client.close()


def test_failed_tool_result_append_retains_full_boundary_and_blocks_terminal_pause(tmp_path):
    scenario = ParallelPermissionScenario(tmp_path)
    old = scenario.worker("old")
    replacement = scenario.worker("replacement", "tool-result-append-failure")
    try:
        old.start()
        cards = sorted(scenario.permissions(old.send("original")), key=lambda card: card["toolUseId"])
        old.wait_for_permission_handoff(cards[0])
        old.stop()
        replacement.start()
        replacement.send(_reply(cards[0]), task_id=cards[0]["requestTaskId"])
        wait_for(lambda: (tmp_path / "shared/post-failure-handoff-checked").exists())
        assert count_lines(tmp_path / "shared/executions.jsonl") == 1
        checkpoint = scenario.checkpoint("replacement", cards[0]["inputId"])
        assert checkpoint["phase"] != "RESOLVED"
        assert isinstance(checkpoint["continuationFrame"], dict)
        assert checkpoint["toolUseId"] == cards[0]["toolUseId"]
        assert (document(tmp_path).get("inputHandoff") or {}).get("phase") != "COMMITTED"
        assert WorkerPermissionProbe(tmp_path, "replacement").inspect()["status"] == "pending"
        current = document(tmp_path)
        if current["phase"] != "terminated":
            stopped = replacement.client.post(
                "/iac-code/execution/terminate",
                json={
                    "contextId": cards[0]["contextId"],
                    "taskId": cards[0]["requestTaskId"],
                    "expectedExecutionId": current["executionId"],
                    "requestId": "stop-after-result-io-failure",
                    "connectionEpoch": max(0, current["connectionEpoch"] + 1),
                    "reason": "user_canceled",
                },
            )
            assert stopped.status_code in {200, 202}, stopped.text
            wait_for(lambda: document(tmp_path).get("releaseReady") is True)
        assert WorkerPermissionProbe(tmp_path, "replacement").inspect()["status"] == "pending"
        checkpoint = scenario.checkpoint("replacement", cards[0]["inputId"])
        assert checkpoint["phase"] != "RESOLVED" and "continuationFrame" in checkpoint
    finally:
        for worker in (replacement, old):
            worker.stop()
            worker.client.close()
