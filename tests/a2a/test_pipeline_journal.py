from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time

import pytest

from iac_code.a2a.pipeline_journal import A2APipelineJournal, A2APipelineJournalReadError


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


def _journal_line(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _failing_fsync_append_process(
    pipeline_dir: str,
    fsync_entered,
    success_done,
    result_queue,
) -> None:
    from iac_code.a2a import pipeline_journal as pipeline_journal_module

    real_fsync = os.fsync
    raised = False

    def fail_once(fd: int) -> None:
        nonlocal raised
        if not raised:
            raised = True
            fsync_entered.set()
            success_done.wait(timeout=0.4)
            raise OSError("fsync failed after write")
        real_fsync(fd)

    pipeline_journal_module.os.fsync = fail_once
    try:
        A2APipelineJournal(pipeline_dir).append(_event(2, "evt-failed"), durable=True)
    except BaseException as exc:
        result_queue.put(("failing", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("failing", "ok", ""))


def _successful_append_process(
    pipeline_dir: str,
    fsync_entered,
    success_done,
    result_queue,
) -> None:
    if not fsync_entered.wait(timeout=2):
        result_queue.put(("success", "timeout", "failing writer did not enter fsync"))
        return
    try:
        A2APipelineJournal(pipeline_dir).append(_event(3, "evt-success"), durable=True)
    except BaseException as exc:
        result_queue.put(("success", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("success", "ok", ""))
    finally:
        success_done.set()


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


def test_append_many_replays_group_as_events(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")

    journal.append_many([_event(1, "evt-cancel"), _event(2, "evt-handoff")], durable=True)

    assert [event["eventId"] for event in journal.read_all_strict()] == ["evt-cancel", "evt-handoff"]


def test_append_many_sorts_group_events_with_regular_events(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")

    journal.append(_event(3, "evt-after"))
    journal.append_many([_event(1, "evt-cancel"), _event(2, "evt-handoff")], durable=True)

    assert [event["eventId"] for event in journal.read_all()] == ["evt-cancel", "evt-handoff", "evt-after"]


@pytest.mark.parametrize("write_method", ["append", "append_many"])
def test_durable_append_fsyncs_parent_directory_when_journal_is_created(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    write_method: str,
) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    calls = []

    monkeypatch.setattr("iac_code.a2a.pipeline_journal.fsync_parent_dir", calls.append, raising=False)

    if write_method == "append":
        journal.append(_event(1, "evt-1"), durable=True)
    else:
        journal.append_many([_event(1, "evt-1"), _event(2, "evt-2")], durable=True)

    assert calls == [journal.path]


@pytest.mark.parametrize("write_method", ["append", "append_many"])
def test_durable_append_rolls_back_when_fsync_fails_after_write(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    write_method: str,
) -> None:
    from iac_code.a2a import pipeline_journal as pipeline_journal_module

    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    real_fsync = os.fsync
    raised = False

    def fail_once(fd: int) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise OSError("fsync failed after write")
        real_fsync(fd)

    monkeypatch.setattr(pipeline_journal_module.os, "fsync", fail_once)

    with pytest.raises(OSError, match="fsync failed after write"):
        if write_method == "append":
            journal.append(_event(2, "evt-after-failed-fsync"), durable=True)
        else:
            journal.append_many(
                [_event(2, "evt-after-failed-fsync"), _event(3, "evt-handoff-failed-fsync")],
                durable=True,
            )

    assert [event["eventId"] for event in journal.read_all_strict()] == ["evt-existing"]


def test_failed_concurrent_append_rollback_keeps_successful_append(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.a2a import pipeline_journal as pipeline_journal_module

    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    real_fsync = os.fsync
    failing_fsync_entered = threading.Event()
    successful_append_done = threading.Event()
    failing_error: list[BaseException] = []
    successful_error: list[BaseException] = []
    failed_once = False

    def controlled_fsync(fd: int) -> None:
        nonlocal failed_once
        if threading.current_thread().name == "failing-writer" and not failed_once:
            failed_once = True
            failing_fsync_entered.set()
            successful_append_done.wait(timeout=0.5)
            raise OSError("fsync failed after write")
        real_fsync(fd)

    monkeypatch.setattr(pipeline_journal_module.os, "fsync", controlled_fsync)

    def failing_writer() -> None:
        try:
            journal.append(_event(2, "evt-failed"), durable=True)
        except BaseException as exc:
            failing_error.append(exc)

    def successful_writer() -> None:
        assert failing_fsync_entered.wait(timeout=1)
        try:
            journal.append(_event(3, "evt-success"), durable=True)
        except BaseException as exc:
            successful_error.append(exc)
        finally:
            successful_append_done.set()

    failing_thread = threading.Thread(target=failing_writer, name="failing-writer")
    successful_thread = threading.Thread(target=successful_writer, name="successful-writer")
    failing_thread.start()
    successful_thread.start()
    failing_thread.join(timeout=2)
    successful_thread.join(timeout=2)

    assert not failing_thread.is_alive()
    assert not successful_thread.is_alive()
    assert [type(exc) for exc in failing_error] == [OSError]
    assert successful_error == []
    assert [event["eventId"] for event in journal.read_all_strict()] == ["evt-existing", "evt-success"]


def test_failed_cross_process_append_rollback_keeps_successful_append(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    ctx = multiprocessing.get_context("spawn")
    fsync_entered = ctx.Event()
    success_done = ctx.Event()
    result_queue = ctx.Queue()
    failing = ctx.Process(
        target=_failing_fsync_append_process,
        args=(str(journal.pipeline_dir), fsync_entered, success_done, result_queue),
    )
    succeeding = ctx.Process(
        target=_successful_append_process,
        args=(str(journal.pipeline_dir), fsync_entered, success_done, result_queue),
    )

    failing.start()
    succeeding.start()
    failing.join(timeout=5)
    succeeding.join(timeout=5)

    try:
        assert not failing.is_alive()
        assert not succeeding.is_alive()
        results = [result_queue.get(timeout=1), result_queue.get(timeout=1)]
        result_by_name = {name: status for name, status, _message in results}
        assert result_by_name == {"failing": "OSError", "success": "ok"}
        assert [event["eventId"] for event in journal.read_all_strict()] == ["evt-existing", "evt-success"]
    finally:
        if failing.is_alive():
            failing.terminate()
        if succeeding.is_alive():
            succeeding.terminate()
        failing.join(timeout=1)
        succeeding.join(timeout=1)


def test_repair_tail_does_not_drop_concurrent_append(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.a2a import pipeline_journal as pipeline_journal_module

    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    journal.path.write_bytes(journal.path.read_bytes() + b'{"eventId":"evt-partial"')
    original_repairable_tail_bytes = pipeline_journal_module._repairable_tail_bytes
    repair_started = threading.Event()

    def slow_repairable_tail_bytes(content: bytes):
        repair = original_repairable_tail_bytes(content)
        repair_started.set()
        time.sleep(0.2)
        return repair

    monkeypatch.setattr(pipeline_journal_module, "_repairable_tail_bytes", slow_repairable_tail_bytes)
    repair_result: list[bool] = []
    repair_error: list[BaseException] = []
    append_error: list[BaseException] = []

    def repair() -> None:
        try:
            repair_result.append(journal.repair_tail())
        except BaseException as exc:
            repair_error.append(exc)

    def append() -> None:
        assert repair_started.wait(timeout=1)
        try:
            journal.append(_event(2, "evt-success"), durable=True)
        except BaseException as exc:
            append_error.append(exc)

    repair_thread = threading.Thread(target=repair)
    append_thread = threading.Thread(target=append)
    repair_thread.start()
    append_thread.start()
    repair_thread.join(timeout=2)
    append_thread.join(timeout=2)

    assert not repair_thread.is_alive()
    assert not append_thread.is_alive()
    assert repair_error == []
    assert append_error == []
    assert repair_result == [True]
    assert [event["eventId"] for event in journal.read_all_strict()] == ["evt-existing", "evt-success"]


def test_read_all_repairing_tail_waits_for_in_progress_append(
    tmp_path,
) -> None:
    from iac_code.a2a import pipeline_journal as pipeline_journal_module

    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    line = json.dumps(_event(2, "evt-success"), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    split_at = line.index('"eventId"') + len('"eventId":"evt')
    partial_written = threading.Event()
    reader_started = threading.Event()
    reader_result: list[list[str]] = []
    reader_error: list[BaseException] = []

    def writer() -> None:
        with pipeline_journal_module._journal_transaction_lock(journal.path):
            with journal.path.open("ab") as handle:
                handle.write(line[:split_at].encode("utf-8"))
                handle.flush()
                partial_written.set()
                assert reader_started.wait(timeout=1)
                time.sleep(0.2)
                handle.write(line[split_at:].encode("utf-8"))
                handle.flush()

    def reader() -> None:
        assert partial_written.wait(timeout=1)
        reader_started.set()
        try:
            reader_result.append([event["eventId"] for event in journal.read_all_repairing_tail()])
        except BaseException as exc:
            reader_error.append(exc)

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    reader_thread.start()
    writer_thread.join(timeout=2)
    reader_thread.join(timeout=2)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert reader_error == []
    assert reader_result == [["evt-existing", "evt-success"]]


def test_read_all_repairing_tail_missing_journal_has_no_side_effects(tmp_path) -> None:
    pipeline_dir = tmp_path / "missing" / "pipeline"
    journal = A2APipelineJournal(pipeline_dir)

    assert journal.read_all_repairing_tail() == []
    assert not pipeline_dir.exists()


def test_read_all_repairing_tail_waits_when_lock_exists_before_journal_creation(tmp_path) -> None:
    from iac_code.a2a import pipeline_journal as pipeline_journal_module

    journal = A2APipelineJournal(tmp_path / "pipeline")
    writer_locked = threading.Event()
    reader_started = threading.Event()
    reader_result: list[list[str]] = []
    reader_error: list[BaseException] = []

    def writer() -> None:
        journal.pipeline_dir.mkdir(parents=True, exist_ok=True)
        with pipeline_journal_module._journal_transaction_lock(journal.path):
            writer_locked.set()
            assert reader_started.wait(timeout=1)
            time.sleep(0.2)
            journal.path.write_text(
                json.dumps(_event(1, "evt-created"), separators=(",", ":"), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def reader() -> None:
        assert writer_locked.wait(timeout=1)
        reader_started.set()
        try:
            reader_result.append([event["eventId"] for event in journal.read_all_repairing_tail()])
        except BaseException as exc:
            reader_error.append(exc)

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    reader_thread.start()
    writer_thread.join(timeout=2)
    reader_thread.join(timeout=2)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert reader_error == []
    assert reader_result == [["evt-created"]]


def test_append_repairs_existing_corrupt_tail_before_writing_new_event(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    corrupt_tail = b'{"eventId":"evt-corrupt"'
    journal.path.write_bytes(journal.path.read_bytes() + corrupt_tail)
    original_read_all = A2APipelineJournal._read_all

    def fail_full_parse(self, *, strict: bool):
        raise AssertionError("append preflight should not parse the full journal")

    monkeypatch.setattr(A2APipelineJournal, "_read_all", fail_full_parse)

    journal.append(_event(2, "evt-success"), durable=True)

    monkeypatch.setattr(A2APipelineJournal, "_read_all", original_read_all)
    assert [event["eventId"] for event in journal.read_all_repairing_tail()] == ["evt-existing", "evt-success"]
    assert journal.path.with_name("a2a-events.jsonl.corrupt").read_bytes() == corrupt_tail + b"\n"


@pytest.mark.parametrize("write_method", ["append", "append_many"])
def test_append_rejects_unrepairable_middle_corruption_without_writing(
    tmp_path,
    write_method: str,
) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    before = journal.path.read_bytes() + b'{"eventId":"evt-corrupt"\n'
    before += json.dumps(_event(2, "evt-after-corrupt"), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    before += b"\n"
    journal.path.write_bytes(before)

    with pytest.raises(A2APipelineJournalReadError):
        if write_method == "append":
            journal.append(_event(3, "evt-new"), durable=True)
        else:
            journal.append_many([_event(3, "evt-new"), _event(4, "evt-new-group")], durable=True)

    assert journal.path.read_bytes() == before


@pytest.mark.parametrize("write_method", ["append", "append_many"])
def test_append_rejects_middle_corruption_outside_recent_tail_window(
    tmp_path,
    write_method: str,
) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing-1"), durable=True)
    journal.append(_event(2, "evt-existing-2"), durable=True)
    before = journal.path.read_bytes() + b'{"eventId":"evt-corrupt"\n'
    for index in range(3, 16):
        before += _journal_line(_event(index, f"evt-after-corrupt-{index}"))
    journal.path.write_bytes(before)

    with pytest.raises(A2APipelineJournalReadError):
        if write_method == "append":
            journal.append(_event(16, "evt-new"), durable=True)
        else:
            journal.append_many([_event(16, "evt-new"), _event(17, "evt-new-group")], durable=True)

    assert journal.path.read_bytes() == before


@pytest.mark.parametrize("write_method", ["append", "append_many"])
def test_append_rejects_malformed_middle_event_group_without_writing(
    tmp_path,
    write_method: str,
) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    malformed_group = {
        "__iac_code_record_type": "event_group",
        "schemaVersion": "1.0",
        "groupId": "bad-group",
        "events": {"not": "a-list"},
    }
    before = journal.path.read_bytes() + _journal_line(malformed_group) + _journal_line(_event(2, "evt-after-group"))
    journal.path.write_bytes(before)

    with pytest.raises(A2APipelineJournalReadError):
        if write_method == "append":
            journal.append(_event(3, "evt-new"), durable=True)
        else:
            journal.append_many([_event(3, "evt-new"), _event(4, "evt-new-group")], durable=True)

    assert journal.path.read_bytes() == before


@pytest.mark.parametrize("write_method", ["append", "append_many"])
def test_append_rejects_or_quarantines_malformed_tail_event_group(
    tmp_path,
    write_method: str,
) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")
    journal.append(_event(1, "evt-existing"), durable=True)
    malformed_group = {
        "__iac_code_record_type": "event_group",
        "schemaVersion": "1.0",
        "groupId": "bad-group",
        "events": {"not": "a-list"},
    }
    before = journal.path.read_bytes() + _journal_line(malformed_group)
    journal.path.write_bytes(before)

    try:
        if write_method == "append":
            journal.append(_event(2, "evt-new"), durable=True)
        else:
            journal.append_many([_event(2, "evt-new"), _event(3, "evt-new-group")], durable=True)
    except A2APipelineJournalReadError:
        assert journal.path.read_bytes() == before
    else:
        assert [event["eventId"] for event in journal.read_all_strict()] == ["evt-existing", "evt-new"] + (
            ["evt-new-group"] if write_method == "append_many" else []
        )
        assert journal.path.with_name("a2a-events.jsonl.corrupt").read_bytes() == _journal_line(malformed_group)


def test_append_preflight_only_parses_tail_record_for_clean_journal(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.a2a import pipeline_journal as pipeline_journal_module

    journal = A2APipelineJournal(tmp_path / "pipeline")
    for index in range(1, 6):
        journal.append(_event(index, f"evt-old-{index}"), durable=True)
    real_loads = json.loads
    parsed_event_ids: list[str] = []

    def recording_loads(value, *args, **kwargs):
        loaded = real_loads(value, *args, **kwargs)
        if isinstance(loaded, dict) and isinstance(loaded.get("eventId"), str):
            parsed_event_ids.append(loaded["eventId"])
        return loaded

    monkeypatch.setattr(pipeline_journal_module.json, "loads", recording_loads)

    journal.append(_event(6, "evt-new"), durable=True)

    assert parsed_event_ids == ["evt-old-5"]
    assert [event["eventId"] for event in journal.read_all_strict()] == [
        "evt-old-1",
        "evt-old-2",
        "evt-old-3",
        "evt-old-4",
        "evt-old-5",
        "evt-new",
    ]


def test_durable_append_unlinks_new_journal_when_parent_fsync_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")

    def fail_parent_fsync(path) -> None:
        raise OSError("parent fsync failed")

    monkeypatch.setattr("iac_code.a2a.pipeline_journal.fsync_parent_dir", fail_parent_fsync)

    with pytest.raises(OSError, match="parent fsync failed"):
        journal.append(_event(1, "evt-parent-fsync-failed"), durable=True)

    assert not journal.path.exists()


def test_durable_append_tolerates_unsupported_parent_fsync(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.utils import state_io

    journal = A2APipelineJournal(tmp_path / "pipeline")

    def fail_parent_open(path, flags):
        raise OSError("directory fsync unsupported")

    monkeypatch.setattr(state_io.os, "open", fail_parent_open)

    journal.append(_event(1, "evt-parent-fsync-unsupported"), durable=True)

    assert [event["eventId"] for event in journal.read_all_strict()] == ["evt-parent-fsync-unsupported"]


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
