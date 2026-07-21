"""Focused tests for anonymous OpenMeta metadata loading and caching."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import iac_code.tools.cloud.aliyun.openmeta as openmeta_module
from iac_code.tools.cloud.aliyun.openmeta import OpenMetaClient, normalize_api_metadata, normalize_product_metadata
from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services
from iac_code.tools.cloud.aliyun.user_agent import build_user_agent

MISSING = object()
FIXTURES = Path(__file__).parent / "fixtures" / "openmeta"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _write_openmeta_cache_from_process(cache_dir: str, summary: str) -> None:
    async def write() -> None:
        payload = load_fixture("ecs_describe_instances.json")
        payload["summary"] = summary
        client = OpenMetaClient(
            cache_dir=Path(cache_dir),
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        )
        try:
            result = await client.get_api("Ecs", "2014-05-26", "DescribeInstances")
            if result.value is None:
                raise RuntimeError(result.error)
        finally:
            await client.aclose()

    asyncio.run(write())


def normalize_api_fixture(*, security: object = MISSING):
    raw = load_fixture("ecs_describe_instances.json")
    if security is MISSING:
        raw.pop("security", None)
    else:
        raw["security"] = security
    return normalize_api_metadata(raw)


@pytest.mark.parametrize(
    ("raw", "declared", "schemes", "scopes"),
    [
        (MISSING, False, (), ()),
        ([], True, (), ()),
        ([{"AK": []}, {"Anonymous": []}], True, (("AK",), ("Anonymous",)), (((),), ((),))),
        ([{"AK": [], "Other": []}], True, (("AK", "Other"),), (((), ()),)),
        ([{"AK": ["scope"]}], True, (("AK",),), ((("scope",),),)),
    ],
)
def test_normalize_security_preserves_openapi_boolean_structure(raw, declared, schemes, scopes) -> None:
    metadata = normalize_api_fixture(security=raw)
    assert metadata.security_declared is declared
    assert tuple(item.schemes for item in metadata.security_requirements) == schemes
    assert tuple(item.scopes for item in metadata.security_requirements) == scopes


def test_normalize_security_preserves_distinct_scopes_for_each_and_scheme() -> None:
    metadata = normalize_api_fixture(security=[{"AK": ["ak:read"], "Other": ["other:write"]}])

    requirement = metadata.security_requirements[0]
    assert requirement.schemes == ("AK", "Other")
    assert requirement.scopes == (("ak:read",), ("other:write",))


def test_normalize_api_keeps_parameter_order_and_immutable_schema_views() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    before = copy.deepcopy(raw)

    metadata = normalize_api_metadata(raw)

    assert [parameter.name for parameter in metadata.parameters] == ["RegionId", "InstanceIds"]
    assert metadata.parameters[0].location == "query"
    assert metadata.parameters[0].style == "repeatList"
    assert metadata.parameters[0].schema["type"] == "string"
    assert metadata.responses["200"]["headers"]["x-acs-request-id"]["schema"]["type"] == "string"
    document_child = metadata.document_components["schemas"]["Instance"]["properties"]["child"]
    validation_child = metadata.validation_components["schemas"]["Instance"]["properties"]["child"]
    assert document_child["$ref"] == "#/components/schemas/Instance"
    assert validation_child["$ref"] == "#/components/schemas/Instance"
    assert raw == before
    with pytest.raises(TypeError):
        metadata.parameters[0].schema["type"] = "number"  # type: ignore[index]


def test_normalize_rpc_api_treats_empty_path_as_root_path() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    raw["path"] = ""

    metadata = normalize_api_metadata(raw)

    assert metadata.style == "RPC"
    assert metadata.pathname == "/"


def test_response_body_type_handles_frozen_schema_annotation_maps() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    raw["responses"]["200"]["schema"] = {
        "type": "object",
        "x-demo": {"shape": "annotation"},
    }

    metadata = normalize_api_metadata(raw)

    assert metadata.response_body_type_for_method("POST") == "json"


@pytest.mark.parametrize(
    ("style", "operation_type", "methods", "expected_method"),
    [
        ("RPC", "read", ["get", " pOsT ", "patch"], "POST"),
        ("ROA", "read", ["post", " head ", "get"], "HEAD"),
        ("ROA", "read", ["patch", "post"], "PATCH"),
        ("ROA", "write", ["delete", " post ", "get"], "POST"),
        ("ROA", None, ["patch", " post "], "POST"),
    ],
)
def test_normalize_api_normalizes_all_methods_and_media_before_selecting_method(
    style: str,
    operation_type: str | None,
    methods: list[str],
    expected_method: str,
) -> None:
    raw = load_fixture("ecs_describe_instances.json")
    raw.update(
        {
            "style": style,
            "methods": methods,
            "consumes": [" Application/JSON ", "TEXT/PLAIN; Charset=UTF-8"],
            "produces": [" Application/Octet-Stream ", "APPLICATION/JSON"],
        }
    )
    if operation_type is None:
        raw.pop("operationType", None)
    else:
        raw["operationType"] = operation_type

    metadata = normalize_api_metadata(raw)

    assert metadata.methods == tuple(method.strip().upper() for method in methods)
    assert metadata.method == expected_method
    assert metadata.consumes == ("application/json", "text/plain; charset=utf-8")
    assert metadata.produces == ("application/octet-stream", "application/json")


def test_normalize_api_ignores_malformed_parameters_with_payload_free_debug_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = load_fixture("ecs_describe_instances.json")
    raw["parameters"] = [
        {"name": "Valid", "in": "query", "schema": {"type": "string"}},
        "DO_NOT_LOG_OBJECT",
        {"name": "DO_NOT_LOG_MISSING", "description": "DO_NOT_LOG_DESCRIPTION"},
        {
            "name": "DO_NOT_LOG_NAME",
            "in": "cookie-secret",
            "description": "DO_NOT_LOG_PAYLOAD",
            "schema": {"type": "string"},
        },
    ]

    with caplog.at_level(logging.DEBUG, logger="iac_code.tools.cloud.aliyun.openmeta"):
        metadata = normalize_api_metadata(raw)

    assert [parameter.name for parameter in metadata.parameters] == ["Valid"]
    assert [parameter.name for parameter in metadata.document_parameters] == ["Valid"]
    assert len(caplog.records) == 3
    assert all("Ignoring malformed OpenMeta parameter" in record.getMessage() for record in caplog.records)
    assert "DO_NOT_LOG" not in caplog.text
    assert "cookie-secret" not in caplog.text


@pytest.mark.parametrize(
    ("method", "responses", "produces", "expected"),
    [
        ("HEAD", {"200": {"schema": {"type": "object"}}}, ["application/json"], "none"),
        ("POST", {"204": {}}, ["application/json"], "none"),
        ("GET", {"200": {"schema": {"type": "object"}}}, ["text/plain"], "json"),
        ("GET", {"200": {"schema": {"type": "string"}}}, [], "string"),
        ("GET", {"200": {"schema": {"type": "string", "format": "binary"}}}, [], "binary"),
        ("GET", {"200": {}}, ["application/octet-stream"], "binary"),
    ],
)
def test_normalize_api_infers_response_body_type_from_method_status_schema_then_media(
    method: str,
    responses: dict[str, object],
    produces: list[str],
    expected: str,
) -> None:
    raw = load_fixture("ecs_describe_instances.json")
    raw.update(
        {
            "style": "ROA",
            "methods": [method],
            "operationType": "read" if method in {"GET", "HEAD"} else "write",
            "responses": responses,
            "produces": produces,
        }
    )

    metadata = normalize_api_metadata(raw)

    assert metadata.response_body_type == expected


def test_normalize_api_treats_sse_scheme_success_response_as_string() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    raw.update(
        {
            "style": "RPC",
            "methods": ["POST"],
            "schemes": ["https", "sse"],
            "responses": {"200": {"schema": {"type": "object"}}},
            "produces": [],
        }
    )

    metadata = normalize_api_metadata(raw)

    assert metadata.response_body_type == "string"


@pytest.mark.parametrize(
    "media_type",
    ["application/pdf", "application/zip", "video/mp4", "application/x-protobuf"],
)
def test_normalize_api_treats_other_success_response_media_as_binary(media_type: str) -> None:
    raw = load_fixture("ecs_describe_instances.json")
    raw.update(
        {
            "style": "ROA",
            "methods": ["GET"],
            "responses": {"200": {}},
            "produces": [media_type],
        }
    )

    metadata = normalize_api_metadata(raw)

    assert metadata.response_body_type == "binary"


def _schema_with_nested_ref(location: str, reference: str) -> dict[str, Any]:
    nested_ref = {"$ref": reference}
    if location == "properties":
        return {"type": "object", "properties": {"child": nested_ref}}
    if location == "items":
        return {"type": "array", "items": nested_ref}
    return {location: [nested_ref]}


def _nested_all_of(depth: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(depth):
        schema = {"allOf": [schema]}
    return schema


@pytest.mark.parametrize("reference", ["#/components/schemas/Missing", "https://example.test/schema"])
@pytest.mark.parametrize("location", ["properties", "items", "allOf", "anyOf", "oneOf", "prefixItems"])
def test_nested_invalid_refs_fail_the_whole_parameter_schema(location: str, reference: str) -> None:
    raw = load_fixture("ecs_describe_instances.json")
    schema = _schema_with_nested_ref(location, reference)
    raw["parameters"] = [{"name": "Nested", "in": "query", "schema": schema}]

    metadata = normalize_api_metadata(raw)

    assert metadata.parameters[0].schema is None
    document_schema = metadata.document_parameters[0].schema
    assert document_schema is not None
    if location == "properties":
        document_ref = document_schema["properties"]["child"]
    elif location == "items":
        document_ref = document_schema["items"]
    else:
        document_ref = document_schema[location][0]
    assert document_ref["$ref"] == reference


@pytest.mark.parametrize(
    "sibling",
    [
        {"allOf": [{"$ref": "https://example.test/schema"}]},
        {"allOf": [{"$ref": "#/components/schemas/Missing"}]},
        {"patternProperties": []},
        {"allOf": [_nested_all_of(33)]},
    ],
    ids=["external-ref", "missing-ref", "invalid-carrier", "depth"],
)
def test_parameter_local_ref_sibling_carriers_fail_closed(sibling: dict[str, Any]) -> None:
    raw = load_fixture("ecs_describe_instances.json")
    reference = "#/components/schemas/Instance"
    schema = {"$ref": reference, **copy.deepcopy(sibling)}
    raw["parameters"] = [{"name": "Sibling", "in": "query", "schema": schema}]

    metadata = normalize_api_metadata(raw)

    assert metadata.document_parameters[0].schema == openmeta_module._freeze(schema)
    assert metadata.parameters[0].schema is None


@pytest.mark.parametrize(
    "sibling",
    [
        {"allOf": [{"$ref": "https://example.test/schema"}]},
        {"allOf": [{"$ref": "#/components/schemas/Missing"}]},
        {"patternProperties": []},
        {"allOf": [_nested_all_of(33)]},
    ],
    ids=["external-ref", "missing-ref", "invalid-carrier", "depth"],
)
def test_response_local_ref_sibling_carriers_fail_closed(sibling: dict[str, Any]) -> None:
    raw = load_fixture("ecs_describe_instances.json")
    schema = {"$ref": "#/components/schemas/Instance", **copy.deepcopy(sibling)}
    raw["responses"] = {"200": {"schema": schema}}

    metadata = normalize_api_metadata(raw)

    assert metadata.responses["200"]["schema"] == openmeta_module._freeze(schema)
    assert metadata.response_schema_references_valid is False


def test_local_ref_sibling_constraints_are_combined_in_execution_schema() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    reference = "#/components/schemas/Instance"
    sibling = {"patternProperties": {"^x-": {"type": "integer"}}}
    schema = {"$ref": reference, **sibling}
    raw["parameters"] = [{"name": "Sibling", "in": "query", "schema": schema}]
    raw["responses"] = {"200": {"schema": schema}}

    metadata = normalize_api_metadata(raw)

    assert metadata.document_parameters[0].schema == schema
    target = metadata.validation_components["schemas"]["Instance"]
    resolved = metadata.parameters[0].schema
    assert resolved is not None
    assert resolved["type"] == target["type"]
    assert resolved["properties"] == target["properties"]
    assert resolved["allOf"] == (sibling,)
    assert metadata.response_schema_references_valid is True


def test_recursive_local_ref_with_sibling_constraints_terminates_and_preserves_both() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    reference = "#/components/schemas/Node"
    recursive_sibling = {"patternProperties": {"^x-": {"type": "string"}}}
    raw["components"]["schemas"]["Node"] = {  # type: ignore[index]
        "type": "object",
        "properties": {
            "child": {"$ref": reference, **recursive_sibling},
        },
    }
    parameter_schema = {"$ref": reference, "allOf": [{"required": ["child"]}]}
    raw["parameters"] = [{"name": "Node", "in": "query", "schema": parameter_schema}]
    raw["responses"] = {"200": {"schema": parameter_schema}}

    metadata = normalize_api_metadata(raw)

    resolved = metadata.parameters[0].schema
    assert resolved is not None
    assert resolved["type"] == "object"
    recursive_child = resolved["properties"]["child"]
    assert recursive_child == {"$ref": reference, "allOf": (recursive_sibling,)}
    assert resolved["allOf"] == ({"allOf": ({"required": ("child",)},)},)
    assert metadata.document_parameters[0].schema == openmeta_module._freeze(parameter_schema)
    assert metadata.response_schema_references_valid is True


def test_schema_validation_distinguishes_legal_null_values_from_invalid_children() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    schema = {
        "type": "object",
        "properties": {"valid": {"type": "string", "default": None, "enum": [None, "ready"]}},
    }
    raw["parameters"] = [{"name": "Valid", "in": "query", "schema": schema}]

    metadata = normalize_api_metadata(raw)

    normalized = metadata.parameters[0].schema
    assert normalized is not None
    valid_property = normalized["properties"]["valid"]
    assert valid_property["default"] is None
    assert valid_property["enum"] == (None, "ready")


def test_schema_views_limit_expansion_and_fail_closed_for_bad_refs() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    nested: dict[str, object] = {"type": "string"}
    for _ in range(33):
        nested = {"type": "object", "properties": {"child": nested}}
    raw["components"]["schemas"]["Deep"] = nested  # type: ignore[index]
    raw["parameters"].append({"name": "deep", "in": "query", "schema": {"$ref": "#/components/schemas/Deep"}})  # type: ignore[index]
    raw["parameters"].append({"name": "missing", "in": "query", "schema": {"$ref": "#/components/schemas/Missing"}})  # type: ignore[index]
    raw["parameters"].append({"name": "external", "in": "query", "schema": {"$ref": "https://example.test/schema"}})  # type: ignore[index]
    raw["parameters"].append(  # type: ignore[index]
        {"name": "invalid-type", "in": "query", "schema": {"type": "object", "properties": {"child": "bad"}}}
    )

    metadata = normalize_api_metadata(raw)

    assert metadata.validation_components["schemas"]["Deep"] is None
    assert metadata.parameters[-4].schema is None
    assert metadata.parameters[-3].schema is None
    assert metadata.parameters[-2].schema is None
    assert metadata.parameters[-1].schema is None
    assert metadata.document_components["schemas"]["Deep"]["properties"]["child"]["properties"]


def test_normalize_product_metadata_handles_products_list() -> None:
    raw = load_fixture("products.json")["products"][0]
    raw["shortName"] = "ecs"
    metadata = normalize_product_metadata(raw)
    assert metadata.product == "Ecs"
    assert metadata.default_version == "2014-05-26"
    assert metadata.versions == ("2014-05-26", "2016-04-28")
    assert metadata.recommended_versions == ("2014-05-26",)
    assert metadata.style == "RPC"
    assert metadata.short_name == "ecs"


@pytest.mark.asyncio
async def test_list_products_reuses_the_normalized_tuple(tmp_path: Path) -> None:
    payload = load_fixture("products.json")
    payload["products"][0]["shortName"] = "ecs"
    payload["products"].append(
        {
            "product": "AiContent",
            "defaultVersion": "2025-01-01",
            "versions": ["2025-01-01"],
        }
    )
    remote = Remote([response(200, payload)])
    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(remote))

    first = await openmeta.list_products()
    second = await openmeta.list_products()

    assert first.value is not None
    assert first.value is second.value
    assert tuple(item.product for item in first.value) == ("Ecs", "FC", "AiContent")
    assert first.value[0].short_name == "ecs"
    assert len(remote.requests) == 1
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_list_products_reports_protocol_error_when_every_product_is_invalid(tmp_path: Path) -> None:
    remote = Remote([response(200, {"products": [{"product": "unsafe/value"}]})])
    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(remote))

    result = await openmeta.list_products()

    assert result.value is None
    assert result.error == "protocol_error"
    assert result.cache_status == "miss"
    await openmeta.aclose()


def test_openmeta_exclusion_entries_preserve_audit_fields(tmp_path: Path) -> None:
    from iac_code.tools.cloud.aliyun.openmeta import load_openmeta_exclusions

    exclusions = tmp_path / "openmeta_exclusions.yml"
    exclusions.write_text(
        """
