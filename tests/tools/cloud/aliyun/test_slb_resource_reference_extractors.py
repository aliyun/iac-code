"""Offline SLB child identities: real response shapes, entirely fake values."""

import copy
import json

import pytest

from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references


def extract(action, body, params=None):
    return extract_api_resource_references(
        product="slb",
        action=action,
        body=body,
        params=params or {},
        region_id="cn-beijing",
        tool_use_id="offline-slb",
        account_scope="fake-account",
        console_partition="aliyun",
    )


def test_ca_certificate_uses_only_own_id_and_safe_metadata():
    result = extract(
        "DescribeCACertificates",
        {
            "CACertificates": {
                "CACertificate": [
                    {
                        "CACertificateId": "ca-fixture",
                        "CACertificateName": "example",
                        "Fingerprint": "00:11",
                        "RegionId": "cn-hangzhou",
                        "CACertificate": "secret-do-not-emit",
                    }
                ]
            }
        },
    )
    assert len(result) == 1
    assert result[0]["resource_type"] == "alicloud_slb_ca_certificate"
    assert result[0]["identifier"] == "ca-fixture"
    assert result[0]["identifier_field"] == "CACertificateId"
    assert result[0]["region_id"] == "cn-hangzhou"
    assert result[0]["sources"][0]["source_path"].endswith("/0/CACertificateId")
    assert "secret-do-not-emit" not in json.dumps(result)


def test_server_certificate_has_own_id_region_and_safe_metadata():
    body = {
        "ServerCertificates": {
            "ServerCertificate": [
                {
                    "ServerCertificateId": "server-fixture",
                    "ServerCertificateName": "server",
                    "Fingerprint": "aa:bb",
                    "RegionId": "cn-hangzhou",
                    "ServerCertificate": "private-content",
                    "PrivateKey": "private-key",
                    "CACertificateId": "not-the-server-id",
                }
            ]
        }
    }
    original = copy.deepcopy(body)
    result = extract("DescribeServerCertificates", body)
    assert len(result) == 1
    assert result[0]["resource_type"] == "alicloud_slb_server_certificate"
    assert result[0]["identifier"] == "server-fixture"
    assert result[0]["identifier_field"] == "ServerCertificateId"
    assert result[0]["region_id"] == "cn-hangzhou"
    assert result[0]["sources"][0]["source_path"].endswith("/0/ServerCertificateId")
    assert result[0]["attributes"]["ServerCertificateName"] == "server"
    assert "private-" not in json.dumps(result)
    assert body == original
    assert extract("DescribeCACertificates", body) == []


@pytest.mark.parametrize(
    "row",
    [
        {"CACertificateId": "ca-not-server"},
        {"ServerCertificateId": True},
        {"ServerCertificateId": ""},
        {"ServerCertificateId": None},
    ],
)
def test_server_certificate_never_invents_a_locator(row):
    assert extract("DescribeServerCertificates", {"ServerCertificates": {"ServerCertificate": [row]}}) == []


def test_listeners_keep_protocol_port_and_exact_parent_without_mutation():
    body = {
        "LoadBalancerId": "lb-fixture",
        "ListenerPortsAndProtocol": {
            "ListenerPortAndProtocol": [
                {"ListenerPort": 80, "ListenerProtocol": "http", "Description": "first"},
                {"ListenerPort": 80, "ListenerProtocol": "udp"},
            ]
        },
    }
    original = copy.deepcopy(body)
    result = extract("DescribeLoadBalancerAttribute", body, {"LoadBalancerId": "lb-fixture"})
    assert [r["identifier"] for r in result] == ["lb-fixture:http:80", "lb-fixture:udp:80"]
    assert all(r["attributes"]["load_balancer_id"] == "lb-fixture" for r in result)
    assert all(r["identifier_field"] == "ListenerId" for r in result)
    assert result[0]["attributes"]["ListenerPort"] == "80"
    assert result[0]["sources"][0]["source_path"].endswith("/0/ListenerPort")
    assert body == original
    assert extract("DescribeLoadBalancerAttribute", body, {"LoadBalancerId": "lb-wrong"}) == []


