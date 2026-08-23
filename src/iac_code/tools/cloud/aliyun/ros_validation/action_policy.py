"""Versioned ROS TemplateBody action policies for the locked 2019-09-10 API."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Callable, Mapping

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    EvaluationMode,
    RequestValidationContext,
    Severity,
    TemplateSemanticMode,
    TrustedRosAccountContext,
    make_diagnostic,
)


class Cardinality(str, Enum):
    EXACTLY_ONE = "EXACTLY_ONE"
    ZERO_OR_ONE = "ZERO_OR_ONE"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True)
class ActionPolicy:
    action: str
    metadata_fields: frozenset[str]
    allowed_fields: frozenset[str]
    cardinality: Cardinality
    semantic_mode: TemplateSemanticMode
    evaluation_mode: EvaluationMode
    required_fields: frozenset[str] = frozenset()
    source_groups: tuple[frozenset[str], ...] = ()
    conditional_predicates: tuple[str, ...] = ()
    active_body_predicate: str = "template_body_present"
    known_differences: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    contract_capability_id: str = "ros-2019-09-10-sdk-3.6.0"

    def request_context(
        self,
        params: Mapping[str, Any],
        *,
        trusted_ros_account_context: TrustedRosAccountContext | None = None,
    ) -> RequestValidationContext:
        return RequestValidationContext(
            action=self.action,
            semantic_mode=self.semantic_mode,
            evaluation_mode=self.evaluation_mode,
            source_kind="INLINE" if "TemplateBody" in params else "REMOTE",
            source_fields=frozenset(key for key in self.allowed_fields if _present(params, key)),
            mode=params.get("Mode") if isinstance(params.get("Mode"), str) else None,
            entity_type=params.get("EntityType") if isinstance(params.get("EntityType"), str) else None,
            trusted_ros_account_context=trusted_ros_account_context,
        )


_B = "TemplateBody"
_U = "TemplateURL"
_I = "TemplateId"
_S = "TemplateScratchId"


def _load_action_policies() -> Mapping[str, ActionPolicy]:
    fixture = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath(
        "data/ros_template_body_action_policies.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    capability_id = str(payload["contract_capability_id"])
    policies: dict[str, ActionPolicy] = {}
    for record in payload["actions"]:
        allowed_fields = frozenset(str(item) for item in record["allowed_fields"])
        policy = ActionPolicy(
            action=str(record["action"]),
            metadata_fields=frozenset(str(item) for item in record["metadata_fields"]),
            allowed_fields=allowed_fields,
            cardinality=Cardinality(record["cardinality"]),
            semantic_mode=TemplateSemanticMode(record["semantic_mode"]),
            evaluation_mode=EvaluationMode(record["evaluation_mode"]),
            required_fields=frozenset(str(item) for item in record["required_fields"]),
            source_groups=(allowed_fields,),
            conditional_predicates=tuple(str(item) for item in record["conditional_predicates"]),
            active_body_predicate=str(record["active_body_predicate"]),
            known_differences=tuple(str(item) for item in record["known_differences"]),
            evidence=tuple(str(item) for item in record["evidence"]),
            contract_capability_id=capability_id,
        )
        if policy.action in policies:
            raise ValueError("duplicate ROS ActionPolicy: {}".format(policy.action))
        policies[policy.action] = policy
    return MappingProxyType(policies)


ACTION_POLICIES: Mapping[str, ActionPolicy] = _load_action_policies()

TEMPLATE_BODY_ACTIONS = tuple(ACTION_POLICIES)


def _present(params: Mapping[str, Any], field: str) -> bool:
    if field == "Parameters" and any(
        re.fullmatch(r"Parameters\.\d+\.ParameterKey", str(key)) and value not in (None, "")
        for key, value in params.items()
    ):
        return True
    value = params.get(field)
    return value is not None and value != "" and value != []


def _request_error(action: str, summary: str, detail: str, *stable: str, suggestion: str | None = None) -> Diagnostic:
    return make_diagnostic(
        code="ROS1201",
        severity=Severity.ERROR,
        category=Category.COMPATIBILITY,
        summary=summary,
        detail=detail,
        suggestion=suggestion,
        stable_args=(action, *stable),
        subject="template-source",
    )


def _request_warning(action: str, summary: str, detail: str, *stable: str) -> Diagnostic:
    return make_diagnostic(
        code="ROS9003",
        severity=Severity.WARNING,
        category=Category.QUALITY,
        summary=summary,
        detail=detail,
        stable_args=(action, *stable),
        subject="template-source",
    )


def _account_limitation(action: str) -> Diagnostic:
    return make_diagnostic(
        code="ROS9101",
        severity=Severity.LIMITATION,
        category=Category.LIMITATION,
        summary=_(
            "Trusted ROS account context is missing, so the Module registration account boundary cannot be verified."
        ),
        detail=_(
            "Local structure and function semantics were validated; account consistency requires trusted host context."
        ),
        stable_args=(action, "trusted-account-context"),
        subject="account-context",
    )


def _count_sources(policy: ActionPolicy, params: Mapping[str, Any]) -> list[str]:
    return [field for field in policy.allowed_fields if _present(params, field)]


_ConditionalPredicate = Callable[
    [ActionPolicy, Mapping[str, Any], list[str], TrustedRosAccountContext | None],
    list[Diagnostic],
]
_ActiveBodyPredicate = Callable[[Mapping[str, Any], list[str]], bool]


def _preview_source_cardinality(
    policy: ActionPolicy,
    params: Mapping[str, Any],
    sources: list[str],
    _trusted_context: TrustedRosAccountContext | None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    updating = _present(params, "StackId")
    if (not updating and len(sources) != 1) or (updating and len(sources) > 1):
        diagnostics.append(
            _request_error(
                policy.action,
                _("PreviewStack has an invalid number of template sources."),
                _("Creating a preview requires one source; updating a preview accepts zero or one source."),
                "update" if updating else "create",
                str(len(sources)),
            )
        )
    if updating and not sources and not _present(params, "Parameters"):
        diagnostics.append(
            _request_error(
                policy.action,
                _("PreviewStack must provide Parameters when reusing the existing template."),
                _("A source-less update preview reuses the template only when Parameters is provided."),
                "update-reuse-without-parameters",
            )
        )
    return diagnostics


def _continue_create_source_policy(
    policy: ActionPolicy,
    params: Mapping[str, Any],
    sources: list[str],
    _trusted_context: TrustedRosAccountContext | None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    mode = params.get("Mode") or "Recreate"
    if mode not in {"Recreate", "Ignore"}:
        diagnostics.append(
            _request_error(
                policy.action,
                _("ContinueCreateStack.Mode is invalid."),
                _("Mode must be Recreate or Ignore."),
                "invalid-mode",
                str(mode),
            )
        )
    if mode == "Recreate" and len(sources) > 1:
        diagnostics.append(
            _request_error(
                policy.action,
                _("ContinueCreateStack provides multiple template sources."),
                _("Recreate accepts at most one source."),
                str(len(sources)),
            )
        )
    return diagnostics


def _change_set_import_source(
    policy: ActionPolicy,
    params: Mapping[str, Any],
    sources: list[str],
    _trusted_context: TrustedRosAccountContext | None,
) -> list[Diagnostic]:
    change_set_type = str(params.get("ChangeSetType") or "").upper()
    scratch_is_valid = _S not in sources or (change_set_type == "IMPORT" and not _present(params, "StackId"))
    if len(sources) == 1 and scratch_is_valid:
        return []
    return [
        _request_error(
            policy.action,
            _("The CreateChangeSet template source does not match ChangeSetType."),
            _("Select one of Body, URL, or TemplateId; ScratchId is valid only for IMPORT of a new stack."),
            change_set_type,
            str(len(sources)),
        )
    ]


def _recommend_source_precedence(
    policy: ActionPolicy,
    _params: Mapping[str, Any],
    sources: list[str],
    _trusted_context: TrustedRosAccountContext | None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not sources or (_B in sources and _U in sources):
        diagnostics.append(
            _request_error(
                policy.action,
                _("GetTemplateRecommendParameters has an invalid template source."),
                _("Provide at least one source, and do not provide TemplateBody and TemplateURL together."),
                str(len(sources)),
            )
        )
    if _I in sources and (_B in sources or _U in sources):
        diagnostics.append(
            _request_warning(
                policy.action,
                _("GetTemplateRecommendParameters provides TemplateId together with a higher-priority source."),
                _("ROS prefers TemplateBody/TemplateURL, so TemplateId is ignored in this call."),
                "ignored-template-id",
            )
        )
    return diagnostics


def _operation_risk_sources_valid(operation_type: str, sources: list[str]) -> bool:
    if operation_type == "CreateStack":
        return len([item for item in sources if item != "StackId"]) == 1 and "StackId" not in sources
    if operation_type == "DeleteStack":
        return sources == ["StackId"]
    return False


def _operation_risk_source(
    policy: ActionPolicy,
    params: Mapping[str, Any],
    sources: list[str],
    _trusted_context: TrustedRosAccountContext | None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    operation_type = str(params.get("OperationType") or "DeleteStack")
    if operation_type not in {"CreateStack", "DeleteStack"}:
        diagnostics.append(
            _request_error(
                policy.action,
                _("ListStackOperationRisks.OperationType is invalid."),
                _("The locked SDK declares only CreateStack and DeleteStack."),
                operation_type,
            )
        )
    if not _operation_risk_sources_valid(operation_type, sources):
        diagnostics.append(
            _request_error(
                policy.action,
                _("The ListStackOperationRisks source does not match OperationType."),
                _("CreateStack requires one template source; DeleteStack accepts only StackId."),
                operation_type,
            )
        )
    return diagnostics


def _stack_group_stack_arn(
    policy: ActionPolicy,
    params: Mapping[str, Any],
    _sources: list[str],
    _trusted_context: TrustedRosAccountContext | None,
) -> list[Diagnostic]:
    if not _present(params, "StackArn"):
        return []
    diagnostics: list[Diagnostic] = []
    if _present(params, "Parameters"):
        diagnostics.append(
            _request_error(
                policy.action,
                _("CreateStackGroup cannot provide Parameters when StackArn is used."),
                _("StackArn references an existing stack template, so Parameters is ineffective in this branch."),
                "stack-arn-parameters",
            )
        )
    if params.get("PermissionModel") != "SELF_MANAGED":
        diagnostics.append(
            _request_error(
                policy.action,
                _("CreateStackGroup must use SELF_MANAGED when StackArn is used."),
                _("The SERVICE_MANAGED branch does not accept StackArn as a template source."),
                "stack-arn-permission-model",
            )
        )
    return diagnostics


def _module_registration_request(
    policy: ActionPolicy,
    params: Mapping[str, Any],
    _sources: list[str],
    trusted_context: TrustedRosAccountContext | None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    entity_type = params.get("EntityType")
    resource_type = params.get("ResourceType")
    if entity_type != "Module":
        diagnostics.append(
            _request_error(
                policy.action,
                _("Local template analysis for RegisterResourceType supports only EntityType=Module."),
                _("EntityType is not Module."),
                str(entity_type),
            )
        )
    if (
        not isinstance(resource_type, str)
        or not re.fullmatch(r"MODULE::[A-Za-z0-9]{2,}::[A-Za-z0-9]{2,}::[A-Za-z0-9]{2,}", resource_type)
        or not (18 <= len(resource_type) <= 100)
    ):
        diagnostics.append(
            _request_error(
                policy.action,
                _("RegisterResourceType.ResourceType has an invalid format."),
                _("It must contain 18 to 100 characters and match MODULE::Organization::Service::Type."),
                "resource-type",
            )
        )
    trusted_context_complete = trusted_context is not None and all(
        isinstance(value, str) and bool(value.strip())
        for value in (
            trusted_context.tenant_id,
            trusted_context.site_owner,
            trusted_context.production_account_id,
            trusted_context.provenance,
        )
    )
    if not trusted_context_complete:
        diagnostics.append(_account_limitation(policy.action))
        return diagnostics
    if not isinstance(resource_type, str) or trusted_context is None:
        return diagnostics
    parts = resource_type.split("::")
    if len(parts) == 4 and parts[1] == "SHARE" and parts[2] != trusted_context.tenant_id:
        diagnostics.append(
            _request_error(
                policy.action,
                _("The service segment of MODULE::SHARE does not match the current tenant."),
                _("The third segment of a SHARE Module must equal tenant_id in the trusted account context."),
                "share-tenant-mismatch",
            )
        )
    is_non_production_tenant = trusted_context.tenant_id != trusted_context.production_account_id
    if len(parts) != 4 or not is_non_production_tenant:
        return diagnostics
    organization = parts[1].lower()
    forbidden_reason: str | None = None
    if trusted_context.site_owner.lower() == "alicloud":
        forbidden_reason = next(
            (_("contains {}").format(token) for token in ("alicloud", "alibaba", "aliyun") if token in organization),
            None,
        )
    if forbidden_reason is None and organization.startswith("acs"):
        forbidden_reason = _("starts with acs")
    if forbidden_reason is None and organization in {"ros", "dev", "test", "debug"}:
        forbidden_reason = _("equals reserved name {}").format(organization)
    if forbidden_reason is not None:
        diagnostics.append(
            _request_error(
                policy.action,
                _("The Module organization uses a name reserved for non-production accounts."),
                _("organization {} in the current trusted account context.").format(forbidden_reason),
                "reserved-module-organization",
                organization,
            )
        )
    return diagnostics


def _template_body_present(_params: Mapping[str, Any], sources: list[str]) -> bool:
    return _B in sources


def _continue_recreate_body(params: Mapping[str, Any], sources: list[str]) -> bool:
    return (params.get("Mode") or "Recreate") == "Recreate" and _B in sources


def _operation_risk_create_body(params: Mapping[str, Any], sources: list[str]) -> bool:
    operation_type = str(params.get("OperationType") or "DeleteStack")
    return operation_type == "CreateStack" and _B in sources and _operation_risk_sources_valid(operation_type, sources)


def _module_entity_body(params: Mapping[str, Any], sources: list[str]) -> bool:
    return params.get("EntityType") == "Module" and _B in sources


_CONDITIONAL_PREDICATES: Mapping[str, _ConditionalPredicate] = MappingProxyType(
    {
        "preview_source_cardinality": _preview_source_cardinality,
        "continue_create_source_policy": _continue_create_source_policy,
        "change_set_import_source": _change_set_import_source,
        "recommend_source_precedence": _recommend_source_precedence,
        "operation_risk_source": _operation_risk_source,
        "stack_group_stack_arn": _stack_group_stack_arn,
        "module_registration_request": _module_registration_request,
    }
)
_ACTIVE_BODY_PREDICATES: Mapping[str, _ActiveBodyPredicate] = MappingProxyType(
    {
        "template_body_present": _template_body_present,
        "continue_recreate_body": _continue_recreate_body,
        "operation_risk_create_body": _operation_risk_create_body,
        "module_entity_body": _module_entity_body,
    }
)


def _validate_policy_registry() -> None:
    for policy in ACTION_POLICIES.values():
        if not policy.allowed_fields <= policy.metadata_fields:
            raise ValueError("ActionPolicy fields exceed locked metadata: {}".format(policy.action))
        missing = set(policy.conditional_predicates) - set(_CONDITIONAL_PREDICATES)
        if missing:
            raise ValueError("unknown ActionPolicy predicates for {}: {}".format(policy.action, sorted(missing)))
        if policy.active_body_predicate not in _ACTIVE_BODY_PREDICATES:
            raise ValueError("unknown active body predicate for {}".format(policy.action))


_validate_policy_registry()


def validate_action_request(
    action: str,
    params: Mapping[str, Any],
    *,
    trusted_ros_account_context: TrustedRosAccountContext | None = None,
) -> tuple[ActionPolicy | None, list[Diagnostic], bool]:
    """Return policy, request diagnostics and whether TemplateBody is active."""

    policy = ACTION_POLICIES.get(action)
    if policy is None:
        return None, [], False
    diagnostics: list[Diagnostic] = []
    sources = _count_sources(policy, params)

    for required_field in sorted(policy.required_fields):
        if not _present(params, required_field):
            diagnostics.append(
                _request_error(
                    action,
                    _("{} is missing required field {}.").format(action, required_field),
                    _("This operation-target field is not part of the mutually exclusive template-source group."),
                    "missing-required-field",
                    required_field,
                )
            )

    if policy.cardinality == Cardinality.EXACTLY_ONE and len(sources) != 1:
        diagnostics.append(
            _request_error(
                action,
                _("{} has an invalid number of template sources.").format(action),
                _("Provide exactly one of these sources: {}; {} were provided.").format(
                    ", ".join(sorted(policy.allowed_fields)), len(sources)
                ),
                str(len(sources)),
                suggestion=_(
                    "Take exactly one source from the page context: TemplateBody/TemplateURL, TemplateId (not the "
                    "displayed template name), or TemplateScratchId."
                ),
            )
        )
    elif policy.cardinality == Cardinality.ZERO_OR_ONE and len(sources) > 1:
        diagnostics.append(
            _request_error(
                action,
                _("{} provides multiple template sources.").format(action),
                _("This operation accepts at most one new template source."),
                str(len(sources)),
            )
        )
    for predicate_id in policy.conditional_predicates:
        diagnostics.extend(_CONDITIONAL_PREDICATES[predicate_id](policy, params, sources, trusted_ros_account_context))
    active_body = _ACTIVE_BODY_PREDICATES[policy.active_body_predicate](params, sources)
    return policy, diagnostics, active_body
