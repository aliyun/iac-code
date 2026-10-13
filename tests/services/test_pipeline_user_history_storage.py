import json
import multiprocessing
import os
import stat
import sys

import pytest

from iac_code.agent.message import Message
from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_storage import (
    PIPELINE_USER_HISTORY_KEY,
    PipelineUserHistoryRecord,
    SessionStorage,
    UserInputHistoryWriteResult,
)


@pytest.fixture
def history(tmp_path):
    storage = SessionStorage(tmp_path / "projects")
    storage.ensure_v2_session_dir_for_new_session("/workspace", "session-1")
    return storage, PipelineUserHistoryRecord("sdk-1", "ctx-1", "task-1", "original user input")


def test_history_preserves_existing_metadata_and_user_rows_and_deduplicates_full_identity(history):
    storage, record = history
    storage.append_meta("/workspace", "session-1", {"type": "pipeline_init", "pipeline_type": "selling"})
    storage.append("/workspace", "session-1", Message(role="user", content="older ordinary message"))
    assert storage.append_user_input_once("/workspace", "session-1", record) is UserInputHistoryWriteResult.APPENDED
    assert (
        storage.append_user_input_once("/workspace", "session-1", record) is UserInputHistoryWriteResult.ALREADY_PRESENT
    )
    other = PipelineUserHistoryRecord("sdk-2", "ctx-1", "task-1", record.text)
    storage.append_user_input_once("/workspace", "session-1", other)
    assert [message.content for message in storage.load("/workspace", "session-1")] == [
        "older ordinary message",
        record.text,
        record.text,
    ]


@pytest.mark.parametrize(
    "damage",
    [
        "partial",
        "json",
        "identity",
        "duplicate",
        "session",
        "cwd",
        "text",
        "task",
        "context",
        "oversize_identity",
        "duplicate_key",
    ],
)
def test_history_damage_or_identity_conflict_fails_closed_without_rewriting(history, damage):
    storage, record = history
    storage.append_user_input_once("/workspace", "session-1", record)
    path = storage.session_path("/workspace", "session-1")
    text = path.read_text(encoding="utf-8")
    row = json.loads(text)
    if damage == "partial":
        text = text.rstrip("\n")
    elif damage == "json":
        text += "{bad}\n"
    elif damage == "duplicate":
        text += text
    elif damage == "duplicate_key":
        text = text.replace('"message_id": "sdk-1"', '"message_id": "different", "message_id": "sdk-1"')
    else:
        if damage == "identity":
            row["metadata"][PIPELINE_USER_HISTORY_KEY] = None
        elif damage == "session":
            row["session_id"] = "different-session"
        elif damage == "oversize_identity":
            row["metadata"][PIPELINE_USER_HISTORY_KEY]["message_id"] = "a" * 1025
        elif damage == "cwd":
            row["cwd"] = "/different/workspace"
        elif damage == "text":
            row["content"] = "different input"
        else:
            row["metadata"][PIPELINE_USER_HISTORY_KEY][damage + "_id"] = "different-id"
        text = json.dumps(row) + "\n"
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        storage.append_user_input_once("/workspace", "session-1", record)
    assert path.read_bytes() == before


@pytest.mark.parametrize("failed_sync", ["file", "parent"])
def test_full_row_left_by_failed_fsync_is_resynchronized_before_duplicate_success(history, monkeypatch, failed_sync):
    if failed_sync == "parent" and sys.platform == "win32":
        pytest.skip("Windows storage has no POSIX directory fsync")
    storage, record = history
    original_sync = os.fsync
    synced = []
    failed = False

    def sync(fd):
        nonlocal failed
        kind = "parent" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        synced.append(kind)
        if kind == failed_sync and not failed:
            failed = True
            raise OSError("offline history durability failure")
        original_sync(fd)

    monkeypatch.setattr("iac_code.services.session_storage.os.fsync", sync)
    with pytest.raises(OSError):
        storage.append_user_input_once("/workspace", "session-1", record)
    path = storage.session_path("/workspace", "session-1")
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    first_count = len(synced)
    assert (
        storage.append_user_input_once("/workspace", "session-1", record) is UserInputHistoryWriteResult.ALREADY_PRESENT
    )
    assert synced[first_count:] == (["file"] if sys.platform == "win32" else ["file", "parent"])
    assert len(storage.load("/workspace", "session-1")) == 1


