"""State helpers for completion guards that depend on prior tool results."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from iac_code.tools.cloud.base_stack import stack_result_from_metadata

logger = logging.getLogger(__name__)

_FILE_MUTATION_TOOLS = {"write_file", "edit_file"}
_SOLUTION_FIRST_ROS_RESULT_TOOLS = {
    "ros_get_template_parameter_constraints",
    "ros_preview_template",
    "ros_estimate_template_cost",
}
_STRUCTURED_RESULT_TOOLS = {
    "infraguard_scan",
    "ros_deploy",
    "ros_stack",
    "ros_validate_template",
    *_SOLUTION_FIRST_ROS_RESULT_TOOLS,
}
_ROS_PREFLIGHT_RESULT_TOOLS = {
    "ros_deploy",
    "ros_stack",
    "ros_validate_template",
    *_SOLUTION_FIRST_ROS_RESULT_TOOLS,
}


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
    record_id: str | None = None,
) -> None:
    """Record tool results that completion guards may need later in the same step."""

    try:
        ensure_completion_guard_state(state)
        if cwd:
            state["cwd"] = cwd
        if tool_name == "ask_user_question":
            _record_ask_user_question(
                state,
                content,
                tool_input=tool_input,
                is_error=is_error,
                record_id=record_id,
            )
            return
        v2_records = state.get("completion_record_contract") == "v2"
        parsed = None
        if tool_name in {"ros_deploy", "ros_stack"}:
            parsed = stack_result_from_metadata(metadata)
            if parsed is None and isinstance(metadata, dict):
                stack_id = metadata.get("stack_id")
                if isinstance(stack_id, str) and stack_id:
                    parsed = {"stack_id": stack_id}
        if parsed is None:
            parsed = _json_object(
                content,
                # A failed tool result carries a localized error message by contract, so a
                # parse miss is expected rather than a defect. Parsing still runs -- some
                # failures do return JSON, and v2 records keep the parsed payload -- but a
                # warning per failure only buries the real error under tracebacks.
                log_failure=tool_name in _STRUCTURED_RESULT_TOOLS and not is_error,
                allow_ros_preflight_suffix=tool_name in _ROS_PREFLIGHT_RESULT_TOOLS,
            )
        if parsed is None and tool_name in _FILE_MUTATION_TOOLS:
            parsed = _file_mutation_result(tool_input, cwd=cwd)
        elif parsed is not None:
            _add_canonical_file_path(parsed, tool_input, cwd=cwd)
        if parsed is None and not v2_records:
            return
        records: list[dict[str, Any]] = state.setdefault("tool_result_records", [])
        sequence = len(records) + 1
        record: dict[str, Any] = {
            "tool_name": tool_name,
            "input": dict(tool_input),
            "result": parsed if isinstance(parsed, dict) else {},
            "is_error": bool(is_error),
        }
        if v2_records:
            record.update(
                {
                    "record_id": record_id or f"record-{sequence}",
                    "sequence": sequence,
                    "error_summary": _bounded_error_summary(content) if is_error or parsed is None else "",
                }
            )
            candidate_set_id = metadata.get("candidate_set_id") if isinstance(metadata, dict) else None
            if isinstance(candidate_set_id, str) and candidate_set_id:
                record["candidate_set_id"] = candidate_set_id
            effective_region = _effective_region_id(tool_input, metadata, parsed)
            if effective_region:
                record["effective_region_id"] = effective_region
        records.append(record)
        if isinstance(parsed, dict) and (not v2_records or not is_error):
            state.setdefault("tool_results", {})[tool_name] = parsed
        if tool_name == "ros_deploy" and isinstance(parsed, dict):
            _record_ros_deploy_owned_stack(state, tool_input, parsed)
        if not is_error:
            state.setdefault("successful_tools", set()).add(tool_name)
    except Exception:
        logger.warning("Failed to rebuild completion guard state", exc_info=True)


def _record_ask_user_question(
    state: dict[str, Any],
    content: Any,
    *,
    tool_input: dict[str, Any] | None = None,
    is_error: bool,
    record_id: str | None = None,
) -> None:
    if is_error:
        if state.get("completion_record_contract") == "v2":
            records: list[dict[str, Any]] = state.setdefault("tool_result_records", [])
            records.append(
                {
                    "record_id": record_id or f"record-{len(records) + 1}",
                    "sequence": len(records) + 1,
                    "tool_name": "ask_user_question",
                    "input": dict(tool_input) if isinstance(tool_input, dict) else {},
                    "result": {},
                    "is_error": True,
                    "error_summary": _bounded_error_summary(content),
                }
            )
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
    # Answered questions also join the ordered records so guards can bind a
    # conclusion to the latest still-valid answer (and detect newer tool runs).
    records: list[dict[str, Any]] = state.setdefault("tool_result_records", [])
    record: dict[str, Any] = {
        "tool_name": "ask_user_question",
        "input": dict(tool_input) if isinstance(tool_input, dict) else {},
        "result": parsed,
        "is_error": False,
    }
    if state.get("completion_record_contract") == "v2":
        record.update(
            {
                "record_id": record_id or f"record-{len(records) + 1}",
                "sequence": len(records) + 1,
                "error_summary": "",
            }
        )
    records.append(record)


def _bounded_error_summary(content: Any) -> str:
    text = str(content or "").strip().replace("\x00", "")
    return text if len(text) <= 500 else text[:500] + "…"


def _effective_region_id(
    tool_input: dict[str, Any],
    metadata: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> str:
    sources = [metadata or {}, result or {}, tool_input]
    for source in sources:
        for key in ("effective_region_id", "region_id", "RegionId", "regionId"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


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


def _json_object(
    value: Any,
    *,
    log_failure: bool = True,
    allow_ros_preflight_suffix: bool = False,
) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = _json_object_before_ros_preflight_suffix(value) if allow_ros_preflight_suffix else None
        if parsed is not None:
            return parsed
        if log_failure:
            logger.warning("Failed to parse completion guard state", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_object_before_ros_preflight_suffix(value: str) -> dict[str, Any] | None:
    """Parse the JSON response before an appended ROS preflight diagnostic block.

    ``attach_ros_validation`` keeps the provider response as leading JSON and appends a
    localized block separated by ``---``.  Completion guards need the provider response,
    while arbitrary trailing text must remain invalid.
    """

    stripped = value.lstrip()
    try:
        parsed, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    trailing = stripped[end:].lstrip("\r\n")
    if not trailing.startswith("---\n"):
        return None
    header = trailing.removeprefix("---\n").splitlines()[0] if trailing.removeprefix("---\n") else ""
    return parsed if "ROS" in header else None
