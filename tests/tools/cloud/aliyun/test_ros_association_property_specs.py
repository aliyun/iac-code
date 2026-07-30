from __future__ import annotations

import json
from importlib.resources import files
from types import MappingProxyType

import pytest

from iac_code.tools.cloud.aliyun.ros_validation.association_property_specs import (
    CONTRACT_CORRECTIONS,
    apply_contract_corrections,
    contract_content_hash,
    load_association_property_specs,
    load_contract_text,
    parse_contract_text,
    registry_from_payload,
    verify_contract_payload,
)
from scripts.aliyun.sync_ros_association_property_specs import _metadata_fields


def _payload() -> dict:
    path = files("iac_code.tools.cloud.aliyun.ros_validation").joinpath("data/ros_association_property_specs.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _rehash(payload: dict) -> None:
    payload["content_sha256"] = contract_content_hash(payload)


def test_canonical_contract_hash_matches_language_neutral_number_and_utf16_vectors() -> None:
    payload = {
        "content_sha256": "ignored",
        "numbers": [1e-7, 1e21, -0.0],
        "keys": {"\ufffd": "bmp", "\U00010000": "astral"},
    }

    assert contract_content_hash(payload) == "afa9b95335995e6ccee74aefe3918c4bbb3e6b4d6a192b4754f59c3e88e54ea4"


def test_vendored_contract_corrections_and_freezing() -> None:
    registry = load_association_property_specs()

    assert registry.contract_version == 3
    assert registry.product == "ROS"
    assert registry.corrections == tuple(MappingProxyType(item) for item in CONTRACT_CORRECTIONS)
    assert len(registry.association_properties) == 296
    assert sum(spec.deprecated for spec in registry.association_properties.values()) == 10
    assert len(registry.excluded_association_properties) == 10
    assert len(registry.components) == 276
    assert registry.component("AutoCompleteInput").coverage == "complete"
    assert registry.association("AutoCompleteInput").component == "AutoCompleteInput"
    assert registry.excluded("ALIYUN::OOS::Component::ActionChoice").scope == "OOS-only"
    assert registry.profile["read_only_selection"]["exact_association_properties"] == (
        "TemplateParameter",
        "Targets",
    )
    assert isinstance(registry.profile, MappingProxyType)
    with pytest.raises(TypeError):
        registry.profile["id"] = "changed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.__setitem__("contract_version", 4),
        lambda item: item.__setitem__("product", "OOS"),
        lambda item: item["corrections"][0].__setitem__("reason", "changed"),
        lambda item: item["profile"].__setitem__("id", "changed"),
        lambda item: item["common_metadata"].__setitem__("coverage", "complete"),
        lambda item: item["consumer_sets"]["auto_complete_generation"].__setitem__("resolution", "consistent"),
        lambda item: item["association_properties"]["AutoCompleteInput"].__setitem__("deprecated", True),
        lambda item: item["components"]["AutoCompleteInput"].__setitem__("coverage", "partial"),
        lambda item: item["excluded_association_properties"]["Default"].__setitem__("scope", "OOS-only"),
    ],
)
def test_hash_protects_every_contract_region(mutate) -> None:
    payload = _payload()
    mutate(payload)

    with pytest.raises(RuntimeError):
        registry_from_payload(payload)


def test_vendored_contract_contains_only_supported_public_fields() -> None:
    payload = _payload()

    assert set(payload) == {
        "association_properties",
        "common_metadata",
        "components",
        "consumer_sets",
        "content_sha256",
        "contract_version",
        "corrections",
        "excluded_association_properties",
        "product",
        "profile",
    }
    assert all(
        set(spec) <= {"component", "deprecated", "replacement", "scope"}
        for spec in payload["association_properties"].values()
    )
    assert all(set(spec) <= {"scope"} for spec in payload["excluded_association_properties"].values())
    assert all(set(spec) <= {"coverage", "metadata", "semantic_rules"} for spec in payload["components"].values())


