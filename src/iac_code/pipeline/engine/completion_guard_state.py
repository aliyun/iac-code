"""State helpers for completion guards that depend on prior tool results."""

from __future__ import annotations

import json
from typing import Any


def ensure_completion_guard_state(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("successful_tools", set())
    state.setdefault("tool_results", {})
    state.setdefault("tool_result_records", [])
    return state


def record_completion_guard_tool_result(
    state: dict[str, Any],
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    content: Any,
    is_error: bool,
) -> None:
    """Record tool results that completion guards may need later in the same step."""

    ensure_completion_guard_state(state)
    if tool_name == "ask_user_question":
        _record_ask_user_question(state, content, is_error=is_error)
        return
    if tool_name == "ros_stack":
        _record_ros_stack(state, tool_input, content, is_error=is_error)


def _record_ask_user_question(state: dict[str, Any], content: Any, *, is_error: bool) -> None:
    if is_error:
        return
    successful_tools: set[str] = state.setdefault("successful_tools", set())
    successful_tools.add("ask_user_question")
    tool_results: dict[str, Any] = state.setdefault("tool_results", {})
    parsed = _json_object(content)
    if parsed is None:
        parsed = {
            "selected_id": "",
            "selected_label": "",
            "free_text": str(content),
        }
    tool_results["ask_user_question"] = parsed


def _record_ros_stack(state: dict[str, Any], tool_input: dict[str, Any], content: Any, *, is_error: bool) -> None:
    parsed = _json_object(content)
    if parsed is None:
        return
    records: list[dict[str, Any]] = state.setdefault("tool_result_records", [])
    record = {
        "tool_name": "ros_stack",
        "input": dict(tool_input),
        "result": parsed,
        "is_error": bool(is_error),
    }
    records.append(record)
    state.setdefault("tool_results", {})["ros_stack"] = parsed
    if not is_error:
        state.setdefault("successful_tools", set()).add("ros_stack")


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
