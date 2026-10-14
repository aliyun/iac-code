"""Offline account-route request region and PrivateLink service alias bindings."""

import importlib.util
import json
from pathlib import Path

import pytest

from iac_code.tools.cloud.resource_references import resource_spec

CASES = [
    (
        "alicloud_pvtz_zone",
        "pvtz",
        "DescribeZones",
        "zone-fake",
        {},
        {"Zones": {"Zone": [{"ZoneId": "zone-fake", "ZoneName": "example.invalid"}]}},
    ),
    (
        "alicloud_pvtz_zone_record",
        "pvtz",
        "DescribeZoneRecords",
        "123",
        {"ZoneId": "zone-fake"},
        {"Records": {"Record": [{"RecordId": "123", "RR": "www", "Type": "A", "Value": "192.0.2.1"}]}},
    ),
    (
        "alicloud_ram_group",
        "ram",
        "ListGroups",
        "g-fake",
        {},
        {"Groups": {"Group": [{"GroupId": "g-fake", "GroupName": "FakeGroup"}]}},
    ),
    (
        "alicloud_ram_policy",
        "ram",
        "ListPolicies",
        "FakePolicy",
        {"PolicyType": "Custom"},
        {"Policies": {"Policy": [{"PolicyName": "FakePolicy", "PolicyType": "Custom"}]}},
    ),
    (
        "alicloud_privatelink_vpc_endpoint_service",
        "privatelink",
        "ListVpcEndpointServices",
        "epsrv-fake",
        {},
        {"Services": [{"ServiceId": "epsrv-fake", "ServiceName": "com.aliyuncs.fake"}]},
    ),
]


def runner_module(tmp_path):
    path = Path(__file__).parents[2] / "scripts/web/e2e/run_resource_link_acceptance.py"
    spec = importlib.util.spec_from_file_location("offline_route_account_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROUTE_CLOUD_SOURCE_PATH = tmp_path / "registry.json"
    return module


def write_fixture(tmp_path, case, mutation):
    source_type, product, action, identifier, params, body = case
    raw = {"RequestId": "offline-request", **body}
    (tmp_path / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    sample = {"id": identifier, "region": "cn-hangzhou", "rawFile": "raw.json"}
    if source_type == "alicloud_pvtz_zone_record":
        sample["parentAttributes"] = {"ZoneId": "zone-fake"}
    event = {
        "product": product,
        "action": action,
        "region": "cn-hangzhou",
        "profile": "test-guima",
        "status": "ok",
        "exitCode": 0,
        "rawFile": "raw.json",
        "params": params.copy(),
    }
    if mutation == "request_region":
        event["params"]["RegionId"] = "cn-beijing"
    elif mutation == "identifier":
        sample["id"] = "not-returned"
    elif mutation == "profile":
        event["profile"] = "wrong-profile"
    report = {
        "purpose": "bounded_cloud_source_preparation_only",
        "profile": "test-guima",
        "observedAt": "2026-10-04T00:00:00Z",
        "regionsCovered": ["cn-hangzhou"],
        "results": [{"region": "cn-hangzhou", "events": [event], "samples": {source_type: [sample]}}],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize("mutation", [None, "request_region", "identifier", "profile"])
def test_source_binds_exact_request_without_inventing_account_resource_region(tmp_path, case, mutation):
    module = runner_module(tmp_path)
    path = write_fixture(tmp_path, case, mutation)
    if mutation:
        with pytest.raises(module.MatrixError):
            module.register_route_cloud_source(path, case[0], "cn-hangzhou")
        assert not module.ROUTE_CLOUD_SOURCE_PATH.exists()
    else:
        proof = module.register_route_cloud_source(path, case[0], "cn-hangzhou")
        assert proof["contractId"] == f"route_catalog:{case[0]}:aliyun"
        assert proof["actualApiCalled"] is True
        assert proof["sampleLevel"] == "L2"
        assert proof["regionId"] == "cn-hangzhou"


def test_privatelink_service_tf_alias_shares_reviewed_production_identity():
    spec = resource_spec("alicloud_privatelink_vpc_endpoint_service")
    assert spec is resource_spec("ALIYUN::PrivateLink::VpcEndpointService")
    assert spec.resource_type == "ALIYUN::PrivateLink::VpcEndpointService"
    assert spec.scope_kind == "regional"
