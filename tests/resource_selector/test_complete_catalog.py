from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from iac_code.resource_selector.profiles import PROFILE_HASH, QueryOperation, get_profile, iter_profiles
from iac_code.resource_selector.query import (
    ResourceSelectorQueryService,
    _adapt_server_parameters,
    _fixed_query_parameters,
    _normalize_dynamic_parameters,
    _normalize_parameter_mapping,
    _operation_region_id,
)
from iac_code.web.app import create_app
from iac_code.web.session_manager import WebSessionManager

_ROOT = Path(__file__).parent
_CASE_DOCUMENT = json.loads((_ROOT / "e2e-cases.json").read_text(encoding="utf-8"))
_FIXTURE_DOCUMENT = json.loads((_ROOT / "contract-fixtures.json").read_text(encoding="utf-8"))
_CASES = _CASE_DOCUMENT["cases"]
_ENABLED_CASES = [
    case for case in _CASES if (profile := get_profile(case["selectorId"])) is not None and profile.enabled
]
_FIXTURES = _FIXTURE_DOCUMENT["fixtures"]
_CAPABILITIES = json.loads(
    (Path(__file__).parents[2] / "src/iac_code/resource_selector/ore-capabilities.json").read_text(encoding="utf-8")
)
_OPERATIONS = json.loads(
    (Path(__file__).parents[2] / "src/iac_code/resource_selector/ore-operations.json").read_text(encoding="utf-8")
)


def test_every_declared_operation_has_an_explicit_response_projector() -> None:
    from iac_code.resource_selector.query import _response_projection_registry

    registry = _response_projection_registry()
    operations = [operation for profile in iter_profiles() for operation in profile.operations]
    assert len(operations) == 143
    assert all(operation.response_projector in registry for operation in operations)


