from __future__ import annotations

from iac_code.a2a.pipeline_journal import A2APipelineJournal


def _event(sequence: int, event_id: str) -> dict:
    return {
        "schemaVersion": "1.0",
        "eventId": event_id,
        "sequence": sequence,
        "eventType": "step_started",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }


def test_append_and_read_all_preserves_order(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")

    journal.append(_event(1, "evt-1"))
    journal.append(_event(2, "evt-2"))

    assert [event["eventId"] for event in journal.read_all()] == ["evt-1", "evt-2"]


def test_read_after_filters_by_sequence(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-1"))
    journal.append(_event(2, "evt-2"))
    journal.append(_event(3, "evt-3"))

    assert [event["eventId"] for event in journal.read_after(1)] == ["evt-2", "evt-3"]


def test_invalid_json_lines_are_skipped(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-1"))
    journal.path.write_text(journal.path.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8")

    assert [event["eventId"] for event in journal.read_all()] == ["evt-1"]


def test_repairing_tail_quarantines_invalid_utf8_partial_line(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    event = _event(1, "evt-1")
    event["eventType"] = "text_delta"
    event["data"] = {"text": "你好"}
    journal.append(event)
    partial = (
        '{"eventId":"evt-partial","sequence":2,"eventType":"text_delta","data":{"text":"'.encode() + "世".encode()[:1]
    )
    journal.path.write_bytes(journal.path.read_bytes() + partial)

    events = journal.read_all_repairing_tail()

    assert [event["eventId"] for event in events] == ["evt-1"]
    assert journal.read_all_strict()[0]["data"]["text"] == "你好"
    assert (journal.path.with_name("a2a-events.jsonl.corrupt")).read_bytes() == partial + b"\n"


def test_append_sanitizes_non_finite_and_non_json_values(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    event = _event(1, "evt-1")
    event["data"] = {"cost": float("nan"), "raw": object()}

    journal.append(event)

    loaded = journal.read_all()[0]
    assert loaded["data"]["cost"] is None
    assert loaded["data"]["raw"].startswith("<object object at ")
