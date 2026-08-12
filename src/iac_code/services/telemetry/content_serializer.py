"""Serialize messages, tools, and content for gen_ai.* span attributes.

Follows the ARMS LLM trace field schema for gen_ai.input.messages,
gen_ai.output.messages, gen_ai.system_instructions, gen_ai.tool.definitions,
gen_ai.tool.call.arguments, and gen_ai.tool.call.result.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from iac_code.tools.cloud.aliyun.result_contract import (
    ALIYUN_HTTP_METADATA_KEY,
    ALIYUN_MIGRATED_RESULT_TOOLS,
    sanitize_aliyun_http_metadata,
)
from iac_code.tools.cloud.registry import (
    ANONYMOUS_ALIYUN_TOOL_NAMES,
    CREDENTIAL_GATED_ALIYUN_TOOL_NAMES,
)
from iac_code.utils.public_errors import sanitize_strict_text
from iac_code.utils.tool_result_redaction import redact_tool_result_file_content

_MAX_CONTENT_BYTES = 4096
_REDACTED_VALUE = "[REDACTED]"
_SENSITIVE_MAPPING_KEY_PATTERN = re.compile(
    r"(?:auth|authorization|cookie|credential|credentials|passphrase|password|passwd|"
    r"private[_-]?key|pwd|secret|session|signature|token|api[_-]?key|"
    r"access[_-]?key(?:[_-]?(?:id|secret))?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TelemetryInputBlock:
    """A non-wire content block used only for request telemetry."""

    type: str
    text: str | None = None
    tool_use_id: str | None = None
    name: str | None = None
    content: str | None = None
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TelemetryInputMessage:
    """A non-wire message paired positionally with a provider message."""

    role: str
    content: str | list[TelemetryInputBlock]


def _truncate(s: str, max_bytes: int = _MAX_CONTENT_BYTES) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "...[truncated]"


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _strict_text(value: Any) -> str:
    return sanitize_strict_text("" if value is None else str(value), fallback_summary="")


def _is_sensitive_mapping_key(value: str) -> bool:
    return _SENSITIVE_MAPPING_KEY_PATTERN.search(value) is not None


def _strict_json_value(value: Any) -> Any:
    """Normalize unknown leaves and strict-sanitize before JSON serialization."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _strict_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            raw_key_text = "" if raw_key is None else str(raw_key)
            key = _strict_text(raw_key_text)
            if key in result:
                index = 2
                while f"{key}#{index}" in result:
                    index += 1
                key = f"{key}#{index}"
            result[key] = _REDACTED_VALUE if _is_sensitive_mapping_key(raw_key_text) else _strict_json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_strict_json_value(item) for item in value]
    return _strict_text(value)


def _strict_json_dumps(obj: Any) -> str:
    return _json_dumps(_strict_json_value(obj))


def _redacted_tool_result_string(value: Any) -> str:
    redacted = redact_tool_result_file_content(value)
    if isinstance(redacted, str):
        return _strict_text(redacted)
    return _strict_json_dumps(redacted)


def serialize_user_input(user_input: str) -> str:
    """Serialize a plain user input string to gen_ai.input.messages JSON."""
    return _json_dumps([{"role": "user", "parts": [{"type": "text", "content": _truncate(_strict_text(user_input))}]}])


def _tool_call_id(block: Any) -> str:
    for attribute in ("tool_use_id", "id"):
        value = getattr(block, attribute, None)
        if isinstance(value, str) and value:
            return _strict_text(value)
    return ""


