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

import json
from typing import Any

from loguru import logger


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
        logger.error("Failed to parse JSON, raw=%s", raw[:200])
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
