"""Serialize messages, tools, and content for gen_ai.* span attributes.

Follows the ARMS LLM trace field schema for gen_ai.input.messages,
gen_ai.output.messages, gen_ai.system_instructions, gen_ai.tool.definitions,
gen_ai.tool.call.arguments, and gen_ai.tool.call.result.
"""

from __future__ import annotations

import json
from typing import Any

_MAX_CONTENT_BYTES = 4096


def _truncate(s: str, max_bytes: int = _MAX_CONTENT_BYTES) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "...[truncated]"


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def serialize_user_input(user_input: str) -> str:
    """Serialize a plain user input string to gen_ai.input.messages JSON."""
    return _json_dumps([{"role": "user", "parts": [{"type": "text", "content": _truncate(user_input)}]}])


def serialize_input_messages(messages: list) -> str:
    """Serialize provider Message list to gen_ai.input.messages JSON string.

    OTel semconv: [{role, parts: [{type, content|...}]}]
    """
    result = []
    for msg in messages:
        role = getattr(msg, "role", "unknown")
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            parts = [{"type": "text", "content": _truncate(content)}]
        elif isinstance(content, list):
            parts = []
            for block in content:
                btype = getattr(block, "type", "text")
                if btype == "text":
                    parts.append({"type": "text", "content": _truncate(getattr(block, "text", "") or "")})
                elif btype == "tool_use":
                    parts.append(
                        {
                            "type": "tool_call",
                            "name": getattr(block, "name", ""),
                            "id": getattr(block, "tool_use_id", ""),
                        }
                    )
                elif btype == "tool_result":
                    parts.append(
                        {
                            "type": "tool_call_response",
                            "id": getattr(block, "tool_use_id", ""),
                            "response": _truncate(getattr(block, "text", "") or ""),
                        }
                    )
                else:
                    parts.append({"type": btype})
        else:
            parts = [{"type": "text", "content": _truncate(str(content))}]
        result.append({"role": role, "parts": parts})
    return _json_dumps(result)


def serialize_output_messages(text: str, finish_reason: str) -> str:
    """Serialize assistant output to gen_ai.output.messages JSON string.

    OTel semconv: [{role, parts: [{type, content}], finish_reason}]
    """
    return _json_dumps(
        [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": _truncate(text)}],
                "finish_reason": finish_reason,
            }
        ]
    )


def serialize_system_instructions(system: str) -> str:
    """Serialize system prompt to gen_ai.system_instructions JSON string."""
    return _json_dumps([{"type": "text", "content": _truncate(system)}])


def serialize_tool_definitions(tools: list | None) -> str:
    """Serialize ToolDefinition list to gen_ai.tool.definitions JSON string."""
    if not tools:
        return "[]"
    result = []
    for td in tools:
        result.append(
            {
                "name": getattr(td, "name", ""),
                "type": "function",
                "description": _truncate(getattr(td, "description", "") or ""),
            }
        )
    return _json_dumps(result)


def serialize_tool_arguments(arguments: dict | Any) -> str:
    """Serialize tool call arguments to JSON string."""
    if isinstance(arguments, str):
        return _truncate(arguments)
    return _truncate(_json_dumps(arguments))


def serialize_tool_result(result: Any) -> str:
    """Serialize tool call result to JSON string (truncated)."""
    if isinstance(result, str):
        return _truncate(result)
    content = getattr(result, "content", None)
    if content is not None:
        return _truncate(str(content))
    return _truncate(_json_dumps(result))
