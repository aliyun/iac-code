from __future__ import annotations

import json
import logging
import math
import os
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class A2APipelineJournalReadError(ValueError):
    pass


class A2APipelineJournal:
    def __init__(self, pipeline_dir: str | Path) -> None:
        self.pipeline_dir = Path(pipeline_dir)
        self.path = self.pipeline_dir / "a2a-events.jsonl"

    def append(self, event: dict[str, Any]) -> None:
        self.pipeline_dir.mkdir(parents=True, exist_ok=True)
        safe_event = to_json_safe(event)
        try:
            line = json.dumps(safe_event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            logger.warning("Skipping non-JSON-safe A2A pipeline journal event in %s", self.path, exc_info=True)
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def read_all(self) -> list[dict[str, Any]]:
        return self._read_all(strict=False)

    def read_all_strict(self) -> list[dict[str, Any]]:
        return self._read_all(strict=True)

    def read_all_repairing_tail(self) -> list[dict[str, Any]]:
        try:
            return self.read_all_strict()
        except A2APipelineJournalReadError:
            if not self.repair_tail():
                raise
        return self.read_all_strict()

    def repair_tail(self) -> bool:
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
            events.append(value)

        events.sort(key=_sequence_value)
        return events

    def read_after(self, sequence: int) -> list[dict[str, Any]]:
        return [event for event in self.read_all() if _sequence_value(event) > sequence]


def _sequence_value(event: dict[str, Any]) -> int:
    value = event.get("sequence", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid_index = index
            break
        if not isinstance(value, dict):
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
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(value, dict):
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
