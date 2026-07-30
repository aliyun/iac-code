"""Load and verify the vendored AssociationProperty contract."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Mapping

ASSOCIATION_PROPERTY_SPECS = "association-property-specs"
CONTRACT_VERSION = 3
CONTRACT_CORRECTIONS = (
    {
        "target": "common_metadata.schema.properties.ValueLabelMapping",
        "reason": "Value labels support either plain strings or localized string maps.",
    },
    {
        "target": "components.BailianApiKey.metadata.properties.OnlyKey",
        "reason": "OnlyKey is a boolean form-mode switch.",
    },
)
_VALUE_LABEL_MAPPING_SCHEMA = {
    "additionalProperties": {
        "anyOf": [
            {"type": "string"},
            {"additionalProperties": {"type": "string"}, "type": "object"},
        ]
    },
    "type": "object",
}
SUPPORTED_SCHEMA_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "anyOf",
        "const",
        "enum",
        "items",
        "minLength",
        "minimum",
        "patternProperties",
        "properties",
        "required",
        "type",
        "x-ore-aliases",
        "x-ore-consumer-set",
        "x-ore-injected-symbols",
        "x-ore-nested-parameter",
        "x-ore-parser",
        "x-ore-precedence",
        "x-ore-reference-context",
        "x-ore-reference-kinds",
        "x-ore-unresolved-reference",
        "x-ore-value-suggestions",
    }
)
SUPPORTED_PARSERS = frozenset(
    {
        "condition-ast",
        "literal-only",
        "lodash-template-interpolation",
        "mapping-selector-segments",
        "whole-value-reference",
    }
)
SUPPORTED_REFERENCE_CONTEXTS = frozenset(
    {"meta-list-row", "nested-parameter-map", "runtime-dependent", "template-root"}
)
SUPPORTED_REFERENCE_KINDS = frozenset({"env", "field-path", "parameter"})
SUPPORTED_SEMANTIC_RULES = frozenset({"auto_complete_character_capacity"})
SUPPORTED_WHEN = frozenset({"current-value-falsy-at-effect", "effective-default-undefined-after-normalization"})
VALUE_FLOW_SCHEMA = {
    "base_default": "initial-value-nullish-coalescing-parameter-default",
    "component_guard": "js-falsy-current-value",
    "default_gate": "ros-ref-value-skips-normalization",
    "host_merge": "runtime-dependent",
    "metadata_effects": ["Value", "DynamicValue"],
    "normalization": "template-form-default-normalization",
}
EXPECTED_AUTO_COMPLETE_CONSUMER_SETS = {
    "auto_complete_generation": {
        "consumers": [
            {
                "id": "template-default-initializer",
                "parser": "literal-only",
                "reference_context": "template-root",
                "when": "effective-default-undefined-after-normalization",
            },
            {
                "id": "associated-component",
                "parser": "whole-value-reference",
                "reference_context": "template-root",
                "reference_kinds": ["parameter", "field-path", "env"],
                "when": "current-value-falsy-at-effect",
            },
        ],
        "resolution": "inconsistent",
        "value_flow": VALUE_FLOW_SCHEMA,
    }
}


@dataclass(frozen=True)
class AssociationPropertySpec:
    key: str
    component: str
    deprecated: bool
    replacement: str | None
    scope: str


@dataclass(frozen=True)
class ExcludedAssociationPropertySpec:
    key: str
    scope: str


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    coverage: str
    metadata: Mapping[str, Any]
    semantic_rules: tuple[str, ...]


@dataclass(frozen=True)
class AssociationPropertySpecRegistry:
    contract_version: int
    product: str
    profile: Mapping[str, Any]
    corrections: tuple[Mapping[str, str], ...]
    common_coverage: str
    common_metadata: Mapping[str, Any]
    common_semantic_rules: tuple[str, ...]
    consumer_sets: Mapping[str, Any]
    association_properties: Mapping[str, AssociationPropertySpec]
    components: Mapping[str, ComponentSpec]
    excluded_association_properties: Mapping[str, ExcludedAssociationPropertySpec]
    raw_contract: Mapping[str, Any]

    def association(self, key: str) -> AssociationPropertySpec | None:
        return self.association_properties.get(key)

    def excluded(self, key: str) -> ExcludedAssociationPropertySpec | None:
        return self.excluded_association_properties.get(key)

    def component(self, name: str) -> ComponentSpec | None:
        return self.components.get(name)


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("AssociationProperty contract contains duplicate key {}".format(key))
        result[key] = value
    return result


def parse_contract_text(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicate_object)
    except (TypeError, ValueError) as error:
        raise RuntimeError("AssociationProperty contract is not valid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("AssociationProperty contract root must be an object")
    return payload


def _canonical_length(value: int) -> bytes:
    return "{}:".format(value).encode("ascii")


def _utf16_sort_key(value: str) -> tuple[int, ...]:
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    return tuple(int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2))


def _canonical_value_bytes(value: Any) -> bytes:
    if value is None:
        return b"N"
    if value is False:
        return b"F"
    if value is True:
        return b"T"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except (OverflowError, ValueError) as error:
            raise RuntimeError("AssociationProperty contract contains an invalid number") from error
        if not math.isfinite(number):
            raise RuntimeError("AssociationProperty contract contains a non-finite number")
        # JSON.stringify serializes negative zero as zero, so normalize it before
        # framing the IEEE-754 value used by both the exporter and loader.
        if number == 0:
            number = 0.0
        return b"D" + struct.pack(">d", number)
    if isinstance(value, str):
        encoded = value.encode("utf-16-le", errors="surrogatepass")
        return b"S" + _canonical_length(len(encoded)) + encoded
    if isinstance(value, (list, tuple)):
        return b"A" + _canonical_length(len(value)) + b"".join(_canonical_value_bytes(item) for item in value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise RuntimeError("AssociationProperty contract contains a non-String object key")
        keys = sorted((key for key in value if isinstance(key, str)), key=_utf16_sort_key)
        return (
            b"O"
            + _canonical_length(len(keys))
            + b"".join(_canonical_value_bytes(key) + _canonical_value_bytes(value[key]) for key in keys)
        )
    raise RuntimeError("AssociationProperty contract contains an unsupported canonical value")


def canonical_contract_bytes(payload: Mapping[str, Any]) -> bytes:
    content = dict(payload)
    content.pop("content_sha256", None)
    return _canonical_value_bytes(content)


def contract_content_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_contract_bytes(payload)).hexdigest()


def _project_entries(value: Any, allowed_fields: frozenset[str]) -> Any:
    if not isinstance(value, Mapping):
        return deepcopy(value)
    return {
        key: (
            {field: deepcopy(entry[field]) for field in allowed_fields if field in entry}
            if isinstance(entry, Mapping)
            else deepcopy(entry)
        )
        for key, entry in value.items()
    }


def apply_contract_corrections(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the local contract projected onto its supported public fields."""

    common = payload.get("common_metadata")
    corrected: dict[str, Any] = {
        "association_properties": _project_entries(
            payload.get("association_properties"),
            frozenset({"component", "deprecated", "replacement", "scope"}),
        ),
        "common_metadata": (
            {field: deepcopy(common[field]) for field in ("coverage", "schema", "semantic_rules") if field in common}
            if isinstance(common, Mapping)
            else deepcopy(common)
        ),
        "components": _project_entries(
            payload.get("components"),
            frozenset({"coverage", "metadata", "semantic_rules"}),
        ),
        "consumer_sets": deepcopy(payload.get("consumer_sets")),
        "contract_version": CONTRACT_VERSION,
        "corrections": deepcopy(list(CONTRACT_CORRECTIONS)),
        "excluded_association_properties": _project_entries(
            payload.get("excluded_association_properties"),
            frozenset({"scope"}),
        ),
        "product": deepcopy(payload.get("product")),
        "profile": deepcopy(payload.get("profile")),
    }

    profile = corrected.get("profile")
    if not isinstance(profile, dict):
        raise RuntimeError("AssociationProperty contract profile is missing")
    profile["id"] = "stock-ros-parameter-form"
    profile["host_read_only_inputs"] = "unknown"

    common = corrected.get("common_metadata")
    common_schema = common.get("schema") if isinstance(common, Mapping) else None
    common_properties = common_schema.get("properties") if isinstance(common_schema, Mapping) else None
    if not isinstance(common_properties, dict) or "ValueLabelMapping" not in common_properties:
        raise RuntimeError("AssociationProperty contract cannot apply the ValueLabelMapping correction")
    common_properties["ValueLabelMapping"] = deepcopy(_VALUE_LABEL_MAPPING_SCHEMA)

    components = corrected.get("components")
    bailian = components.get("BailianApiKey") if isinstance(components, Mapping) else None
    metadata = bailian.get("metadata") if isinstance(bailian, Mapping) else None
    properties = metadata.get("properties") if isinstance(metadata, Mapping) else None
    only_key = properties.get("OnlyKey") if isinstance(properties, Mapping) else None
    if not isinstance(only_key, dict):
        raise RuntimeError("AssociationProperty contract cannot apply the OnlyKey correction")
    only_key["type"] = "boolean"

    corrected["content_sha256"] = contract_content_hash(corrected)
    return corrected


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise RuntimeError("{} must be an object with String keys".format(label))
    return value


