"""Offline coverage for the deliberately narrow ECS pricing compatibility path."""

import copy

import pytest

from iac_code.tools.cloud.aliyun.aliyun_api import _normalize_runtime_input, _with_canonical_runtime_product
from iac_code.tools.cloud.aliyun.api_contract import ApiContractError
from iac_code.tools.cloud.aliyun.api_hooks import normalize_parameters, run_hooks


def test_system_disk_aliases_leave_data_disk_and_other_parameters_unchanged() -> None:
    params = {
        "SystemDisk.1.Category": "cloud_essd",
        "SystemDisk.1.Size": 40,
        "SystemDisk.1.PerformanceLevel": "PL1",
        "DataDisk.1.Category": "cloud_essd",
        "DataDisk.1.Size": 100,
        "ImageId": "example-image",
    }

    normalize_parameters("ecs", "DescribePrice", params)

    assert params == {
        "SystemDisk.Category": "cloud_essd",
        "SystemDisk.Size": 40,
        "SystemDisk.PerformanceLevel": "PL1",
        "DataDisk.1.Category": "cloud_essd",
        "DataDisk.1.Size": 100,
        "ImageId": "example-image",
    }


@pytest.mark.parametrize("action", ["RunInstances", "CreateInstance", "DescribeInstances"])
def test_system_disk_aliases_are_scoped_to_price_queries(action: str) -> None:
    params = {"SystemDisk.1.Size": 40}
    normalize_parameters("ecs", action, params)
    assert params == {"SystemDisk.1.Size": 40}


@pytest.mark.parametrize("value", [41, "40", False])
def test_conflicting_alias_fails_without_partially_mutating_parameters(value) -> None:
    params = {"SystemDisk.1.Category": "cloud_essd", "SystemDisk.1.Size": value, "SystemDisk.Size": 40}
    original = copy.deepcopy(params)

    with pytest.raises(ApiContractError, match="^conflicting_parameter_alias$") as raised:
        normalize_parameters("ecs", "DescribePrice", params)

    assert raised.value.parameter == "SystemDisk.Size"
    assert params == original


def test_matching_alias_is_deduplicated() -> None:
    params = {"SystemDisk.1.Size": 40, "SystemDisk.Size": 40}
    normalize_parameters("ecs", "DescribePrice", params)
    assert params == {"SystemDisk.Size": 40}


@pytest.mark.parametrize(
    "params",
    [
        {"SystemDisk": {"Category": "cloud_essd", "Size": 40, "PerformanceLevel": "PL1"}},
        {"SystemDiskCategory": "cloud_essd", "SystemDiskSize": 40, "SystemDiskPerformanceLevel": "PL1"},
    ],
)
def test_known_object_and_sdk_names_use_the_same_price_parameters(params) -> None:
    normalize_parameters("ecs", "DescribePrice", params)
    assert params == {"SystemDisk.Category": "cloud_essd", "SystemDisk.Size": 40, "SystemDisk.PerformanceLevel": "PL1"}


@pytest.mark.parametrize(
    "params",
    [
        {"SystemDisk": {"Size": 80}, "SystemDisk.Size": 40},
        {"SystemDisk": {"Size": 80}, "SystemDiskSize": 40},
        {"SystemDisk.1.Size": 80, "SystemDiskSize": 40},
    ],
)
def test_known_alternative_shapes_cannot_overwrite_conflicting_inputs(params) -> None:
    original = copy.deepcopy(params)
    with pytest.raises(ApiContractError, match="^conflicting_parameter_alias$"):
        normalize_parameters("ecs", "DescribePrice", params)
    assert params == original


def test_system_disk_object_with_unknown_fields_is_not_partially_flattened() -> None:
    params = {"SystemDisk": {"Size": 40, "Unknown": "retain"}}
    original = copy.deepcopy(params)
    normalize_parameters("ecs", "DescribePrice", params)
    assert params == original


@pytest.mark.parametrize("product", ["ecs", "Ecs", "ECS"])
def test_runtime_normalizes_a_copy_of_price_input(product: str) -> None:
    tool_input = {"product": product, "action": "DescribePrice", "params": {"SystemDisk.1.Size": "40"}}
    original = copy.deepcopy(tool_input)

    normalized = _normalize_runtime_input(tool_input)

    assert normalized["params"] == {"SystemDisk.Size": "40"}
    assert tool_input == original


def test_price_normalization_is_idempotent_and_separate_from_read_only_preflight() -> None:
    params = {"SystemDisk": {"Size": 40}}
    assert run_hooks("ecs", "DescribePrice", params, read_only=True) is None
    assert params == {"SystemDisk": {"Size": 40}}
    normalize_parameters("ecs", "DescribePrice", params)
    assert params == {"SystemDisk.Size": 40}
    normalize_parameters("ecs", "DescribePrice", params)
    assert params == {"SystemDisk.Size": 40}


def test_runtime_does_not_normalize_another_products_price_query() -> None:
    tool_input = {"product": "vpc", "action": "DescribePrice", "params": {"SystemDisk.1.Size": 40}}
    assert _normalize_runtime_input(tool_input)["params"] == tool_input["params"]


def test_metadata_resolved_ecs_product_normalizes_a_copy() -> None:
    tool_input = {"product": "ComputeAlias", "action": "DescribePrice", "params": {"SystemDisk.1.Size": 40}}
    original = copy.deepcopy(tool_input)

    normalized = _with_canonical_runtime_product(tool_input, "Ecs")

    assert normalized == {"product": "Ecs", "action": "DescribePrice", "params": {"SystemDisk.Size": 40}}
    assert tool_input == original
