"""Deterministic resource type consistency checks for generated ROS templates.

The template generation step previously relied on prompt wording alone to keep
generated resource types aligned with the candidate's ``resource_intents``.
This module turns that contract into an offline, code-level check so a template
whose resource types contradict the candidate cannot complete the step.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository
from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load

_CREATE_ACTIONS = frozenset({"create"})
_EXISTING_ACTIONS = frozenset({"use_existing", "reference"})
_FORBID_ACTIONS = frozenset({"forbid"})


@dataclass(frozen=True)
class ResourceTypeIssue:
    """One resource type inconsistency found in a generated template."""

    code: str
    resource_name: str = ""
    resource_type: str = ""
    product: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


@lru_cache(maxsize=1)
def known_resource_types() -> frozenset[str]:
    """Return the offline authoritative ROS resource type catalog.

    The catalog merges the official resource index used by the local ROS
    validator with the vendored architecture metadata, so a hallucinated type
    such as ``ALIYUN::VPC::VPC`` (the real type is ``ALIYUN::ECS::VPC``) is
    recognized as unknown without any network or cloud account access.
    """

    types: set[str] = set()
    index_path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_official_resource_index.json")
    raw_index = json.loads(index_path.read_text(encoding="utf-8"))
    resources = raw_index.get("resources")
    if isinstance(resources, dict):
        types.update(key for key in resources if isinstance(key, str) and key)

    config_path = Path(__file__).with_name("architecture_metas") / "config.json"
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    if isinstance(raw_config, list):
        for item in raw_config:
            if not isinstance(item, dict):
                continue
            resource_type = (item.get("ResourceType") or {}).get("ROS")
            if isinstance(resource_type, str) and resource_type.startswith("ALIYUN::"):
                types.add(resource_type)
    return frozenset(types)


def _normalize(value: Any) -> str:
    return str(value or "").strip().casefold()


def _product_aliases(resource_type: str, repository: ArchitectureMetaRepository) -> frozenset[str]:
    """Return every accepted spelling of the product owning ``resource_type``.

    ``resource_intents[].product`` is model-authored free text, so the same
    product legitimately appears as a ROS namespace segment (``ECS``), a
    product code (``vpc``), a product display name, or the full resource type.
    Matching on all of them keeps the check strict about real mismatches
    without failing a template over spelling differences.
    """

    aliases: set[str] = {_normalize(resource_type)}
    segments = resource_type.split("::")
    if len(segments) >= 2:
        aliases.add(_normalize(segments[1]))
    if len(segments) >= 3:
        aliases.add(_normalize(segments[-1]))
        aliases.add(_normalize(f"{segments[1]}::{segments[-1]}"))

    meta = repository.get_resource(resource_type)
    if meta is not None:
        for value in (meta.product_code, meta.name_en, meta.name_zh):
            if value:
                aliases.add(_normalize(value))
        if meta.product_code:
            product = repository.get_product(meta.product_code)
            if product is not None:
                for value in (product.ros_code, product.name_en, product.name_zh):
                    if value:
                        aliases.add(_normalize(value))
    aliases.discard("")
    return frozenset(aliases)


def _parse_template_resources(template: Any) -> tuple[dict[str, str], ResourceTypeIssue | None]:
    """Extract ``{logical_name: resource_type}`` from a generated template."""

    if not isinstance(template, str) or not template.strip():
        return {}, ResourceTypeIssue("invalid_template_structure", detail="template must be a non-empty string")
    try:
        document = ros_yaml_load(template)
    except yaml.YAMLError as error:
        return {}, ResourceTypeIssue("invalid_template_structure", detail=type(error).__name__)
    if not isinstance(document, dict):
        return {}, ResourceTypeIssue("invalid_template_structure", detail="template root must be a mapping")

    raw_resources = document.get("Resources")
    if raw_resources is None:
        return {}, ResourceTypeIssue("invalid_template_structure", detail="template must define Resources")
    if not isinstance(raw_resources, dict):
        return {}, ResourceTypeIssue("invalid_template_structure", detail="Resources must be a mapping")

    resources: dict[str, str] = {}
    for name, body in raw_resources.items():
        if not isinstance(name, str) or not name:
            return {}, ResourceTypeIssue("invalid_template_structure", detail="resource names must be strings")
        if not isinstance(body, dict):
            return {}, ResourceTypeIssue("invalid_template_structure", resource_name=name, detail="must be a mapping")
        resource_type = body.get("Type")
        if not isinstance(resource_type, str) or not resource_type:
            return {}, ResourceTypeIssue("invalid_template_structure", resource_name=name, detail="missing Type")
        resources[name] = resource_type
    return resources, None


def _collect_intents(resource_intents: Any) -> tuple[dict[str, set[str]], list[ResourceTypeIssue]]:
    """Group declared products by action, keeping the original spelling."""

    issues: list[ResourceTypeIssue] = []
    by_action: dict[str, set[str]] = {}
    if not isinstance(resource_intents, list):
        return by_action, issues
    for intent in resource_intents:
        if not isinstance(intent, dict):
            issues.append(ResourceTypeIssue("invalid_resource_intent", detail="resource intent must be a mapping"))
            continue
        product = intent.get("product")
        action = intent.get("action")
        if not isinstance(product, str) or not product:
            issues.append(ResourceTypeIssue("invalid_resource_intent", detail="missing product"))
            continue
        if not isinstance(action, str) or not action:
            issues.append(ResourceTypeIssue("invalid_resource_intent", product=product, detail="missing action"))
            continue
        by_action.setdefault(_normalize(action), set()).add(product)
    return by_action, issues


def validate_template_resource_types(
    template: Any,
    resource_intents: Any,
    *,
    repository: ArchitectureMetaRepository | None = None,
    resource_type_catalog: frozenset[str] | None = None,
) -> list[ResourceTypeIssue]:
    """Validate generated resource types against the candidate resource intents.

    Templates without ``resource_intents`` are only checked for resource types
    that actually exist, so candidates that never declared lifecycle semantics
    keep their previous behavior.
    """

    repository = repository or ArchitectureMetaRepository.load_default()
    catalog = resource_type_catalog if resource_type_catalog is not None else known_resource_types()

    resources, parse_issue = _parse_template_resources(template)
    if parse_issue is not None:
        return [parse_issue]

    intents_by_action, issues = _collect_intents(resource_intents)

    aliases_by_resource: dict[str, frozenset[str]] = {}
    for name, resource_type in resources.items():
        if resource_type not in catalog:
            issues.append(
                ResourceTypeIssue(
                    "unknown_resource_type",
                    resource_name=name,
                    resource_type=resource_type,
                    detail="not a known ROS resource type",
                )
            )
            continue
        aliases_by_resource[name] = _product_aliases(resource_type, repository)

    declared: set[str] = set()
    for products in intents_by_action.values():
        declared.update(products)
    if not declared:
        return issues

    def matches(product: str) -> list[str]:
        normalized = _normalize(product)
        return [name for name, aliases in aliases_by_resource.items() if normalized in aliases]

    for action, products in intents_by_action.items():
        for product in sorted(products):
            matched = matches(product)
            if action in _CREATE_ACTIONS and not matched:
                issues.append(
                    ResourceTypeIssue(
                        "missing_required_resource",
                        product=product,
                        detail="resource_intents requires creating this product",
                    )
                )
            elif action in _FORBID_ACTIONS:
                for name in matched:
                    issues.append(
                        ResourceTypeIssue(
                            "forbidden_resource_created",
                            resource_name=name,
                            resource_type=resources[name],
                            product=product,
                            detail="resource_intents forbids this product",
                        )
                    )
            elif action in _EXISTING_ACTIONS:
                for name in matched:
                    issues.append(
                        ResourceTypeIssue(
                            "use_existing_resource_created",
                            resource_name=name,
                            resource_type=resources[name],
                            product=product,
                            detail="must be referenced through a Parameter instead of created",
                        )
                    )

    forbidden_aliases: set[str] = set()
    for action, products in intents_by_action.items():
        if action in _FORBID_ACTIONS:
            forbidden_aliases.update(_normalize(product) for product in products)

    declared_aliases = {_normalize(product) for product in declared}
    for name, aliases in aliases_by_resource.items():
        if aliases & declared_aliases:
            continue
        if aliases & forbidden_aliases:
            continue
        issues.append(
            ResourceTypeIssue(
                "extra_resource_product",
                resource_name=name,
                resource_type=resources[name],
                detail="product is not declared in resource_intents",
            )
        )
    return issues


def format_resource_type_issues(issues: list[ResourceTypeIssue]) -> str:
    """Render issues as a compact, model-actionable summary."""

    summaries: list[str] = []
    for issue in issues:
        specifics = ", ".join(
            value for value in (issue.resource_name, issue.resource_type, issue.product, issue.detail) if value
        )
        summaries.append(f"{issue.code}[{specifics}]" if specifics else issue.code)
    return "; ".join(summaries)