def _verify_schema(schema: Any, label: str = "schema") -> None:
    schema = _require_mapping(schema, label)
    unknown_keywords = set(schema) - SUPPORTED_SCHEMA_KEYWORDS
    if unknown_keywords:
        raise RuntimeError("{} contains unsupported keywords {}".format(label, sorted(unknown_keywords)))
    schema_type = schema.get("type")
    if schema_type is not None and schema_type not in SUPPORTED_SCHEMA_TYPES:
        raise RuntimeError("{} has unsupported type {}".format(label, schema_type))
    properties = schema.get("properties")
    if properties is not None:
        properties = _require_mapping(properties, "{}.properties".format(label))
        if schema_type != "object":
            raise RuntimeError("{}.properties requires object type".format(label))
        for key, child in properties.items():
            _verify_schema(child, "{}.properties.{}".format(label, key))
    pattern_properties = schema.get("patternProperties")
    if pattern_properties is not None:
        pattern_properties = _require_mapping(pattern_properties, "{}.patternProperties".format(label))
        if schema_type != "object":
            raise RuntimeError("{}.patternProperties requires object type".format(label))
        for key, child in pattern_properties.items():
            try:
                re.compile(key)
            except re.error as error:
                raise RuntimeError("{}.patternProperties contains invalid pattern {}".format(label, key)) from error
            _verify_schema(child, "{}.patternProperties.{}".format(label, key))
    if "items" in schema:
        if schema_type != "array":
            raise RuntimeError("{}.items requires array type".format(label))
        _verify_schema(schema["items"], "{}.items".format(label))
    required = schema.get("required")
    if required is not None:
        if (
            schema_type != "object"
            or not isinstance(required, list)
            or not all(isinstance(item, str) and item for item in required)
            or len(set(required)) != len(required)
        ):
            raise RuntimeError("{}.required is invalid".format(label))
    additional = schema.get("additionalProperties")
    if additional is not None:
        if schema_type != "object":
            raise RuntimeError("{}.additionalProperties requires object type".format(label))
        if not isinstance(additional, bool):
            _verify_schema(additional, "{}.additionalProperties".format(label))
    any_of = schema.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise RuntimeError("{}.anyOf is invalid".format(label))
        for index, child in enumerate(any_of):
            _verify_schema(child, "{}.anyOf[{}]".format(label, index))
    enum = schema.get("enum")
    if enum is not None:
        if (
            not isinstance(enum, list)
            or not enum
            or len({json.dumps(item, sort_keys=True) for item in enum}) != len(enum)
        ):
            raise RuntimeError("{}.enum is invalid".format(label))
        if schema_type is not None and any(not _schema_value_matches_type(item, schema_type) for item in enum):
            raise RuntimeError("{}.enum does not match type".format(label))
    if "const" in schema and schema_type is not None and not _schema_value_matches_type(schema["const"], schema_type):
        raise RuntimeError("{}.const does not match type".format(label))
    minimum = schema.get("minimum")
    if minimum is not None and (
        schema_type not in {"integer", "number"}
        or isinstance(minimum, bool)
        or not isinstance(minimum, (int, float))
        or not math.isfinite(minimum)
    ):
        raise RuntimeError("{}.minimum is invalid".format(label))
    min_length = schema.get("minLength")
    if min_length is not None and (
        schema_type != "string" or isinstance(min_length, bool) or not isinstance(min_length, int) or min_length < 0
    ):
        raise RuntimeError("{}.minLength is invalid".format(label))
    for keyword in (
        "x-ore-aliases",
        "x-ore-injected-symbols",
        "x-ore-precedence",
        "x-ore-reference-kinds",
    ):
        values = schema.get(keyword)
        if values is not None and (
            not isinstance(values, list)
            or not values
            or len(set(values)) != len(values)
            or not all(isinstance(item, str) and item for item in values)
        ):
            raise RuntimeError("{}.{} is invalid".format(label, keyword))
    aliases = schema.get("x-ore-aliases")
    precedence = schema.get("x-ore-precedence")
    if aliases is not None and (not isinstance(precedence, list) or not all(alias in precedence for alias in aliases)):
        raise RuntimeError("{}.x-ore-aliases requires matching precedence".format(label))
    parser = schema.get("x-ore-parser")
    if parser is not None and parser not in SUPPORTED_PARSERS:
        raise RuntimeError("{}.x-ore-parser is invalid".format(label))
    reference_context = schema.get("x-ore-reference-context")
    if reference_context is not None and reference_context not in SUPPORTED_REFERENCE_CONTEXTS:
        raise RuntimeError("{}.x-ore-reference-context is invalid".format(label))
    reference_kinds = schema.get("x-ore-reference-kinds")
    if isinstance(reference_kinds, list) and not set(reference_kinds) <= SUPPORTED_REFERENCE_KINDS:
        raise RuntimeError("{}.x-ore-reference-kinds is invalid".format(label))
    unresolved_reference = schema.get("x-ore-unresolved-reference")
    if unresolved_reference is not None and unresolved_reference != "literal-segment":
        raise RuntimeError("{}.x-ore-unresolved-reference is invalid".format(label))
    consumer_set = schema.get("x-ore-consumer-set")
    if consumer_set is not None and (not isinstance(consumer_set, str) or not consumer_set):
        raise RuntimeError("{}.x-ore-consumer-set is invalid".format(label))
    nested_parameter = schema.get("x-ore-nested-parameter")
    if nested_parameter is not None and not isinstance(nested_parameter, bool):
        raise RuntimeError("{}.x-ore-nested-parameter is invalid".format(label))
    suggestions = schema.get("x-ore-value-suggestions")
    if suggestions is not None:
        suggestions = _require_mapping(suggestions, "{}.x-ore-value-suggestions".format(label))
        invalid_suggestion = any(
            not key or not isinstance(value, str) or not value for key, value in suggestions.items()
        )
        if (
            not suggestions
            or invalid_suggestion
            or (isinstance(enum, list) and any(value not in enum for value in suggestions.values()))
        ):
            raise RuntimeError("{}.x-ore-value-suggestions is invalid".format(label))


