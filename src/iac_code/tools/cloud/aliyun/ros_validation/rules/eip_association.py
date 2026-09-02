"""Cross-resource checks for EIP associations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.facts import RulePhase
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    Severity,
    make_diagnostic,
    mapping_segment,
)

_PARSED_TEMPLATE = "parsed-template"
_EIP_ASSOCIATION_TYPES = frozenset({"ALIYUN::VPC::EIPAssociation"})
_ECS_TYPES = frozenset({"ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"})


def _path(*parts: Any) -> tuple:
    return tuple(mapping_segment(part) for part in parts)


def _resource_references(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, Mapping):
        ref = value.get("Ref")
        if isinstance(ref, str):
            references.add(ref)

        getatt = value.get("Fn::GetAtt")
        if isinstance(getatt, list) and getatt and isinstance(getatt[0], str):
            references.add(getatt[0])
        elif isinstance(getatt, str):
            references.add(getatt.split(".", 1)[0])

        for child in value.values():
            references.update(_resource_references(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_resource_references(child))
    return references


def _is_explicit_false(value: Any) -> bool:
    return value is False


@dataclass(frozen=True)
class EipAssociationCheck:
    """Check the public-IP invariant for one EIP-to-ECS relationship."""

    def check(self, context: Any) -> tuple[Diagnostic, ...]:
        parsed = context.fact_store.get_required(_PARSED_TEMPLATE)
        template = parsed.data
        if not isinstance(template, Mapping):
            return ()
        resources = template.get("Resources")
        if not isinstance(resources, Mapping):
            return ()

        ecs_resources = {
            name: definition
            for name, definition in resources.items()
            if isinstance(name, str)
            and isinstance(definition, Mapping)
            and definition.get("Type") in _ECS_TYPES
        }
        diagnostics: list[Diagnostic] = []
        for association_name, association in resources.items():
            if not isinstance(association_name, str) or not isinstance(association, Mapping):
                continue
            if association.get("Type") not in _EIP_ASSOCIATION_TYPES:
                continue
            properties = association.get("Properties")
            if not isinstance(properties, Mapping):
                continue
            target_names = _resource_references(properties.get("InstanceId"))
            for target_name in sorted(target_names & set(ecs_resources)):
                target = ecs_resources[target_name]
                target_properties = target.get("Properties")
                allocate_public_ip = (
                    target_properties.get("AllocatePublicIP")
                    if isinstance(target_properties, Mapping)
                    else None
                )
                if _is_explicit_false(allocate_public_ip):
                    continue
                diagnostics.append(
                    make_diagnostic(
                        code="ROS5103",
                        severity=Severity.ERROR,
                        category=Category.QUALITY,
                        summary=_(
                            "ECS resource {} is associated with an EIP but does not explicitly disable "
                            "public IP allocation."
                        ).format(target_name),
                        detail=_(
                            "EIPAssociation {} provides the public entry point; the associated ECS resource "
                            "must set AllocatePublicIP to false."
                        ).format(association_name),
                        path=_path("Resources", target_name, "Properties", "AllocatePublicIP"),
                        source_map=parsed.source_map,
                        subject=target_name,
                        stable_args=(association_name, target_name, type(allocate_public_ip).__name__),
                        expected="false",
                        actual="missing" if allocate_public_ip is None else str(allocate_public_ip),
                        suggestion=_("Set AllocatePublicIP: false on the associated ECS resource."),
                    )
                )
        return tuple(diagnostics)


@dataclass(frozen=True)
class ResourceRelationshipRule:
    """Run checks that validate relationships between ROS resources."""

    rule_id: str = "builtin.resource-relationships"
    phase: RulePhase = RulePhase.STRUCTURE
    requires: frozenset[str] = frozenset({_PARSED_TEMPLATE})
    optional_requires: frozenset[str] = frozenset()
    checks: tuple[EipAssociationCheck, ...] = (EipAssociationCheck(),)

    def check(self, context: Any) -> tuple[Diagnostic, ...]:
        diagnostics: list[Diagnostic] = []
        for relationship_check in self.checks:
            diagnostics.extend(relationship_check.check(context))
        return tuple(diagnostics)
