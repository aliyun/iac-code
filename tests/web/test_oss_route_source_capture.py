from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


def runner():
    path = Path(__file__).parents[2] / "scripts/web/e2e/run_resource_link_acceptance.py"
    spec = importlib.util.spec_from_file_location("oss_source_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture():
    return {
        "captureKind": "oss_v2_sdk_result",
        "statusCode": 200,
        "requestId": "offline-request",
        "headers": {"x-oss-request-id": "offline-request"},
        "body": {"buckets": [{"name": "offline-bucket", "region": "cn-hangzhou"}]},
    }


def event():
    return {"captureKind": "oss_v2_sdk_result", "product": "oss", "action": "ListBuckets"}


def test_sdk_body_is_losslessly_projected_without_invented_request_id():
    module = runner()
    raw = capture()
    original = copy.deepcopy(raw)
    result = module._route_cloud_response_body(raw, event(), "alicloud_oss_bucket")
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
        "failed_body",
        "wrong_kind",
        "wrong_event_kind",
        "wrong_action",
        "wrong_product",
        "wrong_type",
        "wrong_rows",
    ],
)
def test_sdk_binding_fails_closed(mutation):
    module = runner()
    raw, action, source_type = capture(), event(), "alicloud_oss_bucket"
    if mutation == "missing_header":
        raw["headers"] = {}
    elif mutation == "wrong_header":
        raw["headers"]["x-oss-request-id"] = "other-request"
    elif mutation == "missing_request":
        raw.pop("requestId")
    elif mutation == "boolean_status":
        raw["statusCode"] = True
    elif mutation == "failed_status":
        raw["statusCode"] = 403
    elif mutation == "failed_body":
        raw["body"]["Code"] = "AccessDenied"
    elif mutation == "wrong_kind":
        raw["captureKind"] = "unreviewed"
    elif mutation == "wrong_event_kind":
        action.pop("captureKind")
    elif mutation == "wrong_action":
        action["action"] = "GetBucketInfo"
    elif mutation == "wrong_product":
        action["product"] = "tablestore"
    elif mutation == "wrong_type":
        source_type = "alicloud_ots_table"
    else:
        raw["body"]["buckets"] = {}
    with pytest.raises(module.MatrixError, match="OSS SDK request binding"):
        module._route_cloud_response_body(raw, action, source_type)


def test_legacy_missing_business_request_still_rejected():
    module = runner()
    with pytest.raises(module.MatrixError, match="business request"):
        module._route_cloud_response_body({"Buckets": []}, {}, "alicloud_oss_bucket")