def _schema_value_matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, Mapping)
    return False


def _verify_string_list(value: Any, label: str, *, nonempty: bool = True) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
    ):
        raise RuntimeError("{} must be a unique String array".format(label))
    return value


def _verify_consumer_sets(value: Any) -> Mapping[str, Any]:
    consumer_sets = _require_mapping(value, "consumer_sets")
    if not consumer_sets:
        raise RuntimeError("consumer_sets must not be empty")
    for name, raw in consumer_sets.items():
        consumer_set = _require_mapping(raw, "consumer_sets.{}".format(name))
        if set(consumer_set) != {"consumers", "resolution", "value_flow"}:
            raise RuntimeError("consumer set {} has invalid schema".format(name))
        if consumer_set.get("resolution") not in {"consistent", "inconsistent"}:
            raise RuntimeError("consumer set {} has invalid resolution".format(name))
        consumers = consumer_set.get("consumers")
        if not isinstance(consumers, list) or not consumers:
            raise RuntimeError("consumer set {} has no consumers".format(name))
        ids: set[str] = set()
        for index, raw_consumer in enumerate(consumers):
            consumer = _require_mapping(raw_consumer, "consumer_sets.{}.consumers[{}]".format(name, index))
            expected = {"id", "parser", "reference_context", "when"}
            if "reference_kinds" in consumer:
                expected.add("reference_kinds")
            consumer_id = consumer.get("id")
            if (
                set(consumer) != expected
                or not isinstance(consumer_id, str)
                or not consumer_id
                or consumer_id in ids
                or consumer.get("parser") not in SUPPORTED_PARSERS
                or consumer.get("reference_context") not in SUPPORTED_REFERENCE_CONTEXTS
                or consumer.get("when") not in SUPPORTED_WHEN
            ):
                raise RuntimeError("consumer set {} contains an invalid consumer".format(name))
            ids.add(consumer_id)
            if "reference_kinds" in consumer:
                kinds = _verify_string_list(
                    consumer["reference_kinds"], "consumer_sets.{}.reference_kinds".format(name)
                )
                if not set(kinds) <= SUPPORTED_REFERENCE_KINDS:
                    raise RuntimeError("consumer set {} contains an invalid reference kind".format(name))
        if (
            dict(_require_mapping(consumer_set.get("value_flow"), "consumer_sets.{}.value_flow".format(name)))
            != VALUE_FLOW_SCHEMA
        ):
            raise RuntimeError("consumer set {} has invalid value_flow".format(name))
    return consumer_sets


