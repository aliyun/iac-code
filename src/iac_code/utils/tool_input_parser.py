"""Shared tool input JSON parsing for all providers.

Handles three cases:
1. Valid single JSON object → one ToolUseEndEvent
2. Concatenated JSON objects (model intended parallel calls) →
   ToolUseEndEvent for the first, ToolUseStart+End pairs for the rest
3. Unparseable → ToolUseEndEvent with empty {}
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

from loguru import logger

from iac_code.types.stream_events import StreamEvent, ToolUseEndEvent, ToolUseStartEvent
from iac_code.utils.json_utils import parse_concatenated_json, safe_parse_json


def parse_tool_input_events(
    tool_use_id: str,
    tool_name: str,
    raw_json: str,
) -> Generator[StreamEvent, None, None]:
    """Parse tool input JSON and yield appropriate stream events.

    Used by all providers (Anthropic, OpenAI, DashScope) to handle
    tool input parsing consistently, including recovery from
    concatenated JSON objects.
    """
    parsed = safe_parse_json(raw_json)
    if isinstance(parsed, dict):
        yield ToolUseEndEvent(tool_use_id=tool_use_id, input=parsed)
        return

    # Single parse failed on non-empty input — try concatenated JSON recovery
    if raw_json:
        parts = parse_concatenated_json(raw_json)
        if parts:
            logger.info(
                "Recovered %d concatenated tool inputs for tool_use_id=%s",
                len(parts),
                tool_use_id,
            )
            # First part uses the original tool_use_id
            yield ToolUseEndEvent(tool_use_id=tool_use_id, input=parts[0])
            # Additional parts become new synthetic tool calls
            for part in parts[1:]:
                new_id = f"toolu_{uuid.uuid4().hex[:24]}"
                yield ToolUseStartEvent(tool_use_id=new_id, name=tool_name)
                yield ToolUseEndEvent(tool_use_id=new_id, input=part)
            return

        logger.warning(
            "Tool input JSON parse failed: tool_use_id=%s, length=%d, raw=%s",
            tool_use_id,
            len(raw_json),
            raw_json[:200],
        )

    yield ToolUseEndEvent(tool_use_id=tool_use_id, input={})
