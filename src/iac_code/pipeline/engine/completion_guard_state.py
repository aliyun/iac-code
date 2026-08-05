"""State helpers for completion guards that depend on prior tool results."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from iac_code.tools.cloud.base_stack import stack_result_from_metadata

logger = logging.getLogger(__name__)

_FILE_MUTATION_TOOLS = {"write_file", "edit_file"}
_STRUCTURED_RESULT_TOOLS = {"infraguard_scan", "ros_deploy", "ros_stack", "ros_validate_template"}


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
    cwd: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record tool results that completion guards may need later in the same step."""

    try:
        ensure_completion_guard_state(state)
        if cwd:
            state["cwd"] = cwd
        if tool_name == "ask_user_question":
            _record_ask_user_question(state, content, is_error=is_error)
            return
        parsed = None
        if tool_name in {"ros_deploy", "ros_stack"}:
            parsed = stack_result_from_metadata(metadata)
            if parsed is None and isinstance(metadata, dict):
                stack_id = metadata.get("stack_id")
                if isinstance(stack_id, str) and stack_id:
                    parsed = {"stack_id": stack_id}
        if parsed is None:
            parsed = _json_object(content, log_failure=tool_name in _STRUCTURED_RESULT_TOOLS)
        if parsed is None and tool_name in _FILE_MUTATION_TOOLS:
            parsed = _file_mutation_result(tool_input, cwd=cwd)
        elif parsed is not None:
            _add_canonical_file_path(parsed, tool_input, cwd=cwd)
        if parsed is None:
            return
        records: list[dict[str, Any]] = state.setdefault("tool_result_records", [])
        records.append(
            {
                "tool_name": tool_name,
                "input": dict(tool_input),
                "result": parsed,
                "is_error": bool(is_error),
            }
        )
        state.setdefault("tool_results", {})[tool_name] = parsed
        if tool_name == "ros_deploy":
            _record_ros_deploy_owned_stack(state, tool_input, parsed)
        if not is_error:
            state.setdefault("successful_tools", set()).add(tool_name)
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


def record_ros_deploy_observed_stack(
    state: dict[str, Any],
    *,
    tool_input: dict[str, Any],
    stack_id: str,
) -> None:
    """Record a Stack ID observed while the current step is starting it."""
    if tool_input.get("action") not in {"create", "delete_and_create"} or not stack_id:
        return
    ensure_completion_guard_state(state)
    owned = state.setdefault("ros_deploy_owned_stack_ids", {})
    if isinstance(owned, dict):
        owned[stack_id] = {"action": tool_input["action"]}


def _file_mutation_result(tool_input: dict[str, Any], *, cwd: str | None = None) -> dict[str, Any] | None:
    path = tool_input.get("path") or tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    result = {"file_path": path}
    _add_canonical_file_path(result, tool_input, cwd=cwd)
    return result


def _add_canonical_file_path(
    result: dict[str, Any],
    tool_input: dict[str, Any],
    *,
    cwd: str | None = None,
) -> None:
    path = result.get("file_path") or tool_input.get("path") or tool_input.get("file_path")
    if not isinstance(path, str) or not path or "://" in path or not cwd:
        return
    expanded = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    result.setdefault("canonical_file_path", os.path.normcase(os.path.realpath(os.path.abspath(expanded))))


def _json_object(value: Any, *, log_failure: bool = True) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        if log_failure:
            logger.warning("Failed to parse completion guard state", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None
