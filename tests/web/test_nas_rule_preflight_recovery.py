"""Offline NAS matcher false-negative recovery rejects scope/tuple ambiguity."""

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def recovery(monkeypatch):
    directory = Path(__file__).parents[2] / "scripts" / "web" / "e2e"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("nas_recovery", directory / "recover_nas_rule_preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def reference():
    return {
        "identifier": "1",
        "region_id": "cn-hangzhou",
        "attributes": {
            "AccessGroupName": "group-a",
            "FileSystemType": "standard",
            "AccessGroupType": "Classic",
            "SourceCidrIp": "0.0.0.0/0",
            "Priority": "100",
        },
        "sources": [
            {"tool_use_id": "call-1", "action": "DescribeAccessRules", "product": "nas", "evidence": "api_response"}
        ],
    }


@pytest.fixture
def rule():
    return {
        "AccessRuleId": "1",
        "RegionId": "cn-hangzhou",
        "AccessGroupName": "group-a",
        "FileSystemType": "standard",
        "SourceCidrIp": "0.0.0.0/0",
        "Priority": "100",
        "RWAccess": "RDWR",
        "UserAccess": "no_squash",
    }


@pytest.fixture
def raw(recovery):
    path = "/cn-hangzhou/access-group/standard/detail/standard/group-a/Classic/rule"
    return {
        "sourceType": recovery.SOURCE_TYPE,
        "canonicalResourceType": recovery.CANONICAL,
        "source": "route_catalog",
        "contractId": recovery.CONTRACT,
        "navigation": "normal",
        "resourceIdentifier": "1",
        "resourceParentAttributes": {"AccessGroupName": "group-a", "FileSystemType": "standard"},
        "regionId": "cn-hangzhou",
        "actualUiClick": True,
        "loginPassed": True,
        "attemptCount": 1,
        "routeMatched": False,
        "resourceMatched": False,
        "failureReason": "route_error",
        "actualNavigation": "iframe",
        "expectedNavigation": "iframe",
        "newTabObserved": False,
        "finalUrl": "https://nasnext4service.console.aliyun.com" + path,
        "diagnostics": {
            "expectedUrl": "https://nasnext.console.aliyun.com" + path,
            "signals": {
                "login": False,
                "permission": False,
                "wrongAccount": False,
                "wrongVariant": False,
                "missing": False,
                "gateway": False,
                "routeError": False,
            },
            "contentReady": True,
            "nasRule": {
                "parentRouteMatched": True,
                "rows": [
                    {"key": None, "cells": ["0.0.0.0/0", "读写", "所有用户不匿名（no_squash）", "100", "编辑|删除"]}
                ],
            },
        },
    }


def normalize(recovery, raw, reference, rule):
    return recovery.normalize(
        raw,
        mode="normal",
        reference=reference,
        rule=rule,
        raw_path=Path("original.json"),
        digest="a" * 64,
        manifest_digest="b" * 64,
    )


def test_recovery_exact_unique_visible_tuple_without_id(recovery, raw, reference, rule):
    original = deepcopy(raw)
    result = normalize(recovery, raw, reference, rule)
    assert raw == original
    assert result["resourceMatched"] and result["routeMatched"]
    assert result["landingKind"] == "product_list"
    assert result["recovery"]["sourceObservationDigest"] == "a" * 64


@pytest.mark.parametrize(
    "mutation",
    ["wrong_parent", "duplicate", "wrong_rw", "wrong_user", "not_ready", "permission", "no_click", "international"],
)
def test_recovery_rejects_false_proof(recovery, raw, reference, rule, mutation):
    row = raw["diagnostics"]["nasRule"]["rows"][0]
    if mutation == "wrong_parent":
        raw["finalUrl"] = raw["finalUrl"].replace("group-a", "group-b")
    elif mutation == "duplicate":
        raw["diagnostics"]["nasRule"]["rows"].append(deepcopy(row))
    elif mutation == "wrong_rw":
        row["cells"][1] = "只读"
    elif mutation == "wrong_user":
        row["cells"][2] = "所有用户匿名（all_squash）"
    elif mutation == "not_ready":
        raw["diagnostics"]["contentReady"] = False
    elif mutation == "permission":
        raw["diagnostics"]["signals"]["permission"] = True
    elif mutation == "no_click":
        raw["actualUiClick"] = False
    else:
        raw["finalUrl"] = raw["finalUrl"].replace("console.aliyun.com", "console.alibabacloud.com")
    with pytest.raises(recovery.RecoveryError):
        normalize(recovery, raw, reference, rule)


def test_api_tuple_requires_exact_call_parent_and_unique_row(recovery, reference, rule):
    import json

    request = {
        "type": "tool_use",
        "id": "call-1",
        "name": "aliyun_api",
        "input": {
            "product": "nas",
            "action": "DescribeAccessRules",
            "region_id": "cn-hangzhou",
            "params": {"AccessGroupName": "group-a", "FileSystemType": "standard"},
        },
    }
    response = {"RequestId": "request-fixture", "TotalCount": 1, "AccessRules": {"AccessRule": [rule]}}
    result = {"type": "tool_result", "tool_use_id": "call-1", "is_error": False, "content": json.dumps(response)}
    session = [{"content": [request, result]}]
    assert recovery.unique_api_rule(session, reference) == rule
    request["input"]["params"]["AccessGroupName"] = "other-group"
    with pytest.raises(recovery.RecoveryError):
        recovery.unique_api_rule(session, reference)
    request["input"]["params"]["AccessGroupName"] = "group-a"
    response["AccessRules"]["AccessRule"].append(dict(rule, AccessRuleId="2"))
    response["TotalCount"] = 2
    result["content"] = json.dumps(response)
    with pytest.raises(recovery.RecoveryError):
        recovery.unique_api_rule(session, reference)