def test_contract_corrections_are_idempotent_and_sanitizing() -> None:
    payload = _payload()
    payload["unsupported_top_level"] = {"detail": "discarded"}
    payload["association_properties"]["AutoCompleteInput"]["unsupported_detail"] = "discarded"
    payload["excluded_association_properties"]["Default"].update(
        {
            "component": "DiscardedComponent",
            "unsupported_detail": "discarded",
        }
    )
    payload["components"]["AutoCompleteInput"]["unsupported_detail"] = "discarded"

    corrected = apply_contract_corrections(payload)

    assert "unsupported_top_level" not in corrected
    assert "unsupported_detail" not in corrected["association_properties"]["AutoCompleteInput"]
    assert corrected["excluded_association_properties"]["Default"] == {"scope": "unavailable-stock"}
    assert set(corrected["components"]["AutoCompleteInput"]) == {"coverage", "metadata", "semantic_rules"}
    assert corrected["corrections"] == list(CONTRACT_CORRECTIONS)
    assert (
        corrected["common_metadata"]["schema"]["properties"]["ValueLabelMapping"]["additionalProperties"]["anyOf"][1][
            "type"
        ]
        == "object"
    )
    assert corrected["components"]["BailianApiKey"]["metadata"]["properties"]["OnlyKey"]["type"] == "boolean"
    verify_contract_payload(corrected)


def test_loader_rejects_duplicate_json_keys_and_unsupported_schema() -> None:
    with pytest.raises(RuntimeError, match="duplicate key"):
        parse_contract_text('{"contract_version": 1, "contract_version": 1}')

    payload = _payload()
    payload["components"]["AutoCompleteInput"]["metadata"]["properties"]["Length"]["type"] = "float"
    _rehash(payload)
    with pytest.raises(RuntimeError, match="unsupported type"):
        registry_from_payload(payload)


