"""Frozen resource IDs, four dimensions, and conservative historical PASS import."""

from __future__ import annotations

import re
from itertools import combinations
from pathlib import Path
from typing import Any

from . import DIMENSIONS, MODES
from .files import WorkflowError, digest, read_json
from .remaining_review import REVIEW_KIND, SCOPE_KIND, historical_contract, reviewed_targets

LOADED_SHA = digest(Path(__file__))

ID = re.compile(r"R[0-9]{4,}")
GROUPS = {"network-compute", "storage-data", "identity-application"}


def retest_dimensions(resource: dict[str, Any]) -> set[str]:
    """Derive scope from frozen contract identities, never from a worker packet."""
    scope = resource.get("retest_scope")
    if scope is None:
        return set()
    return {dimension for dimension, source in resource["dimensions"].items()
            for contract in source.get("contracts", []) for mode in MODES
            if f"{contract['id']}-{mode}" in scope["scene_ids"]}


def readonly_retest_dimensions(resource: dict[str, Any]) -> set[str]:
    scope = resource.get("retest_scope", {})
    return retest_dimensions(resource) if scope.get("kind") in {"failed_navigation_existing", SCOPE_KIND} else set()


def validate_review_lineage(plan: dict[str, Any]) -> list[dict[str, Any]]:
    lineage = plan.get("review_lineage")
    if lineage is None:
        return []
    path = Path(lineage["path"])
    if not path.is_file() or digest(path) != lineage.get("sha256"):
        raise WorkflowError("original retest review SHA changed")
    review = read_json(path)
    if (review.get("kind") not in {"resource_link_targeted_retest_review", REVIEW_KIND}
            or review.get("dispatch_allowed") is not False
            or review.get("plan_id") != lineage.get("plan_id")):
        raise WorkflowError("invalid retest review lineage")
    if "initial_wave" in review and (
        type(review["initial_wave"]) is not int or plan.get("initial_wave") != review["initial_wave"]
    ):
        raise WorkflowError("executable plan changes reviewed initial_wave")
    originals = (reviewed_targets(review) if review.get("kind") == REVIEW_KIND else
                 {scene: target for target in review["targets"] for scene in target["target_scene_ids"]})
    selected = set()
    for resource in plan["resources"]:
        for scene in resource.get("retest_scope", {}).get("scene_ids", []):
            target = originals.get(scene, {})
            if target.get("resource_id") != resource["id"] or target.get("key") != resource["key"]:
                raise WorkflowError("executable plan expands beyond original reviewed scenes")
            contracts = resource["dimensions"].get(target.get("dimension"), {}).get("contracts", [])
            if not any(c["id"] == target.get("contract_id") and c["source_type"] == target.get("source_type")
                       for c in contracts):
                raise WorkflowError("executable plan changes reviewed source contract")
            selected.add(scene)
    excluded = lineage.get("excluded_scenes", [])
    omitted = {item["scene_id"] for item in excluded}
    if (len(omitted) != len(excluded) or selected & omitted or selected | omitted != set(originals)
            or any(item.get("status") not in {"pass", "unsupported"} for item in excluded)):
        raise WorkflowError("reviewed scope cannot silently drop or duplicate a target")
    return excluded


