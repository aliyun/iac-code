"""One authenticated original Shift cannot promote its unrendered iframe or P4."""

import hashlib
import json
from pathlib import Path

import pytest
import test_failed_resource_preflight as fixture_source


@pytest.fixture
def module():
    return fixture_source.module.__wrapped__()


@pytest.fixture
def paths(tmp_path):
    return fixture_source.paths.__wrapped__(tmp_path)


@pytest.fixture
def cluster_paths(paths):
    plan_path = Path(paths["plan"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["items"][0].update(
        sourceType="alicloud_polardb_cluster",
        canonicalResourceType="ALIYUN::POLARDB::DBCluster",
        contractId="route_catalog:alicloud_polardb_cluster:aliyun",
        product="polardb",
        action="DescribeDBClusters",
        routeSourceBinding={
            "regionId": "cn-hangzhou",
            "resourceIdentifierDigests": [hashlib.sha256(b"pc-fake123").hexdigest()],
        },
    )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    refs_path = Path(paths["references"])
    refs = json.loads(refs_path.read_text(encoding="utf-8"))
    refs["references"][0].update(
        identifier="pc-fake123", resource_type="ALIYUN::POLARDB::DBCluster", attributes={"DBClusterId": "pc-fake123"}
    )
    refs["references"][0]["sources"][0].update(product="polardb", action="DescribeDBClusters")
    refs_path.write_text(json.dumps(refs), encoding="utf-8")
    url = "https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-fake123/baseInfo"
    for p in Path(paths["directory"]).glob("*.json"):
        raw = json.loads(p.read_text(encoding="utf-8"))
        positive = raw["navigation"] == "shift"
        raw.update(
            sourceType="alicloud_polardb_cluster",
            canonicalResourceType="ALIYUN::POLARDB::DBCluster",
            contractId="route_catalog:alicloud_polardb_cluster:aliyun",
            resourceIdentifier="pc-fake123",
            attemptCount=1,
            loginPassed=positive,
            resourceMatched=positive,
            routeMatched=positive,
            failureReason=None if positive else "login_error",
            finalUrl=url if positive else None,
            landingKind="exact_detail" if positive else "unknown",
        )
        raw.pop("resourceParentAttributes")
        raw["diagnostics"].update(
            expectedUrl=url,
            contentReady=positive,
            signals={
                k: False
                for k in ("login", "wrongAccount", "wrongVariant", "permission", "missing", "gateway", "routeError")
            },
        )
        p.write_text(json.dumps(raw), encoding="utf-8")
    return paths


def test_cluster_keeps_shift_only_without_changing_originals(module, cluster_paths):
    before = {p: p.read_bytes() for p in Path(cluster_paths["directory"]).iterdir()}
    capture = module.verified_shift_gate(cluster_paths)
    assert [i["navigation"] for i in capture["batch"]["items"]] == ["shift"]
    assert all(p.read_bytes() == b for p, b in before.items())
    receipt = {
        k: capture["batch"][k]
        for k in ("sessionIdDigest", "accountScopeDigest", "catalogRevisionDigest", "browserAccessMode")
    }
    receipt.update(
        navigation="shift", artifacts=capture["batch"]["items"][0]["artifacts"], liveDestinationStatus="passed"
    )
    module.validate_gate(receipt, capture)
    with pytest.raises(ValueError):
        module.validate_gate({**receipt, "navigation": "normal"}, capture)
    with pytest.raises(ValueError):
        module.gate(cluster_paths)
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parents[2] / "scripts/web/e2e/resource_link_browser_preflight.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(capture))


@pytest.mark.parametrize(
    "branch,field,value",
    [
        ("shift", "loginPassed", False),
        ("shift", "routeMatched", False),
        ("shift", "resourceMatched", False),
        ("shift", "actualUiClick", False),
        ("shift", "canonicalResourceType", "other"),
        ("shift", "resourceIdentifier", "pc-wrong"),
        ("shift", "regionId", "cn-beijing"),
        ("shift", "failureReason", "route_error"),
        ("shift", "finalUrl", "https://attacker.example/baseInfo"),
        ("shift", "finalUrl", "https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-fake123/baseInfo?q=1"),
        ("normal", "actualUiClick", False),
        ("normal", "loginPassed", True),
        ("normal", "resourceIdentifier", "pc-wrong"),
        ("normal", "failureReason", "permission_denied"),
    ],
)
def test_cluster_rejects_wrong_scope_or_nonpositive_shift(module, cluster_paths, branch, field, value):
    p = next(Path(cluster_paths["directory"]).glob(f"*{branch}*.json"))
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw[field] = value
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        module.verified_shift_gate(cluster_paths)


def test_cluster_rejects_stale_source_and_tampered_artifact(module, cluster_paths):
    capture = module.verified_shift_gate(cluster_paths)
    p = next(Path(cluster_paths["directory"]).glob("*shift*.png"))
    p.write_bytes(p.read_bytes() + b"changed")
    receipt = {
        k: capture["batch"][k]
        for k in ("sessionIdDigest", "accountScopeDigest", "catalogRevisionDigest", "browserAccessMode")
    }
    receipt.update(
        navigation="shift", artifacts=capture["batch"]["items"][0]["artifacts"], liveDestinationStatus="passed"
    )
    with pytest.raises(ValueError):
        module.validate_gate(receipt, capture)
    p = Path(cluster_paths["references"])
    refs = json.loads(p.read_text(encoding="utf-8"))
    refs["references"][0]["sources"][0]["observed_at"] -= 3600
    p.write_text(json.dumps(refs), encoding="utf-8")
    with pytest.raises(ValueError, match="fresh UI API source"):
        module.verified_shift_gate(cluster_paths)