def verify_contract_payload(payload: Mapping[str, Any]) -> None:
    expected_fields = {
        "association_properties",
        "common_metadata",
        "components",
        "content_sha256",
        "consumer_sets",
        "contract_version",
        "corrections",
        "excluded_association_properties",
        "product",
        "profile",
    }
    if set(payload) != expected_fields or payload.get("contract_version") != CONTRACT_VERSION:
        raise RuntimeError("AssociationProperty contract has an unsupported top-level schema")
    if payload.get("product") != "ROS":
        raise RuntimeError("AssociationProperty contract is not for ROS")
    profile = _require_mapping(payload.get("profile"), "profile")
    expected_profile = {
        "component_overrides": False,
        "default_read_only_prop": False,
        "form_type": "parameter",
        "host_read_only_inputs": "unknown",
        "host_value_inputs": "unknown",
        "id": "stock-ros-parameter-form",
        "read_only_selection": {
            "component": "ReadOnlyItem",
            "contains": ["InstanceType"],
            "exact_association_properties": ["TemplateParameter", "Targets"],
            "precedence": "before-association-property-resolution",
        },
    }
    if dict(profile) != expected_profile:
        raise RuntimeError("AssociationProperty contract has an unsupported profile")
    corrections = payload.get("corrections")
    if corrections != list(CONTRACT_CORRECTIONS):
        raise RuntimeError("AssociationProperty contract corrections are invalid")
    expected_hash = payload.get("content_sha256")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or expected_hash != contract_content_hash(payload)
    ):
        raise RuntimeError("AssociationProperty contract content_sha256 mismatch")
    associations = _require_mapping(payload.get("association_properties"), "association_properties")
    exclusions = _require_mapping(payload.get("excluded_association_properties"), "excluded_association_properties")
    components = _require_mapping(payload.get("components"), "components")
    if not associations or not components or set(associations) & set(exclusions):
        raise RuntimeError("AssociationProperty keys are not explained exactly once")
    for key, raw in associations.items():
        entry = _require_mapping(raw, "association_properties.{}".format(key))
        if set(entry) != {"component", "deprecated", "replacement", "scope"}:
            raise RuntimeError("AssociationProperty {} has invalid schema".format(key))
        if entry.get("component") not in components or entry.get("scope") != "ROS":
            raise RuntimeError("AssociationProperty {} has no stock ROS component".format(key))
        replacement = entry.get("replacement")
        if not isinstance(entry.get("deprecated"), bool) or not (
            replacement is None or isinstance(replacement, str) and replacement
        ):
            raise RuntimeError("AssociationProperty {} has invalid metadata".format(key))
    for key, raw in exclusions.items():
        entry = _require_mapping(raw, "excluded_association_properties.{}".format(key))
        if set(entry) != {"scope"}:
            raise RuntimeError("excluded AssociationProperty {} has invalid schema".format(key))
        if entry.get("scope") not in {"OOS-only", "unavailable-stock"}:
            raise RuntimeError("excluded AssociationProperty {} has invalid metadata".format(key))
    consumer_sets = _verify_consumer_sets(payload.get("consumer_sets"))
    if dict(consumer_sets) != EXPECTED_AUTO_COMPLETE_CONSUMER_SETS:
        raise RuntimeError("AutoCompleteInput consumer sets differ from the supported local contract")
    for name, raw in components.items():
        entry = _require_mapping(raw, "components.{}".format(name))
        if set(entry) != {"coverage", "metadata", "semantic_rules"}:
            raise RuntimeError("component {} has invalid schema".format(name))
        if entry.get("coverage") not in {"complete", "partial"}:
            raise RuntimeError("component {} has invalid coverage".format(name))
        try:
            semantic_rules = _verify_string_list(
                entry.get("semantic_rules"), "components.{}.semantic_rules".format(name), nonempty=False
            )
        except RuntimeError as error:
            raise RuntimeError("component {} has invalid audit metadata".format(name)) from error
        if not set(semantic_rules) <= SUPPORTED_SEMANTIC_RULES:
            raise RuntimeError("component {} uses an unknown semantic rule".format(name))
        _verify_schema(entry.get("metadata"), "components.{}.metadata".format(name))
    auto_complete = _require_mapping(components.get("AutoCompleteInput"), "components.AutoCompleteInput")
    if (
        auto_complete.get("coverage") != "complete"
        or auto_complete.get("semantic_rules") != ["auto_complete_character_capacity"]
        or any(
            name != "AutoCompleteInput" and "auto_complete_character_capacity" in raw.get("semantic_rules", [])
            for name, raw in components.items()
            if isinstance(raw, Mapping)
        )
    ):
        raise RuntimeError("AutoCompleteInput semantic rule ownership is invalid")
    common = _require_mapping(payload.get("common_metadata"), "common_metadata")
    if set(common) != {"coverage", "schema", "semantic_rules"} or common.get("coverage") not in {
        "complete",
        "partial",
    }:
        raise RuntimeError("common AssociationPropertyMetadata contract is invalid")
    if common.get("semantic_rules") != ["parameter_metadata_condition"]:
        raise RuntimeError("common AssociationPropertyMetadata semantic rules are invalid")
    _verify_schema(common.get("schema"), "common_metadata.schema")

    def walk_schema(schema: Mapping[str, Any]) -> None:
        consumer_set = schema.get("x-ore-consumer-set")
        if consumer_set is not None and consumer_set not in consumer_sets:
            raise RuntimeError("AssociationProperty schema references unknown consumer set {}".format(consumer_set))
        for child in _require_mapping(schema.get("properties", {}), "properties").values():
            walk_schema(child)
        for child in _require_mapping(schema.get("patternProperties", {}), "patternProperties").values():
            walk_schema(child)
        if isinstance(schema.get("items"), Mapping):
            walk_schema(schema["items"])
        if isinstance(schema.get("additionalProperties"), Mapping):
            walk_schema(schema["additionalProperties"])
        for child in schema.get("anyOf", []):
            walk_schema(child)

    walk_schema(common["schema"])
    for raw in components.values():
        walk_schema(raw["metadata"])


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def registry_from_payload(payload: Mapping[str, Any]) -> AssociationPropertySpecRegistry:
    verify_contract_payload(payload)
    common = payload["common_metadata"]
    associations = {
        key: AssociationPropertySpec(
            key=key,
            component=value["component"],
            deprecated=value["deprecated"],
            replacement=value["replacement"],
            scope=value["scope"],
        )
        for key, value in payload["association_properties"].items()
    }
    exclusions = {
        key: ExcludedAssociationPropertySpec(
            key=key,
            scope=value["scope"],
        )
        for key, value in payload["excluded_association_properties"].items()
    }
    components = {
        name: ComponentSpec(
            name=name,
            coverage=value["coverage"],
            metadata=_freeze(value["metadata"]),
            semantic_rules=tuple(value["semantic_rules"]),
        )
        for name, value in payload["components"].items()
    }
    return AssociationPropertySpecRegistry(
        contract_version=payload["contract_version"],
        product=payload["product"],
        profile=_freeze(payload["profile"]),
        corrections=tuple(_freeze(item) for item in payload["corrections"]),
        common_coverage=common["coverage"],
        common_metadata=_freeze(common["schema"]),
        common_semantic_rules=tuple(common["semantic_rules"]),
        consumer_sets=_freeze(payload["consumer_sets"]),
        association_properties=MappingProxyType(associations),
        components=MappingProxyType(components),
        excluded_association_properties=MappingProxyType(exclusions),
        raw_contract=_freeze(payload),
    )


def load_contract_text(text: str) -> AssociationPropertySpecRegistry:
    return registry_from_payload(parse_contract_text(text))


@lru_cache(maxsize=1)
def load_association_property_specs() -> AssociationPropertySpecRegistry:
    data_path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_association_property_specs.json")
    if not data_path.is_file():
        raise RuntimeError("vendored ROS AssociationProperty contract is missing")
    return load_contract_text(data_path.read_text(encoding="utf-8"))
