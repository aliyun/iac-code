"""State helpers for completion guards that depend on prior tool results."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


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

    try:
        ensure_completion_guard_state(state)
        if tool_name == "ask_user_question":
            _record_ask_user_question(state, content, is_error=is_error)
            return
        if tool_name in {"ros_stack", "ros_deploy"}:
            _record_stack_tool(state, tool_name, tool_input, content, is_error=is_error)
    except Exception:
        logger.warning("Failed to rebuild completion guard state", exc_info=True)


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


def _record_stack_tool(
    state: dict[str, Any], tool_name: str, tool_input: dict[str, Any], content: Any, *, is_error: bool
) -> None:
    parsed = _json_object(content)
    if parsed is None:
        return
    records: list[dict[str, Any]] = state.setdefault("tool_result_records", [])
    record = {
        "tool_name": tool_name,
        "input": dict(tool_input),
        "result": parsed,
        "is_error": bool(is_error),
    }
    records.append(record)
    state.setdefault("tool_results", {})[tool_name] = parsed
    if tool_name == "ros_deploy":
        _record_ros_deploy_owned_stack(state, tool_input, parsed)
    if not is_error:
        state.setdefault("successful_tools", set()).add(tool_name)


def _record_ros_deploy_owned_stack(state: dict[str, Any], tool_input: dict[str, Any], result: dict[str, Any]) -> None:
    action = tool_input.get("action")
    if action not in {"create", "delete_and_create"}:
        return
    stack_id = result.get("stack_id")
    if not isinstance(stack_id, str) or not stack_id:
        return
    owned = state.setdefault("ros_deploy_owned_stack_ids", {})
    if isinstance(owned, dict):
        owned[stack_id] = {"action": action}


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logger.warning("Failed to parse completion guard state", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None
