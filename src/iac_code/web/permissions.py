"""Authoritative Web permission and question pending request state."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iac_code.i18n import _
from iac_code.web.events import normalize_event_payload

PERMISSION_ALLOW_ONCE = "allow_once"
PERMISSION_REJECT_ONCE = "reject_once"
PERMISSION_ALWAYS_ALLOW = "always_allow"
PERMISSION_ALWAYS_DENY = "always_deny"
PERMISSION_CHOICES = frozenset(
    {
        PERMISSION_ALLOW_ONCE,
        PERMISSION_REJECT_ONCE,
        PERMISSION_ALWAYS_ALLOW,
        PERMISSION_ALWAYS_DENY,
    }
)
DEFAULT_PERMISSION_CHOICES: tuple[dict[str, str], ...] = (
    {"id": PERMISSION_ALLOW_ONCE, "label": _("Allow once")},
    {"id": PERMISSION_REJECT_ONCE, "label": _("Deny once")},
)
SESSION_RULE_PERMISSION_CHOICES: tuple[dict[str, str], ...] = (
    {"id": PERMISSION_ALLOW_ONCE, "label": _("Allow once")},
    {"id": PERMISSION_ALWAYS_ALLOW, "label": _("Always allow this session")},
    {"id": PERMISSION_REJECT_ONCE, "label": _("Deny once")},
    {"id": PERMISSION_ALWAYS_DENY, "label": _("Always deny this session")},
)


@dataclass
class WebPendingPermission:
    request_id: str
    session_id: str
    payload: dict[str, Any]
    future: asyncio.Future[Any]
    created_at: str
    audit_event: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "payload": normalize_event_payload(self.payload),
        }


@dataclass
class WebPendingQuestion:
    request_id: str
    session_id: str
    payload: dict[str, Any]
    future: asyncio.Future[Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "payload": normalize_event_payload(self.payload),
        }


ELICITATION_ACCEPT = "accept"
ELICITATION_DECLINE = "decline"
ELICITATION_CANCEL = "cancel"
ELICITATION_ACTIONS = frozenset({ELICITATION_ACCEPT, ELICITATION_DECLINE, ELICITATION_CANCEL})


@dataclass
class WebPendingElicitation:
    request_id: str
    session_id: str
    payload: dict[str, Any]
    future: asyncio.Future[Any]
    created_at: str
    # 原始（已解析的）JSON schema，仅后端持有：用于回灌时按字段类型校验/强转用户输入。
    schema: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "payload": normalize_event_payload(self.payload),
        }


def normalize_permission_payload(payload: dict[str, Any], *, request_id: str, session_id: str) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["requestId"] = request_id
    normalized["sessionId"] = session_id
    normalized.setdefault("message", _permission_message(normalized))
    normalized["suggestions"] = _normalize_suggestions(normalized.get("suggestions"))
    normalized["choices"] = _normalize_permission_choices(
        normalized.get("choices"),
        suggestions=normalized["suggestions"],
        allow_always=bool(normalized.get("allowAlways", False)),
    )
    return normalized


def normalize_question_payload(payload: dict[str, Any], *, request_id: str, session_id: str) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["requestId"] = request_id
    normalized["sessionId"] = session_id
    return normalized


def elicitation_schema_from_payload(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Resolve the elicitation JSON schema, tolerating either key MCP servers may send."""
    schema = payload.get("requestedSchema") or payload.get("schema")
    if not isinstance(schema, Mapping):
        return None
    properties = schema.get("properties")
    return schema if isinstance(properties, Mapping) else None


def normalize_elicitation_payload(payload: Mapping[str, Any], *, request_id: str, session_id: str) -> dict[str, Any]:
    """Shape an MCP elicitation request into a frontend-renderable payload.

    ``mode`` collapses to one of ``"url"`` / ``"form"`` / ``"confirm"`` so the browser can
    pick a renderer without re-parsing the schema; ``fields`` describes the form inputs.
    """
    normalized: dict[str, Any] = {}
    normalized["requestId"] = request_id
    normalized["sessionId"] = session_id
    normalized["server"] = str(payload.get("server") or payload.get("serverName") or "")
    normalized["message"] = str(payload.get("message") or "")
    normalized["url"] = str(payload.get("url") or "")
    schema = elicitation_schema_from_payload(payload)
    fields = _elicitation_fields_from_schema(schema)
    raw_mode = str(payload.get("mode") or "")
    if raw_mode == "url":
        mode = "url"
    elif fields:
        mode = "form"
    else:
        mode = "confirm"
    normalized["mode"] = mode
    normalized["fields"] = fields
    return normalized


