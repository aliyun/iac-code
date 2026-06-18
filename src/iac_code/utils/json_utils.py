"""Safe JSON parsing utilities.

Design:
- safe_parse_json() never raises exceptions
- Returns None on failure or empty/None input (caller decides fallback)
- Logs debug when non-empty input fails to parse (callers handle warning)
- parse_concatenated_json() handles model edge case where multiple JSON
  objects are concatenated (e.g. '{"a":1}{"b":2}'), indicating the model
  intended parallel tool calls with different parameters.
"""

from __future__ import annotations

import functools
import json
import re
from typing import Any

from loguru import logger


@functools.lru_cache(maxsize=64)
def _key_value_pattern(key: str) -> re.Pattern:
    """Compiled regex matching ``"key"\\s*:\\s*"`` — bounded LRU cache so a
    pathological caller passing unbounded distinct keys can't leak memory."""
    return re.compile(rf'"{re.escape(key)}"\s*:\s*"')


def extract_json_string_value(accumulated: str, key: str, *, allow_partial: bool = False) -> str | None:
    """Extract a string value from partially-accumulated JSON.

    When *allow_partial* is True and the closing quote hasn't arrived yet,
    return whatever text has been accumulated so far (useful for streaming
    the ``summary`` field while the LLM is still generating it).
    """
    m = _key_value_pattern(key).search(accumulated)
    if not m:
        return None
    start = m.end()
    i = start
    while i < len(accumulated):
        if accumulated[i] == "\\" and i + 1 < len(accumulated):
            i += 2
            continue
        if accumulated[i] == '"':
            raw = accumulated[start:i]
            try:
                return json.loads(f'"{raw}"')
            except (json.JSONDecodeError, ValueError):
                return raw
        i += 1
    if not allow_partial:
        return None
    raw = accumulated[start:]
    try:
        return json.loads(f'"{raw}"')
    except (json.JSONDecodeError, ValueError):
        return raw


@functools.lru_cache(maxsize=64)
def _key_int_pattern(key: str) -> re.Pattern:
    return re.compile(rf'"{re.escape(key)}"\s*:\s*(-?\d+)')


def extract_json_int_value(accumulated: str, key: str) -> int | None:
    """Extract a completed integer value from partially-accumulated JSON."""
    m = _key_int_pattern(key).search(accumulated)
    if not m:
        return None
    if m.end() >= len(accumulated):
        return None
    if accumulated[m.end()] not in ",}] \t\r\n":
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def safe_parse_json(raw: str | None) -> Any | None:
    """Parse a JSON string safely, never raises.

    Returns:
        Parsed value on success, None on failure or empty/None input.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.error("Failed to parse JSON, raw={}", raw[:200])
        return None


def parse_concatenated_json(raw: str) -> list[dict[str, Any]]:
    """Parse concatenated JSON objects like '{"a":1}{"b":2}' into a list.

    Uses json.JSONDecoder.raw_decode to read one object at a time.

    Returns:
        List of parsed dicts. Empty list if nothing could be parsed.
    """
    decoder = json.JSONDecoder()
    results: list[dict[str, Any]] = []
    pos = 0
    length = len(raw)
    while pos < length:
        # Skip whitespace
        while pos < length and raw[pos] in " \t\n\r":
            pos += 1
        if pos >= length:
            break
        try:
            obj, end_pos = decoder.raw_decode(raw, pos)
            if isinstance(obj, dict):
                results.append(obj)
            pos = end_pos
        except json.JSONDecodeError:
            break
    return results


# Matches a fully-closed `"key": "value"` pair in JSON.
# Value may contain any chars except unescaped `"` or `\`, plus standard `\.` escapes.
_PARTIAL_STRING_FIELD_RE = re.compile(r'"([^"\\]+)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def extract_partial_string_fields(partial_json: str, field_names: set[str]) -> dict[str, str]:
    """Best-effort extraction of completed string fields from partial JSON.

    Used by the UI to show tool-use headers (e.g. file path) before the full
    JSON input has finished streaming. Only fields whose closing quote has
    already been streamed are returned, so callers never see truncated values.

    The match is by key name anywhere in the fragment; a same-named key
    inside a nested object would also match. This is acceptable for tools
    with flat top-level inputs (the only current caller).

    Args:
        partial_json: The raw JSON fragment accumulated so far.
        field_names: Set of top-level string field names to extract.

    Returns:
        Mapping of field name to decoded string value. Empty dict if nothing
        matches, the input is empty, or no field names were requested.
    """
    if not partial_json or not field_names:
        return {}
    result: dict[str, str] = {}
    for match in _PARTIAL_STRING_FIELD_RE.finditer(partial_json):
        key = match.group(1)
        if key in field_names and key not in result:
            try:
                result[key] = json.loads(f'"{match.group(2)}"')
            except (json.JSONDecodeError, ValueError):
                continue
    return result
