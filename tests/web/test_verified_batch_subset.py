"""Preserve authenticated exact originals without opening the P4 probe gate."""

import base64
import hashlib
import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def capture(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "subset", Path(__file__).parents[2] / "scripts/web/e2e/failed_resource_preflight.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workspace, session = "/fake/workspace", "own-session"
    key = "ws~" + base64.urlsafe_b64encode(json.dumps([workspace, session]).encode()).decode().rstrip("=")
    paths = {k: tmp_path / (k + ".json") for k in ("plan", "batch", "review", "ready", "create")}
    paths["references"] = tmp_path / re.sub(r"[^A-Za-z0-9-]", "-", workspace) / session / "refs.json"
    now = datetime.now(timezone.utc)
    source, canonical, identifier = "alicloud_privatelink_vpc_endpoint", "ALIYUN::PrivateLink::VpcEndpoint", "ep-fake"
    contract = "route_catalog:" + source + ":aliyun"
    plan = {
        "planId": "a" * 64,
        "catalogRevisionDigest": "b" * 64,
        "items": [
            {
                "sourceType": source,
                "canonicalResourceType": canonical,
                "receiptSource": "route_catalog",
                "contractId": contract,
                "sourceChainStatus": "passed",
                "sampleLevel": "L2",
                "actualApiCalled": True,
                "product": "privatelink",
                "action": "ListVpcEndpoints",
                "routeSourceBinding": {
                    "regionId": "cn-hangzhou",
                    "resourceIdentifierDigests": [hashlib.sha256(identifier.encode()).hexdigest()],
                },
            }
        ],
    }
    raw = {
        "sourceType": source,
        "contractId": contract,
        "source": "route_catalog",
        "navigation": "normal",
        "capturedAt": now.isoformat(),
        "regionId": "cn-hangzhou",
        "resourceIdentifier": identifier,
        "actualUiClick": True,
        "routeMatched": True,
        "loginPassed": True,
        "resourceMatched": True,
        "failureReason": None,
        "attemptCount": 1,
        "newTabObserved": False,
        "expectedNavigation": "iframe",
        "actualNavigation": "iframe",
        "landingKind": "exact_detail",
        "finalUrl": "https://vpc4service.console.aliyun.com/endpoint/cn-hangzhou/endpoints/ep-fake",
        "diagnostics": {
            "contentReady": True,
            "signals": {
                k: False
                for k in ("login", "wrongAccount", "wrongVariant", "permission", "missing", "gateway", "routeError")
            },
        },
    }
    raw_path, png = tmp_path / "raw.json", tmp_path / "raw.png"
    write(raw_path, {**raw, "canonicalResourceType": canonical})
    png.write_bytes(b"\x89PNG\r\n\x1a\noriginal-offline-fixture")
    artifacts = [
        {"kind": kind, "path": str(p), "digest": module.digest(p)}
        for kind, p in (("observation", raw_path), ("screenshot", png))
    ]
    batch = {
        "schemaVersion": 1,
        "planId": plan["planId"],
        "catalogRevisionDigest": plan["catalogRevisionDigest"],
        "browserAccessMode": "existing-chrome",
        "sessionIdDigest": hashlib.sha256(key.encode()).hexdigest(),
        "accountScopeDigest": hashlib.sha256(b"fake-account").hexdigest(),
        "items": [{**raw, "artifacts": artifacts}],
    }
    write(paths["plan"], plan)
    write(paths["batch"], batch)
    write(
        paths["ready"],
        {
            "origin": "http://127.0.0.1:8767",
            "ownSessionKey": key,
            "ready": True,
            "exactVisible": True,
            "ownSessionWasAbsentBefore": True,
        },
    )
    write(paths["create"], {"origin": "http://127.0.0.1:8767", "submitted": True, "preExistingSidebarKeys": []})
    write(
        paths["references"],
        {
            "references": [
                {
                    "resource_type": canonical,
                    "identifier": identifier,
                    "region_id": "cn-hangzhou",
                    "console_partition": "aliyun",
                    "evidence": "api_response",
                    "account_scope": "fake-account",
                    "sources": [
                        {
                            "product": "privatelink",
                            "action": "ListVpcEndpoints",
                            "evidence": "api_response",
                            "tool_use_id": "call-fake",
                            "observed_at": (now - timedelta(seconds=2)).timestamp(),
                        }
                    ],
                }
            ]
        },
    )
    write(
        paths["review"],
        {
            "originalBatchSHA256": module.digest(paths["batch"]),
            "planSHA256": module.digest(paths["plan"]),
            "items": [
                {
                    "sourceType": source,
                    "navigation": "normal",
                    "exactLoadedLandingReviewed": True,
                    "observationSHA256": module.digest(raw_path),
                    "screenshotSHA256": module.digest(png),
                }
            ],
        },
    )
    return module, {k: str(v) for k, v in paths.items()}, raw_path


def test_subset_preserves_original_and_stays_outside_p4(capture):
    module, paths, _ = capture
    gate = module.batch_subset_gate(paths)
    assert gate["status"] != "passed"
    item = gate["batch"]["items"][0]
    receipt = {
        **item,
        "liveDestinationStatus": "passed",
        **{
            k: gate["batch"][k]
            for k in ("sessionIdDigest", "accountScopeDigest", "catalogRevisionDigest", "browserAccessMode")
        },
    }
    module.validate_gate(receipt, gate)
    with pytest.raises(ValueError):
        module.validate_gate({**receipt, "liveDestinationStatus": "blocked"}, gate)
    assert item == json.loads(Path(paths["batch"]).read_text(encoding="utf-8"))["items"][0]


@pytest.mark.parametrize(
    "kind",
    [
        "flag",
        "route",
        "source",
        "account",
        "stale",
        "raw",
        "png",
        "review",
        "own-session",
        "duplicate",
        "outside",
        "empty",
    ],
)
def test_subset_rejects_tampering_and_unreviewed_results(capture, kind):
    module, paths, raw_path = capture
    if kind in {"raw", "png"}:
        path = raw_path if kind == "raw" else raw_path.with_suffix(".png")
        path.write_bytes(b"changed")
    elif kind in {"flag", "route"}:
        batch = module.load(Path(paths["batch"]))
        batch["items"][0].update({"loginPassed": False} if kind == "flag" else {"finalUrl": "https://evil.example"})
        write(Path(paths["batch"]), batch)
        review = module.load(Path(paths["review"]))
        review["originalBatchSHA256"] = module.digest(Path(paths["batch"]))
        write(Path(paths["review"]), review)
    elif kind in {"source", "account", "stale"}:
        refs = module.load(Path(paths["references"]))
        row = refs["references"][0]
        if kind == "source":
            row["identifier"] = "other"
        elif kind == "account":
            row["account_scope"] = "other"
        else:
            row["sources"][0]["observed_at"] -= 3600
        write(Path(paths["references"]), refs)
    elif kind == "own-session":
        create = module.load(Path(paths["create"]))
        create["preExistingSidebarKeys"] = [module.load(Path(paths["ready"]))["ownSessionKey"]]
        write(Path(paths["create"]), create)
    else:
        review = module.load(Path(paths["review"]))
        if kind == "review":
            review["items"][0]["exactLoadedLandingReviewed"] = False
        elif kind == "duplicate":
            review["items"] *= 2
        elif kind == "outside":
            review["items"][0]["sourceType"] = "alicloud_polardb_cluster"
        else:
            review["items"] = []
        write(Path(paths["review"]), review)
    with pytest.raises((ValueError, KeyError)):
        module.batch_subset_gate(paths)
