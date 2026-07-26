from __future__ import annotations

import json

import pytest

from scripts.aliyun.e2e_contract_audit import (
    audit_aliyun_result_contract,
    audit_public_payloads,
    find_latest_aliyun_tool_result,
)


def _metadata() -> dict:
    return {
        "aliyun_http": {
            "contract_version": "aliyun_body_v1",
            "product": "Vpc",
            "version": "2016-04-28",
            "action": "DescribeVpcs",
            "status": 200,
            "response_mode": "json",
            "body_format": "json",
        }
    }


def test_audit_accepts_business_fields_named_like_transport_fields() -> None:
    body = {"status": "business", "headers": ["legal"], "body": {"VpcId": "vpc-fixture"}}

    result = audit_aliyun_result_contract(
        expected_body=body,
        tool_result_content='{"status":"business","headers":["legal"],"body":{"VpcId":"vpc-fixture"}}',
        tool_result_metadata=_metadata(),
        resumed_content='{"status":"business","headers":["legal"],"body":{"VpcId":"vpc-fixture"}}',
        resumed_metadata=_metadata(),
        public_payloads=[body],
    )

    assert result["passed"] is True


def test_audit_rejects_internal_metadata_and_fixture_header_in_public_payload() -> None:
    result = audit_aliyun_result_contract(
        expected_body={"Vpcs": []},
        tool_result_content='{"Vpcs": []}',
        tool_result_metadata=_metadata(),
        public_payloads=[{"result": {"aliyun_http": {}}, "text": "secret-fixture-header"}],
        forbidden_values=["secret-fixture-header"],
    )

    assert result["passed"] is False
    leaks = result["failures"][0]["leaks"]
    assert {leak["reason"] for leak in leaks} == {"internal_key", "forbidden_value"}


def test_find_latest_aliyun_tool_result_selects_internal_result(tmp_path) -> None:
    first = tmp_path / "projects" / "one" / "session.jsonl"
    first.parent.mkdir(parents=True)
    first.write_text(json.dumps({"role": "assistant", "content": "ignore"}) + "\n", encoding="utf-8")
    latest = tmp_path / "projects" / "two" / "session.jsonl"
    latest.parent.mkdir(parents=True)
    expected = {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": '{"Vpcs": []}',
        "metadata": _metadata(),
    }
    latest.write_text(json.dumps({"role": "user", "content": [expected]}) + "\n", encoding="utf-8")

    path, block = find_latest_aliyun_tool_result(tmp_path)

    assert path == latest
    assert block == expected


def test_find_latest_aliyun_tool_result_requires_internal_result(tmp_path) -> None:
    with pytest.raises(AssertionError, match="no persisted aliyun_api ToolResult"):
        find_latest_aliyun_tool_result(tmp_path)


def test_audit_public_payloads_accepts_business_body_and_rejects_nested_metadata(tmp_path) -> None:
    clean = audit_public_payloads([{"result": {"RequestId": "request-1", "Vpcs": {"Vpc": []}}}])
    leaked = audit_public_payloads(
        [{"result": [{"metadata": {"aliyun_http": {"status": 200}}}], "text": "fixture-secret"}],
        forbidden_values=["fixture-secret"],
        output_path=tmp_path / "public-audit.json",
    )

    assert clean["passed"] is True
    assert leaked["passed"] is False
    assert {item["reason"] for item in leaked["leaks"]} == {"internal_key", "forbidden_value"}
    assert json.loads((tmp_path / "public-audit.json").read_text(encoding="utf-8")) == leaked
