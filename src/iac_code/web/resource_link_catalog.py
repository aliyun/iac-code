"""Versioned ROS production resource-console link catalog.

The checked-in JSON contains only resource type keys and URL templates copied
from the public shape of the authenticated ROS console configuration. It never
contains account data, cookies, credentials, or already-rendered user URLs.
Reviewed provider aliases reuse canonical templates after snapshot validation;
the catalog hashes continue to identify the original production snapshot.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

_CATALOG_PATH = Path(__file__).with_name("static") / "ros_resource_links.json"
_RESOURCE_TYPE = re.compile(r"(?:ALIYUN::[A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)+|alicloud_[a-z0-9_]+)\Z")
_PLACEHOLDER = re.compile(r"@?\{([A-Za-z0-9_-]+)\}")
_BASE_FIELDS = frozenset({"PhysicalId", "RegionId", "ResourceName"})


@dataclass(frozen=True)
class ResourceLinkCatalog:
    checked_at: str
    templates: Mapping[str, Mapping[str, str]]
    attributes: Mapping[str, tuple[str, ...]]
    hashes: Mapping[str, str]

    @property
    def resource_types(self) -> frozenset[str]:
        return frozenset(self.templates["aliyun"])


def _nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("invalid ROS resource link catalog: {}".format(name))
    return value


def _expand_catalog(data: Any) -> ResourceLinkCatalog:
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise RuntimeError("invalid ROS resource link catalog schema")
    official_data, sin_data = data.get("official"), data.get("sin")
    if not isinstance(official_data, dict) or not isinstance(sin_data, dict):
        raise RuntimeError("invalid ROS resource link catalog partitions")
    official: dict[str, str] = {}
    for group in official_data.get("groups", []):
        if not isinstance(group, list) or len(group) != 2 or not isinstance(group[1], list):
            raise RuntimeError("invalid ROS resource link catalog group")
        template = _nonempty_text(group[0], "template")
        for resource_type in group[1]:
            resource_type = _nonempty_text(resource_type, "resource type")
            if (
                not _RESOURCE_TYPE.fullmatch(resource_type) and resource_type != "ALIYUN::DRDS::DrdsInstance "
            ) or resource_type in official:
                raise RuntimeError("invalid ROS resource link catalog resource type")
            official[resource_type] = template
    if len(official) != official_data.get("resourceCount"):
        raise RuntimeError("invalid ROS resource link catalog OFFICIAL count")
    substitution = sin_data.get("hostSubstitution")
    if (
        not isinstance(substitution, list)
        or len(substitution) != 2
        or not all(isinstance(value, str) and value for value in substitution)
    ):
        raise RuntimeError("invalid ROS resource link catalog SIN substitution")
    sin = {key: template.replace(substitution[0], substitution[1]) for key, template in official.items()}
    seen_overrides: set[str] = set()
    for override in sin_data.get("overrides", []):
        if not isinstance(override, list) or len(override) != 2:
            raise RuntimeError("invalid ROS resource link catalog SIN override")
        resource_type, template = override
        if resource_type not in official or resource_type in seen_overrides:
            raise RuntimeError("invalid ROS resource link catalog SIN resource type")
        seen_overrides.add(resource_type)
        sin[resource_type] = _nonempty_text(template, "SIN template")
    if len(sin) != sin_data.get("resourceCount") or set(sin) != set(official):
        raise RuntimeError("invalid ROS resource link catalog SIN count")
    for alias, canonical in (("alicloud_slb_acl", "ALIYUN::SLB::AccessControl"),):
        if canonical in official and alias not in official:
            official[alias] = official[canonical]
            sin[alias] = sin[canonical]
    attributes = {
        resource_type: tuple(
            sorted(
                {
                    match.group(1).split("-", 1)[0]
                    for template in (official[resource_type], sin[resource_type])
                    for match in _PLACEHOLDER.finditer(template)
                    if match.group(1).split("-", 1)[0] not in _BASE_FIELDS
                }
            )
        )
        for resource_type in official
    }
    return ResourceLinkCatalog(
        checked_at=_nonempty_text(data.get("checkedAt"), "checkedAt"),
        templates=MappingProxyType(
            {
                "aliyun": MappingProxyType(official),
                "alibabacloud": MappingProxyType(sin),
            }
        ),
        attributes=MappingProxyType(attributes),
        hashes=MappingProxyType(
            {
                "aliyun": _nonempty_text(official_data.get("sha256"), "OFFICIAL sha256"),
                "alibabacloud": _nonempty_text(sin_data.get("sha256"), "SIN sha256"),
            }
        ),
    )


@lru_cache(maxsize=1)
def resource_link_catalog() -> ResourceLinkCatalog:
    return _expand_catalog(json.loads(_CATALOG_PATH.read_text(encoding="utf-8")))


def resource_link_catalog_payload() -> dict[str, Any]:
    """Return the minimal validated payload injected before frontend modules."""
    catalog = resource_link_catalog()
    # Import after the link catalog has been materialized: the resource-spec
    # fallback refers back to this module for legacy types without a reviewed
    # identity.  Shipping the same specs to the browser keeps direct Describe
    # responses and ROS responses on one identity contract.
    from iac_code.tools.cloud.resource_references import resource_spec

    groups: dict[str, list[str]] = {}
    for resource_type, template in catalog.templates["aliyun"].items():
        groups.setdefault(template, []).append(resource_type)
    source, target = ".console.aliyun.com", ".console.alibabacloud.com"
    overrides = {
        resource_type: template
        for resource_type, template in catalog.templates["alibabacloud"].items()
        if template != catalog.templates["aliyun"][resource_type].replace(source, target)
    }
    identities: dict[str, dict[str, Any]] = {}
    for source_type in sorted(catalog.resource_types):
        spec = resource_spec(source_type)
        if spec is None:
            continue
        item = identities.setdefault(
            spec.resource_type,
            {
                "resourceType": spec.resource_type,
                "identifierField": spec.identifier_field,
                "identifierKind": spec.identifier_kind,
                "scopeKind": spec.scope_kind,
                "aliases": set(),
                "parentFields": list(spec.parent_fields),
                "attributeFields": list(spec.attribute_fields),
                "stableField": spec.stable_field or None,
            },
        )
        item["aliases"].update(alias for alias in (*spec.aliases, source_type) if alias in catalog.resource_types)
        item["aliases"].discard(spec.resource_type)
    # The browser uses the same reviewed response contracts as the server
    # extractor to scope generic fields (for example ``Name``) to the product
    # and Action that produced them.  Without this small map, two parallel API
    # cards can make a same-named field reuse a different product's link.
    from iac_code.tools.cloud.aliyun.resource_reference_extractors import direct_api_response_contracts

    direct_api_contracts = [
        {"product": item["product"], "action": item["action"], "resourceType": item["resource_type"]}
        for item in direct_api_response_contracts()
    ]
    return {
        "schemaVersion": 1,
        "checkedAt": catalog.checked_at,
        "hashes": dict(catalog.hashes),
        "officialGroups": [[template, resource_types] for template, resource_types in groups.items()],
        "sinHostSubstitution": [source, target],
        "sinOverrides": overrides,
        "identities": [
            {**item, "aliases": sorted(item["aliases"])}
            for item in sorted(identities.values(), key=lambda value: value["resourceType"])
        ],
        "directApiContracts": direct_api_contracts,
    }
