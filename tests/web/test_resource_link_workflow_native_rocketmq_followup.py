"""A reviewed follow-up retains the native recipe, not the exhausted old authority."""

import copy
import hashlib
import json

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.creation import authorization_path, execution_digest
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.runtime import Runtime, console_inspection
from scripts.web.e2e.resource_link_workflow.store import dumps
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow_native_rocketmq_sample import reviewed  # noqa: F401


@pytest.fixture
def followup(reviewed, monkeypatch):  # noqa: F811
    store, old, admission = reviewed
    store.authorize_temporary_samples(old["task_id"], "inputs/admission.json")
    runtime = Runtime(store)
    runtime.authorize_creation(store.root / "inputs/proposal.json", "inputs/operation.json", 0)
    job, sol = "a" * 32, "b" * 32
    path = store.root / "execution-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    credential = AliyunCredential(mode="AK", access_key_id="offline-fake", access_key_secret="offline-fake-secret")
    account, stack, instance = "1234567890123456", "own-stack", "MQ_INST_1234567890123456_Own"
    for dimension, grants in old["execution_grants"].items():
        gate.install(
            path,
            "0",
            job + ":" + dimension,
            "ws~original",
            account,
            None,
            grants,
            authority=execution_digest(old) + ":" + dimension,
        )
        for grant in grants:
            call = copy.deepcopy(grant["call"])
            if grant.get("parent"):
                call["params"][grant["target_param"]] = stack
            if grant["kind"] == "delete":
                gate.cleanup(path, "0", job + ":" + dimension, "ws~original")
            action = call["action"]
            response = {"RequestId": "OFFLINE-FAKE-" + dimension + "-" + grant["id"]}
            if action == "GetCallerIdentity":
                response["AccountId"] = account
            elif action == "CreateStack":
                response["StackId"] = stack
            elif action == "GetStack":
                response.update(
                    StackId=stack,
                    Status="DELETE_COMPLETE"
                    if grant.get("read_policy", {}).get("phase") == "cleanup"
                    else "CREATE_COMPLETE",
                )
            elif action == "GetStackResource":
                response.update(StackId=stack, PhysicalResourceId=instance, ResourceType="ALIYUN::ROCKETMQ::Instance")
            elif action == "ListStackResources":
                response["Resources"] = [{"PhysicalResourceId": instance, "ResourceType": "ALIYUN::ROCKETMQ::Instance"}]
            elif action == "OnsInstanceInServiceList":
                response["Data"] = {"InstanceVO": []}
            ticket = gate.reserve(
                call,
                "ws~original",
                credential,
                read_only=grant["kind"] not in {"create", "delete"},
                tool_use_id=dimension + "-" + grant["id"],
            )
            gate.finish(ticket, response, True)
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='blocked',attempt=1")
        for ident, role, purpose in ((job, "glm", "dispatch"), (sol, "sol", "review")):
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    old["task_id"],
                    role,
                    purpose,
                    "complete",
                    role + "-thread",
                    role + "-turn",
                    "offline-fake",
                    "result_staged",
                    1,
                ),
            )
    json_write(
        store.root / "inputs/absence.json",
        {"offline_fake": True, "StackId": stack, "Status": "DELETE_COMPLETE", "Data": {"InstanceVO": []}},
    )
    resources = [
        {"account": account, "action": "CreateStack", "id": stack, "product": "ros", "region": "cn-hangzhou"},
        {
            "account": account,
            "id": instance,
            "stack_id": stack,
            "region": "cn-hangzhou",
            "logical_resource_id": "Instance",
            "resource_type": "ALIYUN::ROCKETMQ::Instance",
        },
    ]
    cleanup = []
    for index, resource in enumerate(resources):
        ident, evidence = "owner-" + str(index), "inputs/cleanup-" + str(index) + ".json"
        json_write(
            store.root / evidence,
            {
                "ownership_id": ident,
                "resource": resource,
                "absent": True,
                "failed": False,
                "source_evidence": "inputs/absence.json",
                "source_sha256": digest(store.root / "inputs/absence.json"),
            },
        )
        with store.transaction() as db:
            db.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    old["task_id"],
                    "ROS",
                    dumps(resource),
                    "deleted",
                    dumps({"path": evidence, "sha256": digest(store.root / evidence)}),
                ),
            )
        cleanup.append({"ownership_id": ident, "confirmed_absent": True, "evidence": evidence})
    receipt = {
        "task_id": old["task_id"],
        "attempt": 1,
        "sol_thread": "sol-thread",
        "sol_turn": "sol-turn",
        "decisions": [{"scene": "R0083-ROS-1-open", "status": "fail"}],
        "cleanup": cleanup,
    }
    json_write(store.root / "receipts/original.json", receipt)
    with store.transaction() as db:
        store.event(
            db,
            "acceptance_committed",
            {
                "task_id": old["task_id"],
                "job_id": sol,
                "receipt_path": "receipts/original.json",
                "receipt_sha256": digest(store.root / "receipts/original.json"),
            },
        )
    json_write(store.root / "inputs/remediation.json", {"offline_fake": "verified notice sampler correction"})
    packet = copy.deepcopy(old)
    packet["additional_source_requests"] = [
        {
            "request_id": "offline-followup-" + dim,
            "dimension": dim,
            "predecessor_proposal": "inputs/proposal.json",
            "predecessor_sha256": digest(store.root / "inputs/proposal.json"),
            "prior_scope": job + ":" + dim,
            "remediation_evidence": "inputs/remediation.json",
            "remediation_sha256": digest(store.root / "inputs/remediation.json"),
        }
        for dim in ("ROS", "QUERY")
    ]
    json_write(store.root / "inputs/followup.json", packet)

    def ref(relative):
        return {"path": relative, "sha256": digest(store.root / relative)}

    for name in ("source", "cost"):
        value = read_json(store.root / "inputs" / (name + ".json"))
        value["execution_sha256"] = execution_digest(packet)
        json_write(store.root / "inputs" / ("followup-" + name + ".json"), value)
    proof = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "task_id": old["task_id"],
        "proposal_sha256": digest(store.root / "inputs/followup.json"),
        "additional_sources_approved": packet["additional_source_requests"],
        "carry_forward_dimensions": {},
        "native_rocketmq_followup": {
            "kind": "ops_native_rocketmq_followup_review",
            "approved": True,
            "task_id": old["task_id"],
            "execution_sha256": execution_digest(packet),
            "proposal_sha256": digest(store.root / "inputs/followup.json"),
            "proposal": ref("inputs/followup.json"),
            "original_admission": ref("inputs/admission.json"),
            "source_review": ref("inputs/followup-source.json"),
            "cost_review": ref("inputs/followup-cost.json"),
            "predecessor_cleanup_receipt": ref("receipts/original.json"),
            "within_existing_task_and_hourly_scope": True,
            "not_unlimited_rebuild": True,
            "creation_after_failed_capture_and_verified_cleanup": True,
        },
    }
    json_write(store.root / "inputs/followup-review.json", proof)
    return runtime, packet, proof, admission