@pytest.mark.parametrize(
    "patch",
    [
        {"ListenerPort": True},
        {"ListenerPort": 0},
        {"ListenerPort": 65536},
        {"ListenerPort": "80"},
        {"ListenerProtocol": "smtp"},
        {"ListenerId": "invented"},
        {"load_balancer_id": "lb-wrong"},
        {"LoadBalancerId": "lb-wrong"},
    ],
)
def test_malformed_or_conflicting_listener_components_are_not_resources(patch):
    row = {"ListenerPort": 80, "ListenerProtocol": "http", **patch}
    body = {"LoadBalancerId": "lb-fixture", "ListenerPortsAndProtocol": {"ListenerPortAndProtocol": [row]}}
    assert extract("DescribeLoadBalancerAttribute", body, {"LoadBalancerId": "lb-fixture"}) == []


def test_listener_without_any_parent_is_not_a_resource():
    assert (
        extract(
            "DescribeLoadBalancerAttribute",
            {
                "ListenerPortsAndProtocol": {
                    "ListenerPortAndProtocol": [{"ListenerPort": 80, "ListenerProtocol": "http"}]
                }
            },
        )
        == []
    )


def test_bulk_listeners_bind_each_row_parent_and_do_not_emit_private_configs():
    body = {
        "Listeners": [
            {
                "LoadBalancerId": "lb-first",
                "ListenerPort": 80,
                "ListenerProtocol": "http",
                "HTTPListenerConfig": {"Cookie": "private-do-not-emit"},
            },
            {"LoadBalancerId": "lb-second", "ListenerPort": 80, "ListenerProtocol": "udp"},
        ]
    }
    original = copy.deepcopy(body)
    result = extract("DescribeLoadBalancerListeners", body, {"LoadBalancerId": ["lb-first", "lb-second"]})
    assert [r["identifier"] for r in result] == ["lb-first:http:80", "lb-second:udp:80"]
    assert [r["attributes"]["load_balancer_id"] for r in result] == ["lb-first", "lb-second"]
    assert result[1]["sources"][0]["source_path"].endswith("/Listeners/1/ListenerPort")
    assert "private-do-not-emit" not in json.dumps(result)
    assert body == original
    assert len(extract("DescribeLoadBalancerListeners", body)) == 2


@pytest.mark.parametrize("requested", [[], ["lb-other"], "lb-first", [True], [None], [" "], ["lb-first"] * 11])
def test_bulk_listener_rejects_invalid_or_inconsistent_parent_filter(requested):
    body = {"Listeners": [{"LoadBalancerId": "lb-first", "ListenerPort": 80, "ListenerProtocol": "http"}]}
    assert extract("DescribeLoadBalancerListeners", body, {"LoadBalancerId": requested}) == []


def test_bulk_listener_never_invents_missing_row_parent_from_single_filter():
    body = {"Listeners": [{"ListenerPort": 80, "ListenerProtocol": "http"}]}
    assert extract("DescribeLoadBalancerListeners", body, {"LoadBalancerId": ["lb-first"]}) == []


def test_master_slave_group_parent_is_not_the_group_identifier():
    body = {
        "MasterSlaveServerGroups": {
            "MasterSlaveServerGroup": [
                {"MasterSlaveServerGroupId": "rsp-fixture", "MasterSlaveServerGroupName": "group"}
            ]
        }
    }
    result = extract("DescribeMasterSlaveServerGroups", body, {"LoadBalancerId": "lb-fixture"})
    assert result[0]["identifier"] == "rsp-fixture"
    assert result[0]["attributes"]["LoadBalancerId"] == "lb-fixture"
    assert extract("DescribeMasterSlaveServerGroups", body) == []
    body["MasterSlaveServerGroups"]["MasterSlaveServerGroup"][0]["LoadBalancerId"] = "lb-other"
    assert extract("DescribeMasterSlaveServerGroups", body, {"LoadBalancerId": "lb-fixture"}) == []


def test_rule_requires_same_listener_tuple_and_never_copies_cookie():
    body = {"Rules": {"Rule": [{"RuleId": "rule-fixture", "RuleName": "rule", "Cookie": "private"}]}}
    params = {"LoadBalancerId": "lb-fixture", "ListenerPort": 80, "ListenerProtocol": "http"}
    result = extract("DescribeRules", body, params)
    assert result[0]["identifier"] == "rule-fixture"
    assert result[0]["attributes"]["ListenerPort"] == "80"
    assert result[0]["attributes"]["ListenerProtocol"] == "http"
    assert "private" not in json.dumps(result)
    assert extract("DescribeRules", body, {"LoadBalancerId": "lb-fixture"}) == []
    body["Rules"]["Rule"][0]["ListenerPort"] = 81
    assert extract("DescribeRules", body, params) == []
