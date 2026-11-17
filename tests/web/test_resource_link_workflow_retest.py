"""Failure-only query smoke scopes do not invent support, PASS, or creation coverage."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow.creation import validate_execution
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.plans import expected_scenes, readonly_retest_dimensions, validate_plan
from scripts.web.e2e.resource_link_workflow.reports import make_plan, make_review_plan
from scripts.web.e2e.resource_link_workflow.store import Store

CATALOG = Path(__file__).resolve().parents[2] / "scripts/web/e2e/resource_link_workflow/catalog.json"


def plan():
    resource = copy.deepcopy(next(r for r in read_json(CATALOG)["resources"] if r["id"] == "R0007"))
    resource["retest_scope"] = {
        "kind": "failed_query_navigation",
        "scene_ids": [
            "R0007-QUERY-1-iframe",
            "R0007-QUERY-1-open",
        ],
    }
    return {"schema_version": 1, "plan_id": "retest-fixture", "resources": [resource]}


def test_failure_scope_keeps_four_dimensions_and_only_dispatches_selected_queries(tmp_path):
    value = plan()
    store = Store.initialize(tmp_path)
    store.import_plan(value)
    assert {s["id"] for s in expected_scenes(value["resources"][0])} == {
        "R0007-QUERY-1-iframe",
        "R0007-QUERY-1-open",
    }
    assert value["resources"][0]["dimensions"]["ROS"]["support"] == "unknown"
    assert not value["resources"][0]["dimensions"]["TF"]["creation_verified"]
    value["resources"][0]["dimensions"]["QUERY"]["contracts"][0]["historical"]["iframe"] = "pass"
    assert [s["id"] for s in expected_scenes(value["resources"][0])] == ["R0007-QUERY-1-open"]
    validate_plan(value)  # Legitimate independent acceptance shrinks remaining scope.


@pytest.mark.parametrize(
    "targets", [[], ["R0007-TF-1-open"], ["R0007-QUERY-creation"], ["R0007-QUERY-1-open", "R0007-QUERY-1-open"], [None]]
)
def test_scope_rejects_expansion_and_ambiguous_ids(targets):
    value = plan()
    value["resources"][0]["retest_scope"]["scene_ids"] = targets
    with pytest.raises(WorkflowError):
        validate_plan(value)


@pytest.mark.parametrize("mode,status", [("iframe", "pass"), ("open", "untested"), ("open", "unsupported")])
def test_import_rejects_nonfail_target(tmp_path, mode, status):
    value = plan()
    value["resources"][0]["dimensions"]["QUERY"]["contracts"][0]["historical"][mode] = status
    with pytest.raises(WorkflowError, match="actual failed"):
        Store.initialize(tmp_path).import_plan(value)


def test_import_rejects_discarding_other_positive_results(tmp_path):
    value = plan()
    value["resources"][0]["dimensions"]["TF"]["contracts"][0]["historical"]["open"] = "untested"
    with pytest.raises(WorkflowError, match="positive checkpoints"):
        Store.initialize(tmp_path).import_plan(value)


def test_import_rejects_catalog_source_spoof(tmp_path):
    value = plan()
    value["resources"][0]["dimensions"]["QUERY"]["contracts"][0]["source_type"] = "other"
    with pytest.raises(WorkflowError, match="actual failed"):
        Store.initialize(tmp_path).import_plan(value)


def test_make_targeted_plan(tmp_path):
    output = tmp_path / "plan.json"
    targets = ["R0007-QUERY-1-open"]
    make_plan(CATALOG, "single-retest", ["R0007"], output, retest_scenes=targets)
    assert read_json(output)["resources"][0]["retest_scope"]["scene_ids"] == targets


def test_multiple_existing_resource_navigation_scopes_include_ros_not_creation(tmp_path):
    output = tmp_path / "multi.json"
    targets = ["R0019-QUERY-1-iframe", "R0078-QUERY-1-open", "R0085-QUERY-1-open", "R0020-ROS-1-open"]
    make_plan(CATALOG, "multi", ["R0019", "R0078", "R0085", "R0020"], output, initial_wave=4, retest_scenes=targets)
    value = read_json(output)
    Store.initialize(tmp_path / "state").import_plan(value)
    assert [s["id"] for resource in value["resources"] for s in expected_scenes(resource)] == targets
    ros = value["resources"][-1]
    assert readonly_retest_dimensions(ros) == {"ROS"}
    assert not ros["dimensions"]["ROS"]["creation_verified"]
    assert ros["dimensions"]["ROS"]["contracts"][0]["historical"]["iframe"] == "pass"


@pytest.mark.parametrize("targets", [["R0019-QUERY-1-iframe"], ["R0019-QUERY-1-iframe", "R9999-QUERY-1-open"]])
def test_multiple_scopes_cannot_leave_a_resource_unscoped(tmp_path, targets):
    with pytest.raises(WorkflowError):
        make_plan(CATALOG, "multi", ["R0019", "R0078"], tmp_path / "bad.json", retest_scenes=targets)
    assert not (tmp_path / "bad.json").exists()


def ros_read_packet():
    return {
        "scenes": {"R0020-ROS-1-open": {"account": "fixture"}},
        "execution_grants": {
            "ROS": [
                {
                    "id": "identity",
                    "kind": "identity",
                    "quota": 1,
                    "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
                },
                {
                    "id": "resource",
                    "kind": "read",
                    "quota": 1,
                    "read_policy": {"phase": "execute"},
                    "call": {
                        "product": "ros",
                        "action": "GetStackResource",
                        "region_id": "cn-hangzhou",
                        "params": {"StackId": "offline-original-stack", "LogicalResourceId": "Snat"},
                    },
                },
            ]
        },
    }


def test_readonly_ros_needs_frozen_scope_and_never_becomes_query_or_creation():
    packet = ros_read_packet()
    with pytest.raises(WorkflowError):
        validate_execution(packet, {"ROS"}, require_read_policy=True)
    validate_execution(packet, {"ROS"}, require_read_policy=True, readonly_dimensions={"ROS"})
    assert set(packet["execution_grants"]) == {"ROS"}


@pytest.mark.parametrize("damage", ["write", "query_substitution", "unbounded", "dynamic_stack", "cleanup"])
def test_readonly_ros_rejects_write_or_non_ros_source(damage):
    packet = ros_read_packet()
    entry = packet["execution_grants"]["ROS"][-1]
    if damage == "write":
        entry["kind"] = "create"
        entry.pop("read_policy")
        entry["call"]["action"] = "CreateStack"
    elif damage == "query_substitution":
        entry["call"].update(product="vpc", action="DescribeSnatTableEntries")
    elif damage == "unbounded":
        entry["call"]["params"].pop("StackId")
    elif damage == "dynamic_stack":
        entry["call"]["params"]["StackId"] = "$created_id"
    else:
        entry["read_policy"]["phase"] = "cleanup"
    with pytest.raises(WorkflowError):
        validate_execution(packet, {"ROS"}, require_read_policy=True, readonly_dimensions={"ROS"})


def test_review_derivation_preserves_original_and_excludes_positive_scenes(tmp_path):
    catalog = read_json(CATALOG)
    old = next(r for r in catalog["resources"] if r["id"] == "R0007")
    old["dimensions"]["QUERY"]["contracts"][0]["historical"] = {"iframe": "pass", "open": "pass"}
    catalog_path = tmp_path / "catalog.json"
    json_write(catalog_path, catalog)
    for name in ("historical-results.json", "resource-link-results.html"):
        (tmp_path / name).write_bytes((CATALOG.parent / name).read_bytes())
    ids = ["R0019", "R0078", "R0085", "R0024", "R0007", "R0020"]
    targets = []
    for identifier in ids:
        resource = next(r for r in catalog["resources"] if r["id"] == identifier)
        dimension = "ROS" if identifier == "R0020" else "QUERY"
        contract = resource["dimensions"][dimension]["contracts"][0]
        targets.append(
            {
                "resource_id": identifier,
                "key": resource["key"],
                "dimension": dimension,
                "contract_id": contract["id"],
                "source_type": contract["source_type"],
                "target_scene_ids": [contract["id"] + "-open" if identifier != "R0019" else contract["id"] + "-iframe"],
            }
        )
    review = tmp_path / "review.json"
    json_write(
        review,
        {
            "kind": "resource_link_targeted_retest_review",
            "dispatch_allowed": False,
            "plan_id": "original",
            "targets": targets,
        },
    )
    before = digest(review)
    output = tmp_path / "executable.json"
    make_review_plan(catalog_path, review, "derived", output, Store.initialize(tmp_path / "state"))
    result = read_json(output)
    assert result["initial_wave"] == 4
    assert [r["id"] for r in result["resources"]] == [r for r in ids if r != "R0007"]
    assert result["review_lineage"]["sha256"] == before == digest(review)
    assert result["review_lineage"]["excluded_scenes"][0]["scene_id"] == "R0007-QUERY-1-open"
    assert [s["id"] for s in expected_scenes(result["resources"][-1])] == ["R0020-ROS-1-open"]


@pytest.mark.parametrize("dimension", ["TF", "CREATE"])
def test_existing_navigation_scope_cannot_admit_untargeted_creation_dimensions(tmp_path, dimension):
    resource = copy.deepcopy(next(r for r in read_json(CATALOG)["resources"] if r["id"] == "R0020"))
    resource["retest_scope"] = {"kind": "failed_navigation_existing", "scene_ids": [f"R0020-{dimension}-1-open"]}
    with pytest.raises(WorkflowError):
        Store.initialize(tmp_path).import_plan({"schema_version": 1, "plan_id": "bad", "resources": [resource]})