def test_loader_rejects_unknown_consumer_set_and_semantic_rule() -> None:
    payload = _payload()
    payload["components"]["AutoCompleteInput"]["metadata"]["properties"]["Length"]["x-ore-consumer-set"] = "missing"
    _rehash(payload)
    with pytest.raises(RuntimeError, match="unknown consumer set"):
        registry_from_payload(payload)

    payload = _payload()
    payload["components"]["AutoCompleteInput"]["semantic_rules"].append("unknown")
    _rehash(payload)
    with pytest.raises(RuntimeError, match="unknown semantic rule"):
        registry_from_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.__setitem__("unsupported_top_level", {"count": 1}),
        lambda item: item["association_properties"]["AutoCompleteInput"].__setitem__("unsupported_detail", "discarded"),
        lambda item: item["components"]["AutoCompleteInput"].__setitem__("unsupported_detail", "discarded"),
        lambda item: item["excluded_association_properties"]["Default"].__setitem__("unsupported_detail", "discarded"),
    ],
)
def test_loader_rejects_fields_outside_public_contract(mutate) -> None:
    payload = _payload()
    mutate(payload)
    _rehash(payload)

    with pytest.raises(RuntimeError):
        registry_from_payload(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda item: item["consumer_sets"]["auto_complete_generation"]["consumers"][1].__setitem__(
                "parser", "arbitrary-code"
            ),
            "invalid consumer",
        ),
        (
            lambda item: item["consumer_sets"]["auto_complete_generation"]["consumers"][1].__setitem__(
                "id", "template-default-initializer"
            ),
            "invalid consumer",
        ),
        (
            lambda item: item["consumer_sets"]["auto_complete_generation"]["value_flow"].__setitem__(
                "host_merge", "guess"
            ),
            "invalid value_flow",
        ),
        (
            lambda item: item["components"]["HologresInstanceId"]["metadata"]["properties"][
                "cmsInstanceType"
            ].__setitem__("x-ore-precedence", ["cmsInstanceType"]),
            "requires matching precedence",
        ),
    ],
)
def test_loader_rejects_invalid_consumers_entries_and_alias_precedence(mutate, message: str) -> None:
    payload = _payload()
    mutate(payload)
    _rehash(payload)

    with pytest.raises(RuntimeError, match=message):
        registry_from_payload(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda item: item["components"]["AutoCompleteInput"]["metadata"]["properties"]["CharacterClasses"]["items"][
                "properties"
            ]["Min"].__setitem__("minimum", "bad"),
            "minimum is invalid",
        ),
        (
            lambda item: item["components"]["AutoCompleteInput"]["metadata"]["properties"]["Prefix"].__setitem__(
                "minLength", -1
            ),
            "minLength is invalid",
        ),
        (
            lambda item: item["components"]["Json"]["metadata"]["properties"]["Parameters"].__setitem__(
                "x-ore-nested-parameter", "yes"
            ),
            "x-ore-nested-parameter is invalid",
        ),
        (
            lambda item: item["components"]["AutoCompleteInput"]["metadata"]["properties"]["CharacterClasses"]["items"][
                "properties"
            ]["Class"].__setitem__("x-ore-value-suggestions", ["bad"]),
            "x-ore-value-suggestions",
        ),
        (
            lambda item: item["components"]["AutoCompleteInput"]["metadata"]["properties"]["Length"].__setitem__(
                "x-ore-consumer-set", ""
            ),
            "x-ore-consumer-set is invalid",
        ),
        (
            lambda item: item["common_metadata"]["schema"]["properties"]["DynamicValue"].__setitem__(
                "x-ore-injected-symbols", []
            ),
            "x-ore-injected-symbols is invalid",
        ),
        (
            lambda item: item["common_metadata"]["schema"]["properties"]["MappingMetadata"]["properties"][
                "ValueSelector"
            ].__setitem__("x-ore-unresolved-reference", "error"),
            "x-ore-unresolved-reference is invalid",
        ),
        (
            lambda item: item["common_metadata"]["schema"].__setitem__("patternProperties", {"[": {}}),
            "invalid pattern",
        ),
        (
            lambda item: item["common_metadata"]["schema"]["properties"]["MappingMetadata"].__setitem__(
                "required", [""]
            ),
            "required is invalid",
        ),
        (
            lambda item: item["components"]["AutoCompleteInput"]["metadata"]["properties"]["Prefix"].__setitem__(
                "const", 1
            ),
            "const does not match type",
        ),
        (
            lambda item: item["common_metadata"]["schema"].__setitem__("additionalProperties", "yes"),
            "must be an object",
        ),
        (
            lambda item: item["common_metadata"]["schema"]["properties"]["AutoSelectFirst"].__setitem__(
                "enum", ["true"]
            ),
            "enum does not match type",
        ),
        (
            lambda item: item["common_metadata"]["schema"]["properties"]["LocaleKey"].update({"enum": [False]}),
            "enum does not match type",
        ),
        (
            lambda item: item["common_metadata"]["schema"]["properties"]["ExclusiveTo"].__setitem__("enum", [{}]),
            "enum does not match type",
        ),
        (
            lambda item: item["common_metadata"]["schema"]["properties"]["ValueLabelMapping"].__setitem__("enum", [[]]),
            "enum does not match type",
        ),
    ],
)
def test_loader_rejects_semantically_invalid_supported_schema_keywords(mutate, message: str) -> None:
    payload = _payload()
    mutate(payload)
    _rehash(payload)

    with pytest.raises(RuntimeError, match=message):
        registry_from_payload(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item["consumer_sets"]["auto_complete_generation"]["consumers"][0].__setitem__(
            "id", "another-initializer"
        ),
        lambda item: item["consumer_sets"]["auto_complete_generation"]["consumers"][1].__setitem__(
            "parser", "lodash-template-interpolation"
        ),
        lambda item: item["components"]["AutoCompleteInput"].__setitem__("semantic_rules", []),
        lambda item: item["components"]["String"].__setitem__("semantic_rules", ["auto_complete_character_capacity"]),
    ],
)
def test_loader_rejects_supported_but_source_inconsistent_consumers_and_semantic_rules(mutate) -> None:
    payload = _payload()
    mutate(payload)
    _rehash(payload)

    with pytest.raises(RuntimeError, match="AutoCompleteInput"):
        registry_from_payload(payload)


def test_serialized_contract_round_trips_without_mutation() -> None:
    payload = _payload()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    loaded = load_contract_text(text)

    assert loaded.raw_contract["content_sha256"] == payload["content_sha256"]
    assert loaded.profile["id"] == "stock-ros-parameter-form"


def test_sync_summary_inventory_includes_common_and_deep_metadata_paths() -> None:
    fields = _metadata_fields(_payload())

    assert "common.DynamicValue" in fields
    assert "MetaList.ListMetadata.Order" in fields
    assert "Json.Parameters.*" in fields
    assert "AutoCompleteInput.CharacterClasses[].Class" in fields
