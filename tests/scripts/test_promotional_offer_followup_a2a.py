from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.ci.run_promotional_offer_followup_a2a import (
    DESTINATION,
    activity_followup_checks,
    explicit_activity_checks,
    minimum_memory_observation,
    pipeline_facts,
    visible_pipeline_text,
)


@pytest.mark.parametrize("status, constraints, expected", [
    ("awaiting_selection", [{"operator": "gte", "value": 2}], "passed"),
    ("awaiting_selection", [{"operator": "gte", "value": 2}, {"operator": "eq", "value": 4}], "failed"),
    ("awaiting_selection", [{"operator": "gte", "value": 2}, {"operator": "gte", "value": 4}], "failed"),
    ("awaiting_selection", [], "failed"),
    (None, [], "not_exercised"),
])
def test_memory_quality_keeps_errors_and_missing_coverage_as_nonblocking(status, constraints, expected):
    observation = minimum_memory_observation({"status": status}, constraints)
    assert observation == {"status": expected, "blocking": False, "observed_constraints": constraints}


@pytest.mark.parametrize("visible_activity_link, has_candidate", [(True, True), (False, True), (True, False)])
def test_extraction_quality_does_not_block_e2e_or_mask_a_flow_failure(
    monkeypatch, tmp_path, visible_activity_link, has_candidate,
):
    from scripts.ci import run_promotional_offer_followup_a2a as runner

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))
    constraints = [
        {"property": "memory", "operator": "gte", "value": 2, "source_text": "最低2G"},
        {"property": "memory", "operator": "eq", "value": 4, "source_text": "推荐4G"},
    ]
    selection = {"status": "awaiting_selection", "candidates": [{"name": "ECS"}] if has_candidate else [],
                 "intent": {"hard_constraints": constraints}}
    conclusions = [selection, selection, selection]

    class Server:
        def __init__(self, **kwargs):
            self.stopped = False
            self.process = SimpleNamespace(poll=lambda: 0 if self.stopped else None)

        def start(self):
            pass

        def terminate(self):
            self.stopped = True

    class Capture:
        def __init__(self, *args):
            self.env = {}

        def start(self):
            return self

        def stop(self):
            return [{"attributes": {"gen_ai.request.model": "fixture"}}]

    def turn_index(path):
        return int(path.name.split("-")[1].split(".")[0]) - 1

    def stream(**kwargs):
        return SimpleNamespace(
            context_id="fixture", task_id="fixture", response_content_type="text/event-stream",
            last_status_state="TASK_STATE_INPUT_REQUIRED", normal_handoff_ready=False,
            last_input_required_step_id="solution_planning_and_selection",
        )

    monkeypatch.setattr(runner, "ManagedServer", Server)
    monkeypatch.setattr(runner, "ObserveCapture", Capture)
    monkeypatch.setattr(runner, "wait_for_server", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "stream_message", stream)
    monkeypatch.setattr(runner, "fetch_pipeline_state", lambda **kw: {})
    monkeypatch.setattr(runner, "completion_inputs", lambda _: [])
    monkeypatch.setattr(runner, "pipeline_facts", lambda path: (conclusions[turn_index(path)], []))
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda path: (
        DESTINATION if turn_index(path) == 2 and visible_activity_link else "方案说明"
    ))
    report = runner.run(SimpleNamespace(run_dir=tmp_path, provider="dashscope", model="fixture", timeout=1))
    assert report["status"] == ("passed" if visible_activity_link and has_candidate else "failed")
    assert "minimum 2 GiB remains the lower bound" not in report["checks"]
    assert report["model_quality"]["minimum_memory_extraction"] == {
        "status": "failed", "blocking": False, "observed_constraints": constraints,
    }
    assert report["turns"][0]["memory_constraints"] == constraints
    assert report["checks"]["initial plan displays candidates"] is has_candidate
    assert report["checks"]["explicit activity inquiry has a visible assistant link"] is visible_activity_link


@pytest.mark.parametrize("changes, plan_changes, expected", [
    ({}, {}, True),
    ({"assistant_link_visible": False}, {}, False),
    ({"pipeline_handoff": True, "task_state": "TASK_STATE_COMPLETED", "step": ""},
     {"status": "rejected", "rejection_reason": DESTINATION}, True),
    ({"pipeline_handoff": True, "task_state": "TASK_STATE_COMPLETED", "assistant_link_visible": False},
     {"status": "rejected", "rejection_reason": DESTINATION}, False),
    ({"pipeline_handoff": True, "task_state": "TASK_STATE_COMPLETED"},
     {"status": "rejected", "rejection_reason": "未保存链接"}, False),
    ({"pipeline_handoff": False, "task_state": "TASK_STATE_COMPLETED"},
     {"status": "rejected", "rejection_reason": DESTINATION}, False),
    ({"pipeline_handoff": True, "task_state": "TASK_STATE_FAILED"},
     {"status": "rejected", "rejection_reason": DESTINATION}, False),
    ({"pipeline_handoff": True, "task_state": "TASK_STATE_COMPLETED",
      "started_steps": ["materialize_selected_candidate"]},
     {"status": "rejected", "rejection_reason": DESTINATION}, False),
    ({"step": "materialize_selected_candidate"}, {}, False),
    ({"started_steps": ["materialize_selected_candidate"]}, {}, False),
    ({"task_state": "TASK_STATE_FAILED"}, {}, False),
    ({}, {"status": "selected"}, False),
    ({}, {"candidate_set_id": "new-batch"}, False),
    ({}, {"candidates": [{"name": "changed"}]}, False),
    ({}, {"intent": {"hard_constraints": []}}, False),
    ({}, {"candidates": []}, False),
])
def test_explicit_activity_inquiry_requires_visible_link_and_valid_consultation_or_handoff(
    changes, plan_changes, expected,
):
    evidence = {
        "task_state": "TASK_STATE_INPUT_REQUIRED", "step": "solution_planning_and_selection",
        "pipeline_handoff": False, "started_steps": [], "assistant_link_visible": True, **changes,
    }
    original_plan = {
        "status": "awaiting_selection", "candidate_set_id": "original-batch", "candidates": [{"name": "ECS"}],
        "intent": {"hard_constraints": [{"property": "memory", "operator": "gte", "value": 2}]},
    }
    checks = explicit_activity_checks(evidence, {**original_plan, **plan_changes}, original_plan)
    assert all(checks.values()) is expected


