"""Offline account-level references retain signed request-region provenance."""

import copy

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow import runtime as module


@pytest.fixture
def runtime(tmp_path):
    value = module.Runtime.__new__(module.Runtime)
    value.root = tmp_path
    return value


def reference(resource_type="ALIYUN::RAM::ManagedPolicy"):
    return {
        "resourceType": resource_type,
        "scopeKind": "account",
        "identifier": "offline-policy",
        "accountScope": "credential:offline",
        "stackId": "offline-stack",
        "sources": [
            {
                "toolUseId": "offline-read",
                "evidence": "api_response",
                "requestRegionId": "cn-hangzhou",
            }
        ],
    }


def proof():
    return {
        "account": "123",
        "reference_scope": "credential:offline",
        "region": "cn-hangzhou",
        "tool_use_id": "offline-read",
    }


@pytest.mark.parametrize(
    "resource_type",
    [
        "ALIYUN::RAM::ManagedPolicy",
        "alicloud_ram_policy",
        "ALIYUN::RAM::Role",
        "alicloud_ram_role",
        "ALIYUN::RAM::Group",
        "alicloud_ram_group",
        "ALIYUN::ResourceManager::ResourceGroup",
        "alicloud_resource_manager_resource_group",
    ],
)
def test_account_reference_uses_reviewed_scope_and_same_original_signed_request(runtime, monkeypatch, resource_type):
    monkeypatch.setattr(gate, "reference_observations", lambda *_: [proof()])
    value = reference(resource_type)
    before = copy.deepcopy(value)
    assert runtime.proven_references("job", "ROS", "ws~offline", "123", [value]) == [value]
    assert value == before  # Never manufacture regionId or rewrite original source.


@pytest.mark.parametrize(
    "patch",
    [
        {"resourceType": "ALIYUN::VPC::VSwitch"},
        {"resourceType": "unknown-type"},
        {"scopeKind": "regional"},
        {"scopeKind": None},
        {"regionId": "cn-hangzhou"},
        {"regionId": "cn-beijing"},
        {"accountScope": "credential:other"},
        {"sources": []},
        {"sources": [{"toolUseId": "other", "evidence": "api_response", "requestRegionId": "cn-hangzhou"}]},
        {"sources": [{"toolUseId": "offline-read", "evidence": "tool_input", "requestRegionId": "cn-hangzhou"}]},
        {"sources": [{"toolUseId": "offline-read", "evidence": "api_response"}]},
        {"sources": [{"toolUseId": "offline-read", "evidence": "api_response", "requestRegionId": "cn-beijing"}]},
    ],
)
def test_missing_region_never_loosens_unknown_regional_or_unsigned_references(runtime, monkeypatch, patch):
    monkeypatch.setattr(gate, "reference_observations", lambda *_: [proof()])
    value = {**reference(), **patch}
    before = copy.deepcopy(value)
    assert runtime.proven_references("job", "ROS", "ws~offline", "123", [value]) == []
    assert value == before


def test_proof_source_region_and_tool_must_match_on_the_same_source(runtime, monkeypatch):
    monkeypatch.setattr(gate, "reference_observations", lambda *_: [proof()])
    value = reference()
    value["sources"] = [
        {"toolUseId": "offline-read", "evidence": "api_response", "requestRegionId": "cn-beijing"},
        {"toolUseId": "other", "evidence": "api_response", "requestRegionId": "cn-hangzhou"},
    ]
    assert runtime.proven_references("job", "QUERY", "ws~offline", "123", [value]) == []
    assert runtime.proven_references("job", "QUERY", "ws~offline", "other-account", [reference()]) == []


@pytest.mark.parametrize("dimension,product", [("ROS", "ros"), ("TF", "ros"), ("CREATE", "ram")])
def test_created_account_reference_still_binds_original_stack_or_actual_response_identifier(
    runtime,
    monkeypatch,
    dimension,
    product,
):
    record = {
        "dimension": dimension,
        "session_id": "ws~offline",
        "state": "succeeded",
        "reference_scope": "credential:offline",
        "resource": {
            "id": "offline-stack" if product == "ros" else "offline-policy",
            "account": "123",
            "region": "cn-hangzhou",
            "product": product,
        },
    }
    monkeypatch.setattr(gate, "creations", lambda *_: [record])
    matching = reference()
    wrong_owner = {**matching, "stackId": "other-stack", "identifier": "other-policy"}
    wrong_region = {
        **matching,
        "sources": [{"toolUseId": "offline-read", "evidence": "api_response", "requestRegionId": "cn-beijing"}],
    }
    wrong_scope = {**matching, "accountScope": "credential:other"}
    values = [matching, wrong_owner, wrong_region, wrong_scope]
    before = copy.deepcopy(values)
    assert runtime.created_references("job", dimension, "ws~offline", values) == [matching]
    assert values == before


def test_regional_reference_keeps_exact_region_check(runtime, monkeypatch):
    monkeypatch.setattr(gate, "reference_observations", lambda *_: [proof()])
    value = {**reference("ALIYUN::VPC::VSwitch"), "scopeKind": "regional", "regionId": "cn-hangzhou"}
    assert runtime.proven_references("job", "QUERY", "ws~offline", "123", [value]) == [value]
    assert runtime.proven_references("job", "QUERY", "ws~offline", "123", [{**value, "regionId": "cn-beijing"}]) == []
