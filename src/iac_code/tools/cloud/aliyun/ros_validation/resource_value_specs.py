"""Versioned Ref/GetAtt value catalog for ROS resources and data sources."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Mapping

from iac_code.tools.cloud.aliyun.ros_validation.types import (
    ANY_VALUE,
    INTEGER,
    NULL,
    STRING,
    RosType,
    TypeKind,
    list_of,
    map_of,
    union_of,
)


@dataclass(frozen=True)
class ResourceValueSpec:
    resource_type: str
    ref_type: RosType
    attribute_types: Mapping[str, RosType] = field(default_factory=dict)
    attributes: frozenset[str] = frozenset()
    attributes_complete: bool = False
    official_evidence: tuple[Mapping[str, Any] | str, ...] = ()
    known_differences: tuple[str, ...] = ()


_RESOURCE_REF_OVERRIDES: Mapping[str, RosType] = MappingProxyType(
    {
        "ALIYUN::RandomString": union_of(STRING, NULL),
        "ALIYUN::ROS::Stack": union_of(STRING, NULL),
        "ALIYUN::ECS::PrepayInstance": union_of(list_of(STRING), NULL),
        "ALIYUN::RDS::PrepayDBInstance": union_of(list_of(STRING), NULL),
    }
)

_RAW_CONTENT_PROPERTIES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # Property names alone never imply raw-content semantics.
        "ALIYUN::ROS::Stack": frozenset({"TemplateBody"}),
        "ALIYUN::ROS::StackGroup": frozenset({"TemplateBody"}),
    }
)


def _parse_type(value: str) -> RosType:
    value = value.strip()
    nullable = value.endswith(" | Null")
    if nullable:
        value = value[: -len(" | Null")]
    if value == "String":
        result = STRING
    elif value == "Integer":
        result = INTEGER
    elif value == "Null":
        result = NULL
    elif value == "AnyValue":
        result = ANY_VALUE
    elif value == "Map":
        result = map_of(STRING, ANY_VALUE)
    elif value.startswith("List[") and value.endswith("]"):
        inner = value[5:-1]
        result = list_of(_parse_type(inner))
    else:
        result = ANY_VALUE
    return union_of(result, NULL) if nullable and result.kind != TypeKind.NULL else result


def _verify_catalog_checksum(payload: Mapping[str, Any]) -> None:
    expected_top_level_fields = {
        "content_sha256",
        "datasource_ref_type_counts",
        "official_evidence_snapshot",
        "resources",
        "schema_version",
    }
    if payload.get("schema_version") != 4 or set(payload) != expected_top_level_fields:
        raise RuntimeError("ROS resource value catalog has an invalid schema")
    expected = payload.get("content_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise RuntimeError("ROS resource value catalog has no valid content_sha256")
    content = dict(payload)
    del content["content_sha256"]
    canonical = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    actual = hashlib.sha256(canonical).hexdigest()
    if actual != expected:
        raise RuntimeError("ROS resource value catalog content_sha256 mismatch")
    required_official_fields = {
        "url",
        "resource_page_url",
        "locale",
        "status",
        "content_sha256",
        "extractor_version",
        "retrieved_at",
        "documented_type",
        "snapshot_kind",
    }
    expected_resource_fields = {
        "attribute_types",
        "attributes",
        "attributes_complete",
        "known_differences",
        "official_evidence",
        "ref_type",
        "resource_type",
    }
    for item in payload.get("resources", ()):
        if not isinstance(item, Mapping) or set(item) != expected_resource_fields:
            raise RuntimeError("ROS resource value catalog has an invalid resource schema")
        if not isinstance(item.get("attributes_complete"), bool):
            raise RuntimeError("ROS resource value catalog has an invalid Attribute completeness contract")
        official = item.get("official_evidence")
        if not isinstance(official, list) or not official or not isinstance(official[0], Mapping):
            raise RuntimeError("ROS resource value catalog has no official evidence")
        evidence = official[0]
        if not required_official_fields <= set(evidence) or evidence.get("status") not in {"FOUND", "NOT_FOUND"}:
            raise RuntimeError("ROS resource value catalog has incomplete official evidence")
        evidence_hash = evidence.get("content_sha256")
        if not isinstance(evidence_hash, str) or len(evidence_hash) != 64:
            raise RuntimeError("ROS resource value catalog has an invalid official evidence hash")
        snapshot = payload.get("official_evidence_snapshot")
        if not isinstance(snapshot, Mapping):
            raise RuntimeError("ROS resource value catalog has no official evidence snapshot provenance")
        if evidence.get("snapshot_kind") == "official-resource-detail":
            details = snapshot.get("resource_details")
            detail = details.get(item.get("resource_type")) if isinstance(details, Mapping) else None
            required_detail_fields = {
                "url",
                "locale",
                "content_sha256",
                "extractor_version",
                "retrieved_at",
                "documented_type",
                "normalization",
            }
            detail_matches = isinstance(detail, Mapping) and required_detail_fields <= set(detail)
            if detail_matches:
                detail_matches = all(evidence.get(key) == detail.get(key) for key in required_detail_fields)
                detail_matches = detail_matches and evidence.get("resource_page_url") == detail.get("url")
                detail_matches = detail_matches and evidence.get("observations") == detail.get("observations")
            if not detail_matches:
                raise RuntimeError("ROS resource value catalog official detail evidence does not match its snapshot")
        elif evidence.get("snapshot_kind") == "official-resource-type-index":
            if evidence.get("status") == "FOUND":
                raise RuntimeError("ROS resource value catalog FOUND evidence is not detail-page evidence")
            if evidence.get("url") != snapshot.get("source_url") or evidence_hash != snapshot.get("content_sha256"):
                raise RuntimeError("ROS resource value catalog official evidence does not match its source snapshot")
        else:
            raise RuntimeError("ROS resource value catalog has an unknown official evidence snapshot kind")
        if evidence.get("status") == "FOUND":
            if evidence.get("documented_type") != item.get("resource_type") or not isinstance(
                evidence.get("resource_page_url"), str
            ):
                raise RuntimeError("ROS resource value catalog FOUND evidence is not type-specific")
        elif evidence.get("documented_type") is not None or evidence.get("resource_page_url") is not None:
            raise RuntimeError("ROS resource value catalog NOT_FOUND evidence contains an undocumented type")


class ResourceValueSpecRegistry:
    def __init__(self, specs: Mapping[str, ResourceValueSpec] | None = None) -> None:
        self._specs = dict(specs or _load_specs())
        self._documented_resource_types: frozenset[str] | None = None

    def get(self, resource_type: str) -> ResourceValueSpec | None:
        return self._specs.get(resource_type)

    def ref_type(self, resource_type: str) -> RosType:
        spec = self.get(resource_type)
        if spec is not None:
            return spec.ref_type
        if resource_type.startswith("DATASOURCE::"):
            return union_of(ANY_VALUE, NULL)
        return _RESOURCE_REF_OVERRIDES.get(resource_type, STRING)

    def attribute_type(self, resource_type: str, attribute: str) -> RosType:
        spec = self.get(resource_type)
        if spec is None:
            return ANY_VALUE
        return spec.attribute_types.get(attribute, ANY_VALUE)

    def attribute_exists(self, resource_type: str, attribute: str) -> bool | None:
        spec = self.get(resource_type)
        if spec is None or not spec.attributes_complete:
            return None
        return attribute in spec.attributes

    def documented_resource_types(self) -> frozenset[str]:
        """Return the resource types backed by a FOUND official documentation page.

        Types whose evidence is ``NOT_FOUND`` are excluded: the catalog cannot
        prove they are invalid, so they must not be reported as undocumented.
        """
        if self._documented_resource_types is None:
            self._documented_resource_types = frozenset(
                spec.resource_type for spec in self._specs.values() if _has_found_evidence(spec)
            )
        return self._documented_resource_types

    def is_raw_content_property(self, resource_type: str, property_name: Any) -> bool:
        return isinstance(property_name, str) and property_name in _RAW_CONTENT_PROPERTIES.get(
            resource_type, frozenset()
        )

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def specs(self) -> Mapping[str, ResourceValueSpec]:
        return MappingProxyType(self._specs)


def _has_found_evidence(spec: ResourceValueSpec) -> bool:
    for evidence in spec.official_evidence:
        if not isinstance(evidence, str) and evidence.get("status") == "FOUND":
            return True
    return False


def _load_specs() -> dict[str, ResourceValueSpec]:
    specs: dict[str, ResourceValueSpec] = {}
    data_path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_resource_value_specs.json")
    if data_path.is_file():
        payload = json.loads(data_path.read_text(encoding="utf-8"))
        _verify_catalog_checksum(payload)
        for item in payload.get("resources", []):
            attributes = {name: _parse_type(type_name) for name, type_name in item.get("attribute_types", {}).items()}
            specs[item["resource_type"]] = ResourceValueSpec(
                resource_type=item["resource_type"],
                ref_type=_parse_type(item["ref_type"]),
                attribute_types=MappingProxyType(attributes),
                attributes=frozenset(item.get("attributes", ())),
                attributes_complete=bool(item.get("attributes_complete", False)),
                official_evidence=tuple(item.get("official_evidence", ())),
                known_differences=tuple(item.get("known_differences", ())),
            )
    for resource_type, ref_type in _RESOURCE_REF_OVERRIDES.items():
        existing = specs.get(resource_type)
        specs[resource_type] = ResourceValueSpec(
            resource_type,
            ref_type,
            attribute_types=existing.attribute_types if existing is not None else MappingProxyType({}),
            attributes=existing.attributes if existing is not None else frozenset(),
            attributes_complete=existing.attributes_complete if existing is not None else False,
            official_evidence=existing.official_evidence if existing is not None else (),
            known_differences=existing.known_differences if existing is not None else (),
        )
    return specs


DEFAULT_RESOURCE_SPECS = ResourceValueSpecRegistry()
