from __future__ import annotations

import json

from scripts.ci.run_promotional_offer_followup_a2a import DESTINATION, pipeline_facts, visible_pipeline_text


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
