"""Reviewed multi-source/managed-TF scopes preserve exact historical identities offline."""

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.creation import validate_execution
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.plans import expected_scenes, validate_plan, validate_review_lineage
from scripts.web.e2e.resource_link_workflow.remaining_review import (
    REVIEW_KIND,
    SCOPE_KIND,
    managed_references,
    reviewed_targets,
)
from scripts.web.e2e.resource_link_workflow.reports import make_review_plan, render_results
from scripts.web.e2e.resource_link_workflow.store import Store

DIRECTORY = Path(__file__).resolve().parents[2] / "scripts/web/e2e/resource_link_workflow"


def fixture_review(tmp_path):
    catalog = read_json(DIRECTORY / "catalog.json")
    originals = {r["id"]: r for r in catalog["resources"]}
    selected = {"R0042": [("ROS", 0), ("QUERY", 0)], "R0043": [("TF", 0)], "R0058": []}
    resources, targets = [], []
    for resource_id, entries in selected.items():
        item = originals[resource_id]
        resource = {
            "resource_id": resource_id,
            **{k: item[k] for k in ("key", "service", "name")},
            "group_id": "A",
            "investigation": "offline exact source",
            "contracts": [],
            "target_case_ids": [],
        }
        for dimension, index in entries:
            contract = item["dimensions"][dimension]["contracts"][index]
            resource["contracts"].append(
                {
                    "contract_id": contract["id"],
                    "source_type": contract["source_type"],
                    "execution_dimension": dimension,
                    "baseline_dimension": dimension,
                    "baseline_status": contract["historical"],
                    "target_scene_ids": [contract["id"] + "-iframe"],
                }
            )
        if resource_id == "R0058":
            resource["contracts"].append(
                {
                    "contract_id": "H000468",
                    "source_type": item["key"],
                    "execution_dimension": "QUERY",
                    "baseline_dimension": "API 查询 · 近期",
                    "baseline_status": {"iframe": "fail", "open": "pass"},
                    "target_scene_ids": ["H000468-iframe"],
                }
            )
        for contract in resource["contracts"]:
            for scene in contract["target_scene_ids"]:
                case_id = f"F{len(targets) + 1:04}"
                resource["target_case_ids"].append(case_id)
                targets.append(
                    {
                        "case_id": case_id,
                        "resource_id": resource_id,
                        **{
                            k: contract[k]
                            for k in ("contract_id", "source_type", "execution_dimension", "baseline_dimension")
                        },
                        "scene_id": scene,
                        "mode": "iframe",
                        "baseline_status": "fail",
                    }
                )
        resources.append(resource)
    review = {
        "kind": REVIEW_KIND,
        "dispatch_allowed": False,
        "status": "draft_not_imported",
        "plan_id": "remaining-review",
        "resources": resources,
        "targets": targets,
    }
    review_path = tmp_path / "review.json"
    json_write(review_path, review)
    for name in ("catalog.json", "historical-results.json", "resource-link-results.html"):
        (tmp_path / name).write_bytes((DIRECTORY / name).read_bytes())
    store = Store.initialize(tmp_path / "state")
    output = tmp_path / "execution.json"
    make_review_plan(tmp_path / "catalog.json", review_path, "remaining-executable", output, store)
    return read_json(output), review_path, store


def pending_review(tmp_path, resource_id="R0029", dimension="QUERY"):
    item = next(r for r in read_json(DIRECTORY / "catalog.json")["resources"] if r["id"] == resource_id)
    contract = item["dimensions"][dimension]["contracts"][0]
    scene = contract["id"] + "-open"
    source = {
        "contract_id": contract["id"],
        "source_type": contract["source_type"],
        "execution_dimension": dimension,
        "baseline_dimension": dimension,
        "baseline_status": copy.deepcopy(contract["historical"]),
        "target_scene_ids": [scene],
    }
    review = {
        "kind": REVIEW_KIND,
        "dispatch_allowed": False,
        "status": "draft_not_imported",
        "plan_id": "pending-review",
        "initial_wave": 1,
        "resources": [
            {
                "resource_id": resource_id,
                **{k: item[k] for k in ("key", "service", "name")},
                "group_id": "A",
                "investigation": "Offline pending screenshot review",
                "contracts": [source],
                "target_case_ids": ["P0001"],
            }
        ],
        "targets": [
            {
                "case_id": "P0001",
                "resource_id": resource_id,
                **{k: source[k] for k in ("contract_id", "source_type", "execution_dimension", "baseline_dimension")},
                "scene_id": scene,
                "mode": "open",
                "baseline_status": "review",
            }
        ],
    }
    path = tmp_path / "pending-review.json"
    json_write(path, review)
    return path, Store.initialize(tmp_path / "state"), review


