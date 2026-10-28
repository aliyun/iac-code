"""Offline coverage for the model-facing Step price projection."""

import copy

import pytest

from iac_code.tools.cloud.aliyun.api_hooks import project_step_result


@pytest.mark.parametrize("action", ["DescribePrice", "DescribePrices"])
def test_step_price_keeps_actual_total_and_each_component_without_mutating_inputs(ecs_price_body, action) -> None:
    original = copy.deepcopy(ecs_price_body)
    params = {"PriceUnit": "Month", "Period": 1}

    result = project_step_result("Ecs", action, params, ecs_price_body)

    assert result == {
        "Currency": "CNY",
        "PriceUnit": "Month",
        "Period": 1,
        "TradePrice": 152.75,
        "DetailInfos": {
            "DetailInfo": [
                {"Resource": "instanceType", "TradePrice": 134.78},
                {"Resource": "systemDisk", "TradePrice": 4.15},
                {"Resource": "dataDisk", "TradePrice": 13.82},
                {"Resource": "image", "TradePrice": 0},
            ]
        },
    }
    assert ecs_price_body == original
    assert params == {"PriceUnit": "Month", "Period": 1}


@pytest.mark.parametrize(
    ("params", "unit", "period"),
    [
        ({}, "Hour", 1),
        ({"PriceUnit": "Hour"}, "Hour", 1),
        ({"PriceUnit": "Week", "Period": 2}, "Week", 2),
        ({"PriceUnit": "Year", "Period": 1}, "Year", 1),
    ],
)
def test_price_unit_and_period_come_from_the_effective_request(ecs_price_body, params, unit, period) -> None:
    result = project_step_result("ecs", "DescribePrice", params, ecs_price_body)
    assert (result["PriceUnit"], result["Period"]) == (unit, period)
    assert result["TradePrice"] == 152.75  # Projection never rescales a quote.


def test_missing_actual_price_is_not_replaced_with_list_price_or_savings(ecs_price_body) -> None:
    price = ecs_price_body["PriceInfo"]["Price"]
    price.pop("TradePrice")
    price["DetailInfos"]["DetailInfo"][1].pop("TradePrice")

    result = project_step_result("ecs", "DescribePrice", {}, ecs_price_body)

    assert result["TradePrice"] is None
    assert result["DetailInfos"]["DetailInfo"][1]["TradePrice"] is None
    assert "OriginalPrice" not in str(result)
    assert "DiscountPrice" not in str(result)


@pytest.mark.parametrize("amount", [0, "0.00000001", None])
def test_actual_price_value_is_preserved_verbatim(ecs_price_body, amount) -> None:
    ecs_price_body["PriceInfo"]["Price"]["TradePrice"] = amount
    result = project_step_result("ecs", "DescribePrice", {}, ecs_price_body)
    assert type(result["TradePrice"]) is type(amount)
    assert result["TradePrice"] == amount


@pytest.mark.parametrize("body", [{"Code": "InvalidParameter", "Message": "Bad input"}, {}, None, "text"])
def test_non_price_and_business_error_bodies_are_untouched(body) -> None:
    assert project_step_result("ecs", "DescribePrice", {}, body) is body


@pytest.mark.parametrize(("product", "action"), [("vpc", "DescribePrice"), ("ecs", "DescribeInstances")])
def test_other_api_results_are_untouched(ecs_price_body, product, action) -> None:
    assert project_step_result(product, action, {}, ecs_price_body) is ecs_price_body
