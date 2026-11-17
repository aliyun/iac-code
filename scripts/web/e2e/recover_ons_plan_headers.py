#!/usr/bin/env python3
"""Rebind stale ONS plan headers; never change or replay browser observations.

This is restricted to the reviewed domestic 4.x ONS route. Both official plans
must bind the same canonical/API/region/ID and the same immutable cloud source.
Only batch revision/plan ID and preflight revision are copied from the new
official plan. Ordinary acceptance validators still verify every observation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import run_resource_link_acceptance as gate


def normalize(
    old_plan: dict[str, Any],
    new_plan: dict[str, Any],
    batch: dict[str, Any],
    preflight: dict[str, Any],
    old_source: dict[str, Any],
    new_source: dict[str, Any],
    matrix: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    gate.validate_batch_plan(new_plan, matrix)
    if old_plan.get("planId") != gate._digest_json({k: v for k, v in old_plan.items() if k != "planId"}):
        raise gate.MatrixError("original official plan digest mismatch")
    if len(old_plan.get("items", [])) != 1 or len(new_plan["items"]) != 1:
        raise gate.MatrixError("only the single reviewed ONS route can be rebound")
    old_item, new_item = old_plan["items"][0], new_plan["items"][0]
    required = {
        "sourceType": "alicloud_ons_instance",
        "canonicalResourceType": "ALIYUN::ROCKETMQ::Instance",
        "receiptSource": "route_catalog",
        "contractId": "route_catalog:alicloud_ons_instance:aliyun",
        "product": "ons",
        "action": "OnsInstanceInServiceList",
        "sourceChainStatus": "passed",
        "sampleLevel": "L2",
        "actualApiCalled": True,
    }
    if any(old_item.get(k) != v or new_item.get(k) != v for k, v in required.items()):
        raise gate.MatrixError("unsupported route/source identity")
    if {k: v for k, v in old_item.items() if k != "sourceEvidenceDigest"} != {
        k: v for k, v in new_item.items() if k != "sourceEvidenceDigest"
    }:
        raise gate.MatrixError("plan bindings/navigation changed, not a header-only repair")
    proof_fields = (
        "sourceType", "canonicalResourceType", "contractId", "product", "action", "regionId",
        "resourceIdentifierDigests", "rawResponsePath", "rawResponseDigest", "reportPath", "reportDigest",
        "status", "actualApiCalled", "sampleLevel",
    )
    if any(old_source.get(k) != new_source.get(k) for k in proof_fields):
        raise gate.MatrixError("registered original cloud source changed")
    for plan, proof in ((old_plan, old_source), (new_plan, new_source)):
        if proof.get("catalogRevisionDigest") != plan["catalogRevisionDigest"] or (
            proof.get("sourceEvidenceDigest") != plan["items"][0]["sourceEvidenceDigest"]
        ):
            raise gate.MatrixError("plan is not bound to its official source registration")
        for path_key, digest_key in (("rawResponsePath", "rawResponseDigest"), ("reportPath", "reportDigest")):
            gate._validate_artifact(proof[path_key], proof[digest_key], label=path_key)
    if batch.get("planId") != old_plan["planId"] or (
        batch.get("catalogRevisionDigest") != old_plan["catalogRevisionDigest"]
        or preflight.get("catalogRevisionDigest") != old_plan["catalogRevisionDigest"]
    ):
        raise gate.MatrixError("original batch/preflight not bound to original plan")
    items = batch.get("items", [])
    if len(items) != 2 or {i.get("navigation") for i in items} != {"normal", "shift"}:
        raise gate.MatrixError("requires both original actual clicks, no missing branch repair")
    binding = new_item["routeSourceBinding"]
    for item in items:
        identifier_digest = hashlib.sha256(item.get("resourceIdentifier", "").encode("utf-8")).hexdigest()
        if item.get("regionId") != binding["regionId"] or identifier_digest not in binding["resourceIdentifierDigests"]:
            raise gate.MatrixError("original click does not match exact registered resource")
        if not all(item.get(k) is True for k in ("actualUiClick", "routeMatched", "loginPassed", "resourceMatched")):
            raise gate.MatrixError("cannot repair a failed or unexecuted click")
        if item.get("failureReason") is not None or item.get("landingKind") != "exact_detail":
            raise gate.MatrixError("requires original successful exact detail")
    normalized_batch, normalized_preflight = copy.deepcopy(batch), copy.deepcopy(preflight)
    normalized_batch["catalogRevisionDigest"] = new_plan["catalogRevisionDigest"]
    normalized_batch["planId"] = new_plan["planId"]
    normalized_preflight["catalogRevisionDigest"] = new_plan["catalogRevisionDigest"]
    captured = gate._parse_datetime(preflight["capturedAt"], label="original capturedAt")
    gate.validate_browser_preflight(normalized_preflight, matrix, now=captured + timedelta(seconds=1))
    gate.validate_browser_batch(normalized_batch, matrix, new_plan)
    # Explicitly retain all original flags, times, destinations, artifact paths
    # and SHA hashes; no screenshot or observation is rewritten.
    assert normalized_batch["items"] == batch["items"]
    assert {k: v for k, v in normalized_preflight.items() if k != "catalogRevisionDigest"} == {
        k: v for k, v in preflight.items() if k != "catalogRevisionDigest"
    }
    return normalized_batch, normalized_preflight


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-plan", "new-plan", "batch", "preflight", "old-source", "output-dir"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    inputs = {name: Path(getattr(args, name.replace("-", "_"))).resolve()
              for name in ("old-plan", "new-plan", "batch", "preflight", "old-source")}
    try:
        values = {name: gate._load_json_object(path, label=name) for name, path in inputs.items()}
        matrix = gate._load_json_object(gate.MATRIX_PATH, label="current official matrix")
        gate.validate_matrix(matrix)
        source = gate._route_cloud_source("alicloud_ons_instance")
        if source is None:
            raise gate.MatrixError("current official ONS registration missing")
        batch, preflight = normalize(
            values["old-plan"], values["new-plan"], values["batch"], values["preflight"],
            values["old-source"], source, matrix,
        )
        output = Path(args.output_dir).resolve()
        if any(output == path.parent for path in inputs.values()) or output.exists():
            raise gate.MatrixError("output must be a new directory, never replace originals")
        output.mkdir(parents=True)
        gate._atomic_write_json(output / "browser-batch.json", batch)
        gate._atomic_write_json(output / "browser-preflight-input.json", preflight)
        gate._atomic_write_json(output / "recovery-audit.json", {
            "reason": "supervisor generated plan before final frontend normalization",
            "scope": "header_only_no_click_flags_timestamps_urls_artifacts_changed",
            "inputDigests": {name: {"path": str(path), "sha256": gate._sha256(path)}
                            for name, path in inputs.items()},
            "oldRevision": values["old-plan"]["catalogRevisionDigest"],
            "newRevision": values["new-plan"]["catalogRevisionDigest"],
            "oldPlanId": values["old-plan"]["planId"], "newPlanId": values["new-plan"]["planId"],
            "browserOrCloudReplay": False, "ordinaryValidatorsPassed": True,
        })
        print(json.dumps({"status": "passed", "output": str(output)}))
        return 0
    except (gate.MatrixError, OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