@pytest.mark.parametrize(
    "resource_id,dimension",
    [
        ("R0029", "QUERY"),
        ("R0041", "QUERY"),
        ("R0067", "TF"),
        ("R0083", "ROS"),
        ("R0083", "QUERY"),
    ],
)
def test_reviewed_pending_navigation_is_admitted_without_relabeling_it_failed(tmp_path, resource_id, dimension):
    path, store, review = pending_review(tmp_path, resource_id, dimension)
    original = path.read_bytes()
    output = tmp_path / "pending-plan.json"
    make_review_plan(DIRECTORY / "catalog.json", path, "pending-execution", output, store)
    plan = read_json(output)
    resource = plan["resources"][0]
    assert resource["dimensions"][dimension]["contracts"][0]["historical"]["open"] == "review"
    assert [s["id"] for s in expected_scenes(resource)] == [review["targets"][0]["scene_id"]]
    store.import_plan(plan)
    assert len(store.status()["tasks"]) == 1 and not store.status()["jobs"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("status", ["pass", "unsupported", "untested", "execution_gap"])
def test_pending_review_cannot_expand_to_other_baseline_states(tmp_path, status):
    _, _, review = pending_review(tmp_path)
    review["targets"][0]["baseline_status"] = status
    review["resources"][0]["contracts"][0]["baseline_status"]["open"] = status
    with pytest.raises(WorkflowError, match="actual baseline"):
        reviewed_targets(review)


def test_pending_review_generator_rejects_falsely_relabelled_failure(tmp_path):
    path, store, review = pending_review(tmp_path)
    review["targets"][0]["baseline_status"] = "fail"
    review["resources"][0]["contracts"][0]["baseline_status"]["open"] = "fail"
    json_write(path, review)
    with pytest.raises(WorkflowError, match="authoritative"):
        make_review_plan(DIRECTORY / "catalog.json", path, "fake-failure", tmp_path / "plan.json", store)


@pytest.mark.parametrize("missing_lineage", [False, True])
def test_pending_review_import_requires_exact_reviewed_state_and_cannot_use_legacy_unreviewed_scope(
    tmp_path,
    missing_lineage,
):
    path, store, review = pending_review(tmp_path)
    output = tmp_path / "pending-plan.json"
    make_review_plan(DIRECTORY / "catalog.json", path, "pending-execution", output, store)
    plan = read_json(output)
    if missing_lineage:
        plan.pop("review_lineage")
        plan["resources"][0]["retest_scope"]["kind"] = "failed_query_navigation"
    else:
        review["targets"][0]["baseline_status"] = "fail"
        review["resources"][0]["contracts"][0]["baseline_status"]["open"] = "fail"
        json_write(path, review)
        plan["review_lineage"]["sha256"] = digest(path)
    with pytest.raises(WorkflowError, match="actual failed scenes"):
        store.import_plan(plan)
    assert not store.status()["tasks"]


def test_multi_source_review_freezes_one_task_per_resource_and_preserves_all_other_dimensions(tmp_path):
    plan, review, store = fixture_review(tmp_path)
    before = digest(review)
    assert [r["id"] for r in plan["resources"]] == ["R0042", "R0043", "R0058"]
    assert [s["id"] for r in plan["resources"] for s in expected_scenes(r)] == [
        "R0042-ROS-1-iframe",
        "R0042-QUERY-1-iframe",
        "R0043-TF-1-iframe",
        "H000468-iframe",
    ]
    canonical = {r["id"]: r for r in read_json(DIRECTORY / "catalog.json")["resources"]}
    for resource in plan["resources"]:
        for dimension in ("ROS", "TF", "CREATE", "QUERY"):
            if resource["id"] == "R0058" and dimension == "QUERY":
                continue
            assert resource["dimensions"][dimension] == canonical[resource["id"]]["dimensions"][dimension]
    assert validate_review_lineage(plan) == []
    store.import_plan(plan)
    assert store.status()["completed"] == 0
    assert len(store.status()["tasks"]) == 3
    assert digest(review) == before


def test_explicit_review_wave_one_starts_work_without_requiring_four_packets(tmp_path):
    original, review_path, store = fixture_review(tmp_path)
    assert original["initial_wave"] == 3
    review = read_json(review_path)
    review["initial_wave"] = 1
    json_write(review_path, review)
    output = tmp_path / "rolling-execution.json"
    make_review_plan(tmp_path / "catalog.json", review_path, "rolling-executable", output, store)
    plan = read_json(output)
    assert plan["initial_wave"] == 1
    assert plan["resources"] == original["resources"]
    assert validate_review_lineage(plan) == []
    plan["initial_wave"] = 3
    with pytest.raises(WorkflowError, match="reviewed initial_wave"):
        validate_review_lineage(plan)


@pytest.mark.parametrize("wave", [0, 4, True, "1"])
def test_review_wave_must_fit_exact_plan_capacity(tmp_path, wave):
    _, review_path, store = fixture_review(tmp_path)
    review = read_json(review_path)
    review["initial_wave"] = wave
    json_write(review_path, review)
    with pytest.raises(WorkflowError, match="initial_wave"):
        make_review_plan(tmp_path / "catalog.json", review_path, "invalid-wave", tmp_path / "new.json", store)


def test_independent_sls_history_is_not_merged_with_other_query_or_report_row(tmp_path, monkeypatch):
    plan, _, store = fixture_review(tmp_path)
    resource = plan["resources"][-1]
    contracts = resource["dimensions"]["QUERY"]["contracts"]
    assert [c["id"] for c in contracts] == ["R0058-QUERY-1", "H000468"]
    assert contracts[0]["historical"] == {"iframe": "untested", "open": "untested"}
    assert contracts[1]["historical"] == {"iframe": "fail", "open": "pass"}
    store.import_plan(plan)
    output = tmp_path / "results.html"
    from scripts.web.e2e.resource_link_workflow import reports

    monkeypatch.setattr(reports, "document", lambda _title, groups: json.dumps(groups))
    render_results(store, output)
    groups = read_json(output)
    rows = [row for group in groups if group["key"] == resource["key"] for row in group["rows"]]
    original = [r for r in rows if r["contract_id"] == "H000468"]
    assert len(original) == 1
    assert original[0]["dimension"] == "API 查询 · 近期"
    assert original[0]["status"] == {"iframe": "fail", "open": "pass"}
    assert not any(r["contract_id"] == "R0058-QUERY-1" for r in rows)
    # This unselected, previously unreported catalog entry remains untested in
    # task.spec, but a navigation-only plan must not add it to the old result table.
    with store.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0


def test_reviewed_resource_order_survives_restart_and_blocked_item_does_not_hold_group(tmp_path):
    _, review_path, store = fixture_review(tmp_path)
    review = read_json(review_path)
    review["resources"].reverse()
    json_write(review_path, review)
    output = tmp_path / "ordered.json"
    make_review_plan(tmp_path / "catalog.json", review_path, "ordered-review", output, store)
    store.import_plan(read_json(output))
    store.pause(False)
    assert store.status()["progress"]["next_candidate"]["task_id"].endswith(":R0058")
    action = store.next_action()["actions"][0]
    job = store.execute(action["id"])
    from tests.web.test_resource_link_workflow import bound

    key, thread, turn = bound(store, job)
    store.block(job["job_id"], key, "Offline input blocker, not a live resource failure")
    store.finish_job(job["job_id"], thread, turn, "completed")
    restarted = Store(store.root)
    candidate = restarted.status()["progress"]["next_candidate"]
    assert candidate == {"task_id": "ordered-review:R0043", "kind": "prepare"}
    assert restarted.next_action()["actions"][0]["task_id"] == candidate["task_id"]


def test_resolve_validates_original_full_review_lineage_without_rewriting_frozen_plan(tmp_path):
    plan, review_path, store = fixture_review(tmp_path)
    store.import_plan(plan)
    store.pause(False)
    action = store.next_action()["actions"][0]
    job = store.execute(action["id"])
    from tests.web.test_resource_link_workflow import bound

    key, _, _ = bound(store, job)
    evidence = "inputs/official-offline.json"
    json_write(store.root / evidence, {"fixture": "offline applicability only"})
    dimensions = copy.deepcopy(plan["resources"][0]["dimensions"])
    for dimension, value in dimensions.items():
        value["official_evidence"] = evidence
        if dimension in {"ROS", "QUERY"}:
            value["support"] = "supported"
    with store.transaction() as connection:
        original = connection.execute("SELECT plan FROM runs").fetchone()[0]
    store.resolve_dimensions(job["job_id"], key, dimensions)
    with store.transaction() as connection:
        assert connection.execute("SELECT plan FROM runs").fetchone()[0] == original
        spec = json.loads(connection.execute("SELECT spec FROM tasks WHERE resource_id='R0042'").fetchone()[0])
    assert spec["dimensions"] == dimensions
    assert spec["retest_scope"] == plan["resources"][0]["retest_scope"]
    changed = read_json(review_path)
    changed["targets"].pop()
    json_write(review_path, changed)
    with pytest.raises(WorkflowError):
        store.resolve_dimensions(job["job_id"], key, dimensions)


@pytest.mark.parametrize("resolve_selected", [False, True])
def test_query_only_classification_ignores_unselected_unknowns_but_not_selected_unknown(tmp_path, resolve_selected):
    _, review_path, store = fixture_review(tmp_path)
    review = read_json(review_path)
    review["resources"].reverse()
    json_write(review_path, review)
    output = tmp_path / "query-first.json"
    make_review_plan(tmp_path / "catalog.json", review_path, "query-first", output, store)
    plan = read_json(output)
    resource = plan["resources"][0]
    assert resource["id"] == "R0058"
    store.import_plan(plan)
    store.pause(False)
    job = store.execute(store.next_action()["actions"][0]["id"])
    from tests.web.test_resource_link_workflow import bound

    key, thread, turn = bound(store, job)
    dimensions = copy.deepcopy(resource["dimensions"])
    if resolve_selected:
        evidence = "inputs/official-offline.json"
        json_write(store.root / evidence, {"fixture": "offline applicability only"})
        for dimension, value in dimensions.items():
            value["official_evidence"] = evidence
            if dimension == "QUERY":
                value["support"] = "supported"
        store.resolve_dimensions(job["job_id"], key, dimensions)
    coverage = "inputs/coverage-offline.json"
    json_write(
        store.root / coverage,
        {
            "resources_found": 0,
            "coverage": {"region": "offline", "page": 1},
            "failure_blind_spots": ["Other regions not inspected; fixture, no real cloud calls"],
        },
    )
    if not resolve_selected:
        with pytest.raises(WorkflowError, match="unknown support"):
            store.classify_preparation(job["job_id"], key, coverage)
        return
    store.classify_preparation(job["job_id"], key, coverage)
    store.finish_job(job["job_id"], thread, turn, "completed")
    with store.transaction() as connection:
        task = connection.execute("SELECT * FROM tasks WHERE id=?", ("query-first:R0058",)).fetchone()
        assert task["state"] == "complete"
        assert json.loads(task["spec"])["dimensions"] == dimensions
        assert json.loads(connection.execute("SELECT plan FROM runs").fetchone()[0]) == plan
        verdicts = connection.execute("SELECT scene,status FROM verdicts").fetchall()
    assert [(v["scene"], v["status"]) for v in verdicts] == [("H000468-iframe", "no_sample")]
    assert dimensions["QUERY"]["contracts"][0]["historical"] == {"iframe": "untested", "open": "untested"}
    assert dimensions["QUERY"]["contracts"][1]["historical"]["open"] == "pass"
    assert all(dimensions[d]["support"] == "unknown" for d in ("ROS", "TF", "CREATE"))


def test_no_query_sample_still_cannot_waive_selected_ros_navigation(tmp_path):
    plan, _, store = fixture_review(tmp_path)
    store.import_plan(plan)
    store.pause(False)
    job = store.execute(store.next_action()["actions"][0]["id"])
    from tests.web.test_resource_link_workflow import bound

    key, _, _ = bound(store, job)
    evidence = "inputs/official-offline.json"
    json_write(store.root / evidence, {"fixture": "offline applicability only"})
    dimensions = copy.deepcopy(plan["resources"][0]["dimensions"])
    for dimension, value in dimensions.items():
        value["official_evidence"] = evidence
        if dimension in {"ROS", "QUERY"}:
            value["support"] = "supported"
    store.resolve_dimensions(job["job_id"], key, dimensions)
    coverage = "inputs/coverage-offline.json"
    json_write(store.root / coverage, {"resources_found": 0, "coverage": ["offline"], "failure_blind_spots": []})
    with pytest.raises(WorkflowError, match="cannot waive supported creation"):
        store.classify_preparation(job["job_id"], key, coverage)
    with store.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["missing", "source", "dimension", "duplicate", "case_id"])
