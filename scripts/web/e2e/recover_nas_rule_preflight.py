#!/usr/bin/env python3
"""Reclassify immutable NAS clicks using the same Web session's unique API rule.

No browser or cloud calls. Raw observations remain unchanged. Derived copies
are explicitly labelled and digest-bound to originals, screenshots, the full
Web session, materialized reference, UI-ready evidence and original plan.
The ordinary record-batch validator remains the only receipt/count writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from recover_composite_resource_preflight import (
    RecoveryError,
    _digest,
    _load_object,
    _parse_time,
    _require_manifest_file,
    _verified_manifest,
    _write_json,
)

SOURCE_TYPE = "alicloud_nas_access_rule"
CANONICAL = "ALIYUN::NAS::AccessRule"
CONTRACT = f"route_catalog:{SOURCE_TYPE}:aliyun"


def unique_api_rule(session: list[dict[str, Any]], reference: dict[str, Any]) -> dict[str, Any]:
    attrs = reference["attributes"]
    sources = [
        s
        for s in reference["sources"]
        if s.get("action") == "DescribeAccessRules"
        and s.get("product", "").lower() == "nas"
        and s.get("evidence") == "api_response"
    ]
    if len(sources) != 1:
        raise RecoveryError("reference must have one unambiguous NAS rule API source")
    tool_id = sources[0]["tool_use_id"]
    blocks = [b for message in session for b in message.get("content", []) if isinstance(b, dict)]
    calls = [b for b in blocks if b.get("type") == "tool_use" and b.get("id") == tool_id]
    results = [b for b in blocks if b.get("type") == "tool_result" and b.get("tool_use_id") == tool_id]
    if len(calls) != 1 or len(results) != 1 or results[0].get("is_error") is not False:
        raise RecoveryError("session must contain the exact successful API call/result")
    request = calls[0].get("input", {})
    parents = {k: attrs[k] for k in ("AccessGroupName", "FileSystemType")}
    if (
        calls[0].get("name") != "aliyun_api"
        or request.get("action") != "DescribeAccessRules"
        or request.get("product", "").lower() != "nas"
        or request.get("region_id") != reference["region_id"]
        or any(request.get("params", {}).get(k) != v for k, v in parents.items())
    ):
        raise RecoveryError("API request is not bound to the exact reference parents/region")
    response = json.loads(results[0]["content"])
    rules = response.get("AccessRules", {}).get("AccessRule", [])
    if int(response.get("TotalCount", -1)) != len(rules) or not response.get("RequestId"):
        raise RecoveryError("API rule list is incomplete or has no request ID")
    matches = [
        r
        for r in rules
        if str(r.get("AccessRuleId")) == reference["identifier"]
        and r.get("RegionId") == reference["region_id"]
        and all(r.get(k) == v for k, v in parents.items())
    ]
    if len(matches) != 1:
        raise RecoveryError("API must identify exactly one rule in the exact parent scope")
    rule = matches[0]
    keys = ("SourceCidrIp", "RWAccess", "UserAccess", "Priority")
    if (
        not all(rule.get(k) for k in keys)
        or any(str(rule.get(k)) != str(attrs.get(k)) for k in ("SourceCidrIp", "Priority"))
        or sum(all(str(r.get(k)) == str(rule[k]) for k in keys) for r in rules) != 1
    ):
        raise RecoveryError("API visible tuple is missing, inconsistent or ambiguous")
    return rule


def normalize(
    raw: dict[str, Any],
    *,
    mode: str,
    reference: dict[str, Any],
    rule: dict[str, Any],
    raw_path: Path,
    digest: str,
    manifest_digest: str,
) -> dict[str, Any]:
    attrs = reference["attributes"]
    parents = {k: attrs[k] for k in ("AccessGroupName", "FileSystemType")}
    required = {
        "sourceType": SOURCE_TYPE,
        "canonicalResourceType": CANONICAL,
        "source": "route_catalog",
        "contractId": CONTRACT,
        "navigation": mode,
        "resourceIdentifier": reference["identifier"],
        "resourceParentAttributes": parents,
        "regionId": reference["region_id"],
        "actualUiClick": True,
        "loginPassed": True,
        "attemptCount": 1,
        "routeMatched": False,
        "resourceMatched": False,
        "failureReason": "route_error",
        "newTabObserved": mode == "shift",
        "actualNavigation": "new_tab" if mode == "shift" else "iframe",
        "expectedNavigation": "new_tab" if mode == "shift" else "iframe",
    }
    if any(raw.get(k) != v for k, v in required.items()):
        raise RecoveryError("observation is not the reviewed NAS false-negative click shape")
    diag = raw.get("diagnostics", {})
    nas = diag.get("nasRule", {})
    required_signals = {"login", "wrongAccount", "wrongVariant", "permission", "missing", "gateway", "routeError"}
    if (
        diag.get("contentReady") is not True
        or nas.get("parentRouteMatched") is not True
        or set(diag.get("signals", {})) != required_signals
        or any(v is not False for v in diag["signals"].values())
    ):
        raise RecoveryError("observation is not fully rendered/authenticated or contains a page failure")
    path = (
        f"/{reference['region_id']}/access-group/{attrs['FileSystemType']}/detail/"
        f"{attrs['FileSystemType']}/{attrs['AccessGroupName']}/{attrs['AccessGroupType']}/rule"
    )
    for value, host in (
        (raw["finalUrl"], "nasnext.console.aliyun.com" if mode == "shift" else "nasnext4service.console.aliyun.com"),
        (diag.get("expectedUrl"), "nasnext.console.aliyun.com"),
    ):
        url = urlsplit(value or "")
        if (
            url.scheme != "https"
            or url.hostname != host
            or url.path != path
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise RecoveryError("URL is not the exact domestic parent rule tab")
    rw = {"RDWR": "读写", "RDONLY": "只读"}.get(rule["RWAccess"])
    user = {
        "no_squash": "所有用户不匿名（no_squash）",
        "root_squash": "root用户匿名（root_squash）",
        "all_squash": "所有用户匿名（all_squash）",
    }.get(rule["UserAccess"])
    if not rw or not user:
        raise RecoveryError("API rule uses an unreviewed permission/user mapping")
    expected = [rule["SourceCidrIp"], rw, user, str(rule["Priority"])]
    rows = [r for r in nas.get("rows", []) if [str(c).strip() for c in r.get("cells", [])][:4] == expected]
    if len(rows) != 1:
        raise RecoveryError("visible rule tuple is missing or ambiguous")
    result = dict(raw)
    result.update(
        landingKind="product_list",
        routeMatched=True,
        resourceMatched=True,
        failureReason=None,
        recovery={
            "schemaVersion": 1,
            "rule": "nas_unique_api_visible_tuple_v1",
            "sourceObservationPath": str(raw_path.resolve()),
            "sourceObservationDigest": digest,
            "sourceManifestDigest": manifest_digest,
            "visibleTuple": expected,
        },
    )
    return result


def recover(args: argparse.Namespace) -> dict[str, Any]:
    root = args.manifest.resolve().parent
    verified, manifest_digest = _verified_manifest(root, args.manifest)
    inputs = ("plan", "session", "references", "ready", "normal", "shift", "normal_png", "shift_png")
    digests = {k: _require_manifest_file(root, getattr(args, k), verified, k) for k in inputs}
    plan = _load_object(args.plan, "plan")
    items = plan.get("items", [])
    if (
        len(items) != 1
        or items[0].get("sourceType") != SOURCE_TYPE
        or items[0].get("contractId") != CONTRACT
        or items[0].get("receiptSource") != "route_catalog"
        or items[0].get("actualApiCalled") is not True
        or items[0].get("sourceChainStatus") != "passed"
        or items[0].get("sampleLevel") != "L2"
        or set(items[0].get("allowedNavigations", [])) != {"normal", "shift"}
    ):
        raise RecoveryError("plan is not the exact passed one-contract NAS route plan")
    ready = _load_object(args.ready, "UI ready")
    if (
        ready.get("origin") != "http://127.0.0.1:8767"
        or ready.get("ready") is not True
        or ready.get("ownSessionWasAbsentBefore") is not True
        or ready.get("exactVisible") is not True
        or not str(ready.get("ownSessionKey", "")).startswith("ws~")
    ):
        raise RecoveryError("UI evidence does not establish a fresh formal Web session/key/link")
    refs = _load_object(args.references, "materialized references").get("references", [])
    binding = items[0]["routeSourceBinding"]
    matches = [
        r
        for r in refs
        if r.get("resource_type") == CANONICAL
        and r.get("console_partition") == "aliyun"
        and r.get("region_id") == binding["regionId"]
        and hashlib.sha256(r["identifier"].encode()).hexdigest() in binding["resourceIdentifierDigests"]
        and all(r.get("attributes", {}).get(k) == v for k, v in binding["parentAttributes"].items())
    ]
    if len(matches) != 1 or not matches[0].get("account_scope"):
        raise RecoveryError("materialized reference is absent, ambiguous or lacks account scope")
    reference = matches[0]
    session = [json.loads(line) for line in args.session.read_text(encoding="utf-8").splitlines() if line]
    # Bind the persisted session, not a supervisor-supplied reference digest.
    import base64

    key = ready["ownSessionKey"][3:]
    _cwd, session_id = json.loads(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)))
    if not session or any(message.get("session_id") != session_id for message in session):
        raise RecoveryError("persisted messages do not belong to the GLM-created session")
    if any(message.get("cwd") != _cwd for message in session):
        raise RecoveryError("persisted messages do not belong to the full sidebar workspace key")
    rule = unique_api_rule(session, reference)
    pair = {
        mode: normalize(
            _load_object(getattr(args, mode), mode),
            mode=mode,
            reference=reference,
            rule=rule,
            raw_path=getattr(args, mode),
            digest=digests[mode],
            manifest_digest=manifest_digest,
        )
        for mode in ("normal", "shift")
    }
    times = [_parse_time(pair[m]["capturedAt"], m) for m in ("normal", "shift")]
    if abs((times[1] - times[0]).total_seconds()) > 300:
        raise RecoveryError("clicks are not a bounded same-session pair")
    source = next(s for s in reference["sources"] if s.get("action") == "DescribeAccessRules")
    observed = datetime.fromtimestamp(source["observed_at"], timezone.utc)
    if not 0 <= (min(times) - observed).total_seconds() <= 600:
        raise RecoveryError("rule API source is not from the current bounded Web interaction")
    output = args.output.resolve()
    if output.exists():
        raise RecoveryError("derived output already exists; never overwrite evidence")
    batch_items = []
    branches = {}
    for mode, observation in pair.items():
        target = output / f"normalized-{mode}.json"
        _write_json(target, observation)
        png = getattr(args, f"{mode}_png")
        item = {k: v for k, v in observation.items() if k not in {"canonicalResourceType", "recovery"}}
        item["artifacts"] = [
            {"kind": "observation", "path": str(target), "digest": _digest(target)},
            {"kind": "screenshot", "path": str(png.resolve()), "digest": digests[f"{mode}_png"]},
        ]
        batch_items.append(item)
        branches[mode] = {
            k: observation[k]
            for k in (
                "actualUiClick",
                "actualNavigation",
                "routeMatched",
                "newTabObserved",
                "finalUrl",
                "loginPassed",
                "resourceMatched",
            )
        }
        branches[mode].update(
            observedAt=observation["capturedAt"], artifactPath=str(target), artifactDigest=_digest(target)
        )
    common = {
        "schemaVersion": 1,
        "catalogRevisionDigest": plan["catalogRevisionDigest"],
        "browserAccessMode": "existing-chrome",
        "sessionIdDigest": hashlib.sha256(ready["ownSessionKey"].encode()).hexdigest(),
        "accountScopeDigest": hashlib.sha256(reference["account_scope"].encode()).hexdigest(),
    }
    batch = dict(common, planId=plan["planId"], items=batch_items)
    preflight = dict(
        common,
        capturedAt=min(times).isoformat(),
        expiresAt=(min(times) + timedelta(minutes=25)).isoformat(),
        status="passed",
        surface="iac-code-web",
        sample={
            "resourceType": SOURCE_TYPE,
            "resourceIdentifierDigest": hashlib.sha256(reference["identifier"].encode()).hexdigest(),
            "site": "aliyun",
            "sampleLevel": "L2",
            "actualApiCalled": True,
        },
        **branches,
    )
    _write_json(output / "browser-batch.json", batch)
    _write_json(output / "browser-preflight.json", preflight)
    _write_json(
        output / "recovery-audit.json",
        {
            "schemaVersion": 1,
            "rule": "nas_unique_api_visible_tuple_v1",
            "sourceManifestDigest": manifest_digest,
            "inputDigests": digests,
            "browserBatchDigest": _digest(output / "browser-batch.json"),
            "browserPreflightDigest": _digest(output / "browser-preflight.json"),
            "conclusion": "machine_recovered_without_browser_or_cloud_replay",
        },
    )
    return {"status": "passed", "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "manifest",
        "plan",
        "session",
        "references",
        "ready",
        "normal",
        "shift",
        "normal-png",
        "shift-png",
        "output",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    try:
        result = recover(parser.parse_args())
    except (RecoveryError, KeyError, ValueError, TypeError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