def test_visible_link_reassembles_wire_deltas_without_accepting_tool_or_thinking(tmp_path):
    path = tmp_path / "turn.events.jsonl"
    events = [
        {"eventType": "thinking_delta", "data": {"text": DESTINATION}},
        {"eventType": "tool_result", "data": {"input": {"rejection_reason": DESTINATION}}},
        {"eventType": "text_delta", "data": {"text": "[AI Assistant](https://ros"}},
        {"eventType": "text_delta", "data": {"text": ".console.aliyun.com/cn-hangzhou/chat)"}},
    ]
    wire = [
        {"result": {"statusUpdate": {"metadata": {"iac_code": {"pipeline": event}}}}}
        for event in events
    ]
    path.write_text("\n".join(json.dumps(event) for event in wire), encoding="utf-8")
    assert visible_pipeline_text(path) == f"[AI Assistant]({DESTINATION})"


def test_activity_followup_accepts_explanation_in_displayed_question(tmp_path):
    path = tmp_path / "turn.events.jsonl"
    question = "2 GiB 可以选，4 GiB 是推荐配置。是否要改用更便宜的规格？"
    event = {"eventType": "input_required", "data": {"kind": "ask_user_question", "question": question}}
    wire = {"result": {"statusUpdate": {"metadata": {"iac_code": {"pipeline": event}}}}}
    path.write_text(json.dumps(wire), encoding="utf-8")

    text = visible_pipeline_text(path)

    assert text.strip() == question
    evidence = {
        "task_state": "TASK_STATE_INPUT_REQUIRED", "step": "solution_planning_and_selection",
        "pipeline_handoff": False, "started_steps": [],
    }
    assert all(activity_followup_checks(evidence, {"status": "awaiting_selection"}, text).values())


@pytest.mark.parametrize("event", [
    {"eventType": "tool_use", "data": {"input": {"question": DESTINATION}}},
    {"eventType": "tool_result", "data": {"question": DESTINATION}},
    {"eventType": "thinking_delta", "data": {"question": DESTINATION}},
    {"eventType": "input_required", "data": {"kind": "permission", "question": DESTINATION}},
    {"eventType": "input_required", "data": {"question": DESTINATION}},
    {"eventType": "input_required", "data": {"kind": "ask_user_question", "question": None}},
    {"eventType": "input_required", "data": {"kind": "ask_user_question", "question": [DESTINATION]}},
])
def test_visible_question_excludes_unshown_arguments_and_other_input_kinds(tmp_path, event):
    path = tmp_path / "turn.events.jsonl"
    wire = {"result": {"statusUpdate": {"metadata": {"iac_code": {"pipeline": event}}}}}
    path.write_text(json.dumps(wire), encoding="utf-8")
    assert not visible_pipeline_text(path).strip()


def test_pipeline_facts_use_accepted_conclusion_and_record_unselected_step_advance(tmp_path):
    path = tmp_path / "turn.events.jsonl"
    events = [
        {"eventType": "tool_use", "data": {"input": {"conclusion": {"status": "rejected"}}}},
        {"eventType": "tool_result", "data": {"normalizedConclusion": {"status": "selected"}}},
        {"eventType": "step_started", "step": {"id": "materialize_selected_candidate"}},
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    conclusion, steps = pipeline_facts(path)
    assert conclusion == {"status": "selected"}
    assert steps == ["materialize_selected_candidate"]


@pytest.mark.parametrize(("changes", "conclusion", "text", "expected"), [
    ({}, {"status": "awaiting_selection"}, "最低2G可以运行，4G用于降低内存不足风险。", True),
    ({}, {"status": "awaiting_selection"}, "", False),
    ({"step": "materialize_selected_candidate"}, {"status": "awaiting_confirmation"}, "解释模板", False),
    ({"started_steps": ["materialize_selected_candidate"]}, {"status": "awaiting_selection"}, "解释方案", False),
    ({}, {"status": "selected"}, "解释方案", False),
    ({"pipeline_handoff": True, "task_state": "TASK_STATE_COMPLETED", "step": ""},
     {"status": "rejected", "rejection_reason": DESTINATION}, DESTINATION, True),
    ({"pipeline_handoff": True}, {"status": "rejected", "rejection_reason": DESTINATION}, "无可见链接", False),
    ({"pipeline_handoff": True}, {"status": "rejected", "rejection_reason": "无保存链接"}, DESTINATION, False),
    ({"pipeline_handoff": True}, {"status": "rejected", "rejection_reason": DESTINATION},
     "https://ros.console.aliyun.com/cn-beijing/chat", False),
    ({"pipeline_handoff": False}, {"status": "rejected", "rejection_reason": DESTINATION}, DESTINATION, False),
])
def test_activity_followup_requires_answered_consultation_or_valid_handoff(changes, conclusion, text, expected):
    evidence = {
        "task_state": "TASK_STATE_INPUT_REQUIRED", "step": "solution_planning_and_selection",
        "pipeline_handoff": False, "started_steps": [], **changes,
    }
    assert all(activity_followup_checks(evidence, conclusion, text).values()) is expected