schema_version: 1
apis:
  Ecs:
    "2014-05-26":
      DescribeInstances:
        reason: target_invalid_api_not_found
        category: openmeta_dirty_api
        observed: OpenMeta exposes the API but live validation returned InvalidApi.NotFound.
        discovered_on: "2026-07-15"
        source: live_validation
        note: Safe read alternatives in the same product-version remain eligible.
""",
        encoding="utf-8",
    )

    entry = load_openmeta_exclusions(exclusions).api_entry("ecs", "2014-05-26", "describeinstances")

    assert entry is not None
    assert entry.reason == "target_invalid_api_not_found"
    assert entry.category == "openmeta_dirty_api"
    assert entry.observed == "OpenMeta exposes the API but live validation returned InvalidApi.NotFound."
    assert entry.discovered_on == "2026-07-15"
    assert entry.source == "live_validation"
    assert entry.note == "Safe read alternatives in the same product-version remain eligible."


@pytest.mark.parametrize(
    ("entry_yaml", "expected_error"),
    [
        (
            '    "2014-05-26": "legacy string reason"',
            "invalid OpenMeta exclusion entry",
        ),
        (
            """
    "2014-05-26":
      reason: target_invalid_api_not_found
      category: openmeta_dirty_api
      observed: OpenMeta exposes the API but live validation returned InvalidApi.NotFound.
      discovered_on: "2026-07-15"
""",
            "missing required OpenMeta exclusion audit field",
        ),
        (
            """
    "2014-05-26":
      reason: "   "
      category: openmeta_dirty_api
      observed: OpenMeta exposes the API but live validation returned InvalidApi.NotFound.
      discovered_on: "2026-07-15"
      source: live_validation
""",
            "missing required OpenMeta exclusion audit field",
        ),
        (
            """
    "2014-05-26":
      reason: target_invalid_api_not_found
      category: openmeta_dirty_api
      observed: OpenMeta exposes the API but live validation returned InvalidApi.NotFound.
      discovered_on: "2026-07-15"
      source: live_validation
      note: "   "
""",
            "invalid OpenMeta exclusion entry",
        ),
    ],
)
def test_openmeta_exclusion_entries_require_audit_fields(
    tmp_path: Path,
    entry_yaml: str,
    expected_error: str,
) -> None:
    from iac_code.tools.cloud.aliyun.openmeta import load_openmeta_exclusions

    exclusions = tmp_path / "openmeta_exclusions.yml"
    exclusions.write_text(
        f"""
