from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.ci.live_diagnostics import collect_live_diagnostics


def test_diagnostics_distinguish_incidental_candidate_text_and_real_events(tmp_path: Path) -> None:
    path = tmp_path / "turn.events.jsonl"
    path.write_text(json.dumps({"message": {"text": "candidate_step_started fake-secret"}}), encoding="utf-8")
    assert collect_live_diagnostics(tmp_path, {})["candidate_marker_without_event"] is True
    path.write_text(json.dumps({"pipeline": {"eventType": "candidate_step_started", "data": {"text": "fake-secret"}}}),
                    encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["candidate_marker_without_event"] is False
    assert facts["a2a_event_counts"] == {"candidate_step_started": 1}
    assert "fake-secret" not in json.dumps(facts)


def test_diagnostics_keep_cleanup_categories_and_wait_names_without_raw_bodies(tmp_path: Path) -> None:
    (tmp_path / "cleanup-result.json").write_text(json.dumps({
        "resources": [{"stackId": "private-stack"}], "deletedStackIds": [],
        "failures": ["private-stack: ownership could not be proven", "private-stack: cleanup subprocess exited 1"],
    }), encoding="utf-8")
    (tmp_path / "cleanup-private-stack.log").write_text("NotFound.Stack private-key private-stack", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(json.dumps({
        "type": "expect", "passed": False, "description": "candidate selection controls ready", "tail": "private-key",
    }) + "\n" + json.dumps({"type": "expect", "passed": False, "description": "private-key"}), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {
        "abort_reason": "TimeoutError: candidate selection input was not accepted",
    })
    assert facts["cleanup_failure_categories"] == {"ownership_unproven": 1, "delete_subprocess_failed": 1}
    assert facts["cleanup_known_codes"] == ["NotFound.Stack"]
    assert facts["cleanup_resource_count"] == 1
    assert facts["cleanup_deleted_count"] == 0
    assert facts["failed_wait"] == "candidate selection controls ready"
    assert facts["abort_type"] == "TimeoutError"
    assert facts["abort_category"] == "selection_not_accepted"
    assert "private-" not in json.dumps(facts)


def test_diagnostics_report_durable_unanswered_input_and_image_confirmation(tmp_path: Path) -> None:
    meta = tmp_path / "config/projects/p/s/pipeline/meta.yaml"
    meta.parent.mkdir(parents=True)
    meta.write_text(yaml.safe_dump({"current_step": "materialize_selected_candidate", "execution": {
        "pending_input_kind": "ask_user_question", "pending_ask_user_question_input": {
            "question": "private-question", "toolUseId": "private-id",
        },
    }}), encoding="utf-8")
    (tmp_path / "turn.events.jsonl").write_text(json.dumps({"eventType": "input_received", "data": {
        "kind": "deployment_confirmation", "has_images": True, "selected_value": "private-image-caption",
    }}), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["pending_step"] == "materialize_selected_candidate"
    assert facts["pending_input_kind"] == "ask_user_question"
    assert facts["pending_question_answered"] is False
    assert facts["a2a_event_counts"] == {"input_received": 1, "confirmation_free_text": 1, "confirmation_image": 1}
    assert "private-" not in json.dumps(facts)
