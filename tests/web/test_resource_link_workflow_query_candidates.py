"""Catalog input refresh is a narrow read-only projection, not new test authority."""

import copy
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow.files import read_json
from scripts.web.e2e.resource_link_workflow.runtime import preparation_read_api, refreshed_query_candidates

REPO = Path(__file__).resolve().parents[2]


def fixture():
    resource = {
        "query_candidates": [],
        "dimensions": {
            "QUERY": {
                "contracts": [
                    {"source_type": "ALIYUN::MaxCompute::Project"},
                ]
            }
        },
    }
    catalog = read_json(REPO / "src/iac_code/tools/cloud/aliyun/data/direct_resource_api_catalog.json")
    return resource, catalog


def test_current_catalog_can_supply_missing_frozen_preparation_input_without_mutation():
    resource, catalog = fixture()
    original = copy.deepcopy(resource)
    result = refreshed_query_candidates(resource, catalog)
    assert result == [
        {
            "product": "maxcompute",
            "action": "ListProjects",
            "contract": "maxcompute/ListProjects/ALIYUN::MaxCompute::Project/data.projects.*",
        }
    ]
    assert resource == original


def test_nonempty_original_candidates_are_not_expanded():
    resource, catalog = fixture()
    resource["query_candidates"] = [{"product": "reviewed", "action": "ExactOriginalRead"}]
    assert refreshed_query_candidates(resource, catalog) == resource["query_candidates"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("resourceType", "ALIYUN::Other::Project"),
        ("product", ""),
        ("action", "CreateProject"),
        ("action", "DeleteProject"),
        ("action", "StartExecution"),
    ],
)
def test_unrelated_or_write_contracts_cannot_refresh_candidates(field, value):
    resource, catalog = fixture()
    contract = next(c for c in catalog["responseContracts"] if c["resourceType"] == "ALIYUN::MaxCompute::Project")
    catalog["responseContracts"] = [{**contract, field: value}]
    assert refreshed_query_candidates(resource, catalog) == []


def test_generator_keeps_exact_reviewed_project_mapping():
    from scripts.web.e2e.generate_direct_resource_api_catalog import (
        _ADDITIONAL_CONTRACTS,
        _IDENTITY_ATTRIBUTE_FIELDS,
        _IDENTITY_OVERRIDES,
        _identities,
    )

    _, catalog = fixture()
    identity = next(i for i in _IDENTITY_OVERRIDES if i.resource_type == "ALIYUN::MaxCompute::Project")
    assert (identity.identifier_field, identity.identifier_kind, identity.product) == ("name", "name", "maxcompute")
    actual = next(i for i in catalog["resources"] if i["resourceType"] == identity.resource_type)
    assert list(_IDENTITY_ATTRIBUTE_FIELDS[identity.resource_type]) == actual["attributeFields"]
    contracts = [i for i in _ADDITIONAL_CONTRACTS if i["resourceType"] == identity.resource_type]
    assert contracts == [i for i in catalog["responseContracts"] if i["resourceType"] == identity.resource_type]
    identities, _ = _identities(
        REPO / "offline-no-meta-checkout",
        REPO / "src/iac_code/web/static/ros_resource_links.json",
    )
    assert identities[identity.resource_type] == identity


@pytest.mark.parametrize(
    "product,action,allowed",
    [
        ("ons", "OnsInstanceInServiceList", True),
        ("ons", "OnsInstanceCreate", False),
        ("ons", "OnsInstanceDelete", False),
        ("ons", "OnsMessagePush", False),
        ("ons", "OnsInstanceInServiceListExtra", False),
        ("other", "OnsInstanceInServiceList", False),
        ("rocketmq", "ListInstances", True),
        ("ros", "GetStack", True),
    ],
)
def test_nonprefix_preparation_read_is_exact_not_an_ons_namespace_allowance(product, action, allowed):
    assert preparation_read_api(product, action) is allowed


def test_ons_list_refresh_remains_bound_to_own_canonical_or_alias():
    _, catalog = fixture()
    resource = {
        "query_candidates": [],
        "dimensions": {
            "QUERY": {
                "contracts": [
                    {"source_type": "alicloud_ons_instance"},
                ]
            }
        },
    }
    candidates = refreshed_query_candidates(resource, catalog)
    assert {tuple((q["product"], q["action"])) for q in candidates} == {
        ("ons", "OnsInstanceInServiceList"),
        ("rocketmq", "ListInstances"),
    }
    resource["dimensions"]["QUERY"]["contracts"][0]["source_type"] = "ALIYUN::Other::Instance"
    assert refreshed_query_candidates(resource, catalog) == []
