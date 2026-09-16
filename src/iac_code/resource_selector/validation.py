"""Normalization and validation for selector metadata and answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, cast

import jsonschema

from .profiles import SelectorProfile, get_profile


@dataclass(frozen=True)
class ContractValidation:
    normalized: dict[str, Any]
    missing_required: tuple[str, ...]
    invalid_parameters: tuple[dict[str, str], ...]

    @property
    def valid(self) -> bool:
        return not self.missing_required and not self.invalid_parameters


def normalize_metadata(
    profile: SelectorProfile,
    metadata: object,
    *,
    default_region_provider: Callable[[], str | None] | None = None,
) -> ContractValidation:
    raw = dict(metadata) if isinstance(metadata, dict) else {}
    if "VPCId" in raw and "VpcId" not in raw:
        raw["VpcId"] = raw.pop("VPCId")

    properties = profile.metadata_schema.get("properties", {})
    normalized: dict[str, Any] = {}
    for key, schema in properties.items():
        if key in raw:
            normalized[key] = raw[key]
        elif "default" in schema:
            normalized[key] = schema["default"]
        elif schema.get("default_source") == "session.default_region_id" and default_region_provider is not None:
            value = default_region_provider()
            if value:
                normalized[key] = value
    for key, value in raw.items():
        if key not in normalized:
            normalized[key] = value

    required = profile.metadata_schema.get("required", [])
    missing = tuple(key for key in required if key not in normalized or normalized[key] in (None, ""))
    invalid: list[dict[str, str]] = []
    validator = jsonschema.Draft7Validator(profile.metadata_schema)
    for error in sorted(validator.iter_errors(normalized), key=lambda item: list(item.absolute_path)):
        path = ".".join(str(part) for part in error.absolute_path)
        invalid.append({"path": path, "message": error.message})
    for key, value in normalized.items():
        if isinstance(value, str) and (
            _ROS_PARAMETER_REF.fullmatch(value) or _TERRAFORM_PARAMETER_REF.fullmatch(value)
        ):
            invalid.append(
                {
                    "path": key,
                    "message": "template parameter references must be resolved before selector validation",
                }
            )
    return ContractValidation(normalized=normalized, missing_required=missing, invalid_parameters=tuple(invalid))


def validate_source(
    profile: SelectorProfile,
    source: object,
    *,
    target_metadata: dict[str, Any] | None = None,
    default_region_provider: Callable[[], str | None] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if profile.selection_kind == "cloud_resource":
        return (None, "selector_source_mismatch") if source is not None else (None, None)
    if not isinstance(source, dict) or set(source) - {
        "selector_id",
        "value",
        "association_property_metadata",
    }:
        return None, "selector_source_mismatch"
    source = cast(dict[str, Any], source)
    if source.get("selector_id") != profile.source_selector_id:
        return None, "selector_source_mismatch"
    source_profile = get_profile(str(profile.source_selector_id or ""))
    if source_profile is None or not source_profile.enabled:
        return None, "selector_source_mismatch"
    value = source.get("value")
    if validate_answer_value(source_profile, value, metadata=source.get("association_property_metadata")):
        return None, "selector_source_mismatch"
    normalized: dict[str, Any] = {"selector_id": profile.source_selector_id, "value": value}
    source_metadata = source.get("association_property_metadata")
    source_validation = normalize_metadata(
        source_profile,
        source_metadata,
        default_region_provider=default_region_provider,
    )
    if not source_validation.valid:
        return None, "selector_source_mismatch"
    if source_validation.normalized:
        normalized["association_property_metadata"] = source_validation.normalized
    # Source context is authoritative for a derived selector.  A caller may
    # repeat that context in the target metadata, but it must not be able to
    # silently replace it (for example, selecting a repository from one ACR
    # instance and then querying tags from another instance).
    target_properties = profile.metadata_schema.get("properties", {})
    target = target_metadata or {}
    for key, source_value in source_validation.normalized.items():
        if key not in target_properties or key not in target:
            continue
        if target[key] != source_value:
            return None, "selector_source_mismatch"

    source_parameter = profile.source_parameter_key
    if source_parameter and source_parameter in target:
        expected_source_value: Any = value
        if profile.selector_id == "ess.eci_container":
            expected_source_value = [value]
        if target[source_parameter] != expected_source_value:
            return None, "selector_source_mismatch"
    return normalized, None


def validate_answer_value(
    profile: SelectorProfile,
    value: object,
    *,
    metadata: object = None,
) -> str | None:
    if not isinstance(value, str) or not value or len(value) > profile.value_max_length:
        return "selector_value_invalid"
    pattern = profile.value_pattern
    if profile.value_pattern_by_attribute and isinstance(metadata, dict):
        attribute = cast(dict[str, Any], metadata).get("Attribute")
        pattern = profile.value_pattern_by_attribute.get(attribute if isinstance(attribute, str) else "", pattern)
    if pattern and re.fullmatch(pattern, value) is None:
        return "selector_value_invalid"
    return None


_ROS_PARAMETER_REF = re.compile(r"^\$\{([A-Za-z0-9_.-]+)}$")
_TERRAFORM_PARAMETER_REF = re.compile(r"^\$\$\{([A-Za-z0-9_.-]+)}$")
_LITERAL = re.compile(r"^\$\{!([^{}]+)}$")


def resolve_template_metadata_bindings(
    metadata: dict[str, Any], parameter_values: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, str]], tuple[str, ...]]:
    """Resolve ROS/Terraform references before selector validation."""
    concrete: dict[str, Any] = {}
    bindings: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for key, value in metadata.items():
        if not isinstance(value, str):
            concrete[key] = value
            continue
        literal = _LITERAL.fullmatch(value)
        if literal:
            concrete[key] = "${" + literal.group(1) + "}"
            continue
        match = _ROS_PARAMETER_REF.fullmatch(value) or _TERRAFORM_PARAMETER_REF.fullmatch(value)
        if match:
            parameter = match.group(1)
            bindings[key] = {"parameter": parameter}
            if parameter in parameter_values:
                concrete[key] = parameter_values[parameter]
            else:
                missing.append(key)
            continue
        concrete[key] = value
    return concrete, bindings, tuple(missing)
