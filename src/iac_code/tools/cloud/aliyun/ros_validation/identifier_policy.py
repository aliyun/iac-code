"""Identifier-source policies for ROS actions that carry no TemplateBody field.

The locked ``ros_template_body_action_policies.json`` registry only covers actions
whose SDK request declares ``template_body``. Read-only actions such as
``GetTemplate`` select their target through mutually exclusive identifier
parameters instead, so they are absent from that registry and previously reached
the ROS API without any local pre-call validation. This module adds an
independent policy layer for those actions and keeps the locked TemplateBody
registry, including its ``request_fields_sha256`` evidence, untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    Severity,
    make_diagnostic,
)


@dataclass(frozen=True)
class IdentifierPolicy:
    """Exactly-one identifier contract for one ROS action."""

    action: str
    identifier_fields: frozenset[str]


IDENTIFIER_POLICIES: Mapping[str, IdentifierPolicy] = MappingProxyType(
    {
        "GetTemplate": IdentifierPolicy(
            action="GetTemplate",
            identifier_fields=frozenset({"ChangeSetId", "StackGroupName", "StackId", "TemplateId"}),
        ),
    }
)

IDENTIFIER_SOURCE_ACTIONS = tuple(IDENTIFIER_POLICIES)


def _present(params: Mapping[str, Any], field: str) -> bool:
    value = params.get(field)
    return value is not None and value != "" and value != []


def _identifier_error(action: str, summary: str, detail: str, suggestion: str, *stable: str) -> Diagnostic:
    return make_diagnostic(
        code="ROS1201",
        severity=Severity.ERROR,
        category=Category.COMPATIBILITY,
        summary=summary,
        detail=detail,
        suggestion=suggestion,
        stable_args=(action, *stable),
        subject="identifier-source",
    )


def validate_identifier_request(action: str, params: Mapping[str, Any]) -> list[Diagnostic]:
    """Return ROS1201 diagnostics when identifier-source cardinality is violated."""

    policy = IDENTIFIER_POLICIES.get(action)
    if policy is None:
        return []
    present = sorted(field for field in policy.identifier_fields if _present(params, field))
    if len(present) == 1:
        return []
    fields = ", ".join(sorted(policy.identifier_fields))
    if not present:
        return [
            _identifier_error(
                action,
                _("{} is missing a required identifier parameter.").format(action),
                _("Provide exactly one of these identifiers: {fields}; none were provided.").format(fields=fields),
                _(
                    "Read one identifier from the page context, using the template page TemplateId rather than "
                    "the displayed template name."
                ),
                "missing-identifier",
            )
        ]
    return [
        _identifier_error(
            action,
            _("{} provides multiple mutually exclusive identifier parameters.").format(action),
            _("Provide exactly one of these identifiers: {fields}; {provided} were provided.").format(
                fields=fields, provided=", ".join(present)
            ),
            _("Keep only the identifier that matches the target you want to read."),
            "conflicting-identifier",
            str(len(present)),
        )
    ]


def validate_template_id_shape(action: str, params: Mapping[str, Any]) -> list[Diagnostic]:
    """Distinguish a real ROS TemplateId from a template display name or title."""

    value = params.get("TemplateId")
    if not isinstance(value, str) or not value:
        return []
    # ROS identifiers are opaque ASCII tokens. Whitespace or non-ASCII characters
    # mean the caller passed a template display name or title instead.
    if not any(character.isspace() for character in value) and value.isascii():
        return []
    return [
        _identifier_error(
            action,
            _("TemplateId is a template display name rather than a ROS identifier."),
            _("TemplateId must be the opaque identifier returned by ROS, not the name or title shown on the page."),
            _(
                "Resolve the real TemplateId first via ros_template ListTemplates filtered by TemplateName, "
                "then retry."
            ),
            "template-id-is-display-name",
        )
    ]
