"""Exact multi-source review projection; historical identities are never coalesced."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .files import WorkflowError, digest, json_write, read_json

REVIEW_KIND = "resource_link_remaining_failures_retest_review"
SCOPE_KIND = "failed_navigation_managed"
LOADED_SHA = digest(Path(__file__))
DATA_DIRECTORY = Path(__file__).resolve().parent


def historical_contract(resource: dict[str, Any], dimension: str, contract: dict[str, Any]) -> dict[str, Any]:
    """Verify an independent legacy row against its unchanged original baseline."""
    binding = contract.get("historical_binding", {})
    path = Path(binding.get("path", ""))
    canonical = DATA_DIRECTORY / "historical-results.json"
    if (dimension != "QUERY" or not re.fullmatch(r"H[0-9]{6,}", contract.get("id", ""))
            or not path.is_file() or digest(path) != binding.get("sha256")
            or digest(canonical) != binding.get("sha256")):
        raise WorkflowError("independent historical contract requires its original baseline SHA")
    rows = [row for group in read_json(path)["groups"] if group["key"] == resource["key"]
            for row in group["rows"] if row["contract_id"] == contract["id"]]
    if (len(rows) != 1 or rows[0]["source_type"] != contract["source_type"]
            or rows[0]["dimension"] != binding.get("dimension")
            or not rows[0]["dimension"].startswith("API ")
            or rows[0]["status"] != binding.get("status")):
        raise WorkflowError("independent historical source identity changed")
    return {"id": contract["id"], "source_type": contract["source_type"],
            "historical": copy.deepcopy(rows[0]["status"])}


def reviewed_targets(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Cross-check resource summaries and the flat numbered case list."""
    if (review.get("kind") != REVIEW_KIND or review.get("dispatch_allowed") is not False
            or review.get("status") != "draft_not_imported"):
        raise WorkflowError("expected a non-dispatchable remaining-failures review")
    resources = review.get("resources", [])
    ids = [r["resource_id"] for r in resources]
    if not ids or len(set(ids)) != len(ids):
        raise WorkflowError("review resources must be unique")
    summaries = {}
    for resource in resources:
        for contract in resource["contracts"]:
            dimension = contract["execution_dimension"]
            if dimension not in {"QUERY", "ROS", "TF"}:
                raise WorkflowError("review only admits navigation source dimensions")
            for scene in contract["target_scene_ids"]:
                mode = scene.removeprefix(contract["contract_id"] + "-")
                if mode not in {"iframe", "open"} or scene in summaries:
                    raise WorkflowError("review contains duplicate or cross-contract scenes")
                status = contract["baseline_status"][mode]
                if status not in {"fail", "review"}:
                    raise WorkflowError("review only selects actual baseline failures or pending reviews")
                summaries[scene] = {"resource_id": resource["resource_id"], "key": resource["key"],
                                    "contract_id": contract["contract_id"], "source_type": contract["source_type"],
                                    "dimension": dimension, "baseline_dimension": contract["baseline_dimension"],
                                    "mode": mode, "baseline_status": status}
    targets = review.get("targets", [])
    case_ids = [t["case_id"] for t in targets]
    scenes = [t["scene_id"] for t in targets]
    if (len(set(case_ids)) != len(case_ids) or len(set(scenes)) != len(scenes)
            or set(scenes) != set(summaries)):
        raise WorkflowError("numbered cases must exactly cover resource source summaries")
    for target in targets:
        summary = summaries[target["scene_id"]]
        if (target.get("baseline_status") != summary["baseline_status"] or any(target.get(k) != summary[k] for k in (
                "resource_id", "contract_id", "source_type", "baseline_dimension", "mode"))
                or target.get("execution_dimension") != summary["dimension"]):
            raise WorkflowError("numbered case/source binding differs from resource summary")
        summary["case_id"] = target["case_id"]
    for resource in resources:
        own = [t["case_id"] for t in targets if t["resource_id"] == resource["resource_id"]]
        if own != resource["target_case_ids"]:
            raise WorkflowError("resource case IDs must preserve the reviewed case order")
    return summaries


