"""Positive VPC business codes are accepted only for their exact route source."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).parents[2] / "scripts/web/e2e/run_resource_link_acceptance.py"
    spec = importlib.util.spec_from_file_location("offline_vpc_route_sources", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "code",
        "success",
        "missing_success",
        "message",
        "request_id",
        "product",
        "action",
        "type",
        "envelope",
        "rows",
        "numeric_code",
    ],
)
def test_vpc_route_table_positive_business_envelope(runner, mutation):
    body = {
        "Code": "200",
        "Message": "successful",
        "Success": True,
        "RequestId": "offline-request",
        "RouterTableList": {"RouterTableListType": [{"RouteTableId": "vtb-offline"}]},
    }
    event = {"product": "vpc", "action": "DescribeRouteTableList"}
    source_type = "alicloud_route_table"
    if mutation == "code":
        body["Code"] = "InvalidRegionId"
    elif mutation == "success":
        body["Success"] = False
    elif mutation == "missing_success":
        body.pop("Success")
    elif mutation == "message":
        body["Message"] = "failed"
    elif mutation == "request_id":
        body.pop("RequestId")
    elif mutation == "product":
        event["product"] = "ecs"
    elif mutation == "action":
        event["action"] = "DescribeRouterInterfaces"
    elif mutation == "type":
        source_type = "alicloud_router_interface"
    elif mutation == "envelope":
        body["RouterTableList"] = []
    elif mutation == "rows":
        body["RouterTableList"]["RouterTableListType"] = {}
    elif mutation == "numeric_code":
        body["Code"] = 200
    if mutation is None:
        assert runner._route_cloud_response_body(body, event, source_type) is body
    else:
        with pytest.raises(runner.MatrixError, match="successful business"):
            runner._route_cloud_response_body(body, event, source_type)


def test_legacy_success_without_code_keeps_existing_contract(runner):
    body = {"RequestId": "offline-request", "RouterTableList": {"RouterTableListType": []}}
    assert (
        runner._route_cloud_response_body(
            body, {"product": "vpc", "action": "DescribeRouteTableList"}, "alicloud_route_table"
        )
        is body
    )