schema_version: 1
versions:
  Ecs:
{entry_yaml}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=expected_error):
        load_openmeta_exclusions(exclusions)


def test_default_openmeta_exclusions_mark_small_surface_versions_as_transitional() -> None:
    from iac_code.tools.cloud.aliyun.openmeta import load_openmeta_exclusions

    exclusions = load_openmeta_exclusions()

    entry = exclusions.version_entry("BPStudio", "2020-07-10")
    assert entry is not None
    assert entry.reason == "transitional_small_surface"
    assert entry.category == "openmeta_transitional_small_surface"


def test_default_openmeta_exclusions_include_only_reviewed_exact_live_validation_ignores() -> None:
    from iac_code.tools.cloud.aliyun.openmeta import load_openmeta_exclusions

    exclusions = load_openmeta_exclusions()

    expected_exact_versions = {
        ("AIMath", "2024-11-14"): "openmeta_service_unavailable",
        ("ARMS", "2018-12-19"): "openmeta_deprecated_or_obsolete_version",
        ("ARMS", "2019-02-19"): "openmeta_deprecated_or_obsolete_version",
        ("ARMS", "2021-04-22"): "openmeta_deprecated_or_obsolete_version",
        ("AgentRetailVision", "2026-05-06"): "openmeta_service_unavailable",
        ("Dytnsapi", "2023-01-01"): "openmeta_service_unavailable",
        ("IaCService", "2021-07-22"): "openmeta_deprecated_or_obsolete_version",
        ("Iot", "2016-01-04"): "openmeta_deprecated_or_obsolete_version",
        ("Iot", "2016-05-30"): "openmeta_service_unavailable",
        ("pds", "2020-03-20"): "openmeta_deprecated_or_obsolete_version",
        ("linkedmall", "2023-09-30"): "openmeta_service_unavailable",
        ("rds-data", "2022-03-30"): "openmeta_unrequestable_version",
        ("rtc-white-board", "2020-12-14"): "openmeta_service_unavailable",
        ("Searchplat", "2024-05-29"): "openmeta_unrequestable_version",
    }
    for identity, expected_category in expected_exact_versions.items():
        entry = exclusions.version_entry(*identity)
        assert entry is not None
        assert entry.category == expected_category

    assert not exclusions.version_excluded("ARMS", "2019-08-08")
    assert not exclusions.version_excluded("ARMS", "2021-05-19")
    assert not exclusions.version_excluded("airticketOpen", "2023-01-17")
    assert not exclusions.version_excluded("btripOpen", "2022-05-20")
    assert not exclusions.version_excluded("Cloudauth", "2022-11-25")
    assert not exclusions.version_excluded("CarbonFootprint", "2023-07-11")
    assert not exclusions.version_excluded("Iot", "2018-01-20")
    assert not exclusions.version_excluded("hcs-mgw", "2024-06-26")
    assert not exclusions.version_excluded("ivpd", "2019-06-25")
    assert not exclusions.version_excluded("Searchplat", "2024-04-01")
    assert not exclusions.version_excluded("linkedmall", "2021-01-01")
    assert not exclusions.version_excluded("rds-data", "2022-04-01")
    assert not exclusions.version_excluded("safconsole", "2021-01-12")
    assert not exclusions.version_excluded("safconsole", "2025-05-21")
    assert not exclusions.version_excluded("TrafficFxOpen", "2024-08-15")
    assert not exclusions.version_excluded("Yike", "2026-03-19")


@pytest.mark.asyncio
async def test_client_ignores_bundled_small_surface_versions_without_fetching_api_metadata(
    tmp_path: Path,
) -> None:
    products = {
        "products": [
            {
                "code": "BPStudio",
                "defaultVersion": "2020-07-10",
                "versions": ["2020-07-10", "2021-09-31"],
                "recommendVersions": ["2020-07-10", "2021-09-31"],
                "style": "RPC",
            }
        ]
    }
    excluded_api = load_fixture("ecs_describe_instances.json")
    excluded_api.update(
        {
            "product": "BPStudio",
            "version": "2020-07-10",
            "action": "GetDeployDetail",
        }
    )
    remote = Remote([response(200, products), response(200, excluded_api)])
    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(remote))

    product = await openmeta.get_product("bpstudio")
    api = await openmeta.get_api("BPStudio", "2020-07-10", "GetDeployDetail")
    fallback_api = await openmeta.get_api_for_version_selection("BPStudio", "2020-07-10", "GetDeployDetail")

    assert product.value is not None
    assert product.value.versions == ("2021-09-31",)
    assert product.value.default_version is None
    assert product.value.recommended_versions == ("2021-09-31",)
    assert product.value.first_class_excluded_versions == ("2020-07-10",)
    assert product.value.second_class_excluded_versions == ()
    assert api.value is None
    assert api.error == "not_found"
    assert api.cache_status == "negative_hit"
    assert fallback_api.value is not None
    assert fallback_api.value.version == "2020-07-10"
    assert [request.url.path for request in remote.requests] == [
        "/meta/v1/products.json",
        "/meta/v1/products/BPStudio/versions/2020-07-10/apis/GetDeployDetail/api.json",
    ]
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_version_policy_miss_does_not_hide_real_negative_cache_for_selection_lookup(tmp_path: Path) -> None:
    remote = Remote([response(404)])
    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(remote))

    policy_miss = await openmeta.get_api("BPStudio", "2020-07-10", "GetDeployDetail")
    first_selection = await openmeta.get_api_for_version_selection("BPStudio", "2020-07-10", "GetDeployDetail")
    second_selection = await openmeta.get_api_for_version_selection("BPStudio", "2020-07-10", "GetDeployDetail")

    assert policy_miss.error == "not_found"
    assert policy_miss.cache_status == "negative_hit"
    assert first_selection.error == "not_found"
    assert first_selection.cache_status == "miss"
    assert second_selection.error == "not_found"
    assert second_selection.cache_status == "negative_hit"
    assert len(remote.requests) == 1
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_skips_configured_excluded_product_without_fetching_products(tmp_path: Path) -> None:
    exclusions = tmp_path / "openmeta_exclusions.yml"
    exclusions.write_text(
        """
schema_version: 1
products:
  AiContent:
    reason: invalid_version_format
    category: openmeta_dirty_product
    observed: OpenMeta exposes a non-date default version.
    discovered_on: "2026-07-15"
    source: test_fixture
    note: OpenMeta exposes a non-date default version.
""",
        encoding="utf-8",
    )
    remote = Remote([])
    openmeta = OpenMetaClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(remote),
        exclusions_path=exclusions,
    )

    result = await openmeta.get_product("aicontent")

    assert result.value is None
    assert result.error == "not_found"
    assert result.cache_status == "negative_hit"
    assert remote.requests == []
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_filters_configured_excluded_versions_from_product_metadata(tmp_path: Path) -> None:
    exclusions = tmp_path / "openmeta_exclusions.yml"
    exclusions.write_text(
        """
schema_version: 1
versions:
  Ecs:
    "2016-04-28":
      reason: version_unavailable
      category: openmeta_unrequestable_version
      observed: Version is exposed by OpenMeta but cannot be executed.
      discovered_on: "2026-07-15"
      source: test_fixture
      note: Version is exposed by OpenMeta but cannot be executed.
""",
        encoding="utf-8",
    )
    remote = Remote([response(200, load_fixture("products.json"))])
    openmeta = OpenMetaClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(remote),
        exclusions_path=exclusions,
    )

    result = await openmeta.get_product("ecs")

    assert result.value is not None
    assert result.value.versions == ("2014-05-26",)
    assert result.value.default_version == "2014-05-26"
    assert result.value.recommended_versions == ("2014-05-26",)
    assert result.value.first_class_excluded_versions == ()
    assert result.value.second_class_excluded_versions == ("2016-04-28",)
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_only_classifies_the_actual_unique_official_version_as_first_class(tmp_path: Path) -> None:
    exclusions = tmp_path / "openmeta_exclusions.yml"
    exclusions.write_text(
        """
schema_version: 1
versions:
  Example:
    "2025-01-01":
      reason: unavailable
      category: test
      observed: The only official version is excluded.
      discovered_on: "2026-07-18"
      source: test_fixture
      note: The only official version is excluded.
    "2024-01-01":
      reason: historical
      category: test
      observed: Historical configuration entry absent from current metadata.
      discovered_on: "2026-07-18"
      source: test_fixture
      note: Historical configuration entry absent from current metadata.
""",
        encoding="utf-8",
    )
    products = {
        "products": [
            {
                "code": "Example",
                "defaultVersion": "2025-01-01",
                "versions": ["2025-01-01"],
                "recommendVersions": [],
                "style": "RPC",
            }
        ]
    }
    openmeta = OpenMetaClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(Remote([response(200, products)])),
        exclusions_path=exclusions,
    )

    result = await openmeta.get_product("example")

    assert result.value is not None
    assert result.value.first_class_excluded_versions == ("2025-01-01",)
    assert result.value.second_class_excluded_versions == ("2024-01-01",)
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_skips_configured_excluded_api_without_fetching_metadata(tmp_path: Path) -> None:
    exclusions = tmp_path / "openmeta_exclusions.yml"
    exclusions.write_text(
        """
schema_version: 1
apis:
  Ecs:
    "2014-05-26":
      DescribeInstances:
        reason: api_unavailable
        category: openmeta_dirty_api
        observed: Exact API metadata is known bad for live validation.
        discovered_on: "2026-07-15"
        source: test_fixture
        note: Exact API metadata is known bad for live validation.
""",
        encoding="utf-8",
    )
    remote = Remote([])
    openmeta = OpenMetaClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(remote),
        exclusions_path=exclusions,
    )

    result = await openmeta.get_api("ecs", "2014-05-26", "describeinstances")
    selection_result = await openmeta.get_api_for_version_selection("ecs", "2014-05-26", "describeinstances")

    assert result.value is None
    assert result.error == "not_found"
    assert result.cache_status == "negative_hit"
    assert selection_result.value is None
    assert selection_result.error == "not_found"
    assert selection_result.cache_status == "negative_hit"
    assert remote.requests == []
    await openmeta.aclose()


