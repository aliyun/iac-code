#!/usr/bin/env python3
"""Validate and vendor a sanitized AssociationProperty contract into iac-code."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from iac_code.tools.cloud.aliyun.ros_validation.association_property_specs import (
    apply_contract_corrections,
    parse_contract_text,
    verify_contract_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "src/iac_code/tools/cloud/aliyun/ros_validation/data/ros_association_property_specs.json"
)


def _load_existing(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    payload = apply_contract_corrections(parse_contract_text(path.read_text(encoding="utf-8")))
    verify_contract_payload(payload)
    return payload


def _keys(payload: Mapping[str, Any] | None, section: str) -> set[str]:
    if payload is None:
        return set()
    value = payload.get(section)
    return set(value) if isinstance(value, Mapping) else set()


def _metadata_fields(payload: Mapping[str, Any] | None) -> set[str]:
    if payload is None:
        return set()
    result: set[str] = set()

    def collect(schema: Any, prefix: str) -> None:
        if not isinstance(schema, Mapping):
            return
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key, child in properties.items():
                child_prefix = "{}.{}".format(prefix, key)
                result.add(child_prefix)
                collect(child, child_prefix)
        items = schema.get("items")
        if isinstance(items, Mapping):
            collect(items, "{}[]".format(prefix))
        additional = schema.get("additionalProperties")
        if isinstance(additional, Mapping):
            wildcard = "{}.*".format(prefix)
            result.add(wildcard)
            collect(additional, wildcard)
        patterns = schema.get("patternProperties")
        if isinstance(patterns, Mapping):
            for pattern, child in patterns.items():
                pattern_prefix = "{}.pattern({})".format(prefix, pattern)
                result.add(pattern_prefix)
                collect(child, pattern_prefix)
        any_of = schema.get("anyOf")
        if isinstance(any_of, list):
            for child in any_of:
                collect(child, prefix)

    common = payload.get("common_metadata")
    if isinstance(common, Mapping):
        collect(common.get("schema"), "common")
    components = payload.get("components")
    if isinstance(components, Mapping):
        for component_name, component in components.items():
            if isinstance(component, Mapping):
                collect(component.get("metadata"), str(component_name))
    return result


def _describe_changes(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> str:
    old_keys = _keys(old, "association_properties")
    new_keys = _keys(new, "association_properties")
    old_fields = _metadata_fields(old)
    new_fields = _metadata_fields(new)
    deprecated = sorted(
        key
        for key, value in new["association_properties"].items()
        if isinstance(value, Mapping) and value.get("deprecated")
    )
    return "\n".join(
        (
            "AssociationProperty added: {}".format(", ".join(sorted(new_keys - old_keys)) or "<none>"),
            "AssociationProperty removed: {}".format(", ".join(sorted(old_keys - new_keys)) or "<none>"),
            "AssociationProperty deprecated: {}".format(", ".join(deprecated) or "<none>"),
            "metadata fields added: {}".format(", ".join(sorted(new_fields - old_fields)) or "<none>"),
            "metadata fields removed: {}".format(", ".join(sorted(old_fields - new_fields)) or "<none>"),
        )
    )


def sync_contract(input_path: Path, output_path: Path) -> str:
    payload = apply_contract_corrections(parse_contract_text(input_path.read_text(encoding="utf-8")))
    verify_contract_payload(payload)
    old = _load_existing(output_path)
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized, encoding="utf-8", newline="\n")
    return _describe_changes(old, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(sync_contract(args.input.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()
