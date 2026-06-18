from __future__ import annotations

from typing import Any

from scripts.observability.local_observe.records import Record, new_record

TRACE_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def sample_records(*, raw_content: bool) -> list[Record]:
    common = {"pipeline_name": "selling_pipeline", "session_id": "demo_session_1"}
    raw_attrs: dict[str, Any] = {}
    if raw_content:
        raw_attrs = {
            "gen_ai.input.messages": '[{"role":"user","content":"Build a selling pipeline observability report"}]',
            "gen_ai.system_instructions": "You are testing local pipeline telemetry with prompt capture enabled.",
        }
    tool_call_attrs_1 = (
        {
            "gen_ai.tool.call.arguments": '{"query":"selling pipeline observability","limit":3}',
            "gen_ai.tool.call.result": '{"candidates":["debug_tool","otel_export","pipeline_drilldown"]}',
        }
        if raw_content
        else {}
    )
    tool_call_attrs_2 = (
        {
            "gen_ai.tool.call.arguments": '{"candidate":"pipeline_drilldown","checks":["prompt","tool_use"]}',
            "gen_ai.tool.call.result": '{"ok":true,"reason":"debug details are visible"}',
        }
        if raw_content
        else {}
    )
    final_output_attrs = (
        {
            "gen_ai.output.messages": (
                '[{"role":"assistant","parts":[{"type":"text","content":"Final answer after complete_step"}],'
                '"finish_reason":"end_turn"}]'
            ),
        }
        if raw_content
        else {}
    )

    return [
        _span(
            "demo_run",
            "iac.pipeline.run",
            span_id="1000000000000000",
            attrs={**common, "status": "running"},
        ),
        _span(
            "demo_step_candidate_attempt_1",
            "iac.pipeline.step",
            span_id="1000000000000001",
            parent_span_id="1000000000000000",
            attrs={**common, "step_id": "candidate_selection", "step_attempt": 1, "status": "rolled_back"},
        ),
        new_record(
            "log",
            id="demo_rollback_log",
            name="iac.pipeline.rollback.requested",
            timestamp_unix_nano=1_500,
            attributes={
                **common,
                "step_id": "candidate_selection",
                "step_attempt": 1,
                "rollback_to_step_id": "candidate_selection",
            },
        ),
        _span(
            "demo_step_candidate_attempt_2",
            "iac.pipeline.step",
            span_id="1000000000000002",
            parent_span_id="1000000000000000",
            attrs={**common, "step_id": "candidate_selection", "step_attempt": 2, "status": "completed"},
        ),
        _span(
            "demo_round_1",
            "react step",
            span_id="1000000000000011",
            parent_span_id="1000000000000002",
            attrs={**common, "step_id": "candidate_selection", "step_attempt": 2, "gen_ai.react.round": 1, **raw_attrs},
        ),
        _span(
            "demo_chat_1",
            "chat qwen-plus",
            span_id="1000000000000012",
            parent_span_id="1000000000000011",
            attrs={**common, "step_id": "candidate_selection", "step_attempt": 2, **raw_attrs},
        ),
        _span(
            "demo_tool_1",
            "execute_tool search_candidates",
            span_id="1000000000000013",
            parent_span_id="1000000000000011",
            attrs={
                **common,
                "step_id": "candidate_selection",
                "step_attempt": 2,
                "tool.name": "search_candidates",
                **tool_call_attrs_1,
            },
        ),
        _span(
            "demo_round_2",
            "react step",
            span_id="1000000000000021",
            parent_span_id="1000000000000002",
            attrs={
                **common,
                "step_id": "candidate_selection",
                "step_attempt": 2,
                "gen_ai.react.round": 2,
                **raw_attrs,
                **final_output_attrs,
            },
        ),
        _span(
            "demo_chat_2",
            "chat qwen-plus",
            span_id="1000000000000022",
            parent_span_id="1000000000000021",
            attrs={**common, "step_id": "candidate_selection", "step_attempt": 2, **raw_attrs},
        ),
        _span(
            "demo_tool_2",
            "execute_tool validate_selection",
            span_id="1000000000000023",
            parent_span_id="1000000000000021",
            attrs={
                **common,
                "step_id": "candidate_selection",
                "step_attempt": 2,
                "tool.name": "validate_selection",
                **tool_call_attrs_2,
            },
        ),
        new_record(
            "metric",
            id="demo_step_duration",
            name="iac.pipeline.step.duration",
            timestamp_unix_nano=3_000,
            value=2410.5,
            attributes={**common, "step_id": "candidate_selection", "step_attempt": 2},
        ),
    ]


def _span(
    record_id: str,
    name: str,
    *,
    span_id: str,
    parent_span_id: str = "",
    attrs: dict[str, Any],
) -> Record:
    return new_record(
        "span",
        id=record_id,
        name=name,
        trace_id=TRACE_ID,
        span_id=span_id,
        parent_span_id=parent_span_id,
        timestamp_unix_nano=1_000,
        duration_ms=12.5,
        attributes=attrs,
    )