def test_normalize_api_uses_product_style_when_api_style_is_omitted() -> None:
    product = normalize_product_metadata(load_fixture("products.json")["products"][0])
    raw = load_fixture("ecs_describe_instances.json")
    raw.pop("style")

    metadata = normalize_api_metadata(raw, product_style=product.style)

    assert metadata.style == "RPC"


def test_normalize_api_infers_roa_from_official_non_root_path_when_product_style_is_missing() -> None:
    raw = load_fixture("fc_roa.json")
    for field in ("product", "version", "action", "style"):
        raw.pop(field)

    metadata = normalize_api_metadata(
        raw,
        identity=("FC", "2023-03-30", "GetFunction"),
    )

    assert metadata.style == "ROA"
    assert metadata.pathname == "/2023-03-30/functions/{functionName}"


def test_normalize_api_infers_rpc_from_missing_style_and_omitted_path_when_identity_is_known() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    for field in ("product", "version", "action", "style", "path"):
        raw.pop(field, None)

    metadata = normalize_api_metadata(
        raw,
        identity=("Dms", "2025-04-14", "ListDataLakePartition"),
    )

    assert metadata.style == "RPC"
    assert metadata.pathname == "/"
    assert metadata.method == "POST"


def test_normalize_api_prefers_exact_rpc_shape_over_product_roa_style_when_path_is_omitted() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    for field in ("product", "version", "action", "style", "path"):
        raw.pop(field, None)
    raw["methods"] = ["get"]

    metadata = normalize_api_metadata(
        raw,
        product_style="ROA",
        identity=("cr", "2018-12-01", "ListScanRule"),
    )

    assert metadata.style == "RPC"
    assert metadata.pathname == "/"
    assert metadata.method == "GET"


def test_normalize_api_reads_required_document_fields_from_official_parameter_schema() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    parameter = raw["parameters"][0]
    schema = parameter["schema"]
    expected = {field: parameter[field] for field in ("required", "description", "example")}
    for field in ("required", "description", "example"):
        schema[field] = parameter.pop(field)

    metadata = normalize_api_metadata(raw)

    region_id = metadata.parameters[0]
    assert region_id.required is True
    assert region_id.description == expected["description"]
    assert region_id.example == expected["example"]


def test_normalize_api_ignores_parameter_with_unknown_path_encoding() -> None:
    raw = load_fixture("ecs_describe_instances.json")
    raw["parameters"] = [
        {
            "name": "UnsafePath",
            "in": "path",
            "pathEncoding": "whole-path",
            "schema": {"type": "string"},
        },
        {"name": "SafePath", "in": "path", "pathEncoding": "segment", "schema": {"type": "string"}},
    ]

    metadata = normalize_api_metadata(raw)

    assert [parameter.name for parameter in metadata.parameters] == ["SafePath"]
    assert metadata.parameters[0].path_encoding == "segment"


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 11, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, value: timedelta) -> None:
        self.value += value


class Remote:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def response(status_code: int, payload: object | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=payload, headers=headers)


def client(tmp_path: Path, remote: Remote, clock: FakeClock) -> OpenMetaClient:
    return OpenMetaClient(cache_dir=tmp_path, clock=clock, transport=httpx.MockTransport(remote), exclusions_path=None)


