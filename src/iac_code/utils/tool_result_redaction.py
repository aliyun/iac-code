"""Helpers for redacting sensitive tool-result payload fields."""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED_TOOL_RESULT_VALUE = "[REDACTED]"
_FILE_CONTENT_KEYS = {"file_content", "fileContent"}
_FILE_CONTENT_STRING_FIELD_RE = re.compile(r'("(?:file_content|fileContent)"\s*:\s*)"')
_TRUNCATED_RESULT_MARKER = "\n\n... [truncated"


def is_file_content_key(key: Any) -> bool:
    return isinstance(key, str) and key in _FILE_CONTENT_KEYS


def redact_file_content_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED_TOOL_RESULT_VALUE if is_file_content_key(key) else redact_file_content_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_file_content_fields(item) for item in value]
    if isinstance(value, tuple):
        return [redact_file_content_fields(item) for item in value]
    return value


def redact_file_content_from_json_string(value: str) -> str:
    if not any(key in value for key in _FILE_CONTENT_KEYS):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return _redact_file_content_string_literals(value)
    redacted = redact_file_content_fields(parsed)
    if redacted == parsed:
        return value
    return json.dumps(redacted, ensure_ascii=False)


def _redact_file_content_string_literals(value: str) -> str:
    parts: list[str] = []
    pos = 0
    changed = False

    while True:
        match = _FILE_CONTENT_STRING_FIELD_RE.search(value, pos)
        if match is None:
            parts.append(value[pos:])
            break

        parts.append(value[pos : match.start()])
        parts.append(match.group(1))
        parts.append('"')
        parts.append(REDACTED_TOOL_RESULT_VALUE)
        parts.append('"')
        changed = True

        value_start = match.end()
        value_end = _json_string_end(value, value_start)
        if value_end is None:
            marker_index = value.find(_TRUNCATED_RESULT_MARKER, value_start)
            if marker_index >= 0:
                parts.append(value[marker_index:])
            break
        pos = value_end

    return "".join(parts) if changed else value


def _json_string_end(value: str, start: int) -> int | None:
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return index + 1
    return None


def redact_tool_result_file_content(value: Any) -> Any:
    if isinstance(value, str):
        return redact_file_content_from_json_string(value)
    return redact_file_content_fields(value)