def validate_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != 1 or not plan.get("plan_id"):
        raise WorkflowError("unsupported plan version or missing plan_id")
    if not isinstance(plan.get("resources"), list) or not plan["resources"]:
        raise WorkflowError("a plan must contain resources")
    minimum = plan.get("initial_wave", 1)
    if type(minimum) is not int or not 1 <= minimum <= min(4, len(plan["resources"])):
        raise WorkflowError("initial_wave must fit one to four slots and the plan")
    ids, keys, contract_ids = set(), set(), set()
    for item in plan["resources"]:
        resource_id = item.get("id", "")
        if not ID.fullmatch(resource_id) or resource_id in ids or item.get("key") in keys:
            raise WorkflowError("duplicate/invalid resource ID or canonical key")
        ids.add(resource_id)
        keys.add(item.get("key"))
        policy_version = item.get("execution_policy_version", 1)
        if type(policy_version) is not int or policy_version not in {1, 2}:
            raise WorkflowError("unsupported execution read policy version")
        if not item.get("key") or not item.get("service") or not item.get("name"):
            raise WorkflowError("resource identity is required")
        if item.get("group") not in GROUPS or set(item.get("dimensions", {})) != set(DIMENSIONS):
            raise WorkflowError("every resource requires a group and exactly four source dimensions")
        for dimension, spec in item["dimensions"].items():
            if spec.get("support") not in {"unknown", "supported", "not_applicable"}:
                raise WorkflowError("support must be explicit; an empty plan is not unsupported")
            if spec["support"] == "not_applicable" and not spec.get("official_basis"):
                raise WorkflowError("not_applicable requires official evidence")
            if dimension == "TF" and spec.get("execution") != "ros_managed_terraform":
                raise WorkflowError("Terraform must be hosted by ROS, not local apply/destroy")
            for scene in spec.get("contracts", []):
                if (
                    not scene.get("id")
                    or not scene.get("source_type")
                    or scene["id"] in contract_ids
                    or (not scene["id"].startswith(resource_id + "-") and "historical_binding" not in scene)
                ):
                    raise WorkflowError("each alias/variant needs its own precise contract")
                contract_ids.add(scene["id"])
                if "historical_binding" in scene:
                    historical_contract(item, dimension, scene)
                if set(scene.get("historical", {})) != set(MODES):
                    raise WorkflowError("historical status needs both navigation branches")
                for status in scene["historical"].values():
                    if status not in {"pass", "fail", "review", "untested", "unsupported"}:
                        raise WorkflowError("unknown historical status")
        scope = item.get("retest_scope")
        if scope is not None:
            if (not isinstance(scope, dict) or set(scope) != {"kind", "scene_ids"}
                    or scope["kind"] not in {"failed_query_navigation", "failed_navigation_existing", SCOPE_KIND}):
                raise WorkflowError("retest scope must be explicit failed navigation")
            if scope["kind"] == SCOPE_KIND and not plan.get("review_lineage"):
                raise WorkflowError("managed navigation requires exact reviewed lineage")
            targets = scope["scene_ids"]
            dimensions = ({"QUERY", "ROS", "TF"} if scope["kind"] == SCOPE_KIND else
                          {"QUERY"} if scope["kind"] == "failed_query_navigation" else {"QUERY", "ROS"})
            allowed = {f"{contract['id']}-{mode}"
                       for dimension in dimensions
                       for contract in item["dimensions"][dimension].get("contracts", []) for mode in MODES}
            if (not isinstance(targets, list) or not targets
                    or any(not isinstance(target, str) for target in targets)
                    or len(set(targets)) != len(targets) or not set(targets) <= allowed):
                raise WorkflowError("retest scope needs unique exact existing-source navigation scenes")
            if any(item["dimensions"][dimension]["support"] == "not_applicable"
                   for dimension in retest_dimensions(item)):
                raise WorkflowError("targeted dimension cannot be dropped as not applicable")
    for item in plan["resources"]:
        dependencies = item.get("dependencies", [])
        if item["id"] in dependencies or any(dependency not in ids for dependency in dependencies):
            raise WorkflowError("invalid dependency")
    graph = {item["id"]: item.get("dependencies", []) for item in plan["resources"]}

    def visit(node: str, ancestors: set[str]) -> None:
        if node in ancestors:
            raise WorkflowError("dependency cycle")
        for parent in graph[node]:
            visit(parent, ancestors | {node})

    for node in graph:
        visit(node, set())
    roots = [item for item in plan["resources"] if not item.get("dependencies")]
    if not any(
        len(locks := [lock for item in wave for lock in item.get("locks", [])]) == len(set(locks))
        for wave in combinations(roots, minimum)
    ):
        raise WorkflowError("initial_wave requires independent, non-conflicting resources")


def expected_scenes(resource: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep exact contracts distinct. Old link PASS never proves a creation chain."""
    scenes = []
    for dimension in DIMENSIONS:
        spec = resource["dimensions"][dimension]
        if spec["support"] == "not_applicable":
            continue
        contracts = spec.get("contracts") or [
            {
                "id": f"{resource['id']}-{dimension}",
                "source_type": resource["key"],
                "historical": {mode: "untested" for mode in MODES},
            }
        ]
        for contract in contracts:
            for mode in MODES:
                status = contract["historical"][mode]
                if status not in {"pass", "unsupported"}:
                    scenes.append(
                        {
                            "id": f"{contract['id']}-{mode}",
                            "dimension": dimension,
                            "mode": mode,
                            "contract": contract["id"],
                            "source_type": contract["source_type"],
                        }
                    )
        if dimension != "QUERY" and not spec.get("creation_verified", False):
            scenes.append(
                {
                    "id": f"{resource['id']}-{dimension}-creation",
                    "dimension": dimension,
                    "mode": "creation",
                    "contract": f"{resource['id']}-{dimension}",
                }
            )
    scope = resource.get("retest_scope")
    if scope is not None:
        # Scope is frozen at import. This excludes untested creation/dimensions,
        # not their report rows or obligations in future plans. PASS stays skipped.
        scenes = [scene for scene in scenes if scene["id"] in scope["scene_ids"]]
    return scenes