def write_cache_envelope(path: Path, payload: dict[str, object], clock: FakeClock) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fetched_at": clock().isoformat(),
                "source_url": "https://api.aliyun.com/meta/v1/fixture",
                "payload_sha256": hashlib.sha256(encoded).hexdigest(),
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_client_accepts_official_top_level_product_array_and_normalizes_code(tmp_path: Path) -> None:
    official_payload = [
        {
            "code": "Ecs",
            "defaultVersion": "2014-05-26",
            "versions": ["2014-05-26"],
            "recommendVersions": ["2014-05-26"],
            "style": "RPC",
        }
    ]
    remote = Remote([response(200, official_payload)])
    openmeta = client(tmp_path, remote, FakeClock())

    result = await openmeta.get_product("ecs")

    assert result.value is not None
    assert result.value.product == "Ecs"
    assert result.value.default_version == "2014-05-26"
    assert result.cache_status == "remote"
    envelope = json.loads((tmp_path / "products.zh-cn.json").read_text(encoding="utf-8"))
    assert envelope["payload"]["products"] == official_payload
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_official_product_array_accepts_safe_opaque_version_identifiers(tmp_path: Path) -> None:
    official_payload = [
        {"code": "Ecs", "defaultVersion": "2014-05-26", "style": "RPC"},
        {"code": "FC", "defaultVersion": "2023-03-30", "style": "", "versions": ["2023-03-30"]},
        {
            "code": "AliGenie",
            "defaultVersion": "iap_1.0",
            "style": "RPC",
            "versions": ["iap_1.0", "ssp_1.0"],
            "recommendVersions": ["iap_1.0"],
        },
    ]
    remote = Remote([response(200, official_payload)])
    openmeta = client(tmp_path, remote, FakeClock())

    valid = await openmeta.get_product("Ecs")
    blank_style = await openmeta.get_product("FC")
    semantic_version = await openmeta.get_product("AliGenie")

    assert valid.value is not None and valid.value.default_version == "2014-05-26"
    assert blank_style.value is not None and blank_style.value.style is None
    assert semantic_version.value is not None
    assert semantic_version.value.product == "AliGenie"
    assert semantic_version.value.default_version == "iap_1.0"
    assert semantic_version.value.versions == ("iap_1.0", "ssp_1.0")
    assert semantic_version.value.recommended_versions == ("iap_1.0",)
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_api_request_identity_completes_official_payload_across_all_cache_paths(tmp_path: Path) -> None:
    clock = FakeClock()
    api_payload = load_fixture("ecs_describe_instances.json")
    for field in ("product", "version", "action", "style", "path"):
        api_payload.pop(field)
    first = client(
        tmp_path,
        Remote([response(200, load_fixture("products.json")), response(200, api_payload)]),
        clock,
    )

    assert (await first.get_product("Ecs")).value is not None
    fresh = await first.get_api("Ecs", "2014-05-26", "DescribeInstances")
    assert fresh.value is not None
    memory = await first.get_api("Ecs", "2014-05-26", "DescribeInstances")

    for result in (fresh, memory):
        assert result.value is not None
        assert (result.value.product, result.value.version, result.value.action) == (
            "Ecs",
            "2014-05-26",
            "DescribeInstances",
        )
        assert result.value.style == "RPC"
        assert result.value.pathname == "/"
    assert fresh.cache_status == "remote"
    assert memory.cache_status == "memory_fresh"
    await first.aclose()

    disk_client = client(tmp_path, Remote([]), clock)
    assert (await disk_client.get_product("Ecs")).value is not None
    disk = await disk_client.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert disk.value is not None
    assert (disk.value.product, disk.value.version, disk.value.action) == (
        "Ecs",
        "2014-05-26",
        "DescribeInstances",
    )
    assert disk.value.style == "RPC"
    assert disk.value.pathname == "/"
    assert disk.cache_status == "disk_fresh"
    await disk_client.aclose()

    clock.advance(timedelta(days=8))
    stale_client = client(tmp_path, Remote([response(500), response(500)]), clock)
    assert (await stale_client.get_product("Ecs")).value is not None
    stale = await stale_client.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert stale.value is not None
    assert (stale.value.product, stale.value.version, stale.value.action) == (
        "Ecs",
        "2014-05-26",
        "DescribeInstances",
    )
    assert stale.value.style == "RPC"
    assert stale.value.pathname == "/"
    assert stale.cache_status == "disk_stale"
    await stale_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product", "FC"),
        ("version", "2023-03-30"),
        ("action", "DescribeRegions"),
        ("action", "../DescribeInstances"),
    ],
)
async def test_api_rejects_mismatched_or_invalid_explicit_identity_without_caching(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    invalid = load_fixture("ecs_describe_instances.json")
    invalid[field] = value
    valid = load_fixture("ecs_describe_instances.json")
    remote = Remote([response(200, invalid), response(200, valid)])
    openmeta = client(tmp_path, remote, FakeClock())

    rejected = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert rejected.value is None
    assert rejected.error == "protocol_error"
    cache_file = tmp_path / "apis" / "Ecs" / "2014-05-26" / "DescribeInstances.json"
    assert not cache_file.exists()

    retried = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")
    assert retried.value is not None
    assert len(remote.requests) == 2
    await openmeta.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parameters", {}),
        ("components", []),
        ("responses", []),
        ("errorCodes", []),
        ("changeSet", {}),
        ("staticInfo", []),
        ("security", {}),
    ],
)
async def test_api_rejects_invalid_top_level_field_types_without_caching(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    invalid = load_fixture("ecs_describe_instances.json")
    invalid[field] = value
    valid = load_fixture("ecs_describe_instances.json")
    remote = Remote([response(200, invalid), response(200, valid)])
    openmeta = client(tmp_path, remote, FakeClock())

    rejected = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert rejected.value is None
    assert rejected.error == "protocol_error"
    cache_file = tmp_path / "apis" / "Ecs" / "2014-05-26" / "DescribeInstances.json"
    assert not cache_file.exists()

    retried = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")
    assert retried.value is not None
    assert len(remote.requests) == 2
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_applies_fetched_product_style_to_api_without_style(tmp_path: Path) -> None:
    api_payload = load_fixture("ecs_describe_instances.json")
    api_payload.pop("style")
    remote = Remote([response(200, load_fixture("products.json")), response(200, api_payload)])
    openmeta = client(tmp_path, remote, FakeClock())

    product = await openmeta.get_product("Ecs")
    api = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert product.value is not None
    assert product.value.style == "RPC"
    assert api.value is not None
    assert api.value.style == "RPC"
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_uses_version_docs_style_for_ambiguous_exact_api(tmp_path: Path) -> None:
    api_payload = load_fixture("ecs_describe_instances.json")
    for field in ("product", "version", "action", "style"):
        api_payload.pop(field, None)
    api_payload["methods"] = ["GET"]
    api_payload["path"] = "/"
    api_docs = {
        "info": {"product": "Oss", "version": "2019-05-17", "style": "ROA"},
        "apis": {},
    }
    remote = Remote([response(200, api_payload), response(200, api_docs)])
    openmeta = client(tmp_path, remote, FakeClock())

    api = await openmeta.get_api("Oss", "2019-05-17", "ListBuckets")

    assert api.value is not None
    assert api.value.style == "ROA"
    assert api.value.method == "GET"
    assert remote.requests[1].url.path.endswith("/Oss/versions/2019-05-17/api-docs.json")
    await openmeta.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_outcome", ["without_style", "unavailable"])
async def test_client_product_style_fallback_expires_with_products_cache_envelope(
    tmp_path: Path,
    refresh_outcome: str,
) -> None:
    clock = FakeClock()
    first_api = load_fixture("ecs_describe_instances.json")
    first_api.pop("style")
    second_api = copy.deepcopy(first_api)
    second_api["action"] = "DescribeRegions"
    if refresh_outcome == "without_style":
        refreshed_products = load_fixture("products.json")
        refreshed_products["products"][0].pop("style")  # type: ignore[index,union-attr]
        refresh_response = response(200, refreshed_products)
        elapsed = timedelta(hours=24, microseconds=1)
    else:
        refresh_response = response(500)
        elapsed = timedelta(days=31)
    remote = Remote(
        [
            response(200, load_fixture("products.json")),
            response(200, first_api),
            refresh_response,
            response(200, second_api),
            response(404),
        ]
    )
    openmeta = client(tmp_path, remote, clock)

    assert (await openmeta.get_product("Ecs")).value.style == "RPC"
    assert (await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")).value is not None
    clock.advance(elapsed)
    refreshed = await openmeta.get_product("Ecs")
    after_expiry = await openmeta.get_api("Ecs", "2014-05-26", "DescribeRegions")

    if refresh_outcome == "without_style":
        assert refreshed.value is not None and refreshed.value.style is None
    else:
        assert refreshed.error == "temporarily_unavailable"
    assert after_expiry.value is None
    assert after_expiry.error == "protocol_error"
    assert len(remote.requests) == 5
    assert remote.requests[-1].url.path.endswith("/Ecs/versions/2014-05-26/api-docs.json")
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_uses_exact_anonymous_api_url_and_headers(tmp_path: Path) -> None:
    clock = FakeClock()
    remote = Remote([response(200, load_fixture("ecs_describe_instances.json"))])
    openmeta = client(tmp_path, remote, clock)

    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.value is not None and result.source == "fresh"
    assert result.cache_status == "remote"
    request = remote.requests[0]
    assert (
        str(request.url)
        == "https://api.aliyun.com/meta/v1/products/Ecs/versions/2014-05-26/apis/DescribeInstances/api.json?language=ZH_CN"
    )
    assert request.headers["user-agent"] == build_user_agent()
    assert "authorization" not in request.headers
    assert "x-acs-accesskey-id" not in request.headers
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_exact_api_uses_version_components_for_response_refs(tmp_path: Path) -> None:
    clock = FakeClock()
    api_payload = {
        "style": "ROA",
        "path": "/api/v1/instances/{InstanceId}",
        "methods": ["get"],
        "schemes": ["https"],
        "security": [{"AK": []}],
        "operationType": "read",
        "parameters": [
            {
                "name": "InstanceId",
                "in": "path",
                "schema": {"type": "string", "required": True},
            }
        ],
        "responses": {
            "200": {
                "schema": {
                    "type": "object",
                    "properties": {"UserVpc": {"$ref": "#/components/schemas/UserVpc"}},
                }
            }
        },
    }
    api_docs_payload = {
        "version": "1.0",
        "info": {"style": "ROA", "product": "pai-dsw", "version": "2021-02-26"},
        "components": {
            "schemas": {
                "UserVpc": {
                    "type": "object",
                    "properties": {"VpcId": {"type": "string"}},
                }
            }
        },
        "apis": {},
    }
    remote = Remote([response(200, api_payload), response(200, api_docs_payload)])
    openmeta = client(tmp_path, remote, clock)

    result = await openmeta.get_api("pai-dsw", "2021-02-26", "GetInstance")

    assert result.value is not None
    assert result.value.response_schema_references_valid is True
    assert result.value.document_components["schemas"]["UserVpc"]["properties"]["VpcId"]["type"] == "string"
    assert len(remote.requests) == 2
    assert (
        str(remote.requests[1].url)
        == "https://api.aliyun.com/meta/v1/products/pai-dsw/versions/2021-02-26/api-docs.json?language=ZH_CN"
    )
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_prefetched_api_docs_serve_actions_and_authoritative_misses_without_exact_requests(
    tmp_path: Path,
) -> None:
    describe_instances = load_fixture("ecs_describe_instances.json")
    describe_regions = copy.deepcopy(describe_instances)
    describe_regions["action"] = "DescribeRegions"
    describe_regions.pop("style", None)
    describe_regions.pop("path", None)
    api_docs_payload = {
        "info": {"style": "RPC", "product": "Ecs", "version": "2014-05-26"},
        "components": describe_instances.pop("components"),
        "apis": {
            "DescribeInstances": describe_instances,
            "DescribeRegions": describe_regions,
        },
    }
    remote = Remote([response(200, api_docs_payload)])
    openmeta = client(tmp_path, remote, FakeClock())

    openmeta.prefetch_api_docs("Ecs", ("2014-05-26",))
    await asyncio.gather(*tuple(openmeta._prefetch_tasks.values()))
    await asyncio.sleep(0)

    found = await openmeta.get_api("Ecs", "2014-05-26", "DescribeRegions")
    missing = await openmeta.get_api("Ecs", "2014-05-26", "RunInstances")

    assert found.value is not None and found.value.action == "DescribeRegions"
    assert found.value.style == "RPC" and found.value.pathname == "/"
    assert found.cache_status == "memory_fresh"
    assert missing.value is None and missing.error == "not_found"
    assert len(remote.requests) == 1
    assert remote.requests[0].url.path.endswith("/Ecs/versions/2014-05-26/api-docs.json")
    assert openmeta.prefetch_size == 0
    assert all(key.resource != "api" for key in openmeta._memory)
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_prefetched_api_docs_merge_top_level_components_into_the_action(tmp_path: Path) -> None:
    api_payload = {
        "style": "ROA",
        "path": "/api/v1/instances/{InstanceId}",
        "methods": ["get"],
        "schemes": ["https"],
        "security": [{"AK": []}],
        "operationType": "read",
        "parameters": [
            {
                "name": "InstanceId",
                "in": "path",
                "schema": {"type": "string", "required": True},
            }
        ],
        "responses": {
            "200": {
                "schema": {
                    "type": "object",
                    "properties": {"UserVpc": {"$ref": "#/components/schemas/UserVpc"}},
                }
            }
        },
    }
    api_docs_payload = {
        "info": {"style": "ROA", "product": "pai-dsw", "version": "2021-02-26"},
        "components": {
            "schemas": {
                "UserVpc": {
                    "type": "object",
                    "properties": {"VpcId": {"type": "string"}},
                }
            }
        },
        "apis": {"GetInstance": api_payload},
    }
    remote = Remote([response(200, api_docs_payload)])
    openmeta = client(tmp_path, remote, FakeClock())

    openmeta.prefetch_api_docs("pai-dsw", ("2021-02-26",))
    await asyncio.gather(*tuple(openmeta._prefetch_tasks.values()))
    result = await openmeta.get_api("pai-dsw", "2021-02-26", "GetInstance")

    assert result.value is not None
    assert result.value.response_schema_references_valid is True
    assert result.value.document_components["schemas"]["UserVpc"]["properties"]["VpcId"]["type"] == "string"
    assert len(remote.requests) == 1
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_stale_prefetched_api_docs_are_a_positive_fallback_for_exact_metadata_outage(tmp_path: Path) -> None:
    clock = FakeClock()
    describe_regions = load_fixture("ecs_describe_instances.json")
    describe_regions["action"] = "DescribeRegions"
    api_docs_payload = {
        "info": {"style": "RPC", "product": "Ecs", "version": "2014-05-26"},
        "components": describe_regions.pop("components"),
        "apis": {"DescribeRegions": describe_regions},
    }
    remote = Remote([response(200, api_docs_payload), response(503)])
    openmeta = client(tmp_path, remote, clock)

    openmeta.prefetch_api_docs("Ecs", ("2014-05-26",))
    await asyncio.gather(*tuple(openmeta._prefetch_tasks.values()))
    clock.advance(timedelta(days=8))
    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeRegions")

    assert result.value is not None and result.value.action == "DescribeRegions"
    assert result.source == "stale_cache"
    assert result.cache_status == "disk_stale"
    assert len(remote.requests) == 2
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_api_docs_prefetch_has_bounded_concurrency(tmp_path: Path) -> None:
    active = 0
    peak = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        version = request.url.path.split("/")[-2]
        active += 1
        peak = max(peak, active)
        if peak == 3:
            started.set()
        try:
            await release.wait()
            return response(
                200,
                {
                    "info": {"product": "Example", "version": version},
                    "components": {"schemas": {}},
                    "apis": {},
                },
            )
        finally:
            active -= 1

    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    versions = tuple(f"2026-01-{day:02d}" for day in range(1, 7))
    openmeta.prefetch_api_docs("Example", versions)
    tasks = tuple(openmeta._prefetch_tasks.values())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert peak == 3
        assert active == 3
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert peak == 3
        assert openmeta.prefetch_size == 0
    finally:
        release.set()
        await openmeta.aclose()


@pytest.mark.asyncio
async def test_speculative_api_docs_prefetches_are_disk_only(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        version = request.url.path.split("/")[-2]
        return response(
            200,
            {
                "info": {"product": "Example", "version": version},
                "components": {"schemas": {}},
                "apis": {},
            },
        )

    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler), exclusions_path=None)
    openmeta.prefetch_api_docs("Example", ("2026-01-01", "2026-01-02"))
    await asyncio.gather(*tuple(openmeta._prefetch_tasks.values()))

    memory_docs = {key.version for key in openmeta._memory if key.resource == "api_docs"}
    assert memory_docs == {"2026-01-01"}
    assert (tmp_path / "api-docs" / "Example" / "2026-01-01.json").exists()
    assert (tmp_path / "api-docs" / "Example" / "2026-01-02.json").exists()
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_background_response_budget_limits_prefetches_by_reserved_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openmeta_module, "_MAX_API_DOCS_RESPONSE_BYTES", 1024)
    monkeypatch.setattr(openmeta_module, "_MAX_BACKGROUND_INFLIGHT_BYTES", 2048)
    active = 0
    peak = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        version = request.url.path.split("/")[-2]
        active += 1
        peak = max(peak, active)
        if peak == 2:
            started.set()
        try:
            await release.wait()
            return response(200, {"info": {"product": "Example", "version": version}, "apis": {}})
        finally:
            active -= 1

    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler), exclusions_path=None)
    openmeta.prefetch_api_docs("Example", tuple(f"2026-02-{day:02d}" for day in range(1, 5)))
    tasks = tuple(openmeta._prefetch_tasks.values())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert peak == 2
        assert openmeta.background_inflight_bytes == 2048
        release.set()
        await asyncio.gather(*tasks)
        assert openmeta.background_inflight_bytes == 0
    finally:
        release.set()
        await openmeta.aclose()