def elicitation_result_from_body(data: Mapping[str, Any], *, schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate a browser answer body into the ``{action, content?}`` MCP handler contract."""
    action = str(data.get("action") or "").strip().lower()
    if action not in ELICITATION_ACTIONS:
        raise ValueError(_("Unknown elicitation action {!r}.").format(action))
    if action != ELICITATION_ACCEPT:
        return {"action": action}
    content = _elicitation_content_from_answer(schema, data.get("content"))
    result: dict[str, Any] = {"action": ELICITATION_ACCEPT}
    if content:
        result["content"] = content
    return result


def canceled_elicitation_answer() -> dict[str, Any]:
    return {"action": ELICITATION_CANCEL, "canceled": True}


def _elicitation_fields_from_schema(schema: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(schema, Mapping):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    required = {str(value) for value in schema.get("required", []) if str(value)}
    fields: list[dict[str, Any]] = []
    for raw_name, raw_field in properties.items():
        name = str(raw_name)
        if not isinstance(raw_field, Mapping):
            continue
        field: dict[str, Any] = {
            "name": name,
            "label": str(raw_field.get("title") or name),
            "description": str(raw_field.get("description") or ""),
            "required": name in required,
        }
        enum_values = raw_field.get("enum")
        if isinstance(enum_values, list) and enum_values:
            field["type"] = "enum"
            field["enum"] = [str(value) for value in enum_values]
        else:
            field_type = str(raw_field.get("type") or "string")
            field["type"] = field_type if field_type in {"string", "integer", "number", "boolean"} else "string"
        fields.append(field)
    return fields


def _elicitation_content_from_answer(schema: Mapping[str, Any] | None, raw_content: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    required = {str(value) for value in schema.get("required", []) if str(value)}
    provided = raw_content if isinstance(raw_content, Mapping) else {}
    content: dict[str, Any] = {}
    for raw_name, raw_field in properties.items():
        name = str(raw_name)
        if not isinstance(raw_field, Mapping):
            continue
        raw_value = provided.get(name)
        if raw_value is None or raw_value == "":
            if name in required:
                raise ValueError(_("A value is required for {}.").format(name))
            continue
        content[name] = _coerce_elicitation_value(raw_value, raw_field)
    return content


def _coerce_elicitation_value(raw_value: Any, field_schema: Mapping[str, Any]) -> Any:
    enum_values = field_schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        allowed = {str(value): value for value in enum_values}
        key = raw_value if isinstance(raw_value, str) else str(raw_value)
        if key in allowed:
            return allowed[key]
        raise ValueError(_("Invalid value {!r}.").format(raw_value))
    field_type = str(field_schema.get("type") or "string")
    if field_type == "boolean":
        if isinstance(raw_value, bool):
            return raw_value
        normalized = str(raw_value).strip().lower()
        if normalized in {"yes", "y", "true", "1"}:
            return True
        if normalized in {"no", "n", "false", "0"}:
            return False
        raise ValueError(_("Invalid value {!r}.").format(raw_value))
    if field_type == "integer":
        try:
            return int(str(raw_value).strip())
        except (TypeError, ValueError):
            raise ValueError(_("Invalid value {!r}.").format(raw_value)) from None
    if field_type == "number":
        try:
            return float(str(raw_value).strip())
        except (TypeError, ValueError):
            raise ValueError(_("Invalid value {!r}.").format(raw_value)) from None
    return str(raw_value)


def offered_permission_choice_ids(payload: dict[str, Any]) -> set[str]:
    return {
        str(choice.get("id"))
        for choice in payload.get("choices", [])
        if isinstance(choice, dict) and isinstance(choice.get("id"), str)
    }


def permission_choice_to_allowed(choice: str) -> bool:
    return choice in {PERMISSION_ALLOW_ONCE, PERMISSION_ALWAYS_ALLOW}


def question_answer_from_body(data: dict[str, Any]) -> dict[str, str]:
    return {
        "selected_id": data["selected_id"],
        "selected_label": data["selected_label"],
        "free_text": data["free_text"],
    }


def _permission_message(payload: dict[str, Any]) -> str:
    tool_name = payload.get("toolName") or payload.get("action") or _("this action")
    return _("Allow {}?").format(tool_name)


def _normalize_suggestions(raw_suggestions: Any) -> list[dict[str, str]]:
    if not isinstance(raw_suggestions, list):
        return []

    suggestions: list[dict[str, str]] = []
    for suggestion in raw_suggestions:
        if isinstance(suggestion, dict):
            tool_name = suggestion.get("toolName", suggestion.get("tool_name", ""))
            rule_content = suggestion.get("ruleContent", suggestion.get("rule_content", ""))
        else:
            tool_name = getattr(suggestion, "tool_name", "")
            rule_content = getattr(suggestion, "rule_content", "")
        suggestions.append(
            {
                "toolName": str(tool_name),
                "ruleContent": str(rule_content),
            }
        )
    return suggestions


def _normalize_permission_choices(
    raw_choices: Any,
    *,
    suggestions: list[dict[str, str]],
    allow_always: bool,
) -> list[dict[str, str]]:
    if raw_choices is None:
        if suggestions:
            rules = ", ".join(suggestion["ruleContent"] for suggestion in suggestions if suggestion["ruleContent"])
            choices = [dict(choice) for choice in SESSION_RULE_PERMISSION_CHOICES]
            if rules:
                choices[1]["label"] = _("Always allow {}").format(rules)
                choices[3]["label"] = _("Always deny {}").format(rules)
            return choices
        choices = [
            {"id": PERMISSION_ALLOW_ONCE, "label": _("Allow once")},
            {"id": PERMISSION_REJECT_ONCE, "label": _("Deny once")},
            {"id": PERMISSION_ALWAYS_DENY, "label": _("Always deny this tool")},
        ]
        if allow_always:
            choices.insert(1, {"id": PERMISSION_ALWAYS_ALLOW, "label": _("Always allow this tool")})
        return choices
    if not isinstance(raw_choices, list):
        return [dict(choice) for choice in DEFAULT_PERMISSION_CHOICES]

    choices: list[dict[str, str]] = []
    for choice in raw_choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("id"), str):
            continue
        choice_id = choice["id"]
        if choice_id not in PERMISSION_CHOICES:
            continue
        label = choice.get("label", choice_id)
        choices.append({"id": choice_id, "label": str(label)})
    return choices or [dict(choice) for choice in DEFAULT_PERMISSION_CHOICES]
