"""Human-authorized PendingDelete is a disposition, not absence or navigation PASS."""

import copy
import json

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.pending_delete import prior_user_decisions, validate
from scripts.web.e2e.resource_link_workflow.store import Store


@pytest.fixture(params=["ROS", "TF", "CREATE"])
def pending(tmp_path, request):
    store = Store.initialize(tmp_path / "state")
    task, owner = "run:R0076", "owned-group"
    dimension = request.param
    resource = {
        "id": "rg-test",
        "account": "123",
        "region": "cn-hangzhou",
        "resource_type": (
            "ALIYUN::ResourceManager::ResourceGroup"
            if dimension == "ROS"
            else "alicloud_resource_manager_resource_group"
        ),
    }
    root = store.root
    json_write(root / "inputs/creation.json", {"original": True})
    original = {"path": "inputs/creation.json", "sha256": digest(root / "inputs/creation.json")}
    if dimension == "CREATE":
        resource = {
            "id": "rg-test",
            "account": "123",
            "region": "cn-hangzhou",
            "product": "resourcemanager",
            "action": "CreateResourceGroup",
        }
        creation = {
            "job_id": "job",
            "dimension": "CREATE",
            "state": "succeeded",
            "resource": resource,
            "attempt_id": "actual-create",
            "tool_use_id": "actual-tool",
            "session_id": "full-session",
            "response": {"RequestId": "actual-request", "ResourceGroup": {"Id": "rg-test", "AccountId": "123"}},
        }
        json_write(root / "jobs/job/output/creation.json", creation)
        original = {"path": "jobs/job/output/creation.json", "sha256": digest(root / "jobs/job/output/creation.json")}
    human = {
        "kind": "human_pending_delete_authorization",
        "approved": True,
        "task_id": task,
        "ownership_id": owner,
        "resource_id": resource["id"],
        "original_user_message": "PendingDelete 当作已删除",
    }
    source = {
        "task_id": task,
        "account": "123",
        "original_scope": f"job:{dimension}",
        "cloud_writes": 0,
        "calls": [
            {"product": "sts", "action": "GetCallerIdentity", "status_code": 200, "response": {"AccountId": "123"}},
            {
                "product": "resourcemanager",
                "version": "2020-03-31",
                "action": "ListResourceGroups",
                "status_code": 200,
                "params": {"ResourceGroupId": "rg-test", "PageNumber": 1, "PageSize": 10},
                "response": {
                    "RequestId": "request-test",
                    "TotalCount": 1,
                    "ResourceGroups": {
                        "ResourceGroup": [{"Id": "rg-test", "AccountId": "123", "Status": "PendingDelete"}]
                    },
                },
            },
        ],
    }
    json_write(root / "inputs/human.json", human)
    json_write(root / "inputs/source.json", source)
    proof = {
        "kind": "ops_user_accepted_pending_delete",
        "task_id": task,
        "ownership_id": owner,
        "resource": resource,
        "absent": False,
        "failed": False,
        "cleanup_disposition": "user_accepted_pending_delete",
        "scope": f"job:{dimension}",
        "human_authorization": "inputs/human.json",
        "human_sha256": digest(root / "inputs/human.json"),
        "source_evidence": "inputs/source.json",
        "source_sha256": digest(root / "inputs/source.json"),
        "original_creation": original,
    }
    with store.transaction() as c:
        c.execute(
            "INSERT INTO tasks(id,run_id,resource_id,spec,state) VALUES(?,?,?,?,?)",
            (task, "run", "R0076", "{}", "blocked"),
        )
        c.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            (owner, task, dimension, json.dumps(resource), "present", json.dumps(original)),
        )
    return store, task, owner, resource, proof


