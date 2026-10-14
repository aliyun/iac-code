"""Offline VPN customer gateway alias, same-object locator and safe projection."""

import copy
import json

import pytest

from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.resource_references import resource_spec


def extract(body):
    return extract_api_resource_references(
        product="vpc",
        action="DescribeCustomerGateways",
        params={"RegionId": "cn-hangzhou"},
        body=body,
        region_id="cn-hangzhou",
        tool_use_id="offline-vpn",
        account_scope="fake-account",
        console_partition="aliyun",
    )


def test_tf_alias_is_the_reviewed_regional_customer_gateway():
    spec = resource_spec("alicloud_vpn_customer_gateway")
    assert spec == resource_spec("ALIYUN::VPC::CustomerGateway")
    assert spec.resource_type == "ALIYUN::VPC::CustomerGateway"
    assert spec.identifier_field == "CustomerGatewayId"
    assert spec.scope_kind == "regional"


def test_own_id_and_response_path_without_mutation_or_private_metadata():
    body = {
        "CustomerGateways": {
            "CustomerGateway": [
                {
                    "CustomerGatewayId": "cgw-offline",
                    "Name": "fixture",
                    "IpAddress": "192.0.2.1",
                    "VpnGatewayId": "vpn-not-customer",
                    "PrivateKey": "private-do-not-emit",
                }
            ]
        }
    }
    original = copy.deepcopy(body)
    refs = extract(body)
    assert len(refs) == 1
    assert refs[0]["resource_type"] == "ALIYUN::VPC::CustomerGateway"
    assert refs[0]["identifier"] == "cgw-offline"
    assert refs[0]["identifier_field"] == "CustomerGatewayId"
    assert refs[0]["region_id"] == "cn-hangzhou"
    assert refs[0]["sources"][0]["source_path"].endswith("/0/CustomerGatewayId")
    assert "private-do-not-emit" not in json.dumps(refs)
    assert body == original


@pytest.mark.parametrize(
    "row",
    [
        {"VpnGatewayId": "vpn-not-customer"},
        {"CustomerGatewayId": True},
        {"CustomerGatewayId": 123},
        {"CustomerGatewayId": ""},
        {"CustomerGatewayId": None},
    ],
)
def test_no_coerced_or_borrowed_customer_gateway_locator(row):
    assert extract({"CustomerGateways": {"CustomerGateway": [row]}}) == []


def test_waf3_response_is_not_legacy_waf_instance_source():
    refs = extract_api_resource_references(
        product="waf-openapi",
        action="DescribeInstance",
        params={"RegionId": "cn-hangzhou"},
        body={"InstanceId": "waf3-offline", "RegionId": "cn-hangzhou"},
        region_id="cn-hangzhou",
        tool_use_id="offline-waf",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    assert all(ref["resource_type"] != "alicloud_waf_instance" for ref in refs)