def test_followup_uses_new_operation_authority_without_resetting_original_calls(followup):
    runtime, packet, proof, _ = followup

    def snapshot():
        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            return {
                t: [dict(r) for r in db.execute("SELECT * FROM " + t + " ORDER BY rowid")]
                for t in ("grants", "attempts", "authority_limits", "identities", "active")
            }

    before = snapshot()
    runtime.authorize_creation(runtime.root / "inputs/followup.json", "inputs/followup-review.json", 0)
    approval = read_json(authorization_path(runtime.root, packet))
    authority = (
        hashlib.sha256(
            gate.canonical(
                {"task_id": packet["task_id"], "operation": packet["additional_source_requests"][0]}
            ).encode()
        ).hexdigest()
        + ":ROS"
    )
    assert approval["dimension_authorities"]["ROS"] == authority
    old = read_json(runtime.root / "inputs/proposal.json")
    assert authority != execution_digest(old) + ":ROS"
    assert snapshot() == before
    # The already approved current generation may have its own live owner.
    # Context must not misclassify that owner as an uncleared predecessor.
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            ("current", packet["task_id"], "ROS", dumps({"id": "NEW-OFFLINE-STACK"}), "present", "offline-current"),
        )
        task = db.execute("SELECT * FROM tasks").fetchone()
        assert admitted(runtime.root, db, task, packet) == {"ROS": authority}
        assert runtime.store.readonly_dimensions(db, task, packet) == {"QUERY"}
        original_admission = json.loads(
            db.execute("SELECT value FROM meta WHERE key=?", ("temporary_samples:" + task["id"],)).fetchone()[0]
        )
        assert original_admission["execution_sha256"] == execution_digest(old)
    assert snapshot() == before


@pytest.mark.parametrize("damage", ["unreviewed", "old-authority", "source", "cost", "receipt", "owner", "delete"])
def test_native_followup_rejects_incomplete_review_or_unclean_predecessor(followup, damage):
    runtime, packet, proof, _ = followup
    if damage == "unreviewed":
        proof.pop("native_rocketmq_followup")
    elif damage == "old-authority":
        proof["native_rocketmq_followup"]["original_admission"]["sha256"] = "0" * 64
    elif damage in {"source", "cost"}:
        proof["native_rocketmq_followup"][damage + "_review"]["sha256"] = "0" * 64
    elif damage == "receipt":
        proof["native_rocketmq_followup"]["predecessor_cleanup_receipt"]["sha256"] = "0" * 64
    elif damage == "owner":
        with runtime.store.transaction() as db:
            db.execute("UPDATE owned SET state='present' WHERE id='owner-1'")
    else:
        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            db.execute("UPDATE grants SET used=0 WHERE id='delete'")
            db.execute("UPDATE authority_limits SET used=0 WHERE id='delete'")
            db.execute("DELETE FROM attempts WHERE grant_id='delete'")
    json_write(runtime.root / "inputs/followup-review.json", proof)
    with pytest.raises(WorkflowError):
        runtime.authorize_creation(runtime.root / "inputs/followup.json", "inputs/followup-review.json", 0)
    assert not authorization_path(runtime.root, packet).exists()


def test_explicit_notice_profile_keeps_runtime_wire_bound_inspector():
    binding = {
        "console_inspection": "rocketmq_instance_notice",
        "mode": "open",
        "dimension": "QUERY",
        "account": "1234567890123456",
        "render_text": ["MQ_INST_1234567890123456_Own", "offline-own"],
    }
    assert console_inspection(binding, "ALIYUN::ROCKETMQ::Instance", "MQ_INST_1234567890123456_Own") is None
    for damage in ({"mode": "iframe"}, {"account": "9999999999999999"}, {"dimension": "CREATE"}):
        with pytest.raises(WorkflowError):
            console_inspection({**binding, **damage}, "ALIYUN::ROCKETMQ::Instance", "MQ_INST_1234567890123456_Own")
