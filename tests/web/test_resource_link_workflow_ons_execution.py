"""Exact non-prefix ONS metadata admission, entirely offline."""

import copy

import pytest

from scripts.web.e2e.resource_link_workflow.execution_gate import GateDeniedError, validate_grants


def entries():
    return [
        {
            "id": "identity",
            "kind": "identity",
            "quota": 1,
            "call": {
                "product": "sts",
                "action": "GetCallerIdentity",
                "version": "2015-04-01",
                "region_id": "cn-hangzhou",
                "params": {},
            },
        },
        {
            "id": "instances",
            "kind": "read",
            "quota": 1,
            "read_policy": {"phase": "execute"},
            "call": {
                "product": "ons",
                "action": "OnsInstanceInServiceList",
                "version": "2019-02-14",
                "region_id": "cn-hangzhou",
                "params": {"RegionId": "cn-hangzhou"},
            },
        },
    ]


@pytest.mark.parametrize("counts", [None, True, False])
def test_exact_ons_list_and_optional_statistics_are_readonly(counts):
    value = entries()
    if counts is not None:
        value[1]["call"]["params"]["NeedResourceInfo"] = counts
    original = copy.deepcopy(value)
    validate_grants(value, require_read_policy=True)
    assert value == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("product", "rocketmq"),
        ("version", "2022-08-01"),
        ("region_id", "us-west-1"),
        ("action", "OnsInstanceCreate"),
        ("action", "OnsInstanceDelete"),
        ("action", "OnsInstanceUpdate"),
        ("action", "OnsInstanceInServiceListExtra"),
    ],
)
def test_no_other_ons_operation_version_or_region_is_admitted(field, value):
    grants = entries()
    grants[1]["call"][field] = value
    with pytest.raises(GateDeniedError):
        validate_grants(grants, require_read_policy=True)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"RegionId": "cn-beijing"},
        {"RegionId": "cn-hangzhou", "NeedResourceInfo": 1},
        {"RegionId": "cn-hangzhou", "NeedResourceInfo": "true"},
        {"RegionId": "cn-hangzhou", "PageSize": 10},
        {"RegionId": "cn-hangzhou", "InstanceId": "existing-business"},
    ],
)
def test_reject_undocumented_or_wrongly_typed_ons_list_parameters(params):
    grants = entries()
    grants[1]["call"]["params"] = params
    with pytest.raises(GateDeniedError):
        validate_grants(grants, require_read_policy=True)