@pytest.mark.parametrize(
    "damage",
    [
        "absent",
        "kind",
        "resource_type",
        "account",
        "scope",
        "active",
        "different_id",
        "no_identity",
        "unapproved",
        "changed_sha",
        "unknown",
    ],
)
def test_pending_delete_rejects_false_or_unapproved_evidence(pending, damage):
    store, task, owner, resource, proof = pending
    proof = copy.deepcopy(proof)
    if damage == "absent":
        proof["absent"] = True
    elif damage == "kind":
        proof["kind"] = "normal_absence"
    elif damage == "resource_type":
        resource["resource_type"] = "ALIYUN::OSS::Bucket"
    elif damage == "account":
        resource["account"] = "999"
    elif damage == "scope":
        proof["scope"] = "other:ROS"
    else:
        path = store.root / ("inputs/human.json" if damage == "unapproved" else "inputs/source.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        if damage == "unapproved":
            value["approved"] = False
        elif damage == "active":
            value["calls"][1]["response"]["ResourceGroups"]["ResourceGroup"][0]["Status"] = "OK"
        elif damage == "different_id":
            value["calls"][1]["response"]["ResourceGroups"]["ResourceGroup"][0]["Id"] = "rg-other"
        elif damage == "no_identity":
            value["calls"] = value["calls"][1:]
        elif damage == "unknown":
            value["calls"][1]["status_code"] = 500
        else:
            value["account"] = "modified"
        json_write(path, value)
        if damage != "changed_sha":
            proof["human_sha256" if damage == "unapproved" else "source_sha256"] = digest(path)
    with pytest.raises(WorkflowError):
        validate(store.root, task, owner, resource, proof)


def test_ops_pending_delete_records_honest_disposition_only_and_is_idempotent(pending):
    store, task, owner, resource, proof = pending
    path = store.root / "inputs/proof.json"
    json_write(path, proof)
    result = store.accept_pending_delete(task, "inputs/proof.json")
    assert result["actual_absence_verified"] is False
    assert result["pass_added"] is False and result["slot_released"] is False
    with store.transaction() as c:
        row = c.execute("SELECT * FROM owned WHERE id=?", (owner,)).fetchone()
        assert row["state"] == "deleted"
        assert c.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0
        assert c.execute("SELECT state FROM tasks WHERE id=?", (task,)).fetchone()[0] == "blocked"
        assert c.execute("SELECT count(*) FROM events WHERE kind='ops_user_accepted_pending_delete'").fetchone()[0] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["absent"] is False
    assert store.accept_pending_delete(task, "proof.json")["already_recorded"] is True
    validate(store.root, task, owner, resource, proof)


def test_ops_pending_delete_cannot_run_during_dispatch(pending):
    store, task, _, _, proof = pending
    json_write(store.root / "inputs/proof.json", proof)
    store.pause(False)
    with pytest.raises(WorkflowError, match="paused"):
        store.accept_pending_delete(task, "proof.json")


@pytest.mark.parametrize("damage", [None, "human", "proof", "owner", "other_task"])
def test_prior_human_decision_handoff_is_exact_readonly_and_does_not_accept_new_owner(pending, damage):
    store, task, owner, _, proof = pending
    json_write(store.root / "inputs/proof.json", proof)
    store.accept_pending_delete(task, "proof.json")
    if damage in {"human", "proof"}:
        json_write(store.root / ("inputs/human.json" if damage == "human" else "inputs/proof.json"), {})
    with store.transaction() as c:
        if damage == "owner":
            c.execute("UPDATE owned SET state='present' WHERE id=?", (owner,))
        before = c.execute("SELECT count(*) FROM events").fetchone()[0]
        if damage in {"human", "proof", "owner"}:
            with pytest.raises(WorkflowError):
                prior_user_decisions(store.root, c, task)
        else:
            result = prior_user_decisions(store.root, c, "other" if damage == "other_task" else task)
            if damage == "other_task":
                assert result == []
            else:
                assert result[0]["original_user_message"] == "PendingDelete 当作已删除"
                assert result[0]["original_ownership_id"] == owner
                assert result[0]["accepts_new_owner"] is False
                assert result[0]["actual_absence_verified"] is False
                assert result[0]["new_owner_independent_ops_sol_review_required"] is True
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == before
        assert c.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "resource_type",
    [
        "alicloud_resource_manager_folder",
        "ALIYUN::ROS::Stack",
        "alicloud_oss_bucket",
        "alicloud_resource_manager_resource_group_other",
    ],
)
def test_pending_delete_does_not_expand_to_other_resource_types(pending, resource_type):
    store, task, owner, resource, proof = pending
    resource["resource_type"] = resource_type
    with pytest.raises(WorkflowError, match="exact ResourceGroup"):
        validate(store.root, task, owner, resource, proof)


@pytest.mark.parametrize(
    "damage",
    [
        "action",
        "product",
        "scope",
        "sha",
        "state",
        "id",
        "account",
        "job",
        "dimension",
        "session",
        "request",
        "attempt",
        "tool",
        "resource",
    ],
)
def test_api_group_requires_original_successful_same_generation_wire_source(pending, damage):
    store, task, owner, resource, proof = pending
    if resource.get("action") != "CreateResourceGroup":
        pytest.skip("API-specific provenance case")
    proof = copy.deepcopy(proof)
    if damage in {"action", "product"}:
        resource[damage] = "wrong"
    elif damage == "scope":
        proof["scope"] = "other:CREATE"
    elif damage == "sha":
        proof["original_creation"]["sha256"] = "wrong"
    else:
        path = store.root / proof["original_creation"]["path"]
        source = json.loads(path.read_text(encoding="utf-8"))
        if damage in {"id", "account"}:
            source["response"]["ResourceGroup"]["Id" if damage == "id" else "AccountId"] = "wrong"
        elif damage == "request":
            source["response"]["RequestId"] = ""
        else:
            field = {"job": "job_id", "session": "session_id", "attempt": "attempt_id", "tool": "tool_use_id"}.get(
                damage, damage
            )
            source[field] = "wrong" if damage in {"state", "job", "dimension"} else None
        json_write(path, source)
        proof["original_creation"]["sha256"] = digest(path)
    with pytest.raises(WorkflowError, match="exact ResourceGroup"):
        validate(store.root, task, owner, resource, proof)
