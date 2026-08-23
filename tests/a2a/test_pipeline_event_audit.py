from __future__ import annotations

from typing import Any

from iac_code.a2a.pipeline_event_audit import (
    authoritative_pipeline_events,
    count_authoritative_pipeline_events,
    is_authoritative_pipeline_event,
)

RUN_ID = "ctx-1"


def _event(
    sequence: int,
    event_type: str,
    *,
    visibility: str | None = None,
    authoritative: bool | None = None,
    status: str = "canceled",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "eventId": "evt-{}".format(sequence),
        "sequence": sequence,
        "eventType": event_type,
        "scope": "pipeline",
        "pipelineRunId": RUN_ID,
        "taskId": "task-1",
        "contextId": RUN_ID,
        "status": status,
        "data": data or {},
    }
    if visibility is not None:
        event["visibility"] = visibility
    if authoritative is not None:
        event["authoritative"] = authoritative
    return event


def _cancel_session_events() -> list[dict[str, Any]]:
    """一次取消的完整 journal:预写 → 权威发布 → 恢复补发 → 降级兜底。"""
    return [
        _event(1, "pipeline_started", status="working"),
        # 备份预写通道
        _event(2, "pipeline_canceled", visibility="pending_backup", authoritative=False),
        _event(3, "pipeline_handoff_ready", visibility="pending_backup", authoritative=False),
        # 权威发布
        _event(4, "pipeline_canceled", visibility="committed", authoritative=True),
        _event(5, "pipeline_handoff_ready", visibility="committed", authoritative=True),
        _event(6, "backup_committed", status="working", data={"committedEventId": "evt-4"}),
        # 重启恢复路径的幂等补发,仍是 committed 权威通道
        _event(7, "pipeline_canceled", visibility="committed", authoritative=True, data={"recovered": True}),
        _event(8, "pipeline_handoff_ready", visibility="committed", authoritative=True, data={"recovered": True}),
        # 降级兜底标记
        _event(
            9,
            "pipeline_handoff_ready",
            visibility="unavailable",
            authoritative=False,
            data={"action": "switch_to_normal_unavailable", "unavailable": True},
        ),
    ]


def test_one_cancel_yields_exactly_one_authoritative_terminal_event() -> None:
    counts = count_authoritative_pipeline_events(_cancel_session_events())

    assert counts["pipeline_canceled"] == 1
    assert counts["pipeline_handoff_ready"] == 1


def test_backup_channel_events_are_excluded_from_counts() -> None:
    merged = authoritative_pipeline_events(_cancel_session_events())
    visibilities = {event.get("visibility") for event in merged}

    assert "pending_backup" not in visibilities
    assert "unavailable" not in visibilities


def test_merge_keeps_the_highest_sequence_authoritative_event() -> None:
    merged = authoritative_pipeline_events(_cancel_session_events())
    canceled = [event for event in merged if event["eventType"] == "pipeline_canceled"]
    handoff = [event for event in merged if event["eventType"] == "pipeline_handoff_ready"]

    assert [event["sequence"] for event in canceled] == [7]
    assert [event["sequence"] for event in handoff] == [8]


def test_non_terminal_events_are_passed_through() -> None:
    merged = authoritative_pipeline_events(_cancel_session_events())
    event_types = [event["eventType"] for event in merged]

    assert event_types == [
        "pipeline_started",
        "backup_committed",
        "pipeline_canceled",
        "pipeline_handoff_ready",
    ]


def test_distinct_pipeline_runs_are_counted_separately() -> None:
    first = _event(1, "pipeline_canceled", visibility="committed", authoritative=True)
    second = _event(2, "pipeline_canceled", visibility="committed", authoritative=True)
    second["pipelineRunId"] = "ctx-2"

    assert count_authoritative_pipeline_events([first, second])["pipeline_canceled"] == 2


def test_legacy_envelopes_without_authoritative_flag_fall_back_to_visibility() -> None:
    pending = _event(1, "pipeline_canceled", visibility="pending_backup")
    committed = _event(2, "pipeline_canceled", visibility="committed")
    untagged = _event(3, "pipeline_completed", status="completed")

    assert not is_authoritative_pipeline_event(pending)
    assert is_authoritative_pipeline_event(committed)
    # 完全没有 visibility 的历史信封默认视为权威,避免旧 journal 计数归零。
    assert is_authoritative_pipeline_event(untagged)
    assert count_authoritative_pipeline_events([pending, committed])["pipeline_canceled"] == 1


def test_authoritative_flag_wins_over_visibility() -> None:
    event = _event(1, "pipeline_canceled", visibility="committed", authoritative=False)

    assert not is_authoritative_pipeline_event(event)
    assert count_authoritative_pipeline_events([event]) == {}


def test_non_mapping_entries_are_ignored() -> None:
    events = [_event(1, "pipeline_canceled", visibility="committed", authoritative=True), None, "not-an-event"]

    assert count_authoritative_pipeline_events(events)["pipeline_canceled"] == 1