def test_legacy_history_is_explicitly_unsupported_without_changing_ordinary_business(tmp_path):
    storage = SessionStorage(tmp_path / "projects")
    path = storage._legacy_session_path("/workspace", "legacy-1")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(Message(role="user", content="legacy input").to_dict()) + "\n", encoding="utf-8")
    before = path.read_bytes()
    record = PipelineUserHistoryRecord("sdk-1", "ctx-1", "task-1", "new query")
    assert storage.append_user_input_once("/workspace", "legacy-1", record) is UserInputHistoryWriteResult.NOT_SUPPORTED
    assert path.read_bytes() == before
    assert storage.load("/workspace", "legacy-1")[0].content == "legacy input"
    storage.append("/workspace", "legacy-1", Message(role="user", content="ordinary continuation"))
    assert len(storage.load("/workspace", "legacy-1")) == 2


def _history_process(projects, barrier, queue, message_id, text):
    storage = SessionStorage(projects)
    barrier.wait(10)
    try:
        result = storage.append_user_input_once(
            "/workspace", "session-1", PipelineUserHistoryRecord(message_id, "ctx-1", "task-1", text)
        )
        queue.put(result.value)
    except ValueError:
        queue.put("conflict")


@pytest.mark.parametrize("race", ["same", "different", "conflict"])
def test_two_os_process_history_writers_are_atomic_and_isolated(history, race):
    storage, record = history
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    args = [(record.message_id, record.text)]
    args.append(("sdk-2" if race == "different" else record.message_id, "other" if race == "conflict" else record.text))
    processes = [
        context.Process(target=_history_process, args=(str(storage._projects_dir), barrier, queue, mid, text))
        for mid, text in args
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    results = sorted(queue.get(timeout=2) for _ in processes)
    assert (
        results
        == {
            "same": ["already_present", "appended"],
            "different": ["appended", "appended"],
            "conflict": ["appended", "conflict"],
        }[race]
    )
    messages = storage.load("/workspace", "session-1")
    assert len(messages) == (2 if race == "different" else 1)
    storage.ensure_v2_session_dir_for_new_session("/workspace", "session-2")
    storage.append_user_input_once("/workspace", "session-2", record)
    assert len(storage.load("/workspace", "session-2")) == 1
    queue.close()
    queue.join_thread()


@pytest.mark.parametrize("damage", ["symlink", "directory", "metadata"])
def test_existing_v2_history_with_unsafe_entry_cannot_fall_back_to_legacy(history, tmp_path, damage):
    storage, record = history
    directory = storage.v2_session_dir("/workspace", "session-1")
    path = directory / "session.jsonl"
    target = tmp_path / "unrelated.jsonl"
    target.write_text("unrelated data\n", encoding="utf-8")
    if damage == "symlink":
        path.symlink_to(target)
    elif damage == "directory":
        path.mkdir()
    else:
        (directory / "metadata.json").write_text("{bad}", encoding="utf-8")
    with pytest.raises((ValueError, OSError, UnsupportedSessionLayoutError)):
        storage.append_user_input_once("/workspace", "session-1", record)
    assert target.read_text(encoding="utf-8") == "unrelated data\n"


@pytest.mark.parametrize("shadow", ["v2", "future", "invalid"])
def test_legacy_authority_preserves_business_when_metadata_only_shadow_exists(tmp_path, shadow):
    from iac_code.services.session_metadata import SessionMetadata, write_session_metadata

    storage = SessionStorage(tmp_path / "projects")
    path = storage.legacy_session_path("/workspace", "session-1")
    path.parent.mkdir(parents=True)
    path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    directory = path.parent / "session-1"
    directory.mkdir()
    if shadow == "invalid":
        (directory / "metadata.json").write_text("{broken", encoding="utf-8")
    else:
        write_session_metadata(
            directory,
            SessionMetadata(session_id="session-1", cwd="/workspace", layout_version=2 if shadow == "v2" else 99),
        )
    record = PipelineUserHistoryRecord("sdk-1", "ctx-1", "task-1", "new input")
    assert (
        storage.append_user_input_once("/workspace", "session-1", record) is UserInputHistoryWriteResult.NOT_SUPPORTED
    )
    storage.append("/workspace", "session-1", Message(role="assistant", content="ordinary continuation"))
    assert [row.content for row in storage.load("/workspace", "session-1")] == ["old", "ordinary continuation"]
    assert not (directory / "session.jsonl").exists()
