"""Offline header-only recovery never upgrades failed clicks or changed samples."""

import hashlib
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def setup(monkeypatch, tmp_path):
    directory = Path(__file__).parents[2] / "scripts" / "web" / "e2e"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("ons_recovery", directory / "recover_ons_plan_headers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks = []
    monkeypatch.setattr(module.gate, "validate_batch_plan", lambda *a: checks.append("plan"))
    monkeypatch.setattr(module.gate, "validate_browser_batch", lambda *a: checks.append("batch"))
    monkeypatch.setattr(module.gate, "validate_browser_preflight", lambda *a, **k: checks.append("preflight"))
    raw, report = tmp_path / "raw.json", tmp_path / "report.json"
    raw.write_text('{"InstanceId":"MQ_INST_fake"}', encoding="utf-8")
    report.write_text('{"profile":"fake"}', encoding="utf-8")
    binding = {"regionId": "cn-hangzhou", "resourceIdentifierDigests": [hashlib.sha256(b"MQ_INST_fake").hexdigest()]}
    item = {
        "sourceType": "alicloud_ons_instance",
        "canonicalResourceType": "ALIYUN::ROCKETMQ::Instance",
        "receiptSource": "route_catalog",
        "contractId": "route_catalog:alicloud_ons_instance:aliyun",
        "product": "ons",
        "action": "OnsInstanceInServiceList",
        "sourceChainStatus": "passed",
        "sampleLevel": "L2",
        "actualApiCalled": True,
        "sourceEvidenceDigest": "old-source",
        "allowedNavigations": ["normal", "shift"],
        "routeSourceBinding": binding,
    }
    old = {"items": [item], "catalogRevisionDigest": "old"}
    old["planId"] = module.gate._digest_json(old)
    new = deepcopy(old)
    new["items"][0]["sourceEvidenceDigest"] = "new-source"
    new["catalogRevisionDigest"] = "new"
    new["planId"] = module.gate._digest_json({k: v for k, v in new.items() if k != "planId"})
    proof = {
        **{k: v for k, v in item.items() if k not in {"receiptSource", "routeSourceBinding"}},
        "status": "passed",
        **binding,
        "catalogRevisionDigest": "old",
        "rawResponsePath": str(raw),
        "rawResponseDigest": module.gate._sha256(raw),
        "reportPath": str(report),
        "reportDigest": module.gate._sha256(report),
    }
    new_proof = {**proof, "catalogRevisionDigest": "new", "sourceEvidenceDigest": "new-source"}
    observation = {
        "regionId": "cn-hangzhou",
        "resourceIdentifier": "MQ_INST_fake",
        "actualUiClick": True,
        "routeMatched": True,
        "loginPassed": True,
        "resourceMatched": True,
        "failureReason": None,
        "landingKind": "exact_detail",
        "capturedAt": "2026-01-01T00:00:01Z",
        "finalUrl": "https://ons.console.aliyun.com/region/cn-hangzhou/instance/MQ_INST_fake/detail",
        "artifacts": [{"digest": "fake-original-digest"}],
    }
    batch = {
        "catalogRevisionDigest": "old",
        "planId": old["planId"],
        "items": [
            {**observation, "navigation": "normal"},
            {**observation, "navigation": "shift"},
        ],
    }
    preflight = {"catalogRevisionDigest": "old", "capturedAt": "2026-01-01T00:00:00Z"}
    return module, [old, new, batch, preflight, proof, new_proof, {}], checks


def test_only_headers_change_and_ordinary_gates_run(setup):
    module, args, checks = setup
    original = deepcopy(args)
    batch, preflight = module.normalize(*args)
    assert args == original
    assert batch["items"] == original[2]["items"]
    assert batch["catalogRevisionDigest"] == "new"
    assert batch["planId"] == original[1]["planId"]
    assert preflight["capturedAt"] == original[3]["capturedAt"]
    assert checks == ["plan", "preflight", "batch"]


@pytest.mark.parametrize("kind", ["failed", "missing", "changed_id", "changed_region", "changed_proof", "bad_plan"])
def test_rejects_not_header_only(setup, kind):
    module, args, _checks = setup
    if kind == "failed":
        args[2]["items"][0]["actualUiClick"] = False
    elif kind == "missing":
        args[2]["items"].pop()
    elif kind == "changed_id":
        args[2]["items"][0]["resourceIdentifier"] = "MQ_INST_other"
    elif kind == "changed_region":
        args[1]["items"][0]["routeSourceBinding"]["regionId"] = "cn-beijing"
    elif kind == "changed_proof":
        args[5]["rawResponseDigest"] = "0" * 64
    else:
        args[0]["planId"] = "fake"
    with pytest.raises(module.gate.MatrixError):
        module.normalize(*args)


def test_rejects_modified_original_cloud_file(setup):
    module, args, _checks = setup
    Path(args[4]["rawResponsePath"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(module.gate.MatrixError):
        module.normalize(*args)