@pytest.mark.asyncio
async def test_aclose_cancels_api_docs_prefetch_and_its_singleflight_refresh(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    openmeta.prefetch_api_docs("Ecs", ("2014-05-26",))
    await asyncio.wait_for(started.wait(), timeout=1)

    await openmeta.aclose()

    assert cancelled.is_set()
    assert openmeta.prefetch_size == 0
    assert openmeta.singleflight_size == 0
    assert openmeta.background_inflight_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (200, load_fixture("error_envelopes.json")["api_not_found"], "not_found"),
        (204, None, "not_found"),
        (404, None, "not_found"),
        (429, None, "temporarily_unavailable"),
        (500, None, "temporarily_unavailable"),
        (200, load_fixture("error_envelopes.json")["unknown_error"], "protocol_error"),
    ],
)
async def test_client_classifies_semantic_and_http_errors(
    tmp_path: Path, status: int, payload: object, error: str
) -> None:
    clock = FakeClock()
    remote = Remote([response(status, payload)])
    openmeta = client(tmp_path, remote, clock)

    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.value is None
    assert result.error == error
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_rejects_invalid_json_and_unsafe_redirects(tmp_path: Path) -> None:
    clock = FakeClock()
    remote = Remote(
        [
            httpx.Response(200, content=b"not-json"),
            httpx.Response(302, headers={"location": "https://evil.example/meta"}),
        ]
    )
    openmeta = client(tmp_path, remote, clock)

    invalid_json = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")
    redirected = await openmeta.get_api("Ecs", "2014-05-26", "DescribeRegions")

    assert invalid_json.error == "temporarily_unavailable"
    assert redirected.error == "temporarily_unavailable"
    assert len(remote.requests) == 2
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_never_follows_cross_host_redirect_even_if_next_hop_returns_to_official_host(
    tmp_path: Path,
) -> None:
    remote = Remote(
        [
            httpx.Response(302, headers={"location": "https://evil.example/forward"}),
            httpx.Response(302, headers={"location": "https://api.aliyun.com/meta/v1/final"}),
            response(200, load_fixture("ecs_describe_instances.json")),
        ]
    )
    openmeta = client(tmp_path, remote, FakeClock())

    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.error == "temporarily_unavailable"
    assert [request.url.host for request in remote.requests] == ["api.aliyun.com"]
    await openmeta.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "http://api.aliyun.com/meta/v1/final",
        "https://user@api.aliyun.com/meta/v1/final",
        "https://api.aliyun.com:444/meta/v1/final",
    ],
)
async def test_client_rejects_unsafe_redirect_variants(tmp_path: Path, location: str) -> None:
    clock = FakeClock()
    openmeta = client(tmp_path, Remote([httpx.Response(302, headers={"location": location})]), clock)

    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.error == "temporarily_unavailable"
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_allows_only_same_origin_https_redirects(tmp_path: Path) -> None:
    clock = FakeClock()
    remote = Remote(
        [
            httpx.Response(302, headers={"location": "/meta/v1/final"}),
            response(200, load_fixture("ecs_describe_instances.json")),
        ]
    )
    openmeta = client(tmp_path, remote, clock)

    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.value is not None
    assert [request.url.host for request in remote.requests] == ["api.aliyun.com", "api.aliyun.com"]
    assert [request.url.scheme for request in remote.requests] == ["https", "https"]
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_accepts_exactly_two_safe_redirects(tmp_path: Path) -> None:
    clock = FakeClock()
    remote = Remote(
        [
            httpx.Response(302, headers={"location": "/meta/v1/first"}),
            httpx.Response(307, headers={"location": "/meta/v1/second"}),
            response(200, load_fixture("ecs_describe_instances.json")),
        ]
    )
    openmeta = client(tmp_path, remote, clock)

    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.value is not None
    assert len(remote.requests) == 3
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_rejects_a_third_safe_redirect(tmp_path: Path) -> None:
    clock = FakeClock()
    remote = Remote(
        [
            httpx.Response(302, headers={"location": "/meta/v1/first"}),
            httpx.Response(307, headers={"location": "/meta/v1/second"}),
            httpx.Response(308, headers={"location": "/meta/v1/third"}),
        ]
    )
    openmeta = client(tmp_path, remote, clock)

    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.error == "temporarily_unavailable"
    assert len(remote.requests) == 3
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_classifies_timeout_as_temporarily_unavailable(tmp_path: Path) -> None:
    clock = FakeClock()
    openmeta = client(tmp_path, Remote([httpx.ReadTimeout("timed out")]), clock)

    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.error == "temporarily_unavailable"
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_streamed_response_without_content_length_stops_at_resource_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(load_fixture("ecs_describe_instances.json")).encode()
    monkeypatch.setattr(openmeta_module, "_MAX_API_RESPONSE_BYTES", len(payload) - 1)

    async def handler(_request: httpx.Request) -> httpx.Response:
        midpoint = len(payload) // 2
        return httpx.Response(200, stream=ChunkedStream((payload[:midpoint], payload[midpoint:])))

    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler), exclusions_path=None)
    result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.value is None
    assert result.error == "protocol_error"
    assert not (tmp_path / "apis" / "Ecs" / "2014-05-26" / "DescribeInstances.json").exists()
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_memory_cache_uses_lru_entry_and_deep_weight_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openmeta_module, "_MAX_MEMORY_ENTRIES", 2)
    monkeypatch.setattr(openmeta_module, "_MAX_MEMORY_BYTES", 1024 * 1024)
    clock = FakeClock()
    openmeta = OpenMetaClient(
        cache_dir=tmp_path,
        clock=clock,
        transport=httpx.MockTransport(lambda _: response(500)),
    )
    keys = tuple(openmeta_module._CacheKey("api", "Example", "2026-01-01", f"Action{index}") for index in range(3))
    cached = tuple(
        openmeta_module._CachedPayload(
            fetched_at=clock(),
            source_url="https://api.aliyun.com/meta/v1/fixture",
            payload={"value": str(index) * 128},
        )
        for index in range(3)
    )

    assert openmeta._memory_put(keys[0], cached[0])
    assert openmeta._memory_put(keys[1], cached[1])
    assert openmeta._memory_fresh(keys[0], timedelta(days=7)) is not None
    assert openmeta._memory_put(keys[2], cached[2])
    assert tuple(openmeta._memory) == (keys[0], keys[2])

    one_weight = openmeta_module._deep_size(cached[0])
    monkeypatch.setattr(openmeta_module, "_MAX_MEMORY_BYTES", one_weight)
    openmeta._memory.clear()
    openmeta._memory_weight_bytes = 0
    assert openmeta._memory_put(keys[0], cached[0])
    assert openmeta._memory_put(keys[1], cached[1])
    assert tuple(openmeta._memory) == (keys[1],)
    assert openmeta.memory_weight_bytes <= one_weight
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_auxiliary_memory_tables_are_bounded_and_close_clears_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openmeta_module, "_MAX_NEGATIVE_ENTRIES", 2)
    monkeypatch.setattr(openmeta_module, "_MAX_PREFETCH_FAILURE_ENTRIES", 2)
    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(lambda _: response(500)))
    keys = tuple(openmeta_module._CacheKey("api", "Example", "2026-01-01", f"Action{index}") for index in range(3))
    for key in keys:
        openmeta._remember_negative(key)
        openmeta._remember_prefetch_failure(key)

    assert tuple(openmeta._negative) == keys[1:]
    assert tuple(openmeta._prefetch_failures) == keys[1:]
    await openmeta.aclose()
    assert openmeta.memory_entry_count == 0
    assert openmeta._negative == {}
    assert openmeta._prefetch_failures == {}