def serialize_input_messages(messages: list) -> str:
    """Serialize provider Message list to gen_ai.input.messages JSON string.

    OTel semconv: [{role, parts: [{type, content|...}]}]
    """
    unmatched_tool_names: dict[str, deque[str]] = defaultdict(deque)
    result = []
    for msg in messages:
        role = getattr(msg, "role", "unknown")
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            parts = [{"type": "text", "content": _truncate(_strict_text(content))}]
        elif isinstance(content, list):
            parts = []
            for block in content:
                btype = getattr(block, "type", "text")
                if btype == "text":
                    parts.append({"type": "text", "content": _truncate(_strict_text(getattr(block, "text", "") or ""))})
                elif btype == "tool_use":
                    tool_use_id = _tool_call_id(block)
                    tool_name = _strict_text(getattr(block, "name", ""))
                    if tool_use_id and isinstance(tool_name, str):
                        unmatched_tool_names[tool_use_id].append(tool_name)
                    parts.append(
                        {
                            "type": "tool_call",
                            "name": tool_name,
                            "id": tool_use_id,
                        }
                    )
                elif btype == "tool_result":
                    response = getattr(block, "text", None)
                    if response is None or response == "":
                        response = getattr(block, "content", "") or ""
                    tool_use_id = _tool_call_id(block)
                    explicit_tool_name = getattr(block, "name", None)
                    matched = unmatched_tool_names.get(tool_use_id)
                    matched_tool_name = matched.popleft() if matched else None
                    tool_name = explicit_tool_name if isinstance(explicit_tool_name, str) else matched_tool_name
                    serialized_response = (
                        _aliyun_tool_result(
                            block,
                            tool_name=tool_name,
                            provider_input=True,
                        )
                        if tool_name in _ALIYUN_TOOL_NAMES
                        else _redacted_tool_result_string(response)
                    )
                    parts.append(
                        {
                            "type": "tool_call_response",
                            "id": tool_use_id,
                            "response": _truncate(serialized_response),
                        }
                    )
                else:
                    parts.append({"type": btype})
        else:
            parts = [{"type": "text", "content": _truncate(_strict_text(content))}]
        result.append({"role": _strict_text(role), "parts": parts})
    return _json_dumps(result)


def serialize_output_messages(text: str, finish_reason: str) -> str:
    """Serialize assistant output to gen_ai.output.messages JSON string.

    OTel semconv: [{role, parts: [{type, content}], finish_reason}]
    """
    return _json_dumps(
        [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": _truncate(_strict_text(text))}],
                "finish_reason": finish_reason,
            }
        ]
    )


def serialize_system_instructions(system: str) -> str:
    """Serialize system prompt to gen_ai.system_instructions JSON string."""
    return _json_dumps([{"type": "text", "content": _truncate(_strict_text(system))}])


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


_ALIYUN_TOOL_NAMES = frozenset(ANONYMOUS_ALIYUN_TOOL_NAMES + CREDENTIAL_GATED_ALIYUN_TOOL_NAMES)
_API_STYLES = frozenset({"RPC", "ROA"})
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
_DOC_DETAILS = frozenset({"summary", "full"})
_ALIYUN_PRESENCE_FIELDS = {
    "aliyun_doc_search": ("keywords", "category_id"),
    "aliyun_api_doc": ("product", "action", "version", "detail"),
    "ros_validate_template": ("template_url", "region_id", "parameters", "stack_name"),
    "ros_get_template_parameter_constraints": ("template_url", "region_id", "parameters", "stack_name"),
    "ros_preview_template": ("template_url", "region_id", "parameters", "stack_name"),
    "ros_estimate_template_cost": ("template_url", "region_id", "parameters", "stack_name"),
    "ros_stack": ("action", "params", "region_id"),
    "ros_stack_instances": ("action", "params", "region_id"),
    "ros_stack_group": ("action", "params", "region_id"),
    "ros_template": ("action", "params", "region_id"),
    "ros_template_scratch": ("action", "params", "region_id"),
    "ros_diagnostic": ("action", "params", "region_id"),
    "ros_resource_type_registration": ("action", "params", "region_id"),
    "ros_tag": ("action", "params", "region_id"),
}


