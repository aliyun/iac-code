#!/usr/bin/env python3
"""Recover a real browser preflight pair after a composite-ID matcher false negative.

This tool never drives a browser and never invents a receipt. It verifies a
worker manifest, two immutable Chrome observations, their screenshots, the
materialized iac-code session reference, and the exact current batch plan. It
then emits normalized observation copies plus a browser batch/preflight that
the ordinary acceptance gate can validate and archive.

The initial reviewed recovery rule is intentionally narrow: a domestic
``ALIYUN::CEN::RouteEntry`` reference whose composite identity ends in a
``vtb-*`` child route-table ID and whose real UI branches both reached that
exact VPC route-table detail page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


class RecoveryError(RuntimeError):
    """Raised when immutable evidence cannot be safely recovered."""


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryError(f"{label} is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise RecoveryError(f"{label} must be a JSON object: {path}")
    return value


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise RecoveryError(f"{label} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryError(f"{label} is not a valid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise RecoveryError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _contains_exact(value: Any, literal: str) -> bool:
    text = unquote(str(value or ""))
    pattern = re.compile(rf"(^|[^A-Za-z0-9_-]){re.escape(literal)}($|[^A-Za-z0-9_-])")
    return pattern.search(text) is not None


def _verified_manifest(root: Path, manifest_path: Path) -> tuple[dict[str, str], str]:
    manifest = _load_object(manifest_path, "worker manifest")
    files = manifest.get("files")
    if manifest.get("schemaVersion") != 1 or not isinstance(files, list) or not files:
        raise RecoveryError("worker manifest must contain a non-empty schema-v1 files array")
    verified: dict[str, str] = {}
    root_resolved = root.resolve()
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RecoveryError("worker manifest contains an invalid file entry")
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.name == "manifest.json":
            raise RecoveryError(f"worker manifest contains an unsafe path: {relative}")
        target = (root / relative).resolve()
        if root_resolved not in target.parents:
            raise RecoveryError(f"worker manifest path escapes its output root: {relative}")
        expected = entry.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RecoveryError(f"worker manifest digest is invalid: {relative}")
        if not target.is_file() or _digest(target) != expected:
            raise RecoveryError(f"worker manifest digest mismatch: {relative}")
        verified[relative.as_posix()] = expected
    return verified, _digest(manifest_path)


def _manifest_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise RecoveryError(f"input is outside the worker output root: {path}") from error


def _require_manifest_file(root: Path, path: Path, verified: dict[str, str], label: str) -> str:
    relative = _manifest_relative(root, path)
    digest = verified.get(relative)
    if digest is None or not path.is_file() or _digest(path) != digest:
        raise RecoveryError(f"{label} is not protected by the verified worker manifest")
    return digest


def _route_table_identity(identifier: Any) -> tuple[str, str, str]:
    if not isinstance(identifier, str):
        raise RecoveryError("composite resource identifier is missing")
    parts = identifier.split("/")
    patterns = (r"cen-[A-Za-z0-9]+", r"vpc-[A-Za-z0-9]+", r"vtb-[A-Za-z0-9]+")
    if len(parts) != 3 or any(re.fullmatch(pattern, part) is None for pattern, part in zip(patterns, parts)):
        raise RecoveryError("RouteEntry identifier is not the reviewed cen/vpc/vtb composite shape")
    return parts[0], parts[1], parts[2]


def _matching_reference(references: dict[str, Any], identifier: str) -> tuple[dict[str, Any], str]:
    items = references.get("items")
    if not isinstance(items, list):
        raise RecoveryError("session resource references have no items array")
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("resourceType") == "ALIYUN::CEN::RouteEntry"
        and item.get("identifier") == identifier
    ]
    if len(matches) != 1:
        raise RecoveryError("session must contain exactly one matching RouteEntry reference")
    reference = matches[0]
    if reference.get("consolePartition") != "aliyun" or not reference.get("accountScope"):
        raise RecoveryError("RouteEntry reference is not bound to a domestic Aliyun account")
    sources = reference.get("sources")
    if not isinstance(sources, list):
        raise RecoveryError("RouteEntry reference has no source facts")
    ros_sources = [
        source
        for source in sources
        if isinstance(source, dict)
        and str(source.get("product", "")).casefold() == "ros"
        and source.get("action") in {"GetStackResource", "ListStackResources"}
        and source.get("evidence") == "api_response"
    ]
    regions = {source.get("requestRegionId") for source in ros_sources if source.get("requestRegionId")}
    if len(ros_sources) != 1 or len(regions) != 1:
        raise RecoveryError("RouteEntry reference must have one unambiguous ROS API source region")
    region = next(iter(regions))
    if not isinstance(region, str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", region) is None:
        raise RecoveryError("RouteEntry source region is invalid")
    return reference, region


def _normalize_observation(
    raw: dict[str, Any], *, navigation: str, identifier: str, region: str, child_id: str,
    raw_path: Path, raw_digest: str, manifest_digest: str, paired_body_identity: bool,
) -> dict[str, Any]:
    required = {
        "sourceType": "ALIYUN::CEN::RouteEntry",
        "canonicalResourceType": "ALIYUN::CEN::RouteEntry",
        "source": "ros_stack",
        "contractId": "ros_stack:ALIYUN::CEN::RouteEntry",
        "navigation": navigation,
        "resourceIdentifier": identifier,
        "actualUiClick": True,
        "loginPassed": True,
        "attemptCount": 1,
    }
    mismatched = [field for field, expected in required.items() if raw.get(field) != expected]
    if mismatched:
        raise RecoveryError(f"{navigation} observation has mismatched fields: {', '.join(mismatched)}")
    if navigation == "normal":
        if raw.get("actualNavigation") != "iframe" or raw.get("newTabObserved") is not False:
            raise RecoveryError("ordinary observation is not a real iframe click")
        expected_host = "vpc4service.console.aliyun.com"
    else:
        if raw.get("actualNavigation") != "new_tab" or raw.get("newTabObserved") is not True:
            raise RecoveryError("Shift observation is not a real new-tab click")
        expected_host = "vpc.console.aliyun.com"
    if raw.get("resourceMatched") is not False or raw.get("routeMatched") is not False:
        raise RecoveryError("recovery only accepts the reviewed composite-ID false-negative shape")
    if raw.get("failureReason") not in {"route_error", "inconclusive"}:
        raise RecoveryError("observation failure is not the reviewed matcher false negative")

    diagnostics = raw.get("diagnostics")
    if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("signals"), dict):
        raise RecoveryError(f"{navigation} observation has no page diagnostics")
    if any(value is not False for value in diagnostics["signals"].values()):
        raise RecoveryError(f"{navigation} observation contains a real page failure signal")
    body = diagnostics.get("bodyExcerpt")
    body_identity_matched = _contains_exact(body, child_id) and "路由条目列表" in str(body)
    if not body_identity_matched and not (navigation == "shift" and paired_body_identity):
        raise RecoveryError(f"{navigation} observation does not render the exact child route-table detail")

    final = urlsplit(str(raw.get("finalUrl") or ""))
    expected = urlsplit(str(diagnostics.get("expectedUrl") or ""))
    expected_path = f"/vpc/{region}/route-tables/{child_id}"
    if (
        final.scheme != "https"
        or final.hostname != expected_host
        or final.path.rstrip("/") != expected_path
        or final.username
        or final.password
        or final.query
        or final.fragment
    ):
        raise RecoveryError(f"{navigation} final URL is not the exact reviewed VPC route-table detail")
    if expected.scheme != "https" or expected.hostname != "vpc.console.aliyun.com" or expected.path.rstrip("/") != expected_path:
        raise RecoveryError(f"{navigation} expected URL is not the exact production destination")

    normalized = dict(raw)
    normalized.update(
        regionId=region,
        landingKind="exact_detail",
        routeMatched=True,
        resourceMatched=True,
        failureReason=None,
        recovery={
            "schemaVersion": 1,
            "rule": "composite_child_detail_v1",
            "childIdentifier": child_id,
            "sourceObservationPath": str(raw_path.resolve()),
            "sourceObservationDigest": raw_digest,
            "workerManifestDigest": manifest_digest,
            "identityEvidence": (
                "body_child_identifier"
                if body_identity_matched
                else "paired_normal_body_plus_manifest_verified_shift_screenshot"
            ),
        },
    )
    return normalized


def recover(args: argparse.Namespace) -> dict[str, Any]:
    root = args.manifest.resolve().parent
    verified, manifest_digest = _verified_manifest(root, args.manifest)
    session_digest = _require_manifest_file(root, args.session_materialization, verified, "session materialization")
    references_digest = _require_manifest_file(root, args.session_references, verified, "session references")
    normal_raw_digest = _require_manifest_file(root, args.normal_observation, verified, "normal observation")
    shift_raw_digest = _require_manifest_file(root, args.shift_observation, verified, "Shift observation")
    normal_screenshot_digest = _require_manifest_file(root, args.normal_screenshot, verified, "normal screenshot")
    shift_screenshot_digest = _require_manifest_file(root, args.shift_screenshot, verified, "Shift screenshot")

    plan = _load_object(args.plan, "batch plan")
    items = plan.get("items")
    if (
        plan.get("schemaVersion") != 1
        or not isinstance(items, list)
        or len(items) != 1
        or items[0].get("sourceType") != "ALIYUN::CEN::RouteEntry"
        or items[0].get("receiptSource") != "ros_stack"
        or items[0].get("contractId") != "ros_stack:ALIYUN::CEN::RouteEntry"
        or set(items[0].get("allowedNavigations", [])) != {"normal", "shift"}
        or items[0].get("sourceChainStatus") != "passed"
        or items[0].get("sampleLevel") not in {"L2", "L3"}
        or items[0].get("actualApiCalled") is not True
    ):
        raise RecoveryError("batch plan is not the reviewed one-contract RouteEntry plan")
    if not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("catalogRevisionDigest") or "")):
        raise RecoveryError("batch plan catalog revision is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("planId") or "")):
        raise RecoveryError("batch plan ID is invalid")

    materialization = _load_object(args.session_materialization, "session materialization")
    session_id = materialization.get("sessionId")
    if materialization.get("phase") != "complete" or not isinstance(session_id, str) or not session_id:
        raise RecoveryError("session materialization is incomplete")
    references = _load_object(args.session_references, "session references")
    if references.get("sessionId") != session_id:
        raise RecoveryError("session references do not belong to the materialized session")

    normal_raw = _load_object(args.normal_observation, "normal observation")
    shift_raw = _load_object(args.shift_observation, "Shift observation")
    if normal_raw.get("resourceIdentifier") != shift_raw.get("resourceIdentifier"):
        raise RecoveryError("ordinary and Shift observations do not use the same resource")
    identifier = normal_raw.get("resourceIdentifier")
    _parent_id, _vpc_id, child_id = _route_table_identity(identifier)
    reference, region = _matching_reference(references, identifier)
    normal = _normalize_observation(
        normal_raw,
        navigation="normal",
        identifier=identifier,
        region=region,
        child_id=child_id,
        raw_path=args.normal_observation,
        raw_digest=normal_raw_digest,
        manifest_digest=manifest_digest,
        paired_body_identity=False,
    )
    shift = _normalize_observation(
        shift_raw,
        navigation="shift",
        identifier=identifier,
        region=region,
        child_id=child_id,
        raw_path=args.shift_observation,
        raw_digest=shift_raw_digest,
        manifest_digest=manifest_digest,
        paired_body_identity=_contains_exact(normal_raw.get("diagnostics", {}).get("bodyExcerpt"), child_id),
    )
    captured = min(_parse_time(normal["capturedAt"], "normal capturedAt"), _parse_time(shift["capturedAt"], "Shift capturedAt"))
    if abs((_parse_time(normal["capturedAt"], "normal capturedAt") - _parse_time(shift["capturedAt"], "Shift capturedAt")).total_seconds()) > 300:
        raise RecoveryError("ordinary and Shift observations are not one bounded browser pair")

    output_dir = args.output_dir.resolve()
    normal_output = output_dir / "browser-artifacts" / "recovered-route-entry-normal.json"
    shift_output = output_dir / "browser-artifacts" / "recovered-route-entry-shift.json"
    _write_json(normal_output, normal)
    _write_json(shift_output, shift)
    normal_output_digest = _digest(normal_output)
    shift_output_digest = _digest(shift_output)

    def batch_item(observation: dict[str, Any], observation_path: Path, observation_digest: str,
                   screenshot: Path, screenshot_digest: str) -> dict[str, Any]:
        item = {
            key: value
            for key, value in observation.items()
            if key not in {"canonicalResourceType", "recovery"}
        }
        item["artifacts"] = [
            {"kind": "observation", "path": str(observation_path), "digest": observation_digest},
            {"kind": "screenshot", "path": str(screenshot.resolve()), "digest": screenshot_digest},
        ]
        return item

    session_id_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    account_scope_digest = hashlib.sha256(str(reference["accountScope"]).encode("utf-8")).hexdigest()
    batch = {
        "schemaVersion": 1,
        "catalogRevisionDigest": plan["catalogRevisionDigest"],
        "planId": plan["planId"],
        "browserAccessMode": "existing-chrome",
        "sessionIdDigest": session_id_digest,
        "accountScopeDigest": account_scope_digest,
        "items": [
            batch_item(normal, normal_output, normal_output_digest, args.normal_screenshot, normal_screenshot_digest),
            batch_item(shift, shift_output, shift_output_digest, args.shift_screenshot, shift_screenshot_digest),
        ],
    }
    preflight = {
        "schemaVersion": 1,
        "capturedAt": captured.isoformat().replace("+00:00", "Z"),
        "expiresAt": (captured + timedelta(minutes=25)).isoformat().replace("+00:00", "Z"),
        "catalogRevisionDigest": plan["catalogRevisionDigest"],
        "status": "passed",
        "surface": "iac-code-web",
        "browserAccessMode": "existing-chrome",
        "sessionIdDigest": session_id_digest,
        "accountScopeDigest": account_scope_digest,
        "sample": {
            "resourceType": "ALIYUN::CEN::RouteEntry",
            "resourceIdentifierDigest": hashlib.sha256(identifier.encode("utf-8")).hexdigest(),
            "site": "aliyun",
            "sampleLevel": items[0]["sampleLevel"],
            "actualApiCalled": True,
        },
        "normal": {
            "actualUiClick": True,
            "actualNavigation": "iframe",
            "routeMatched": True,
            "newTabObserved": False,
            "finalUrl": normal["finalUrl"],
            "loginPassed": True,
            "resourceMatched": True,
            "observedAt": normal["capturedAt"],
            "artifactPath": str(normal_output),
            "artifactDigest": normal_output_digest,
        },
        "shift": {
            "actualUiClick": True,
            "actualNavigation": "new_tab",
            "routeMatched": True,
            "newTabObserved": True,
            "finalUrl": shift["finalUrl"],
            "loginPassed": True,
            "resourceMatched": True,
            "observedAt": shift["capturedAt"],
            "artifactPath": str(shift_output),
            "artifactDigest": shift_output_digest,
        },
    }
    batch_path = output_dir / "browser-batch.json"
    preflight_path = output_dir / "browser-preflight.json"
    _write_json(batch_path, batch)
    _write_json(preflight_path, preflight)
    audit = {
        "schemaVersion": 1,
        "rule": "composite_child_detail_v1",
        "workerManifestPath": str(args.manifest.resolve()),
        "workerManifestDigest": manifest_digest,
        "planPath": str(args.plan.resolve()),
        "planDigest": _digest(args.plan),
        "sessionMaterializationDigest": session_digest,
        "sessionReferencesDigest": references_digest,
        "sourceObservationDigests": {"normal": normal_raw_digest, "shift": shift_raw_digest},
        "sourceScreenshotDigests": {"normal": normal_screenshot_digest, "shift": shift_screenshot_digest},
        "derivedObservationDigests": {"normal": normal_output_digest, "shift": shift_output_digest},
        "browserBatchPath": str(batch_path),
        "browserBatchDigest": _digest(batch_path),
        "browserPreflightPath": str(preflight_path),
        "browserPreflightDigest": _digest(preflight_path),
        "sessionIdDigest": session_id_digest,
        "accountScopeDigest": account_scope_digest,
        "resourceIdentifierDigest": hashlib.sha256(identifier.encode("utf-8")).hexdigest(),
        "regionId": region,
        "conclusion": "machine_recovered_without_browser_replay",
    }
    audit_path = output_dir / "recovery-audit.json"
    _write_json(audit_path, audit)
    return {
        "status": "passed",
        "browserBatch": str(batch_path),
        "browserPreflight": str(preflight_path),
        "recoveryAudit": str(audit_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--session-materialization", type=Path, required=True)
    parser.add_argument("--session-references", type=Path, required=True)
    parser.add_argument("--normal-observation", type=Path, required=True)
    parser.add_argument("--normal-screenshot", type=Path, required=True)
    parser.add_argument("--shift-observation", type=Path, required=True)
    parser.add_argument("--shift-screenshot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    try:
        result = recover(parse_args())
    except RecoveryError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
