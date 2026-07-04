from __future__ import annotations

import json
import logging
import math
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from iac_code.i18n import _
from iac_code.utils.path_locks import PathLockRegistry
from iac_code.utils.state_io import cross_process_append_lock, fsync_parent_dir

logger = logging.getLogger(__name__)
_EVENT_GROUP_RECORD_TYPE = "event_group"
_EVENT_GROUP_RECORD_KEY = "__iac_code_record_type"
_JOURNAL_PATH_LOCKS = PathLockRegistry()
_APPEND_TAIL_CLEAN = "clean"
_APPEND_TAIL_REPAIRABLE = "repairable_tail"
_APPEND_TAIL_UNREPAIRABLE = "unrepairable"
_APPEND_TAIL_SCAN_BYTES = 1024 * 1024


class A2APipelineJournalReadError(ValueError):
    pass


class A2APipelineJournal:
    def __init__(self, pipeline_dir: str | Path) -> None:
        self.pipeline_dir = Path(pipeline_dir)
        self.path = self.pipeline_dir / "a2a-events.jsonl"

    def append(self, event: dict[str, Any], durable: bool = False) -> None:
        self.pipeline_dir.mkdir(parents=True, exist_ok=True)
        safe_event = to_json_safe(event)
        try:
            line = json.dumps(safe_event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            logger.warning("Skipping non-JSON-safe A2A pipeline journal event in %s", self.path, exc_info=True)
            return
        _append_journal_bytes(self.path, (line + "\n").encode("utf-8"), durable=durable)

    def append_many(self, events: list[dict[str, Any]], durable: bool = False) -> None:
        if not events:
            return

        self.pipeline_dir.mkdir(parents=True, exist_ok=True)
        safe_events = []
        for event in events:
            safe_event = to_json_safe(event)
            if not isinstance(safe_event, dict):
                raise TypeError("A2A journal group events must be JSON objects")
            safe_events.append(safe_event)
        record = {
            _EVENT_GROUP_RECORD_KEY: _EVENT_GROUP_RECORD_TYPE,
            "schemaVersion": "1.0",
            "groupId": uuid.uuid4().hex,
            "events": safe_events,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        _append_journal_bytes(self.path, (line + "\n").encode("utf-8"), durable=durable)

    def read_all(self) -> list[dict[str, Any]]:
        return self._read_all(strict=False)

    def read_all_strict(self) -> list[dict[str, Any]]:
        return self._read_all(strict=True)

    def read_all_repairing_tail(self) -> list[dict[str, Any]]:
        if not self.path.exists() and not _journal_lock_path(self.path).exists():
            return []
        with _journal_transaction_lock(self.path):
            if not self.path.exists():
                return []
            try:
                return self._read_all(strict=True)
            except A2APipelineJournalReadError:
                if not self._repair_tail_locked():
                    raise
            return self._read_all(strict=True)

    def repair_tail(self) -> bool:
        with _journal_transaction_lock(self.path):
            return self._repair_tail_locked()

    def _repair_tail_locked(self) -> bool:
        if not self.path.exists():
            return False
        try:
            content = self.path.read_bytes()
        except OSError:
            return False
        repair = _repairable_tail_bytes(content)
        if repair is None:
            return False
        valid_bytes, corrupt_bytes = repair
        if valid_bytes == content:
            return False
        self.pipeline_dir.mkdir(parents=True, exist_ok=True)
        corrupt_path = self.path.with_name(f"{self.path.name}.corrupt")
        try:
            tmp_path = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
            with tmp_path.open("wb") as handle:
                handle.write(valid_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(self.path)
            if corrupt_bytes:
                with corrupt_path.open("ab") as handle:
                    handle.write(corrupt_bytes)
                    if not corrupt_bytes.endswith(b"\n"):
                        handle.write(b"\n")
                    handle.flush()
            return True
        except OSError:
            logger.warning("Failed to repair A2A pipeline journal tail in %s", self.path, exc_info=True)
            return False
        finally:
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink()

    def _read_all(self, *, strict: bool) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []

        events: list[dict[str, Any]] = []
        raw_content = self.path.read_bytes()
        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            if strict:
                raise A2APipelineJournalReadError(f"Invalid UTF-8 in A2A pipeline journal {self.path}") from exc
            logger.warning("Skipping invalid UTF-8 A2A pipeline journal bytes in %s", self.path)
            content = raw_content.decode("utf-8", errors="ignore")
        if strict and content and not content.endswith("\n"):
            raise A2APipelineJournalReadError(f"Partial A2A pipeline journal line in {self.path}")

        for line_number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                if strict:
                    raise A2APipelineJournalReadError(
                        f"Invalid A2A pipeline journal line {line_number} in {self.path}"
                    ) from exc
                logger.warning("Skipping invalid A2A pipeline journal line in %s", self.path)
                continue
            if not isinstance(value, dict):
                if strict:
                    raise A2APipelineJournalReadError(
                        f"Non-object A2A pipeline journal line {line_number} in {self.path}"
                    )
                continue
            events.extend(_events_from_journal_record(value, strict=strict, line_number=line_number, path=self.path))

        events.sort(key=_sequence_value)
        return events

    def read_after(self, sequence: int) -> list[dict[str, Any]]:
        return [event for event in self.read_all() if _sequence_value(event) > sequence]


def _append_journal_bytes(path: Path, payload: bytes, *, durable: bool) -> None:
    with _journal_transaction_lock(path):
        _repair_existing_tail_before_append(path)
        created = not path.exists()
        offset: int | None = None
        try:
            with path.open("ab+") as handle:
                handle.seek(0, os.SEEK_END)
                offset = handle.tell()
                handle.write(payload)
                handle.flush()
                if durable:
                    os.fsync(handle.fileno())
            if durable and created:
                fsync_parent_dir(path)
        except Exception:
            if offset is not None:
                _rollback_journal_append(
                    path,
                    offset=offset,
                    durable=durable,
                    unlink_empty_created=created and offset == 0,
                )
            raise


def _rollback_journal_append(
    path: Path,
    *,
    offset: int,
    durable: bool,
    unlink_empty_created: bool = False,
) -> None:
    try:
        with path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        if unlink_empty_created:
            path.unlink()
    except OSError as exc:
        logger.warning(
            "Failed to roll back A2A pipeline journal append error_type=%s",
            type(exc).__name__,
        )


def _repair_existing_tail_before_append(path: Path) -> None:
    if not path.exists():
        return
    tail_state = _append_tail_state(path)
    if tail_state == _APPEND_TAIL_CLEAN:
        return
    if tail_state == _APPEND_TAIL_UNREPAIRABLE:
        raise A2APipelineJournalReadError(_("Unrepairable A2A pipeline journal tail"))
    journal = A2APipelineJournal(path.parent)
    if not journal._repair_tail_locked():
        raise A2APipelineJournalReadError(_("Unrepairable A2A pipeline journal tail"))


def _append_tail_state(path: Path) -> str:
    try:
        file_size = path.stat().st_size
    except OSError:
        return _APPEND_TAIL_CLEAN
    if file_size <= 0:
        return _APPEND_TAIL_CLEAN
    read_size = min(file_size, _APPEND_TAIL_SCAN_BYTES)
    try:
        with path.open("rb") as handle:
            handle.seek(file_size - read_size)
            content = handle.read(read_size)
    except OSError:
        return _APPEND_TAIL_CLEAN
    if not content:
        return _APPEND_TAIL_CLEAN
    if read_size < file_size:
        newline_index = content.find(b"\n")
        if newline_index < 0:
            return _APPEND_TAIL_UNREPAIRABLE
        content = content[newline_index + 1 :]
    nonempty_lines = [line for line in content.splitlines(keepends=True) if line.strip()]
    if not nonempty_lines:
        return _APPEND_TAIL_CLEAN
    last_line = nonempty_lines[-1]
    for line in nonempty_lines[:-1]:
        if not _json_line_is_valid_journal_record(line, use_json_loads=False):
            return _APPEND_TAIL_UNREPAIRABLE
    last_line_valid = _json_line_is_valid_journal_record(last_line, use_json_loads=True)
    if not last_line_valid:
        return _APPEND_TAIL_REPAIRABLE
    if not content.endswith(b"\n"):
        return _APPEND_TAIL_REPAIRABLE
    return _APPEND_TAIL_CLEAN


def _json_line_is_valid_journal_record(line: bytes | str, *, use_json_loads: bool) -> bool:
    try:
        text = line.decode("utf-8") if isinstance(line, bytes) else line
        value = json.loads(text) if use_json_loads else json.JSONDecoder().decode(text)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    try:
        _events_from_journal_record(value, strict=True, line_number=0, path=Path("<journal-tail>"))
    except A2APipelineJournalReadError:
        return False
    return True


@contextmanager
def _journal_transaction_lock(path: Path) -> Iterator[None]:
    with _JOURNAL_PATH_LOCKS.lock_for(path):
        with cross_process_append_lock(path):
            yield


def _journal_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _sequence_value(event: dict[str, Any]) -> int:
    value = event.get("sequence", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _events_from_journal_record(
    value: dict[str, Any],
    *,
    strict: bool,
    line_number: int,
    path: Path,
) -> list[dict[str, Any]]:
    if value.get(_EVENT_GROUP_RECORD_KEY) != _EVENT_GROUP_RECORD_TYPE:
        return [value]

    group_events = value.get("events")
    if not isinstance(group_events, list) or not all(isinstance(event, dict) for event in group_events):
        if strict:
            raise A2APipelineJournalReadError(f"Invalid A2A pipeline journal event group line {line_number} in {path}")
        logger.warning("Skipping invalid A2A pipeline journal event group in %s", path)
        return []
    return group_events


def _repairable_tail_bytes(content: bytes) -> tuple[bytes, bytes] | None:
    if not content:
        return None
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        line_start = content.rfind(b"\n", 0, exc.start) + 1
        valid_bytes = content[:line_start]
        corrupt_bytes = content[line_start:]
        if any(part.strip() for part in corrupt_bytes.splitlines(keepends=True)[1:]):
            return None
        try:
            valid_text = valid_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not _contains_only_complete_json_records(valid_text):
            return None
        return valid_bytes, corrupt_bytes

    repair = _repairable_tail(decoded)
    if repair is None:
        return None
    valid_content, corrupt_content = repair
    return valid_content.encode("utf-8"), corrupt_content.encode("utf-8")


def _repairable_tail(content: str) -> tuple[str, str] | None:
    if not content:
        return None

    lines = content.splitlines(keepends=True)
    if not lines:
        return None

    invalid_index: int | None = None
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if not _json_line_is_valid_journal_record(line, use_json_loads=True):
            invalid_index = index
            break

    if invalid_index is not None:
        if any(raw_line.strip() for raw_line in lines[invalid_index + 1 :]):
            return None
        valid_content = "".join(lines[:invalid_index])
        if valid_content and not valid_content.endswith("\n"):
            valid_content += "\n"
        corrupt_content = "".join(lines[invalid_index:])
        return valid_content, corrupt_content

    if not content.endswith("\n"):
        return content + "\n", ""
    return None


def _contains_only_complete_json_records(content: str) -> bool:
    if content and not content.endswith("\n"):
        return False
    for line in content.splitlines():
        if not line.strip():
            continue
        if not _json_line_is_valid_journal_record(line, use_json_loads=True):
            return False
    return True


def to_json_safe(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= 64:
        return "[truncated-depth]"
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): to_json_safe(item, _depth=_depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [to_json_safe(item, _depth=_depth + 1) for item in value]
    return repr(value)