def test_review_rejects_omission_duplication_or_cross_source_case_binding(tmp_path, damage):
    _, path, _ = fixture_review(tmp_path)
    review = read_json(path)
    if damage == "missing":
        review["targets"].pop()
    elif damage == "duplicate":
        review["targets"].append(copy.deepcopy(review["targets"][0]))
    elif damage == "case_id":
        review["targets"][1]["case_id"] = review["targets"][0]["case_id"]
    elif damage == "source":
        review["targets"][0]["source_type"] = "different"
    else:
        review["targets"][0]["execution_dimension"] = "QUERY"
    with pytest.raises(WorkflowError):
        reviewed_targets(review)


@pytest.mark.parametrize("damage", ["drop", "substitute", "no_lineage", "historical_sha", "historical_identity"])
def test_managed_scope_rejects_forged_lineage_or_unbound_legacy_id(tmp_path, damage):
    plan, _, store = fixture_review(tmp_path)
    if damage == "drop":
        plan["resources"][0]["retest_scope"]["scene_ids"].pop()
    elif damage == "substitute":
        plan["resources"][0]["dimensions"]["ROS"]["contracts"][0]["source_type"] = "different"
    elif damage == "no_lineage":
        plan.pop("review_lineage")
    elif damage == "historical_sha":
        plan["resources"][-1]["dimensions"]["QUERY"]["contracts"][-1]["historical_binding"]["sha256"] = "bad"
    else:
        plan["resources"][-1]["dimensions"]["QUERY"]["contracts"][-1]["id"] = "H000469"
    with pytest.raises(WorkflowError):
        store.import_plan(plan)
    assert not store.status()["tasks"]


