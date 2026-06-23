from __future__ import annotations

import json
import os
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


def test_append_jsonl_locked_writes_one_complete_line_per_record(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"

    append_jsonl_locked(path, [{"a": 1}, {"b": 2}], durable=False)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": 2}]


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