def make_remaining_plan(catalog: Path, review_path: Path, plan_id: str, destination: Path, store: Any) -> None:
    # Lazy imports keep plans/store/report dependency directions unchanged.
    from .plans import validate_plan, validate_review_lineage
    from .reports import make_plan

    if destination.exists():
        raise WorkflowError("plans are immutable; choose a new output path")
    review = read_json(review_path)
    targets = reviewed_targets(review)
    ids = [r["resource_id"] for r in review["resources"]]
    plan = make_plan(catalog, plan_id, ids, None, store)
    originals = {r["resource_id"]: r for r in review["resources"]}
    baseline_path = catalog.with_name("historical-results.json")
    excluded, retained = [], []
    for resource in plan["resources"]:
        original = originals[resource["id"]]
        if any(resource[k] != original[k] for k in ("key", "service", "name")):
            raise WorkflowError("review changes permanent resource identity")
        resource["review_group"] = original["group_id"]
        resource["investigation"] = original["investigation"]
        for source in original["contracts"]:
            dimension = source["execution_dimension"]
            contracts = resource["dimensions"][dimension]["contracts"]
            contract = next((c for c in contracts if c["id"] == source["contract_id"]), None)
            if contract is None and source["contract_id"].startswith("H"):
                contract = {"id": source["contract_id"], "source_type": source["source_type"],
                            "historical": copy.deepcopy(source["baseline_status"]),
                            "historical_binding": {"path": str(baseline_path.resolve()),
                                                   "sha256": digest(baseline_path),
                                                   "dimension": source["baseline_dimension"],
                                                   "status": copy.deepcopy(source["baseline_status"])}}
                historical_contract(resource, dimension, contract)
                with store.transaction() as connection:
                    rows = list(connection.execute("SELECT spec FROM tasks WHERE resource_id=? ORDER BY rowid",
                                                   (resource["id"],)))
                for row in rows:
                    for old in json.loads(row[0])["dimensions"][dimension]["contracts"]:
                        if old["id"] == contract["id"] and old["source_type"] == contract["source_type"]:
                            contract["historical"].update({m: s for m, s in old["historical"].items()
                                                           if s in {"pass", "unsupported"}})
                contracts.append(contract)
            if contract is None or contract["source_type"] != source["source_type"]:
                raise WorkflowError("review contract differs from its permanent source")
        own = []
        for scene, target in targets.items():
            if target["resource_id"] != resource["id"]:
                continue
            contract = next(c for c in resource["dimensions"][target["dimension"]]["contracts"]
                            if c["id"] == target["contract_id"])
            status = contract["historical"][target["mode"]]
            if status in {"pass", "unsupported"}:
                excluded.append({"scene_id": scene, "resource_id": resource["id"], "status": status,
                                 "reason": "retained exact positive checkpoint"})
            elif status in {"fail", "review"} and status == target["baseline_status"]:
                own.append(scene)
            else:
                raise WorkflowError("review baseline differs from the authoritative failed/pending source")
        if own:
            resource["retest_scope"] = {"kind": SCOPE_KIND, "scene_ids": own}
            retained.append(resource)
    if not retained:
        raise WorkflowError("all reviewed targets already positive; no execution plan to create")
    plan["resources"] = retained
    # Four slots are capacity, not necessarily a barrier before useful work starts.
    # Preserve legacy reviews while allowing OPS to freeze an explicit new-plan
    # startup wave; validate_plan rejects invalid/bool/out-of-capacity values.
    plan["initial_wave"] = review.get("initial_wave", min(4, len(retained)))
    plan["review_lineage"] = {
        "plan_id": review["plan_id"], "path": str(review_path.resolve()), "sha256": digest(review_path),
        "excluded_scenes": excluded,
        "baseline": {p.name: digest(p) for p in (
            catalog, baseline_path, catalog.with_name("resource-link-results.html"),
        )},
    }
    validate_plan(plan)
    validate_review_lineage(plan)
    json_write(destination, plan)


def managed_references(path: Path, scope: str, session: str, account: str,
                       references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require same-signer ROS Terraform stack AND its resource source in this execution.

    This only reads immutable successful wire responses. It neither calls cloud APIs
    nor treats an old local Terraform result or an unrelated QUERY as ROS hosting.
    """
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = list(connection.execute(
            "SELECT a.*,g.call FROM attempts a JOIN grants g ON g.scope=a.scope AND g.id=a.grant_id "
            "JOIN identities i ON i.scope=a.scope AND i.session=a.session AND i.credential=a.credential "
            "WHERE a.scope=? AND a.session=? AND a.state='succeeded' AND g.kind='read' AND i.account=?",
            (scope, session, account),
        ))
    stacks, sources = set(), {}
    for row in rows:
        call = json.loads(row["call"])
        if call.get("product", "").lower() != "ros":
            continue
        stack_id = call.get("params", {}).get("StackId")
        if not isinstance(stack_id, str) or not stack_id or stack_id.startswith("$"):
            continue
        response = json.loads(row["response"] or "null")
        if not isinstance(response, dict):
            continue
        if "body" in response:
            if type(response.get("status")) is not int or response["status"] != 200:
                continue
            response = response["body"]
        if not isinstance(response, dict) or response.get("Code") or response.get("ErrorCode"):
            continue
        binding = (stack_id, call["region_id"], row["credential"])
        if call["action"] == "GetStack":
            if (response.get("StackId") == stack_id and response.get("StackType") == "Terraform"
                    and response.get("RegionId") == call["region_id"]
                    and response.get("Status") in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_CREATE_COMPLETE"}
                    and isinstance(response.get("RequestId"), str) and response["RequestId"]):
                stacks.add(binding)
        elif call["action"] in {"GetStackResource", "ListStackResources"} and row["tool_use_id"]:
            if row["tool_use_id"] in sources and sources[row["tool_use_id"]] != binding:
                raise WorkflowError("ambiguous managed resource source identity")
            sources[row["tool_use_id"]] = binding
    return [reference for reference in references
            if reference.get("resourceType", "").startswith("alicloud_") and any(
                source.get("evidence") == "api_response"
                and sources.get(source.get("toolUseId")) in stacks
                and sources[source["toolUseId"]][0] == reference.get("stackId")
                and source.get("product") == "ros"
                and source.get("action") in {"GetStackResource", "ListStackResources"}
                for source in reference.get("sources", []))]