def tf_packet():
    return {
        "scenes": {"R0043-TF-1-iframe": {"account": "fixture"}},
        "execution_grants": {
            "TF": [
                {
                    "id": "identity",
                    "kind": "identity",
                    "quota": 1,
                    "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
                },
                {
                    "id": "stack",
                    "kind": "read",
                    "quota": 1,
                    "read_policy": {"phase": "execute", "min_interval_seconds": 30},
                    "call": {
                        "product": "ros",
                        "action": "GetStack",
                        "region_id": "cn-hangzhou",
                        "params": {"StackId": "offline-stack"},
                    },
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
                        "params": {"StackId": "offline-stack", "LogicalResourceId": "Table"},
                    },
                },
            ]
        },
    }


def test_tf_readonly_admission_requires_ros_getstack_not_a_query_or_local_apply():
    packet = tf_packet()
    validate_execution(packet, {"TF"}, require_read_policy=True, readonly_dimensions={"TF"})
    packet["execution_grants"]["TF"].pop(1)
    with pytest.raises(WorkflowError):
        validate_execution(packet, {"TF"}, require_read_policy=True, readonly_dimensions={"TF"})


@pytest.mark.parametrize("damage", [None, "native", "deleted", "account", "session", "signer", "stack", "query"])
def test_managed_navigation_requires_same_signed_real_ros_terraform_stack(tmp_path, damage):
    path = tmp_path / "gate.sqlite3"
    packet = tf_packet()
    gate.initialize(path)
    gate.install(path, "0", "job:TF", "ws~original", "123", None, packet["execution_grants"]["TF"])
    with gate.database(path) as connection:
        connection.execute("INSERT INTO identities VALUES(?,?,?,?)", ("job:TF", "ws~original", "signer", "123"))
        stack = {
            "StackId": "offline-stack",
            "StackType": "Terraform",
            "Status": "CREATE_COMPLETE",
            "RegionId": "cn-hangzhou",
            "RequestId": "offline-request",
        }
        if damage == "native":
            stack["StackType"] = "ROS"
        if damage == "deleted":
            stack["Status"] = "DELETE_COMPLETE"
        for index, grant in enumerate(("stack", "resource")):
            connection.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created,"
                "reference_scope,tool_use_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(index),
                    "job:TF",
                    grant,
                    "ws~original",
                    "succeeded",
                    json.dumps(stack if not index else {}),
                    "other" if damage == "signer" and index else "signer",
                    index,
                    "opaque",
                    "tool-" + grant,
                ),
            )
    reference = {
        "resourceType": "alicloud_ots_table",
        "stackId": "offline-stack",
        "sources": [
            {"evidence": "api_response", "product": "ros", "action": "GetStackResource", "toolUseId": "tool-resource"}
        ],
    }
    if damage == "stack":
        reference["stackId"] = "other"
    if damage == "query":
        reference["sources"][0]["product"] = "ots"
    before = copy.deepcopy(reference)
    result = managed_references(
        path,
        "job:TF",
        "other" if damage == "session" else "ws~original",
        "other" if damage == "account" else "123",
        [reference],
    )
    assert result == ([reference] if damage is None else [])
    assert reference == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 2


def test_new_scope_without_review_cannot_expand_old_protocol_into_tf(tmp_path):
    plan, _, _ = fixture_review(tmp_path)
    plan.pop("review_lineage")
    assert plan["resources"][0]["retest_scope"]["kind"] == SCOPE_KIND
    with pytest.raises(WorkflowError, match="lineage"):
        validate_plan(plan)
