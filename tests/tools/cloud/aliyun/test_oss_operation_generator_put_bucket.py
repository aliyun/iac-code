"""Exact reviewed OSS SDK/OpenMeta body alias; no network or cloud credentials."""

from __future__ import annotations

import copy
import inspect
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from alibabacloud_oss_v2.aio.client import AsyncClient

ROOT = Path(__file__).resolve().parents[4]


def fixture():
    generator = runpy.run_path(str(ROOT / "scripts/aliyun/generate_oss_operations.py"))
    document = json.loads(
        (ROOT / "tests/tools/cloud/aliyun/fixtures/oss/openmeta_operations.json").read_text(encoding="utf-8")
    )
    metadata = next(row for row in document["operations"] if row["action"] == "PutBucket")
    model = inspect.signature(AsyncClient.put_bucket).parameters["request"].annotation
    return generator, metadata, model


def test_exact_put_bucket_xml_generates_supported_typed_body_mapping():
    generator, metadata, _ = fixture()
    row = generator["_operation_row"]("put_bucket", AsyncClient.put_bucket, metadata)
    assert row["supported"] is True and row["unsupported_reasons"] == []
    assert row["body_type"] == "xml" and row["response_mode"] == "headers_only"
    assert [field for field in row["field_mapping"] if field["location"] == "body"] == [
        {
            "location": "body",
            "openmeta_name": "body",
            "required": False,
            "sdk_field": "create_bucket_configuration",
            "sdk_type": "xml",
            "wire_name": "CreateBucketConfiguration",
        }
    ]


@pytest.mark.parametrize(
    "key,value",
    [
        ("action", "PutBucketPolicy"),
        ("method", "POST"),
        ("sdk_method", "put_bucket_policy"),
        ("consumes", ["application/json"]),
    ],
)
def test_put_bucket_xml_alias_rejects_other_operation_or_protocol(key, value):
    generator, metadata, model = fixture()
    metadata[key] = value
    assert generator["_reviewed_put_bucket_xml"](model, metadata) is False
    _, reasons = generator["_field_mapping"](model, metadata)
    assert "field_mapping_missing:body:body" in reasons


@pytest.mark.parametrize("change", ["model", "field", "rename", "type", "extra_body"])
def test_put_bucket_xml_alias_rejects_changed_sdk_body_schema(change):
    generator, metadata, model = fixture()
    attributes = copy.deepcopy(model._attribute_map)
    name = model.__name__
    if change == "model":
        name = "PutBucketPolicyRequest"
    elif change == "field":
        attributes["configuration"] = attributes.pop("create_bucket_configuration")
    elif change == "extra_body":
        attributes["other"] = copy.deepcopy(attributes["create_bucket_configuration"])
    else:
        attributes["create_bucket_configuration"][change] = "unexpected"
    altered = SimpleNamespace(__name__=name, _attribute_map=attributes)
    assert generator["_reviewed_put_bucket_xml"](altered, metadata) is False
    _, reasons = generator["_field_mapping"](altered, metadata)
    assert "field_mapping_missing:body:body" in reasons
