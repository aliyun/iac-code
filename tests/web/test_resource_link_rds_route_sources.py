"""Reviewed RDS Terraform instance aliases keep their own route evidence."""

import importlib.util
import json
from pathlib import Path

import pytest

from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.resource_references import resource_spec

ALIASES = ["alicloud_rds_clone_db_instance", "alicloud_rds_upgrade_db_instance"]


def test_parameter_template_has_its_own_regional_identity():
    spec = resource_spec("alicloud_rds_parameter_group")
    assert spec.resource_type == "alicloud_rds_parameter_group"
    assert spec.identifier_field == "ParameterGroupId"
    assert spec.scope_kind == "regional"
    assert spec is not resource_spec("ALIYUN::RDS::DBInstanceParameterGroup")


def test_parameter_template_route_source_is_bound_to_its_own_api_sample(tmp_path):
    runner_path = Path(__file__).parents[2] / "scripts/web/e2e/run_resource_link_acceptance.py"
    spec = importlib.util.spec_from_file_location("offline_parameter_route_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.ROUTE_CLOUD_SOURCE_PATH = tmp_path / "registry.json"
    source_type = "alicloud_rds_parameter_group"
    body = {
        "RequestId": "offline-request",
        "ParameterGroups": {
            "ParameterGroup": [
                {
                    "ParameterGroupId": "rpg-offline0001",
                    "ParameterGroupName": "offline",
                    "ParameterGroupType": 1,
                }
            ]
        },
    }
    (tmp_path / "raw.json").write_text(json.dumps(body), encoding="utf-8")
    event = {
        "product": "rds",
        "action": "DescribeParameterGroups",
        "profile": "resource-link-test",
        "region": "cn-hangzhou",
        "params": {"RegionId": "cn-hangzhou"},
        "status": "ok",
        "exitCode": 0,
        "rawFile": "raw.json",
    }
    report = {
        "purpose": "bounded_cloud_source_preparation_only",
        "profile": "resource-link-test",
        "observedAt": "2026-10-04T00:00:00Z",
        "regionsCovered": ["cn-hangzhou"],
        "results": [
            {
                "region": "cn-hangzhou",
                "events": [event],
                "samples": {
                    source_type: [
                        {
                            "id": "rpg-offline0001",
                            "region": "cn-hangzhou",
                            "rawFile": "raw.json",
                        }
                    ]
                },
            }
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    proof = runner.register_route_cloud_source(path, source_type, "cn-hangzhou")
    assert proof["contractId"] == f"route_catalog:{source_type}:aliyun"
    assert proof["canonicalResourceType"] == source_type
    assert proof["actualApiCalled"] is True
    assert proof["sampleLevel"] == "L2"


@pytest.mark.parametrize("mutation", [None, "wrong_product", "wrong_action", "wrong_envelope", "missing_id"])
def test_parameter_template_inventory_is_exact_reviewed_api(mutation):
    row = {"ParameterGroupId": "rpg-offline0001", "ParameterGroupName": "offline", "ParameterGroupType": 1}
    body = {"ParameterGroups": {"ParameterGroup": [row]}}
    product, action = "rds", "DescribeParameterGroups"
    if mutation == "wrong_product":
        product = "polardb"
    elif mutation == "wrong_action":
        action = "DescribeDBInstances"
    elif mutation == "wrong_envelope":
        body = {"Items": {"DBInstance": [row]}}
    elif mutation == "missing_id":
        row.pop("ParameterGroupId")
    refs = extract_api_resource_references(
        product=product,
        action=action,
        params={"RegionId": "cn-hangzhou"},
        body=body,
        region_id="cn-hangzhou",
        tool_use_id="offline-template",
        console_partition="aliyun",
    )
    if mutation:
        assert not refs
    else:
        assert len(refs) == 1
        assert refs[0]["resource_type"] == "alicloud_rds_parameter_group"
        assert refs[0]["identifier"] == "rpg-offline0001"
        assert refs[0]["region_id"] == "cn-hangzhou"


@pytest.mark.parametrize("source_type", ALIASES)
def test_rds_alias_is_exact_instance_identity(source_type):
    assert resource_spec(source_type) is resource_spec("ALIYUN::RDS::DBInstance")
    assert resource_spec(source_type).identifier_field == "DBInstanceId"
    assert resource_spec("alicloud_rds_parameter_group").resource_type != "ALIYUN::RDS::DBInstance"


@pytest.mark.parametrize("source_type", ALIASES)
@pytest.mark.parametrize("mutation", [None, "identifier", "region", "product", "profile"])
def test_rds_route_source_requires_own_exact_original_response(tmp_path, source_type, mutation):
    runner_path = Path(__file__).parents[2] / "scripts/web/e2e/run_resource_link_acceptance.py"
    spec = importlib.util.spec_from_file_location("offline_rds_route_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.ROUTE_CLOUD_SOURCE_PATH = tmp_path / "registry.json"
    body = {
        "RequestId": "offline-request",
        "Items": {
            "DBInstance": [
                {"DBInstanceId": "rm-offline0001", "RegionId": "cn-hangzhou"},
            ]
        },
    }
    (tmp_path / "raw.json").write_text(json.dumps(body), encoding="utf-8")
    refs = extract_api_resource_references(
        product="rds",
        action="DescribeDBInstances",
        params={"RegionId": "cn-hangzhou"},
        body=body,
        region_id="cn-hangzhou",
        tool_use_id="offline-source",
        console_partition="aliyun",
    )
    assert len(refs) == 1
    assert refs[0]["resource_type"] == resource_spec(source_type).resource_type
    event = {
        "product": "rds",
        "action": "DescribeDBInstances",
        "profile": "resource-link-test",
        "region": "cn-hangzhou",
        "params": {"RegionId": "cn-hangzhou"},
        "status": "ok",
        "exitCode": 0,
        "rawFile": "raw.json",
    }
    sample = {"id": refs[0]["identifier"], "region": "cn-hangzhou", "rawFile": "raw.json"}
    if mutation == "identifier":
        sample["id"] = "rm-not-returned"
    elif mutation == "region":
        event["params"]["RegionId"] = "cn-beijing"
    elif mutation == "product":
        event["product"] = "polardb"
    elif mutation == "profile":
        event["profile"] = "other-profile"
    report = {
        "purpose": "bounded_cloud_source_preparation_only",
        "profile": "resource-link-test",
        "observedAt": "2026-10-04T00:00:00Z",
        "regionsCovered": ["cn-hangzhou"],
        "results": [{"region": "cn-hangzhou", "events": [event], "samples": {source_type: [sample]}}],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    if mutation:
        with pytest.raises(runner.MatrixError):
            runner.register_route_cloud_source(path, source_type, "cn-hangzhou")
        assert not runner.ROUTE_CLOUD_SOURCE_PATH.exists()
    else:
        proof = runner.register_route_cloud_source(path, source_type, "cn-hangzhou")
        assert proof["contractId"] == f"route_catalog:{source_type}:aliyun"
        assert proof["actualApiCalled"] is True
        assert proof["sampleLevel"] == "L2"
        assert proof["canonicalResourceType"] == "ALIYUN::RDS::DBInstance"
