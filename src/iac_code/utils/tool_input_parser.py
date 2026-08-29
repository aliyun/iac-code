"""Shared tool input JSON parsing for all providers.

Handles three cases:
1. Valid single JSON object → one ToolUseEndEvent
2. Concatenated JSON objects (model intended parallel calls) →
   ToolUseEndEvent for the first, ToolUseStart+End pairs for the rest
3. Unparseable → ToolUseEndEvent with empty {} **and** ``input_error`` set, so
   the caller reports the real defect instead of executing the tool with no
   arguments. Executing on ``{}`` makes the tool answer with its own schema
   error ("missing required field ..."), which tells the model the opposite of
   the truth — it did send that field — and the model then retries the identical
   call. Each such round trip costs a full generation, so the parse failure has
   to travel with the event.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

from loguru import logger

from iac_code.types.stream_events import StreamEvent, ToolUseEndEvent, ToolUseStartEvent
from iac_code.utils.json_utils import parse_concatenated_json, parse_json_tolerant

# Model-facing text: it comes back as the tool result, so it must say what is
# actually wrong and what to do about it, instead of a schema error the model
# cannot act on.
INVALID_TOOL_INPUT_MESSAGE = (
    "Tool arguments were not valid JSON, so this tool call was not executed "
    "(no arguments reached the tool). Details: {detail}. Resend the same tool call with the "
    "complete arguments as one valid JSON object: escape newlines, tabs and quotes inside "
    "string values, and do not truncate the JSON."
)


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
    parsed, parse_error = parse_json_tolerant(raw_json)
    if isinstance(parsed, dict):
        yield ToolUseEndEvent(tool_use_id=tool_use_id, name=tool_name, input=parsed)
        return

    # Single parse failed on non-empty input — try concatenated JSON recovery
    if raw_json:
        parts = parse_concatenated_json(raw_json)
        if parts:
            logger.info(
                "Recovered {} concatenated tool inputs for tool_use_id={}",
                len(parts),
                tool_use_id,
            )
            # First part uses the original tool_use_id
            yield ToolUseEndEvent(tool_use_id=tool_use_id, name=tool_name, input=parts[0])
            # Additional parts become new synthetic tool calls
            for part in parts[1:]:
                new_id = f"toolu_{uuid.uuid4().hex[:24]}"
                yield ToolUseStartEvent(tool_use_id=new_id, name=tool_name)
                yield ToolUseEndEvent(tool_use_id=new_id, name=tool_name, input=part)
            return

        logger.warning(
            "Tool input JSON parse failed: tool_use_id={}, tool={}, {}",
            tool_use_id,
            tool_name,
            parse_error,
        )
        yield ToolUseEndEvent(
            tool_use_id=tool_use_id,
            name=tool_name,
            input={},
            input_error=INVALID_TOOL_INPUT_MESSAGE.format(detail=parse_error or "unparseable arguments"),
        )
        return

    # Empty arguments are legitimate for zero-parameter tools.
    yield ToolUseEndEvent(tool_use_id=tool_use_id, name=tool_name, input={})
