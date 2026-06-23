from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

from iac_code.utils.state_io import append_jsonl_locked, atomic_write_json, atomic_write_text


def test_atomic_write_text_replaces_file_and_removes_temp(tmp_path: Path) -> None:
    path = tmp_path / "state.txt"
    path.write_text("old", encoding="utf-8")

    atomic_write_text(path, "new", durable=True)

    assert path.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".state.txt.*.tmp"))


def test_atomic_write_json_fails_without_overwriting_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"ok": true}\n', encoding="utf-8")

    def fail_replace(src: str, dst: str) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr("iac_code.utils.state_io.os.replace", fail_replace)

    with pytest.raises(PermissionError, match="locked"):
        atomic_write_json(path, {"ok": False}, durable=True, replace_attempts=1)

    assert path.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_write_json_rejects_invalid_replace_attempts_without_overwriting_target(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"ok": true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="attempts must be >= 1"):
        atomic_write_json(path, {"ok": False}, durable=True, replace_attempts=0)

    assert path.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_append_jsonl_locked_writes_one_complete_line_per_record(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"

    append_jsonl_locked(path, [{"a": 1}, {"b": 2}], durable=False)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": 2}]


def test_durable_append_jsonl_fsyncs_parent_directory_for_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.jsonl"
    calls: list[Path] = []

    monkeypatch.setattr("iac_code.utils.state_io.fsync_parent_dir", calls.append)

    append_jsonl_locked(path, [{"created": True}], durable=True)

    assert calls == [path]


def test_append_jsonl_locked_fails_loudly_when_posix_lock_acquisition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX fcntl lock path is not available on Windows")

    import fcntl

    path = tmp_path / "session.jsonl"

    def fail_flock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX:
            raise OSError("lock unavailable")

    monkeypatch.setattr(fcntl, "flock", fail_flock)

    with pytest.raises(RuntimeError, match="could not acquire append lock"):
        append_jsonl_locked(path, [{"a": 1}])

    assert not path.exists()


def test_windows_append_lock_seeks_before_lock_and_unlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.utils import state_io

    events: list[tuple[str, int] | tuple[str, int, int]] = []

    class FakeLockFile:
        def __enter__(self) -> FakeLockFile:
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def fileno(self) -> int:
            return 42

        def seek(self, offset: int) -> None:
            events.append(("seek", offset))

    def fake_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> FakeLockFile:
        return FakeLockFile()

    def fake_locking(fd: int, mode: int, nbytes: int) -> None:
        events.append(("locking", mode, nbytes))

    fake_msvcrt = types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=2, locking=fake_locking)
    monkeypatch.setattr("iac_code.utils.state_io.sys.platform", "win32")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(Path, "open", fake_open)

    with state_io._cross_process_append_lock(tmp_path / "session.jsonl"):
        events.append(("yield", 0))

    assert events == [
        ("seek", 0),
        ("locking", 1, 1),
        ("yield", 0),
        ("seek", 0),
        ("locking", 2, 1),
    ]


def test_parent_directory_fsync_is_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.txt"
    calls: list[int] = []
    original_fsync = os.fsync

    def flaky_fsync(fd: int) -> None:
        calls.append(fd)
        if len(calls) > 1:
            raise OSError("directory fsync unsupported")
        original_fsync(fd)

    monkeypatch.setattr("iac_code.utils.state_io.os.fsync", flaky_fsync)

    atomic_write_text(path, "ok", durable=True)

    assert path.read_text(encoding="utf-8") == "ok"