@pytest.mark.asyncio
async def test_disk_cleanup_limits_managed_cache_and_leaves_product_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(openmeta_module, "_MAX_DISK_CACHE_BYTES", 100)
    openmeta = OpenMetaClient(cache_dir=tmp_path, clock=clock, transport=httpx.MockTransport(lambda _: response(500)))
    oldest = tmp_path / "apis" / "Example" / "2026-01-01" / "Old.json"
    newest = tmp_path / "api-docs" / "Example" / "2026-01-01.json"
    products = tmp_path / "products.zh-cn.json"
    for path in (oldest, newest, products):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 80)
    now = clock().timestamp()
    os.utime(oldest, (now - 2, now - 2))
    os.utime(newest, (now - 1, now - 1))

    openmeta._cleanup_disk_cache(clock())

    assert not oldest.exists()
    assert newest.exists()
    assert products.exists()
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_client_fresh_stale_negative_and_corrupt_disk_cache_boundaries(tmp_path: Path) -> None:
    clock = FakeClock()
    payload = load_fixture("ecs_describe_instances.json")
    remote = Remote([response(200, payload), response(500), response(404)])
    openmeta = client(tmp_path, remote, clock)

    remote_result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")
    assert remote_result.source == "fresh"
    assert remote_result.cache_status == "remote"
    memory_result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")
    assert memory_result.source == "fresh"
    assert memory_result.cache_status == "memory_fresh"
    clock.advance(timedelta(days=8))
    stale = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")
    assert stale.source == "stale_cache"
    assert stale.cache_status == "disk_stale"
    missing = await openmeta.get_api("Ecs", "2014-05-26", "Missing")
    assert missing.error == "not_found"
    assert missing.cache_status == "miss"
    negative = await openmeta.get_api("Ecs", "2014-05-26", "Missing")
    assert negative.error == "not_found"
    assert negative.cache_status == "negative_hit"
    await openmeta.aclose()

    cache_file = tmp_path / "apis" / "Ecs" / "2014-05-26" / "DescribeInstances.json"
    cache_file.write_text('{"payload_sha256":"wrong"}', encoding="utf-8")
    second = client(tmp_path, Remote([response(500)]), clock)
    assert (await second.get_api("Ecs", "2014-05-26", "DescribeInstances")).error == "temporarily_unavailable"
    await second.aclose()


@pytest.mark.asyncio
async def test_client_reports_disk_fresh_when_cache_is_promoted_to_memory(tmp_path: Path) -> None:
    clock = FakeClock()
    first = client(
        tmp_path,
        Remote([response(200, load_fixture("ecs_describe_instances.json"))]),
        clock,
    )
    await first.get_api("Ecs", "2014-05-26", "DescribeInstances")
    await first.aclose()

    remote = Remote([])
    second = client(tmp_path, remote, clock)
    result = await second.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.source == "cache"
    assert result.cache_status == "disk_fresh"
    assert remote.requests == []
    await second.aclose()


@pytest.mark.asyncio
async def test_structurally_invalid_checksummed_api_cache_refreshes_without_logging_payload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    cache_file = tmp_path / "apis" / "Ecs" / "2014-05-26" / "DescribeInstances.json"
    invalid_payload: dict[str, object] = {
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "style": "RPC",
        "methods": [],
        "path": "/",
        "private_marker": "DO_NOT_LOG_PAYLOAD",
    }
    write_cache_envelope(cache_file, invalid_payload, clock)
    remote = Remote([response(200, load_fixture("ecs_describe_instances.json"))])
    openmeta = client(tmp_path, remote, clock)

    with caplog.at_level(logging.DEBUG, logger="iac_code.tools.cloud.aliyun.openmeta"):
        result = await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.value is not None
    assert result.cache_status == "remote"
    assert len(remote.requests) == 1
    assert "Ignoring structurally invalid OpenMeta disk cache" in caplog.text
    assert "DO_NOT_LOG_PAYLOAD" not in caplog.text
    assert str(tmp_path) not in caplog.text
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_structurally_invalid_checksummed_product_cache_refreshes_remotely(tmp_path: Path) -> None:
    clock = FakeClock()
    invalid_payload = load_fixture("products.json")
    invalid_payload["products"].append({"product": 7, "private_marker": "DO_NOT_LOG_PAYLOAD"})  # type: ignore[union-attr]
    write_cache_envelope(tmp_path / "products.zh-cn.json", invalid_payload, clock)
    remote = Remote([response(200, load_fixture("products.json"))])
    openmeta = client(tmp_path, remote, clock)

    result = await openmeta.get_product("Ecs")

    assert result.value is not None
    assert result.cache_status == "remote"
    assert len(remote.requests) == 1
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_product_lookup_uses_products_cache_until_freshness_expiry(tmp_path: Path) -> None:
    clock = FakeClock()
    remote = Remote(
        [
            response(200, load_fixture("products.json")),
            response(200, load_fixture("products.json")),
            response(200, load_fixture("products.json")),
        ]
    )
    openmeta = client(tmp_path, remote, clock)

    assert (await openmeta.get_product("ecs")).value.product == "Ecs"
    assert (await openmeta.get_product("missing")).error == "not_found"
    clock.advance(timedelta(hours=24, microseconds=1))
    assert (await openmeta.get_product("missing")).error == "not_found"
    assert len(remote.requests) == 3
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_product_lookup_refreshes_fresh_summary_before_negative_caching_new_product(tmp_path: Path) -> None:
    clock = FakeClock()
    cached_products = load_fixture("products.json")
    refreshed_products = copy.deepcopy(cached_products)
    refreshed_products["products"].append(  # type: ignore[union-attr]
        {
            "code": "AgentTeams",
            "defaultVersion": "2026-06-05",
            "versions": ["2026-06-05"],
            "recommendVersions": ["2026-06-05"],
            "style": "RPC",
        }
    )
    api_payload = {
        "path": "/",
        "methods": ["get", "post"],
        "operationType": "read",
        "parameters": [],
        "responses": {"200": {"schema": {"type": "object"}}},
        "components": {"schemas": {}},
    }
    write_cache_envelope(tmp_path / "products.zh-cn.json", cached_products, clock)
    remote = Remote([response(200, refreshed_products), response(200, api_payload)])
    openmeta = client(tmp_path, remote, clock)

    product = await openmeta.get_product("AgentTeams")
    api = await openmeta.get_api("AgentTeams", "2026-06-05", "ListInstances")

    assert product.value is not None
    assert product.value.product == "AgentTeams"
    assert product.cache_status == "remote"
    assert api.value is not None
    assert api.value.style == "RPC"
    assert api.value.product == "AgentTeams"
    assert len(remote.requests) == 2
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_product_lookup_does_not_negative_cache_fresh_summary_miss_when_refresh_is_temporary(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    cached_products = load_fixture("products.json")
    refreshed_products = copy.deepcopy(cached_products)
    refreshed_products["products"].append(  # type: ignore[union-attr]
        {
            "code": "AgentTeams",
            "defaultVersion": "2026-06-05",
            "versions": ["2026-06-05"],
            "recommendVersions": ["2026-06-05"],
            "style": "RPC",
        }
    )
    write_cache_envelope(tmp_path / "products.zh-cn.json", cached_products, clock)
    remote = Remote([response(500, {"message": "temporary"}), response(200, refreshed_products)])
    openmeta = client(tmp_path, remote, clock)

    unavailable = await openmeta.get_product("AgentTeams")
    recovered = await openmeta.get_product("agentteams")

    assert unavailable.value is None
    assert unavailable.error == "temporarily_unavailable"
    assert recovered.value is not None
    assert recovered.value.product == "AgentTeams"
    assert len(remote.requests) == 2
    await openmeta.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_product",
    [
        "not-an-object",
        {"product": 7},
        {"product": "Bad", "defaultVersion": 7},
        {"product": "Bad", "versions": ["2020-01-01", 7]},
        {"product": "Bad", "recommendVersions": "2020-01-01"},
        {"product": "Bad", "style": 7},
    ],
)
async def test_product_payload_is_fully_validated_before_caching(
    tmp_path: Path,
    invalid_product: object,
) -> None:
    payload = load_fixture("products.json")
    payload["products"].append(invalid_product)  # type: ignore[union-attr]
    remote = Remote([response(200, payload)])
    openmeta = client(tmp_path, remote, FakeClock())

    result = await openmeta.get_product("Ecs")

    assert result.value is None
    assert result.error == "protocol_error"
    assert not (tmp_path / "products.zh-cn.json").exists()
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_product_negative_lookup_expires_after_ten_minutes_and_is_memory_only(tmp_path: Path) -> None:
    clock = FakeClock()
    remote = Remote([response(200, load_fixture("products.json")), response(200, load_fixture("products.json"))])
    openmeta = client(tmp_path, remote, clock)

    first = await openmeta.get_product("Missing")
    second = await openmeta.get_product("missing")
    clock.advance(timedelta(minutes=10, microseconds=1))
    expired = await openmeta.get_product("MISSING")

    assert first.error == "not_found"
    assert first.cache_status == "remote"
    assert second.error == "not_found"
    assert second.cache_status == "negative_hit"
    assert expired.error == "not_found"
    assert expired.cache_status == "remote"
    assert len(remote.requests) == 2
    assert not (tmp_path / "apis" / "Missing").exists()
    await openmeta.aclose()

    restarted_remote = Remote([response(200, load_fixture("products.json"))])
    restarted = client(tmp_path, restarted_remote, clock)
    after_restart = await restarted.get_product("missing")
    assert after_restart.error == "not_found"
    assert after_restart.cache_status == "remote"
    assert len(restarted_remote.requests) == 1
    await restarted.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (204, None),
        (404, None),
        (200, load_fixture("error_envelopes.json")["product_not_found"]),
    ],
    ids=["http-204", "http-404", "semantic-not-found"],
)
async def test_product_list_not_found_negative_caches_requested_product_for_ten_minutes(
    tmp_path: Path,
    status: int,
    payload: object | None,
) -> None:
    clock = FakeClock()
    refreshed_products = load_fixture("products.json")
    refreshed_products["products"].append(  # type: ignore[union-attr]
        {"product": "Missing", "defaultVersion": "2020-01-01"}
    )
    remote = Remote([response(status, payload), response(200, refreshed_products)])
    openmeta = client(tmp_path, remote, clock)

    first = await openmeta.get_product("Missing")
    negative = await openmeta.get_product("missing")

    assert first.error == "not_found"
    assert negative.error == "not_found"
    assert negative.cache_status == "negative_hit"
    assert len(remote.requests) == 1

    clock.advance(timedelta(minutes=10, microseconds=1))
    refreshed = await openmeta.get_product("MISSING")
    assert refreshed.value is not None
    assert refreshed.value.product == "Missing"
    assert len(remote.requests) == 2
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_cache_write_is_atomic_and_leaves_a_checksummed_envelope(tmp_path: Path) -> None:
    clock = FakeClock()
    openmeta = client(tmp_path, Remote([response(200, load_fixture("ecs_describe_instances.json"))]), clock)

    await openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

    cache_file = tmp_path / "apis" / "Ecs" / "2014-05-26" / "DescribeInstances.json"
    envelope = json.loads(cache_file.read_text(encoding="utf-8"))
    assert envelope["schema_version"] == 1
    assert envelope["payload_sha256"]
    assert not list(cache_file.parent.glob("*.tmp"))
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_cache_replace_failure_preserves_previous_complete_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    original = load_fixture("ecs_describe_instances.json")
    original["summary"] = "old"
    first = client(tmp_path, Remote([response(200, original)]), clock)
    assert (await first.get_api("Ecs", "2014-05-26", "DescribeInstances")).value is not None
    await first.aclose()
    cache_file = tmp_path / "apis" / "Ecs" / "2014-05-26" / "DescribeInstances.json"
    previous = cache_file.read_bytes()

    clock.advance(timedelta(days=8))
    refreshed = load_fixture("ecs_describe_instances.json")
    refreshed["summary"] = "new"
    second = client(tmp_path, Remote([response(200, refreshed)]), clock)
    monkeypatch.setattr(openmeta_module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))

    result = await second.get_api("Ecs", "2014-05-26", "DescribeInstances")

    assert result.value is not None and result.value.summary == "new"
    assert cache_file.read_bytes() == previous
    assert not list(cache_file.parent.glob("*.tmp"))
    await second.aclose()