def operation_parameters(operation: QueryOperation, case: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Replay the audited ORE request while rebinding trusted context values."""

    result = deepcopy(request["parameters"])
    profile = get_profile(case["selectorId"])
    assert profile is not None

    def bind(parameters: dict[str, Any], allowed: Iterable[str]) -> dict[str, Any]:
        bound = {key: value for key, value in parameters.items() if key in allowed}
        for metadata_key, api_parameter in operation.metadata_parameters:
            if api_parameter not in bound:
                continue
            if metadata_key in case["metadata"]:
                bound[api_parameter] = case["metadata"][metadata_key]
            else:
                bound.pop(api_parameter)
        source = case.get("source")
        source_parameter = profile.source_parameter_for(operation.key)
        if source_parameter and source_parameter in bound and isinstance(source, dict):
            source_value = source.get("value")
            bound[source_parameter] = [source_value] if profile.selector_id == "ess.eci_container" else source_value
        return bound

    if operation.request_kind != "multiApi":
        return bind(result, operation.dynamic_parameters)
    requests = result.get("requests")
    assert isinstance(requests, list) and requests
    return {
        "requests": [
            {
                **(
                    {"customRequestKey": item["customRequestKey"]}
                    if isinstance(item.get("customRequestKey"), str)
                    else {}
                ),
                "parameters": bind(item["parameters"], operation.batch_parameters),
            }
            for item in requests
            if isinstance(item, dict) and isinstance(item.get("parameters"), dict)
        ]
    }


def expected_server_calls(
    profile, operation: QueryOperation, case: dict[str, Any], request: dict[str, Any]
) -> list[tuple[str, str, str | None, dict[str, Any]]]:
    """Materialize the exact fixed BFF call shapes from the audited ORE request."""

    dynamic = operation_parameters(operation, case, request)
    params = _fixed_query_parameters(profile, operation, case["metadata"], case["source"])
    params.update(_normalize_dynamic_parameters(operation, dynamic))
    region_id = _operation_region_id(case["metadata"], params, operation=operation)
    if operation.request_kind != "multiApi":
        return [(operation.product, operation.action, region_id, _adapt_server_parameters(operation, params))]
    calls = []
    for item in params["requests"]:
        normalized = _normalize_parameter_mapping(item["parameters"])
        for key, value in params.items():
            if key != "requests" and key in operation.batch_parameters:
                normalized[key] = value
        calls.append((operation.product, operation.action, region_id, _adapt_server_parameters(operation, normalized)))
    return calls


def test_complete_catalog_sets_and_contract_evidence_are_exact() -> None:
    all_profiles = iter_profiles()
    profiles = iter_profiles(include_disabled=False)
    all_profile_ids = {profile.selector_id for profile in all_profiles}
    profile_ids = {profile.selector_id for profile in profiles}
    capability_ids = {item["selectorId"] for item in _CAPABILITIES["selectors"] if item.get("enabled", True)}
    operation_ids = {item["selectorId"] for item in _OPERATIONS["selectors"]}
    case_ids = {item["selectorId"] for item in _CASES}

    assert len(all_profiles) == len(all_profile_ids) == 117
    assert sum(profile.selection_kind == "cloud_resource" for profile in all_profiles) == 101
    assert sum(profile.selection_kind == "cloud_resource_derived_value" for profile in all_profiles) == 16
    assert len(profiles) == len(profile_ids) == 116
    assert sum(profile.selection_kind == "cloud_resource" for profile in profiles) == 100
    assert sum(profile.selection_kind == "cloud_resource_derived_value" for profile in profiles) == 16
    assert all_profile_ids == operation_ids == case_ids
    assert profile_ids == capability_ids
    assert all(profile.enabled and profile.unsupported_reason is None and profile.operations for profile in profiles)
    assert {(profile.selector_id, profile.unsupported_reason) for profile in all_profiles if not profile.enabled} == {
        ("dashvector.cluster", "public_endpoint_unavailable")
    }

    operation_count = sum(len(profile.operations) for profile in all_profiles)
    server_signatures = {
        (operation.request_kind, operation.product, operation.action)
        for profile in all_profiles
        for operation in profile.operations
    }
    assert operation_count == _OPERATIONS["counts"]["selectorOperations"] == 143
    ore_signatures = {
        (operation["requestKind"], operation["product"], operation["action"])
        for selector in _OPERATIONS["selectors"]
        for operation in selector["operations"]
    }
    assert len(ore_signatures) == _OPERATIONS["counts"]["uniqueApiSignatures"] == 121
    # Server-side aliases and transport overrides expand the fixed browser
    # signatures into their distinct public API contracts.
    assert len(server_signatures) == 124

    fixture_ids = {request["fixtureId"] for case in _CASES for request in case["expectedRequests"]}
    assert fixture_ids == set(_FIXTURES)
    assert all(_FIXTURES[fixture_id]["response"] for fixture_id in fixture_ids)
    requests = [request for case in _CASES for request in case["expectedRequests"]]
    assert all(set(_FIXTURES[request["fixtureId"]]) == {"api", "response"} for request in requests)
    assert all(
        set(request)
        in (
            {"operationKey", "fixtureId", "parameters"},
            {"operationKey", "fixtureId", "parameters", "parameterVariants"},
        )
        for request in requests
    )
    assert all(
        set(variant) == {"parameters"} for request in requests for variant in request.get("parameterVariants", [])
    )

    bundle = (Path(__file__).parents[2] / "src/iac_code/web/static/js/vendor/ore-resource-selector.min.js").read_bytes()
    generated_from = {
        "bundleSha256": hashlib.sha256(bundle).hexdigest(),
        "profileHash": PROFILE_HASH,
    }
    assert _CASE_DOCUMENT["generatedFrom"] == generated_from
    assert _FIXTURE_DOCUMENT["generatedFrom"] == generated_from

    for case in _CASES:
        profile = get_profile(case["selectorId"])
        assert profile is not None
        operations = {operation.key: operation for operation in profile.operations}
        for request in case["expectedRequests"]:
            fixture = _FIXTURES[request["fixtureId"]]
            operation = operations[request["operationKey"]]
            assert fixture["api"]["version"] == operation.api_version


@pytest.mark.parametrize("case", _ENABLED_CASES, ids=lambda case: case["selectorId"])
def test_every_selector_round_trips_through_the_real_web_bff(case, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    profile = get_profile(case["selectorId"])
    assert profile is not None and profile.enabled

    by_signature: dict[tuple[str, str], tuple[QueryOperation, dict[str, Any]]] = {}
    for request in case["expectedRequests"]:
        operation = next(item for item in profile.operations if item.key == request["operationKey"])
        by_signature[(operation.product, operation.action)] = (operation, _FIXTURES[request["fixtureId"]]["response"])

    calls: list[tuple[str, str, str | None, dict[str, Any]]] = []

    async def caller(product: str, action: str, region_id: str | None, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((product, action, region_id, params))
        operation, response = by_signature[(product, action)]
        if operation.request_kind == "multiApi":
            return next(iter(response.values()))
        return response

    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    session = manager.create_session(cwd=str(project))
    input_id = "resource-{}".format(case["selectorId"].replace(".", "-"))
    pending_payload = {
        "toolUseId": "tool-{}".format(case["selectorId"]),
        "inputId": input_id,
        "question": "请选择资源",
        "selector": {
            "id": profile.selector_id,
            "associationProperty": profile.association_property,
            "outputKind": case["expected"]["outputKind"],
            "associationPropertyMetadata": case["metadata"],
            "source": case["source"],
            "profileHash": PROFILE_HASH,
        },
    }
    request_id = manager.add_resource_selection_request(session, pending_payload)
    query_service = ResourceSelectorQueryService(caller)
    expected_calls: list[tuple[str, str, str | None, dict[str, Any]]] = []

    with TestClient(create_app(session_manager=manager, resource_selector_query_service=query_service)) as client:
        for request in case["expectedRequests"]:
            operation = next(item for item in profile.operations if item.key == request["operationKey"])
            expected_calls.extend(expected_server_calls(profile, operation, case, request))
            response = client.post(
                "/api/resource-selector/query",
                json={
                    "requestId": request_id,
                    "sessionId": session.session_id,
                    "inputId": input_id,
                    "operationKey": operation.key,
                    "params": operation_parameters(operation, case, request),
                },
            )
            assert response.status_code == 200, response.text

        answer = client.post(
            "/api/resource-selections/{}/answer".format(request_id),
            json={
                "sessionId": session.session_id,
                "inputId": input_id,
                "selectorId": profile.selector_id,
                "value": str(case["expected"]["value"]),
                "label": str(case["expected"]["value"]),
            },
        )
        assert answer.status_code == 200, answer.text

    assert calls
    # Some components deliberately repeat a parent/detail request while opening
    # a child picker or revalidating the chosen item. Every server invocation
    # must nevertheless be byte-for-byte one of the audited request shapes,
    # and every declared operation shape must have reached the BFF caller.
    assert all(call in expected_calls for call in calls)
    assert all(call in calls for call in expected_calls)
