"""Export the browser build contract from ORE facts and server profiles."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .profiles import PROFILE_HASH, SelectorProfile, _hash_payload, get_profile, iter_profiles

_CAPABILITY_FIELDS = (
    "selectorId",
    "associationProperty",
    "oreAssociationProperty",
    "selectionKind",
    "outputKind",
    "sourceSelectorId",
    "enabled",
    "metadata",
    "metadataKeys",
    "operationKeys",
)


def _public_capabilities(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": value.get("schemaVersion"),
        "selectors": [
            {key: item.get(key) for key in _CAPABILITY_FIELDS}
            for item in value.get("selectors", [])
        ],
    }


def _public_operations(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": value.get("schemaVersion"),
        "counts": value.get("counts"),
        "selectors": [
            {
                "selectorId": selector.get("selectorId"),
                "operations": [
                    {
                        key: operation.get(key)
                        for key in ("key", "requestKind", "product", "action", "allowedParameters")
                    }
                    for operation in selector.get("operations", [])
                ],
            }
            for selector in value.get("selectors", [])
        ],
    }


def _source_contract(profile: SelectorProfile) -> dict[str, Any] | None:
    if not profile.source_selector_id:
        return None
    source = get_profile(profile.source_selector_id)
    if source is None or not source.enabled:
        raise ValueError("enabled source profile is missing for {}".format(profile.selector_id))
    return {
        "selectorId": source.selector_id,
        "sourceSchema": {
            "type": "object",
            "additionalProperties": False,
            # Python accepts an omitted metadata object, normalizes defaults,
            # and then validates required fields.  The browser contract keeps
            # the same shape; ORE performs that normalization before applying
            # this nested schema.
            "required": ["selector_id", "value"],
            "properties": {
                "selector_id": {"const": source.selector_id},
                "value": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": source.value_max_length,
                    "pattern": source.value_pattern,
                    "patternByAttribute": source.value_pattern_by_attribute,
                },
                "association_property_metadata": source.metadata_schema,
            },
        },
        "metadataConflictPolicy": "equal_if_repeated",
        "sourceParameterKey": profile.source_parameter_key,
        "operationParameters": {
            operation.key: profile.source_parameter_for(operation.key)
            for operation in profile.operations
        },
        "valueTransform": "singleton_list" if profile.selector_id == "ess.eci_container" else "scalar",
    }


def _selector_contract(profile: SelectorProfile) -> dict[str, Any]:
    return {
        "selectorId": profile.selector_id,
        "associationProperty": profile.association_property,
        "oreAssociationProperty": profile.standalone_association_property,
        "selectionKind": profile.selection_kind,
        "metadataSchema": profile.metadata_schema,
        "outputKind": profile.output_kind,
        "outputKindByAttribute": profile.output_kind_by_attribute,
        "valuePattern": profile.value_pattern,
        "valuePatternByAttribute": profile.value_pattern_by_attribute,
        "valueMaxLength": profile.value_max_length,
        "source": _source_contract(profile),
    }


def export_contract(*, browser_facts_path: Path, output_path: Path) -> dict[str, Any]:
    facts_bytes = browser_facts_path.read_bytes()
    facts = json.loads(facts_bytes.decode("utf-8"))
    if type(facts.get("schemaVersion")) is not int or facts.get("schemaVersion") != 1:
        raise ValueError("unsupported ORE browser facts schema")

    root = Path(__file__).resolve().parent
    expected_capabilities = json.loads((root / "ore-capabilities.json").read_text(encoding="utf-8"))
    expected_operations = json.loads((root / "ore-operations.json").read_text(encoding="utf-8"))
    expected_projections = json.loads((root / "ore-response-projections.json").read_text(encoding="utf-8"))
    if _public_capabilities(facts.get("capabilities") or {}) != expected_capabilities:
        raise ValueError("ORE browser capability facts do not match iac-code snapshot")
    if _public_operations(facts.get("operations") or {}) != expected_operations:
        raise ValueError("ORE browser operation facts do not match iac-code snapshot")
    if facts.get("responseProjections") != expected_projections:
        raise ValueError("ORE browser response projections do not match iac-code snapshot")

    selectors = [_selector_contract(profile) for profile in iter_profiles(include_disabled=False)]
    if len(selectors) != 116 or len({item["selectorId"] for item in selectors}) != 116:
        raise ValueError("build contract must contain exactly 116 enabled selectors")
    contract = {
        "schemaVersion": 1,
        "browserFactsSha256": hashlib.sha256(facts_bytes).hexdigest(),
        "profileHash": PROFILE_HASH,
        "profileHashPayload": json.loads(json.dumps(_hash_payload(), ensure_ascii=True)),
        "selectors": selectors,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return contract


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-facts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export_contract(browser_facts_path=args.browser_facts, output_path=args.output)


if __name__ == "__main__":
    main()