def test_multiprocess_cache_refresh_leaves_one_complete_last_writer_envelope(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_write_openmeta_cache_from_process, args=(str(tmp_path), summary))
        for summary in ("writer-one", "writer-two")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    cache_file = tmp_path / "apis" / "Ecs" / "2014-05-26" / "DescribeInstances.json"
    envelope = json.loads(cache_file.read_text(encoding="utf-8"))
    payload = envelope["payload"]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert envelope["payload_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert payload["summary"] in {"writer-one", "writer-two"}
    assert not list(cache_file.parent.glob("*.tmp"))


@pytest.mark.asyncio
async def test_singleflight_deduplicates_owner_and_allows_waiter_cancellation(tmp_path: Path) -> None:
    clock = FakeClock()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return response(200, load_fixture("ecs_describe_instances.json"))

    openmeta = OpenMetaClient(cache_dir=tmp_path, clock=clock, transport=httpx.MockTransport(handler))
    owner = asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances"))
    await started.wait()
    waiter = asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert (await owner).value is not None
    assert calls == 1
    assert openmeta.singleflight_size <= 256
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_singleflight_owner_cancellation_keeps_refresh_available_to_waiters(tmp_path: Path) -> None:
    clock = FakeClock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return response(200, load_fixture("ecs_describe_instances.json"))

    openmeta = OpenMetaClient(cache_dir=tmp_path, clock=clock, transport=httpx.MockTransport(handler))
    owner = asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances"))
    await started.wait()
    waiter = asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances"))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    release.set()
    assert (await waiter).value is not None
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_aclose_reaps_cancelled_last_waiter_refresh_and_notifications_before_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_started = asyncio.Event()
    refresh_cancelled = asyncio.Event()
    release_refresh_cleanup = asyncio.Event()
    notification_started = asyncio.Event()
    notification_reaped = asyncio.Event()
    propagated_cancellations: list[asyncio.CancelledError] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        refresh_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            refresh_cancelled.set()
            await release_refresh_cleanup.wait()
            raise

    async def blocked_notification() -> None:
        notification_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            notification_reaped.set()

    openmeta = OpenMetaClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(openmeta._singleflight, "_notify_capacity", blocked_notification)
    completed = asyncio.get_running_loop().create_future()
    completed.set_result(None)
    openmeta._singleflight._schedule_capacity_notification(completed)
    await asyncio.wait_for(notification_started.wait(), timeout=1)
    request_task = asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances"))
    close_task: asyncio.Task[None] | None = None

    async def close() -> None:
        try:
            await openmeta.aclose()
        except asyncio.CancelledError as error:
            propagated_cancellations.append(error)
            raise

    try:
        await asyncio.wait_for(refresh_started.wait(), timeout=1)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task

        close_task = asyncio.create_task(close())
        try:
            await asyncio.wait_for(refresh_cancelled.wait(), timeout=0.1)
            refresh_was_cancelled = True
        except asyncio.TimeoutError:
            refresh_was_cancelled = False

        if refresh_was_cancelled:
            close_task.cancel("first cancellation")
            await asyncio.sleep(0)
            close_task.cancel("second cancellation")
            await asyncio.sleep(0)
        close_waited_for_cleanup = not close_task.done()
        release_refresh_cleanup.set()
        close_result = (await asyncio.gather(close_task, return_exceptions=True))[0]

        assert refresh_was_cancelled is True
        assert close_waited_for_cleanup is True
        assert isinstance(close_result, asyncio.CancelledError)
        assert propagated_cancellations[0].args == ("first cancellation",)
        assert notification_reaped.is_set()
        assert openmeta.singleflight_size == 0
        assert not openmeta._singleflight._notification_tasks
        with pytest.raises(RuntimeError, match="closed"):
            await openmeta.get_api("Ecs", "2014-05-26", "RunInstances")
    finally:
        release_refresh_cleanup.set()
        for entry in list(openmeta._singleflight._entries.values()):
            entry.task.cancel()
        await asyncio.gather(
            *(entry.task for entry in list(openmeta._singleflight._entries.values())),
            return_exceptions=True,
        )
        pending_notifications = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and task.get_coro().__name__ == "blocked_notification"
        ]
        for task in pending_notifications:
            task.cancel()
        if pending_notifications:
            await asyncio.gather(*pending_notifications, return_exceptions=True)
        if close_task is not None and not close_task.done():
            await asyncio.gather(close_task, return_exceptions=True)
        await openmeta._client.aclose()


@pytest.mark.asyncio
async def test_singleflight_does_not_block_a_different_cache_key(tmp_path: Path) -> None:
    clock = FakeClock()
    blocked_started = asyncio.Event()
    release = asyncio.Event()
    other_key_completed = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.split("/")[-2]
        if action == "Blocked":
            blocked_started.set()
            await release.wait()
        else:
            other_key_completed.set()
        payload = load_fixture("ecs_describe_instances.json")
        payload["action"] = action
        return response(200, payload)

    openmeta = OpenMetaClient(cache_dir=tmp_path, clock=clock, transport=httpx.MockTransport(handler))
    blocked = asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "Blocked"))
    await blocked_started.wait()
    other = await openmeta.get_api("Ecs", "2014-05-26", "Other")
    assert other.value is not None
    assert other_key_completed.is_set()
    release.set()
    assert (await blocked).value is not None
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_singleflight_evicts_an_idle_entry_before_admitting_a_new_key(tmp_path: Path) -> None:
    clock = FakeClock()
    calls: list[str] = []
    new_action_started = asyncio.Event()
    release_new_action = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.split("/")[-2]
        calls.append(action)
        if action == "NewAction":
            new_action_started.set()
            await release_new_action.wait()
        payload = load_fixture("ecs_describe_instances.json")
        payload["action"] = action
        return response(200, payload)

    openmeta = OpenMetaClient(cache_dir=tmp_path, clock=clock, transport=httpx.MockTransport(handler))
    await asyncio.gather(*(openmeta.get_api("Ecs", "2014-05-26", f"Action{index}") for index in range(256)))
    assert openmeta.singleflight_size == 256

    first = asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "NewAction"))
    await new_action_started.wait()
    second = asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "NewAction"))
    await asyncio.sleep(0)
    release_new_action.set()
    await asyncio.gather(first, second)

    assert calls.count("NewAction") == 1
    assert openmeta.singleflight_size == 256
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_singleflight_waits_for_capacity_and_deduplicates_the_overflow_key(tmp_path: Path) -> None:
    clock = FakeClock()
    release = asyncio.Event()
    started = asyncio.Event()
    calls: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.split("/")[-2]
        calls[action] = calls.get(action, 0) + 1
        if action != "Overflow":
            if len(calls) == 256:
                started.set()
            await release.wait()
        payload = load_fixture("ecs_describe_instances.json")
        payload["action"] = action
        return response(200, payload)

    openmeta = OpenMetaClient(cache_dir=tmp_path, clock=clock, transport=httpx.MockTransport(handler))
    active = [asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", f"Action{index}")) for index in range(256)]
    await started.wait()
    overflow = [asyncio.create_task(openmeta.get_api("Ecs", "2014-05-26", "Overflow")) for _ in range(2)]
    await asyncio.sleep(0)

    assert "Overflow" not in calls
    assert openmeta.singleflight_size == 256
    release.set()
    await asyncio.gather(*active, *overflow)
    assert calls["Overflow"] == 1
    assert openmeta.singleflight_size <= 256
    await openmeta.aclose()


@pytest.mark.asyncio
async def test_runtime_factory_injects_dependencies_and_closes_idempotently(tmp_path: Path) -> None:
    clock = FakeClock()
    services = create_aliyun_runtime_services(cache_dir=tmp_path, clock=clock, random_fn=lambda: 0.5)

    assert services.clock is clock
    assert services.random() == 0.5
    await services.aclose()
    await services.aclose()
