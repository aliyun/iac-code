from __future__ import annotations

import base64
import copy
import importlib.util
import json
from pathlib import Path

import pytest
from tablestore.protobuf.table_store_pb2 import ListTableResponse


def runner():
    path = Path(__file__).parents[2] / "scripts/web/e2e/run_resource_link_acceptance.py"
    spec = importlib.util.spec_from_file_location("ots_source_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture():
    wire = ListTableResponse(table_names=["offline_table"]).SerializeToString()
    return {
        "captureKind": "ots_sdk_list_table_result",
        "statusCode": 200,
        "requestId": "offline-request",
        "headers": {"x-ots-requestid": "offline-request"},
        "version": "2015-12-31",
        "instanceName": "offline-instance",
        "regionId": "cn-hangzhou",
        "endpoint": "https://offline-instance.cn-hangzhou.ots.aliyuncs.com",
        "wireResponseBase64": base64.b64encode(wire).decode("ascii"),
        "decodedTableNames": ["offline_table"],
        "body": {
            "InstanceName": "offline-instance",
            "Tables": [
                {"TableName": "offline_table", "InstanceName": "offline-instance"},
            ],
        },
    }


def event():
    return {
        "captureKind": "ots_sdk_list_table_result",
        "product": "tablestore",
        "action": "ListTable",
        "version": "2015-12-31",
        "region": "cn-hangzhou",
        "params": {"InstanceName": "offline-instance"},
        "profile": "resource-link-test",
        "status": "ok",
        "exitCode": 0,
        "rawFile": "raw.json",
    }


def test_sdk_body_is_unchanged_and_bound_to_wire_names_and_parent():
    module = runner()
    raw = capture()
    original = copy.deepcopy(raw)
    result = module._route_cloud_response_body(raw, event(), "alicloud_ots_table")
    assert result is raw["body"]
    assert "RequestId" not in result
    assert raw == original


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_header",
        "wrong_header",
        "missing_request",
        "boolean_status",
        "failed_status",
        "wrong_kind",
        "wrong_event_kind",
        "wrong_action",
        "wrong_product",
        "wrong_type",
        "wrong_version",
        "wrong_event_version",
        "wrong_endpoint",
        "wrong_region",
        "wrong_request_region",
        "wrong_parent",
        "wrong_body_parent",
        "wrong_row_parent",
        "wrong_wire_names",
        "wrong_decoded_names",
        "invalid_base64",
        "invalid_proto",
        "empty_wire",
        "extra_body_field",
        "wrong_rows",
        "endpoint_injection",
    ],
)
def test_sdk_binding_fails_closed(mutation):
    module = runner()
    raw, action, source_type = capture(), event(), "alicloud_ots_table"
    if mutation == "missing_header":
        raw["headers"] = {}
    elif mutation == "wrong_header":
        raw["headers"]["x-ots-requestid"] = "other"
    elif mutation == "missing_request":
        raw.pop("requestId")
    elif mutation == "boolean_status":
        raw["statusCode"] = True
    elif mutation == "failed_status":
        raw["statusCode"] = 403
    elif mutation == "wrong_kind":
        raw["captureKind"] = "unreviewed"
    elif mutation == "wrong_event_kind":
        action.pop("captureKind")
    elif mutation == "wrong_action":
        action["action"] = "DeleteTable"
    elif mutation == "wrong_product":
        action["product"] = "oss"
    elif mutation == "wrong_type":
        source_type = "alicloud_ots_instance"
    elif mutation == "wrong_version":
        raw["version"] = "2020-12-09"
    elif mutation == "wrong_event_version":
        action["version"] = "2020-12-09"
    elif mutation == "wrong_endpoint":
        raw["endpoint"] = "https://other.example.com"
    elif mutation == "wrong_region":
        action["region"] = "cn-beijing"
    elif mutation == "wrong_request_region":
        action["params"]["RegionId"] = "cn-beijing"
    elif mutation == "wrong_parent":
        action["params"]["InstanceName"] = "other"
    elif mutation == "wrong_body_parent":
        raw["body"]["InstanceName"] = "other"
    elif mutation == "wrong_row_parent":
        raw["body"]["Tables"][0]["InstanceName"] = "other"
    elif mutation == "wrong_wire_names":
        raw["wireResponseBase64"] = base64.b64encode(
            ListTableResponse(table_names=["other"]).SerializeToString()
        ).decode("ascii")
    elif mutation == "wrong_decoded_names":
        raw["decodedTableNames"] = ["other"]
    elif mutation == "invalid_base64":
        raw["wireResponseBase64"] = "@@invalid@@"
    elif mutation == "invalid_proto":
        raw["wireResponseBase64"] = base64.b64encode(b"\xff").decode("ascii")
    elif mutation == "empty_wire":
        raw["wireResponseBase64"] = ""
    elif mutation == "extra_body_field":
        raw["body"]["RequestId"] = "invented"
    elif mutation == "wrong_rows":
        raw["body"]["Tables"] = {}
    else:
        raw["instanceName"] = "offline-instance/../other"
    with pytest.raises(module.MatrixError, match="SDK request binding"):
        module._route_cloud_response_body(raw, action, source_type)


@pytest.mark.parametrize("mutation", [None, "parent", "identifier", "profile", "path", "failed_event", "raw_change"])
def test_registered_route_uses_own_exact_table_sample(tmp_path, mutation):
    module = runner()
    module.ROUTE_CLOUD_SOURCE_PATH = tmp_path / "registry.json"
    raw = capture()
    report = {
        "purpose": "bounded_cloud_source_preparation_only",
        "profile": "resource-link-test",
        "observedAt": "2026-10-03T23:00:00Z",
        "regionsCovered": ["cn-hangzhou"],
        "results": [
            {
                "region": "cn-hangzhou",
                "events": [event()],
                "samples": {
                    "alicloud_ots_table": [
                        {
                            "id": "offline_table",
                            "region": "cn-hangzhou",
                            "rawFile": "raw.json",
                            "parentAttributes": {"InstanceName": "offline-instance"},
                        }
                    ]
                },
            }
        ],
    }
    sample = report["results"][0]["samples"]["alicloud_ots_table"][0]
    if mutation == "parent":
        sample["parentAttributes"]["InstanceName"] = "other"
    elif mutation == "identifier":
        sample["id"] = "other"
    elif mutation == "profile":
        report["results"][0]["events"][0]["profile"] = "other"
    elif mutation == "path":
        sample["rawFile"] = "../raw.json"
    elif mutation == "failed_event":
        report["results"][0]["events"][0]["exitCode"] = 1
    elif mutation == "raw_change":
        raw["body"]["Tables"][0]["TableName"] = "other"
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    (tmp_path / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    if mutation:
        with pytest.raises(module.MatrixError):
            module.register_route_cloud_source(path, "alicloud_ots_table", "cn-hangzhou")
        assert not module.ROUTE_CLOUD_SOURCE_PATH.exists()
    else:
        proof = module.register_route_cloud_source(path, "alicloud_ots_table", "cn-hangzhou")
        assert proof["contractId"] == "route_catalog:alicloud_ots_table:aliyun"
        assert proof["parentAttributes"] == {"InstanceName": "offline-instance"}
        assert (proof["sampleLevel"], proof["actualApiCalled"], proof["status"]) == ("L2", True, "passed")
