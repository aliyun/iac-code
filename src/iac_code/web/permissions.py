"""Authoritative Web permission and question pending request state."""

from __future__ import annotations

import asyncio
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