def _aliyun_tool_arguments(arguments: Any, tool_name: str) -> str:
    values = arguments if isinstance(arguments, dict) else {}
    if tool_name == "aliyun_api":
        style = values.get("style")
        method = values.get("method")
        body_present = "body" in values
        body_file_present = "body_file" in values
        if body_present and body_file_present:
            body_source = "conflicting"
        elif body_present:
            body_source = "body"
        elif body_file_present:
            body_source = "body_file"
        else:
            body_source = "none"
        result: dict[str, Any] = {
            "body_source": body_source,
            "product_present": "product" in values,
            "version_present": "version" in values,
            "action_present": "action" in values,
            "region_id_present": "region_id" in values,
            "pathname_present": "pathname" in values,
            "params_present": "params" in values,
            "body_present": body_present,
            "body_file_present": body_file_present,
        }
        if isinstance(style, str) and style.upper() in _API_STYLES:
            result["style"] = style.upper()
        if isinstance(method, str) and method.upper() in _HTTP_METHODS:
            result["method"] = method.upper()
        return _json_dumps(result)
    result = {f"{field}_present": field in values for field in _ALIYUN_PRESENCE_FIELDS.get(tool_name, ())}
    if tool_name == "aliyun_api_doc":
        detail = values.get("detail", "summary")
        if isinstance(detail, str) and detail in _DOC_DETAILS:
            result["detail"] = detail
    return _json_dumps(result)


def serialize_tool_arguments(arguments: dict | Any, *, tool_name: str | None = None) -> str:
    """Serialize tool call arguments to JSON string."""
    if tool_name in _ALIYUN_TOOL_NAMES:
        return _aliyun_tool_arguments(arguments, tool_name)
    if isinstance(arguments, str):
        return _truncate(_strict_text(arguments))
    return _truncate(_strict_json_dumps(arguments))


def _aliyun_tool_result(
    result: Any,
    *,
    tool_name: str | None = None,
    provider_input: bool = False,
) -> str:
    raw_content = getattr(result, "content", None)
    if raw_content is None:
        raw_content = getattr(result, "text", result)
    if isinstance(raw_content, str):
        try:
            payload = json.loads(raw_content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
    else:
        payload = raw_content
    payload = payload if isinstance(payload, dict) else {}
    status = payload.get("status")
    valid_status = isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
    metadata = getattr(result, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    aliyun_http = sanitize_aliyun_http_metadata(metadata.get(ALIYUN_HTTP_METADATA_KEY))
    is_error = bool(getattr(result, "is_error", False))
    if aliyun_http is not None and tool_name in ALIYUN_MIGRATED_RESULT_TOOLS:
        output: dict[str, Any] = {
            "is_error": is_error,
            "headers_present": bool(aliyun_http.get("headers_present")),
            "body_present": bool(aliyun_http.get("body_present")),
            "content_type_present": bool(aliyun_http.get("content_type_present")),
            "content_encoding_present": bool(aliyun_http.get("content_encoding_present")),
            "size_present": bool(aliyun_http.get("size_present")),
            "headers_nonempty": bool(aliyun_http.get("headers_nonempty")),
            "header_count": int(aliyun_http.get("header_count", 0)),
            "artifact_present": False if provider_input else bool(metadata.get("artifacts")),
        }
        status = aliyun_http.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            output["status"] = status
            output["status_class"] = aliyun_http.get("status_class", "{}xx".format(status // 100))
        return _json_dumps(output)

    if tool_name in ALIYUN_MIGRATED_RESULT_TOOLS and (provider_input or is_error):
        return _json_dumps(
            {
                "is_error": is_error,
                "headers_present": False,
                "body_present": False,
                "content_type_present": False,
                "content_encoding_present": False,
                "size_present": False,
                "artifact_present": False if provider_input else bool(metadata.get("artifacts")),
            }
        )

    output: dict[str, Any] = {
        "is_error": is_error,
        "headers_present": "headers" in payload,
        "body_present": "body" in payload,
        "content_type_present": "content_type" in payload,
        "content_encoding_present": payload.get("content_encoding") is not None,
        "size_present": "size" in payload,
        "artifact_present": "artifact_path" in payload or bool(metadata.get("artifacts")),
    }
    if valid_status and isinstance(status, int):
        output["status"] = status
        output["status_class"] = "{}xx".format(status // 100)
    return _json_dumps(output)


def serialize_tool_result(result: Any, *, tool_name: str | None = None) -> str:
    """Serialize tool call result to JSON string (truncated)."""
    if tool_name in _ALIYUN_TOOL_NAMES:
        return _aliyun_tool_result(result, tool_name=tool_name)
    if isinstance(result, str):
        return _truncate(_redacted_tool_result_string(result))
    content = getattr(result, "content", None)
    if content is not None:
        return _truncate(_redacted_tool_result_string(content))
    return _truncate(_redacted_tool_result_string(result))
