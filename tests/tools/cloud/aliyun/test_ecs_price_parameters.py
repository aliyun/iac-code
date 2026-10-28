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
@pytest.mark.parametrize("alias", ["SystemDisk.1.Size", "SystemDisk.Capacity"])
def test_system_disk_aliases_are_scoped_to_price_queries(action: str, alias: str) -> None:
    params = {alias: 40}
    normalize_parameters("ecs", action, params)
    assert params == {alias: 40}


@pytest.mark.parametrize("value", [41, "40", False])
@pytest.mark.parametrize("alias", ["SystemDisk.1.Size", "SystemDisk.Capacity"])
def test_conflicting_alias_fails_without_partially_mutating_parameters(value, alias: str) -> None:
    params = {"SystemDisk.1.Category": "cloud_essd", alias: value, "SystemDisk.Size": 40}
    original = copy.deepcopy(params)

    with pytest.raises(ApiContractError, match="^conflicting_parameter_alias$") as raised:
        normalize_parameters("ecs", "DescribePrice", params)

    assert raised.value.parameter == "SystemDisk.Size"
    assert params == original


@pytest.mark.parametrize("alias", ["SystemDisk.1.Size", "SystemDisk.Capacity"])
def test_matching_alias_is_deduplicated(alias: str) -> None:
    params = {alias: 40, "SystemDisk.Size": 40}
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
        {"SystemDisk.Capacity": 80, "SystemDiskSize": 40},
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
@pytest.mark.parametrize("alias", ["SystemDisk.1.Size", "SystemDisk.Capacity"])
def test_runtime_normalizes_a_copy_of_price_input(product: str, alias: str) -> None:
    tool_input = {"product": product, "action": "DescribePrice", "params": {alias: "40"}}
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


@pytest.mark.parametrize("product", ["ecs", "Ecs", "ECS"])
def test_price_action_alias_normalizes_with_parameters_on_a_copy(product: str) -> None:
    tool_input = {
        "product": product,
        "action": "DescribePrices",
        "region_id": "cn-hangzhou",
        "params": {"SystemDisk.Capacity": 60, "PeriodType": "Monthly", "Period": 1},
    }
    original = copy.deepcopy(tool_input)

    normalized = _normalize_runtime_input(tool_input)

    assert normalized["action"] == "DescribePrice"
    assert normalized["params"] == {"SystemDisk.Size": 60, "PriceUnit": "Month", "Period": 1}
    assert _normalize_runtime_input(normalized) == normalized
    assert tool_input == original


@pytest.mark.parametrize(
    "params",
    [
        {"PeriodType": "Monthly"},
        {"PeriodType": "Month"},
        {"PriceUnit": "Monthly"},
        {"PeriodType": "Monthly", "PriceUnit": "Monthly"},
        {"PeriodType": "Monthly", "PriceUnit": "Month"},
        {"PeriodType": "Month", "PriceUnit": "Monthly"},
    ],
)
def test_price_period_aliases_share_the_canonical_month_unit(params) -> None:
    normalize_parameters("ecs", "DescribePrice", params)
    assert params == {"PriceUnit": "Month"}


@pytest.mark.parametrize("period_type", ["Monthly", "Year", None])
def test_conflicting_period_alias_does_not_partially_normalize_disk_params(period_type) -> None:
    params = {"SystemDisk.Capacity": 60, "PeriodType": period_type, "PriceUnit": "Hour"}
    original = copy.deepcopy(params)

    with pytest.raises(ApiContractError, match="^conflicting_parameter_alias$") as raised:
        normalize_parameters("ecs", "DescribePrice", params)

    assert raised.value.parameter == "PriceUnit"
    assert params == original


@pytest.mark.parametrize(("product", "action"), [("vpc", "DescribePrices"), ("ecs", "RunInstances")])
def test_price_action_and_period_aliases_do_not_affect_other_operations(product: str, action: str) -> None:
    tool_input = {"product": product, "action": action, "params": {"PeriodType": "Monthly"}}
    normalized = _normalize_runtime_input(tool_input)
    assert normalized["action"] == action
    assert normalized["params"] == tool_input["params"]


def test_metadata_resolved_ecs_product_normalizes_action_and_period_aliases() -> None:
    tool_input = {"product": "ComputeAlias", "action": "DescribePrices", "params": {"PeriodType": "Monthly"}}
    original = copy.deepcopy(tool_input)

    normalized = _with_canonical_runtime_product(tool_input, "Ecs")

    assert normalized == {"product": "Ecs", "action": "DescribePrice", "params": {"PriceUnit": "Month"}}
    assert tool_input == original


def test_conflicting_disk_alias_does_not_partially_normalize_period_params() -> None:
    params = {"SystemDisk.Capacity": 60, "SystemDisk.Size": 40, "PeriodType": "Monthly"}
    original = copy.deepcopy(params)
    with pytest.raises(ApiContractError, match="^conflicting_parameter_alias$"):
        normalize_parameters("ecs", "DescribePrice", params)
    assert params == original
