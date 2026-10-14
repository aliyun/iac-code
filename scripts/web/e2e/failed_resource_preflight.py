"""Recover exact immutable probes without opening the two-branch P4 PASS gate.

The browser runner stops at a failed probe before writing its aggregate batch.
Recover reviewed failures or a strictly positive branch from a mixed pair,
bound to the worker's own UI session and product-projected API references.
No browser/cloud calls, no receipt writer, no alteration of flags, timestamps,
URLs, JSON or screenshots; permission denial always stays a failed observation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

STATUS = "failed_preflight_observations"
VERIFIED_SHIFT_STATUS = "verified_polardb_shift_observation"
OTS_TABLE_STATUS = "verified_ots_table_observations"
BATCH_SUBSET_STATUS = "verified_reviewed_batch_subset"


def _verified_ots_table(raw: dict[str, Any], planned: dict[str, Any]) -> None:
    """Recover immutable mixed results, never turn a permission error into PASS."""
    if (
        planned.get("sourceType"), planned.get("canonicalResourceType"),
        planned.get("receiptSource"), planned.get("product"), planned.get("action"), planned.get("contractId"),
    ) != (
        "alicloud_ots_table", "ALIYUN::OTS::Table", "route_catalog", "tablestore", "ListTable",
        "route_catalog:alicloud_ots_table:aliyun",
    ):
        raise ValueError("mixed recovery is limited to the reviewed OTS Table route")
    parent = raw.get("resourceParentAttributes", {}).get("InstanceName")
    region = raw.get("regionId")
    identifier = raw.get("resourceIdentifier")
    binding = planned.get("routeSourceBinding", {})
    if (
        not isinstance(parent, str) or re.fullmatch(r"[A-Za-z0-9_-]+", parent) is None
        or not isinstance(region, str) or re.fullmatch(r"cn-[a-z0-9-]+", region) is None
        or not isinstance(identifier, str) or re.fullmatch(r"[A-Za-z0-9_-]+", identifier) is None
        or binding.get("parentAttributes") != {"InstanceName": parent}
        or raw.get("resourceParentAttributes") != {"InstanceName": parent}
        or binding.get("regionId") != region
        or hashlib.sha256(identifier.encode()).hexdigest() not in binding.get("resourceIdentifierDigests", [])
        or raw.get("canonicalResourceType") != "ALIYUN::OTS::Table"
    ):
        raise ValueError("OTS Table identity must equal the registered real API sample")
    diag = raw.get("diagnostics", {})
    actual = urlsplit(str(raw.get("finalUrl", "")))
    expected = urlsplit(str(diag.get("expectedUrl", "")))
    normal = raw.get("navigation") == "normal"
    host = "ots4service.console.aliyun.com" if normal else "ots.console.aliyun.com"
    route = f"/{region}/{parent}/{identifier}/dataManage"
    if (
        actual.scheme != "https" or actual.hostname != host
        or expected.scheme != "https" or expected.hostname != "ots.console.aliyun.com"
        or any(u.username or u.password or u.port or u.query or u.fragment or u.path != route
               for u in (actual, expected))
        or raw.get("resourceMatched") is not True
        or raw.get("attemptCount") != 1
        or any(diag.get("signals", {}).get(k) is not False
               for k in ("login", "wrongAccount", "wrongVariant", "missing", "gateway", "routeError"))
    ):
        raise ValueError("OTS Table probe lacks the exact domestic resource route or clean identity signals")
    if normal:
        if (
            raw.get("failureReason") != "permission_denied"
            or diag.get("signals", {}).get("permission") is not True
            or raw.get("routeMatched") is not False or raw.get("loginPassed") is not False
            or diag.get("contentReady") is not False or raw.get("landingKind") != "unknown"
        ):
            raise ValueError("normal mixed observation must preserve the original permission failure")
    elif (
        raw.get("navigation") != "shift" or raw.get("failureReason") is not None
        or raw.get("routeMatched") is not True or raw.get("loginPassed") is not True
        or diag.get("contentReady") is not True or raw.get("landingKind") != "exact_detail"
        or diag.get("signals", {}).get("permission") is not False
    ):
        raise ValueError("Shift mixed observation must already have every original PASS flag")


def _verified_polardb_shift(raw: dict[str, Any], planned: dict[str, Any]) -> None:
    """Preserve one proved child row, not a passed two-branch P4 preflight."""
    if planned.get("sourceType") == "alicloud_polardb_cluster":
        _verified_polardb_cluster_probe(raw, planned)
        return
    if (planned.get("sourceType"), planned.get("product"), planned.get("action")) != (
        "alicloud_polardb_account",
        "polardb",
        "DescribeAccounts",
    ):
        raise ValueError("verified Shift recovery is limited to the reviewed PolarDB account route")
    parent = raw.get("resourceParentAttributes", {}).get("DBClusterId")
    region = raw.get("regionId")
    binding = planned.get("routeSourceBinding", {})
    if not parent or binding.get("parentAttributes") != {"DBClusterId": parent} or binding.get("regionId") != region:
        raise ValueError("verified Shift parent and region must equal the registered source")
    diag = raw.get("diagnostics", {})
    actual = urlsplit(str(raw.get("finalUrl", "")))
    expected = urlsplit(str(diag.get("expectedUrl", "")))
    route = f"/{region}/cluster/{parent}/account"
    if (
        any(
            u.scheme != "https"
            or u.hostname != "polardb.console.aliyun.com"
            or u.username
            or u.password
            or u.query
            or u.fragment
            for u in (actual, expected)
        )
        or expected.path != route
        or actual.path != route + "/user"
    ):
        raise ValueError("verified Shift must show the exact domestic parent account route")
    body = str(diag.get("bodyExcerpt", ""))
    table = body.split("账号名", 1)[1] if "账号名" in body else ""
    if (
        raw.get("navigation") != "shift"
        or raw.get("landingKind") != "parent_list"
        or raw.get("failureReason") is not None
        or raw.get("routeMatched") is not True
        or raw.get("resourceMatched") is not True
        or not parent
        or parent not in body
        or any(
            diag.get("signals", {}).get(k) is not False
            for k in ("login", "wrongAccount", "wrongVariant", "permission", "missing", "gateway", "routeError")
        )
        or "状态" not in table
        or "类型" not in table
        or sum(line.strip() == raw.get("resourceIdentifier") for line in table.splitlines()) != 1
        or "可用" not in table
        or "修改密码" not in table
    ):
        raise ValueError("verified Shift lacks the exact loaded account row; global search text is not evidence")


def _verified_polardb_cluster_probe(raw: dict[str, Any], planned: dict[str, Any]) -> None:
    """Keep only an already-positive cluster Shift; never promote its failed iframe."""
    if (
        planned.get("canonicalResourceType"), planned.get("receiptSource"), planned.get("product"),
        planned.get("action"), planned.get("contractId"),
    ) != (
        "ALIYUN::POLARDB::DBCluster", "route_catalog", "polardb", "DescribeDBClusters",
        "route_catalog:alicloud_polardb_cluster:aliyun",
    ):
        raise ValueError("verified cluster Shift requires its exact route contract")
    binding = planned.get("routeSourceBinding", {})
    identifier, region = raw.get("resourceIdentifier"), raw.get("regionId")
    if (
        not isinstance(identifier, str) or re.fullmatch(r"pc-[a-z0-9]+", identifier) is None
        or not isinstance(region, str) or re.fullmatch(r"cn-[a-z0-9-]+", region) is None
        or binding.get("regionId") != region or raw.get("resourceParentAttributes")
        or hashlib.sha256(identifier.encode()).hexdigest() not in binding.get("resourceIdentifierDigests", [])
        or raw.get("canonicalResourceType") != planned["canonicalResourceType"]
        or raw.get("attemptCount") != 1
    ):
        raise ValueError("verified cluster identity must equal the registered API sample")
    diag = raw.get("diagnostics", {})
    expected = urlsplit(str(diag.get("expectedUrl", "")))
    route = f"/{region}/cluster/{identifier}/baseInfo"
    if (
        expected.scheme != "https" or expected.hostname != "polardb.console.aliyun.com"
        or expected.path != route or expected.query or expected.fragment or expected.port
        or expected.username or expected.password
        or any(diag.get("signals", {}).get(k) is not False
               for k in ("login", "wrongAccount", "wrongVariant", "permission", "missing", "gateway", "routeError"))
    ):
        raise ValueError("verified cluster probe lacks the exact clean domestic route")
    if raw.get("navigation") == "normal":
        if (
            raw.get("failureReason") != "login_error" or raw.get("finalUrl") is not None
            or raw.get("landingKind") != "unknown"
            or any(raw.get(k) is not False for k in ("loginPassed", "resourceMatched", "routeMatched"))
            or diag.get("contentReady") is not False
        ):
            raise ValueError("cluster recovery must preserve the original unrendered iframe failure")
    elif (
        raw.get("navigation") != "shift" or raw.get("failureReason") is not None
        or raw.get("landingKind") != "exact_detail"
        or any(raw.get(k) is not True for k in ("loginPassed", "resourceMatched", "routeMatched"))
        or diag.get("contentReady") is not True
        or raw.get("finalUrl") != expected.geturl()
    ):
        raise ValueError("cluster Shift must already have every original loaded PASS flag")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("failure input must be an object")
    return value


def reconstruct(paths: dict[str, str], *, verified_shift: bool = False, ots_table: bool = False) -> dict[str, Any]:
    if verified_shift and ots_table:
        raise ValueError("observation recovery modes cannot be combined")
    plan = load(Path(paths["plan"]))
    ready = load(Path(paths["ready"]))
    create = load(Path(paths["create"]))
    key = ready.get("ownSessionKey", "")
    if (
        ready.get("origin") != "http://127.0.0.1:8767"
        or create.get("origin") != ready["origin"]
        or ready.get("ready") is not True
        or ready.get("exactVisible") is not True
        or ready.get("ownSessionWasAbsentBefore") is not True
        or create.get("submitted") is not True
        or not key.startswith("ws~")
        or key in create.get("preExistingSidebarKeys", [])
    ):
        raise ValueError("failure capture must belong to the worker's own ready formal UI session")
    workspace, session = json.loads(base64.urlsafe_b64decode(key[3:] + "=" * (-len(key[3:]) % 4)))
    refs_path = Path(paths["references"])
    if (
        refs_path.parent.name != session
        or not workspace
        or not session
        or refs_path.parent.parent.name != re.sub(r"[^A-Za-z0-9-]", "-", workspace)
    ):
        raise ValueError("reference cache must belong to the exact full UI session key")
    refs = load(refs_path).get("references", [])
    if isinstance(refs, dict):
        refs = list(refs.values())
    if len(plan.get("items", [])) != 1:
        raise ValueError("failed probe import is limited to one exact planned contract")
    planned = plan["items"][0]
    cluster_shift = verified_shift and planned.get("sourceType") == "alicloud_polardb_cluster"
    if (
        planned.get("sourceChainStatus") != "passed"
        or planned.get("sampleLevel") not in {"L2", "L3"}
        or planned.get("actualApiCalled") is not True
    ):
        raise ValueError("failed probe requires a valid real API source")
    items = []
    accounts = set()
    directory = Path(paths["directory"])
    for navigation in ("normal", "shift"):
        files = list(directory.glob(f"{plan['planId'][:12]}-preflight-{navigation}-*.json"))
        if len(files) != 1:
            raise ValueError("failed capture must have exactly one immutable probe per branch")
        raw_path = files[0]
        raw = load(raw_path)
        diag = raw.get("diagnostics", {})
        if ots_table:
            _verified_ots_table(raw, planned)
        if cluster_shift:
            _verified_polardb_cluster_probe(raw, planned)
        if (
            raw.get("navigation") != navigation
            or raw.get("actualUiClick") is not True
            or (not ots_table and not (cluster_shift and navigation == "normal")
                and raw.get("loginPassed") is not True)
            or (not ots_table and not (cluster_shift and navigation == "normal")
                and diag.get("contentReady") is not True)
            or (
                not verified_shift and not ots_table
                and raw.get("failureReason") not in {"route_error", "inconclusive", "resource_absent"}
            )
            or (not verified_shift and not ots_table and raw.get("routeMatched") is not False)
            or (not verified_shift and not ots_table and raw.get("resourceMatched") is not False)
            or raw.get("actualNavigation") != raw.get("expectedNavigation")
            or raw.get("actualNavigation") != ("iframe" if navigation == "normal" else "new_tab")
            or raw.get("newTabObserved") is not (navigation == "shift")
            or (not ots_table and any(
                diag.get("signals", {}).get(k) is not False for k in ("login", "wrongAccount", "permission", "gateway")
            ))
        ):
            raise ValueError("only rendered authenticated actual-click failures can be imported")
        if verified_shift and navigation == "shift":
            _verified_polardb_shift(raw, planned)
        if (
            raw.get("sourceType") != planned["sourceType"]
            or raw.get("source") != planned["receiptSource"]
            or raw.get("contractId") != planned["contractId"]
        ):
            raise ValueError("failed capture is outside its exact plan")
        matches = [
            r
            for r in refs
            if r.get("resource_type") == planned["canonicalResourceType"]
            and r.get("identifier") == raw["resourceIdentifier"]
            and r.get("region_id") == raw["regionId"]
            and r.get("console_partition") == "aliyun"
            and r.get("evidence") == "api_response"
            and all(r.get("attributes", {}).get(k) == v for k, v in raw.get("resourceParentAttributes", {}).items())
            and any(
                s.get("evidence") == "api_response"
                and s.get("product") == planned["product"]
                and s.get("action") == planned["action"]
                for s in r.get("sources", [])
            )
        ]
        if len(matches) != 1 or not matches[0].get("account_scope"):
            raise ValueError("failed capture lacks an exact product-projected API reference")
        captured = datetime.fromisoformat(raw["capturedAt"].replace("Z", "+00:00"))
        if captured.tzinfo is None or captured > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("failed capture has an invalid timestamp")
        sources = [
            s
            for s in matches[0]["sources"]
            if s.get("product") == planned["product"]
            and s.get("action") == planned["action"]
            and s.get("evidence") == "api_response"
        ]
        if not any(
            isinstance(s.get("observed_at"), (int, float))
            and s.get("tool_use_id")
            and timedelta(0)
            <= captured - datetime.fromtimestamp(s["observed_at"], timezone.utc)
            <= timedelta(minutes=10)
            for s in sources
        ):
            raise ValueError("failed capture must follow the same fresh UI API source")
        accounts.add(matches[0]["account_scope"])
        screenshot = raw_path.with_suffix(".png")
        if not screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("failed capture requires its original PNG")
        item = {k: v for k, v in raw.items() if k != "canonicalResourceType"}
        item["artifacts"] = [
            {"kind": kind, "path": str(p.resolve()), "digest": digest(p)}
            for kind, p in (("observation", raw_path), ("screenshot", screenshot))
        ]
        items.append(item)
    if len(accounts) != 1 or any(
        items[0].get(k) != items[1].get(k) for k in ("resourceIdentifier", "regionId", "resourceParentAttributes")
    ):
        raise ValueError("failed branches must share one account, region, parent and resource")
    times = [datetime.fromisoformat(item["capturedAt"].replace("Z", "+00:00")) for item in items]
    if not timedelta(0) <= times[1] - times[0] <= timedelta(minutes=5):
        raise ValueError("failed probe pair must be captured in order within five minutes")
    return {
        "schemaVersion": 1,
        "catalogRevisionDigest": plan["catalogRevisionDigest"],
        "planId": plan["planId"],
        "browserAccessMode": "existing-chrome",
        "sessionIdDigest": hashlib.sha256(key.encode()).hexdigest(),
        "accountScopeDigest": hashlib.sha256(next(iter(accounts)).encode()).hexdigest(),
        "items": items[1:] if verified_shift else items,
    }


def gate(paths: dict[str, str]) -> dict[str, Any]:
    return {
        "status": STATUS,
        "paths": paths,
        "inputDigests": {k: digest(Path(v)) for k, v in paths.items() if k != "directory"},
        "batch": reconstruct(paths),
    }


def verified_shift_gate(paths: dict[str, str]) -> dict[str, Any]:
    return {
        "status": VERIFIED_SHIFT_STATUS,
        "paths": paths,
        "inputDigests": {k: digest(Path(v)) for k, v in paths.items() if k != "directory"},
        "batch": reconstruct(paths, verified_shift=True),
    }


def ots_table_gate(paths: dict[str, str]) -> dict[str, Any]:
    return {
        "status": OTS_TABLE_STATUS,
        "paths": paths,
        "inputDigests": {k: digest(Path(v)) for k, v in paths.items() if k != "directory"},
        "batch": reconstruct(paths, ots_table=True),
    }


def batch_subset_gate(paths: dict[str, str]) -> dict[str, Any]:
    """Preserve only reviewed positive originals; never reuse a premature probe as P4."""
    plan, original, review = (load(Path(paths[k])) for k in ("plan", "batch", "review"))
    ready, create = (load(Path(paths[k])) for k in ("ready", "create"))
    key = ready.get("ownSessionKey", "")
    if (
        ready.get("origin") != "http://127.0.0.1:8767" or create.get("origin") != ready.get("origin")
        or any(ready.get(k) is not True for k in ("ready", "exactVisible", "ownSessionWasAbsentBefore"))
        or create.get("submitted") is not True or not key.startswith("ws~")
        or key in create.get("preExistingSidebarKeys", [])
        or original.get("sessionIdDigest") != hashlib.sha256(key.encode()).hexdigest()
        or original.get("planId") != plan.get("planId")
        or original.get("catalogRevisionDigest") != plan.get("catalogRevisionDigest")
        or review.get("originalBatchSHA256") != digest(Path(paths["batch"]))
        or review.get("planSHA256") != digest(Path(paths["plan"]))
    ):
        raise ValueError("reviewed subset must bind the immutable original plan and own ready session")
    workspace, session = json.loads(base64.urlsafe_b64decode(key[3:] + "=" * (-len(key[3:]) % 4)))
    refs_path = Path(paths["references"])
    if refs_path.parent.name != session or refs_path.parent.parent.name != re.sub(r"[^A-Za-z0-9-]", "-", workspace):
        raise ValueError("reviewed references must belong to the exact own session")
    refs = load(refs_path).get("references", [])
    if isinstance(refs, dict):
        refs = list(refs.values())
    selected, seen = [], set()
    for selection in review.get("items", []):
        scope = (selection.get("sourceType"), selection.get("navigation"))
        allowed = {("alicloud_polardb_database", "shift"),
                   ("alicloud_privatelink_vpc_endpoint", "normal"),
                   ("alicloud_privatelink_vpc_endpoint", "shift")}
        if scope not in allowed or scope in seen or selection.get("exactLoadedLandingReviewed") is not True:
            raise ValueError("subset is limited to independently reviewed database Shift and PrivateLink branches")
        seen.add(scope)
        items = [i for i in original.get("items", []) if (i.get("sourceType"), i.get("navigation")) == scope]
        plans = [p for p in plan.get("items", []) if p.get("sourceType") == scope[0]]
        if len(items) != 1 or len(plans) != 1:
            raise ValueError("reviewed branch must occur exactly once in the original batch and plan")
        item, planned = items[0], plans[0]
        diag = item.get("diagnostics", {})
        normal = scope[1] == "normal"
        if (
            any(item.get(k) is not True for k in ("actualUiClick", "routeMatched", "loginPassed", "resourceMatched"))
            or diag.get("contentReady") is not True or item.get("failureReason") is not None
            or item.get("actualNavigation") != ("iframe" if normal else "new_tab")
            or item.get("expectedNavigation") != item.get("actualNavigation")
            or item.get("newTabObserved") is not (not normal) or item.get("attemptCount") != 1
            or any(diag.get("signals", {}).get(k) is not False
                   for k in ("login", "wrongAccount", "wrongVariant", "permission", "missing", "gateway", "routeError"))
            or planned.get("sourceChainStatus") != "passed" or planned.get("actualApiCalled") is not True
            or planned.get("sampleLevel") not in {"L2", "L3"}
            or item.get("source") != "route_catalog" or item.get("contractId") != planned.get("contractId")
        ):
            raise ValueError("reviewed subset cannot change original flags or accept an unexecuted/failed branch")
        identifier, region = item["resourceIdentifier"], item["regionId"]
        binding = planned.get("routeSourceBinding", {})
        parents = item.get("resourceParentAttributes", {})
        if (binding.get("regionId") != region or binding.get("parentAttributes", {}) != parents
                or hashlib.sha256(identifier.encode()).hexdigest() not in binding.get("resourceIdentifierDigests", [])):
            raise ValueError("reviewed resource must equal the registered exact route source")
        actual = urlsplit(item["finalUrl"])
        if scope[0] == "alicloud_polardb_database":
            host, route = "polardb.console.aliyun.com", f"/{region}/cluster/{parents.get('DBClusterId')}/database/list"
        else:
            host = "vpc4service.console.aliyun.com" if normal else "vpc.console.aliyun.com"
            route = f"/endpoint/{region}/endpoints/{identifier}"
        if (actual.scheme != "https" or actual.hostname != host or actual.path != route
                or actual.username or actual.password or actual.port or actual.query or actual.fragment):
            raise ValueError("reviewed subset requires the exact domestic parent/resource route")
        matches = [r for r in refs if r.get("resource_type") == planned.get("canonicalResourceType")
                   and r.get("identifier") == identifier and r.get("region_id") == region
                   and r.get("console_partition") == "aliyun" and r.get("evidence") == "api_response"
                   and all(r.get("attributes", {}).get(k) == v for k, v in parents.items())]
        if len(matches) != 1 or not matches[0].get("account_scope"):
            raise ValueError("reviewed branch needs an exact product-projected API reference")
        if original.get("accountScopeDigest") != hashlib.sha256(matches[0]["account_scope"].encode()).hexdigest():
            raise ValueError("reviewed branch account mismatch")
        captured = datetime.fromisoformat(item["capturedAt"].replace("Z", "+00:00"))
        if captured.tzinfo is None or captured > datetime.now(timezone.utc) + timedelta(minutes=5) or not any(
            s.get("product") == planned.get("product") and s.get("action") == planned.get("action")
            and s.get("evidence") == "api_response" and s.get("tool_use_id")
            and isinstance(s.get("observed_at"), (int, float))
            and timedelta(0) <= captured - datetime.fromtimestamp(s["observed_at"], timezone.utc)
            <= timedelta(minutes=10)
            for s in matches[0].get("sources", [])
        ):
            raise ValueError("reviewed branch must follow its own fresh UI API source")
        artifacts = {a["kind"]: a for a in item["artifacts"]}
        for kind in ("observation", "screenshot"):
            artifact = artifacts[kind]
            if (artifact["digest"] != digest(Path(artifact["path"]))
                    or selection.get(kind + "SHA256") != artifact["digest"]):
                raise ValueError("reviewed immutable artifact changed")
        raw = load(Path(artifacts["observation"]["path"]))
        if {k: v for k, v in raw.items() if k != "canonicalResourceType"} != {
            k: v for k, v in item.items() if k != "artifacts"
        } or not Path(artifacts["screenshot"]["path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("reviewed batch item is not its exact original JSON/PNG")
        selected.append(item)
    if not selected:
        raise ValueError("empty reviewed subset")
    return {"status": BATCH_SUBSET_STATUS, "paths": paths,
            "inputDigests": {k: digest(Path(v)) for k, v in paths.items()},
            "batch": {**original, "items": selected}}


def validate_gate(receipt: dict[str, Any], capture: dict[str, Any]) -> None:
    if capture.get("status") == BATCH_SUBSET_STATUS:
        if receipt.get("liveDestinationStatus") != "passed":
            raise ValueError("reviewed subset accepts only the already-positive original branches")
        fresh = batch_subset_gate(capture["paths"])
        if fresh != capture:
            raise ValueError("reviewed subset inputs changed")
        batch = fresh["batch"]
        if any(receipt[k] != batch[k] for k in
               ("sessionIdDigest", "accountScopeDigest", "catalogRevisionDigest", "browserAccessMode")):
            raise ValueError("reviewed subset receipt scope mismatch")
        matches = [i for i in batch["items"] if (i["sourceType"], i["contractId"], i["navigation"]) ==
                   (receipt["sourceType"], receipt["contractId"], receipt["navigation"])]
        if len(matches) != 1 or matches[0]["artifacts"] != receipt["artifacts"]:
            raise ValueError("receipt is not a selected immutable branch")
        return
    ots_table = capture.get("status") == OTS_TABLE_STATUS
    verified = capture.get("status") == VERIFIED_SHIFT_STATUS
    if ots_table:
        verdict = {"normal": "blocked", "shift": "passed"}.get(receipt.get("navigation"))
        if verdict is None or receipt.get("liveDestinationStatus") != verdict:
            raise ValueError("OTS mixed capture cannot change either original verdict")
    if verified and (receipt.get("liveDestinationStatus") != "passed" or receipt.get("navigation") != "shift"):
        raise ValueError("verified Shift capture cannot record any other branch or verdict")
    if not verified and not ots_table and (
        capture.get("status") != STATUS or receipt.get("liveDestinationStatus") != "blocked"
    ):
        raise ValueError("failed capture cannot record PASS or open the normal browser preflight gate")
    factory = ots_table_gate if ots_table else verified_shift_gate if verified else gate
    fresh = factory(capture["paths"])
    if fresh != capture:
        raise ValueError("failed capture inputs or immutable artifacts changed")
    batch = fresh["batch"]
    for k in ("sessionIdDigest", "accountScopeDigest", "catalogRevisionDigest", "browserAccessMode"):
        if receipt[k] != batch[k]:
            raise ValueError("failed capture receipt scope mismatch")
    matches = [item for item in batch["items"] if item["navigation"] == receipt["navigation"]]
    if len(matches) != 1 or matches[0]["artifacts"] != receipt["artifacts"]:
        raise ValueError("failed receipt is not an exact immutable probe")
