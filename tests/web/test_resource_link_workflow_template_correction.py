"""Exact OTS failed-plan correction, offline only; no entitlement reset."""

import copy
import hashlib
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.additional_source import validate_additional_source
from scripts.web.e2e.resource_link_workflow.creation import authorization_path, execution_digest
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, initialize, install
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow_temporary_samples import sample  # noqa: F401


@pytest.fixture
def correction(sample):  # noqa: F811
    store, original, admission = sample
    original = copy.deepcopy(original)
    create = original["execution_grants"]["TF"][1]
    template = json.loads(create["call"]["params"]["TemplateBody"])
    template["Workspace"]["main.tf"] += '\nversion = "= 1.140.0"\nprimary_key { type = "STRING" }'
    create["call"]["params"]["TemplateBody"] = json.dumps(template)
    stack = {
        "id": "stack-source",
        "kind": "read",
        "quota": 4,
        "parent": "create",
        "response_path": ["StackId"],
        "target_param": "StackId",
        "read_policy": {"phase": "execute", "min_interval_seconds": 30},
        "call": {
            "product": "ros",
            "action": "GetStack",
            "region_id": "cn-hangzhou",
            "params": {"StackId": "$created_id"},
        },
    }
    original["execution_grants"]["TF"].append(stack)
    members = copy.deepcopy(stack)
    members.update(id="stack-members", quota=1)
    members["call"]["action"] = "ListStackResources"
    members["read_policy"].pop("min_interval_seconds")
    original["execution_grants"]["TF"].append(members)
    admission["sample_bindings"]["TF"]["template_sha256"] = hashlib.sha256(
        create["call"]["params"]["TemplateBody"].encode()
    ).hexdigest()
    json_write(store.root / "inputs/proposal.json", original)
    admission["proposal_sha256"] = digest(store.root / "inputs/proposal.json")
    admission["execution_sha256"] = execution_digest(original)
    json_write(store.root / "inputs/review.json", admission)
    store.authorize_temporary_samples(original["task_id"], "inputs/review.json")
    original["cleanup_recipe"] = {"TF": "response-bound stack delete and absence"}
    json_write(store.root / "inputs/original.json", original)
    json_write(store.root / "inputs/original-review.json", {"approved": True})
    approval = {
        "schema_version": 3,
        "authorized": True,
        "identity_mode": "existing_account",
        "resource_ids": ["R0043"],
        "account": "1234567890123456",
        "max_budget_cents": 100,
        "execution_sha256": execution_digest(original),
        "lifetime": "task",
        "expires_at": None,
        "authorization_evidence": "inputs/original-review.json",
        "authorization_sha256": digest(store.root / "inputs/original-review.json"),
    }
    json_write(authorization_path(store.root, original), approval)
    scope = "a" * 32 + ":TF"
    authority = execution_digest(original) + ":TF"
    gate = store.root / "execution-gate.sqlite3"
    initialize(gate)
    install(
        gate,
        "0",
        scope,
        "offline~original",
        approval["account"],
        None,
        original["execution_grants"]["TF"],
        authority=authority,
    )
    with store.transaction() as c:
        c.execute(
            "INSERT INTO jobs(id,task_id,role,purpose,state,turn_id,capability,submission,created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("a" * 32, original["task_id"], "glm", "execute", "complete", "original-turn", "offline", "confirmed", 1),
        )
        c.execute("INSERT INTO budget_reservations VALUES(?,?)", (original["task_id"], 100))
    error = "expected primary_key.0.type to be one of [Integer Binary String], got STRING"
    failure = {
        "StackId": "offline-stack",
        "StackType": "Terraform",
        "Status": "CREATE_ROLLBACK_COMPLETE",
        "RequestId": "offline-request",
        "OperationInfo": {"ResourceType": "alicloud_ots_table", "Message": error},
        "Log": {"TerraformLogs": [{"Command": "plan", "Stream": "stderr", "Content": "Error: " + error}]},
    }
    with sqlite3.connect(gate) as c:
        c.execute("UPDATE grants SET used=1 WHERE id IN ('identity','create','delete','stack-source')")
        c.execute("UPDATE authority_limits SET used=1 WHERE id IN ('identity','create','delete','stack-source')")
        c.execute(
            "INSERT INTO identities VALUES(?,?,?,?)", (scope, "offline~original", "offline-key", approval["account"])
        )
        for index, (grant, body) in enumerate(
            [
                ("identity", {"AccountId": approval["account"]}),
                ("create", {"StackId": "offline-stack", "RequestId": "offline-create"}),
                ("stack-source", failure),
                ("delete", {"RequestId": "offline-delete"}),
            ]
        ):
            c.execute(
                "INSERT INTO attempts(id,scope,grant_id,state,response,created) VALUES(?,?,?,?,?,?)",
                (grant, scope, grant, "succeeded", canonical(body), index + 1),
            )
    packet = copy.deepcopy(original)
    packet["execution_grants"] = {"TF": packet["execution_grants"]["TF"]}
    packet["scenes"] = {"R0043-TF-1-iframe": {"account": approval["account"]}}
    before = packet["execution_grants"]["TF"][1]["call"]["params"]["TemplateBody"]
    after = before.replace("STRING", "String")
    packet["execution_grants"]["TF"][1]["call"]["params"]["TemplateBody"] = after
    json_write(store.root / "inputs/remediation.json", {"offline": "enum correction"})
    request = {
        "request_id": "offline-ots-primary-key-correction",
        "dimension": "TF",
        "prior_scope": scope,
        "predecessor_proposal": "inputs/original.json",
        "predecessor_sha256": digest(store.root / "inputs/original.json"),
        "remediation_evidence": "inputs/remediation.json",
        "remediation_sha256": digest(store.root / "inputs/remediation.json"),
    }
    packet["additional_source_requests"] = [request]
    provider = store.root / "inputs/provider.go"
    provider.write_text(
        "validation.StringInSlice([]string{string(StringType)}, false)\n"
        "reservedThroughput := new(tablestore.ReservedThroughput)\n",
        encoding="utf-8",
    )
    correction_review = {
        "kind": "ops_ots_tf_primary_key_correction",
        "approved": True,
        "task_id": packet["task_id"],
        "original_table_never_created": True,
        "human_scope_interpretation": "offline pre-apply failure; one actual table remains permitted",
        "original_admission": {"path": "inputs/review.json", "sha256": digest(store.root / "inputs/review.json")},
        "original_template_sha256": hashlib.sha256(before.encode()).hexdigest(),
        "corrected_template_sha256": hashlib.sha256(after.encode()).hexdigest(),
        "original_failure_attempt_id": "stack-source",
        "provider_source": {
            "path": "inputs/provider.go",
            "sha256": digest(provider),
            "url": "https://raw.githubusercontent.com/aliyun/terraform-provider-alicloud/v1.140.0/"
            "alicloud/resource_alicloud_ots_table.go",
        },
    }
    proof = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "additional_sources_approved": [request],
        "carry_forward_dimensions": {},
        "temporary_template_corrections": {request["request_id"]: correction_review},
    }
    json_write(store.root / "inputs/official-fee.json", {"offline": "fake fee basis"})
    cost = {
        "kind": "ops_cumulative_fee_review",
        "approved": True,
        "task_id": packet["task_id"],
        "account": approval["account"],
        "execution_sha256": execution_digest(packet),
        "additional_operations": [request],
        "human_authorization": "inputs/human.json",
        "human_sha256": digest(store.root / "inputs/human.json"),
        "budget_cents": 100,
        "includes_previous_operations": True,
        "estimate_basis": "offline",
        "limitations": "not hard cap",
        "official_sources": [
            {"path": "inputs/official-fee.json", "sha256": digest(store.root / "inputs/official-fee.json")}
        ],
    }
    json_write(store.root / "inputs/cost.json", cost)
    proof["paid_operation_review"] = {
        "human_authorization": cost["human_authorization"],
        "human_sha256": cost["human_sha256"],
        "cost_evidence": "inputs/cost.json",
        "cost_sha256": digest(store.root / "inputs/cost.json"),
    }
    proof["budget_reservation_reuse"] = {
        "task_id": packet["task_id"],
        "reserved_cents": 100,
        "cost_evidence": "inputs/cost.json",
        "cost_sha256": digest(store.root / "inputs/cost.json"),
    }
    json_write(store.root / "inputs/corrected.json", packet)
    proof["proposal_sha256"] = digest(store.root / "inputs/corrected.json")
    json_write(store.root / "inputs/corrected-review.json", proof)
    return store, packet, proof, authority


def test_exact_corrected_source_is_new_operation_without_mutating_admission_or_journal(correction, monkeypatch):
    store, packet, proof, old_authority = correction
    before = (store.root / "execution-gate.sqlite3").read_bytes()
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        old_meta = c.execute("SELECT value FROM meta WHERE key LIKE 'temporary_samples:%'").fetchone()[0]
        assert admitted(store.root, c, task, packet) == {}
        authority = admitted(store.root, c, task, packet, review=proof)["TF"]
        assert authority != old_authority
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.selection.selected_scenes", lambda *_: [{"dimension": "TF"}]
    )
    assert (
        Runtime(store).authorize_creation(store.root / "inputs/corrected.json", "inputs/corrected-review.json", 100)[
            "cloud_write_performed"
        ]
        is False
    )
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        assert admitted(store.root, c, task, packet) == {"TF": authority}
        assert c.execute("SELECT value FROM meta WHERE key LIKE 'temporary_samples:%'").fetchone()[0] == old_meta
        assert c.execute("SELECT amount FROM budget_reservations").fetchone()[0] == 100
    assert (store.root / "execution-gate.sqlite3").read_bytes() == before


def test_authorized_context_can_review_current_owner_but_cannot_admit_another_operation(correction, monkeypatch):
    store, packet, proof, _ = correction
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.selection.selected_scenes", lambda *_: [{"dimension": "TF"}]
    )
    Runtime(store).authorize_creation(store.root / "inputs/corrected.json", "inputs/corrected-review.json", 100)
    before = (store.root / "execution-gate.sqlite3").read_bytes()
    with store.transaction() as c:
        c.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            ("current-generation", packet["task_id"], "TF", '{"id":"new-stack"}', "present", "{}"),
        )
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        # Classification must be available before sole Sol can verify cleanup.
        assert admitted(store.root, c, task, packet)["TF"]
        assert store.readonly_dimensions(c, task, packet) == {"ROS"}
        # Explicit new/repeated OPS authorization still checks live ownership.
        with pytest.raises(WorkflowError, match="verified cleanup"):
            admitted(store.root, c, task, packet, review=proof)
    assert (store.root / "execution-gate.sqlite3").read_bytes() == before


@pytest.mark.parametrize("damage", ["approval", "provider", "cost", "human", "journal", "authority"])
def test_authorized_context_still_rejects_changed_immutable_review(correction, monkeypatch, damage):
    store, packet, proof, _ = correction
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.selection.selected_scenes", lambda *_: [{"dimension": "TF"}]
    )
    Runtime(store).authorize_creation(store.root / "inputs/corrected.json", "inputs/corrected-review.json", 100)
    if damage == "approval":
        json_write(store.root / "inputs/corrected-review.json", {"approved": True})
    elif damage in {"cost", "human"}:
        json_write(store.root / f"inputs/{damage}.json", {})
    elif damage == "provider":
        (store.root / "inputs/provider.go").write_text("wrong", encoding="utf-8")
    elif damage == "journal":
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as c:
            c.execute("UPDATE attempts SET state='unknown' WHERE id='stack-source'")
    else:
        path = authorization_path(store.root, packet)
        approval = json.loads(path.read_text(encoding="utf-8"))
        approval["dimension_authorities"]["TF"] = "wrong"
        json_write(path, approval)
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        with pytest.raises(WorkflowError):
            admitted(store.root, c, task, packet)


@pytest.mark.parametrize(
    "damage",
    [
        "no-review",
        "provider",
        "approval",
        "admission",
        "template-hash",
        "parent",
        "name",
        "provider-version",
        "extra-template-change",
        "quota",
        "delete-call",
        "open-scene",
        "failure-id",
        "real-apply",
        "different-error",
        "identity",
        "unknown",
        "unused-write",
        "human",
        "cost",
        "renamed-request",
    ],
)
def test_correction_refuses_unreviewed_or_expanded_scope(correction, damage):
    store, packet, proof, _ = correction
    review = next(iter(proof["temporary_template_corrections"].values()))
    if damage == "no-review":
        proof.pop("temporary_template_corrections")
    elif damage == "provider":
        (store.root / "inputs/provider.go").write_text("drift", encoding="utf-8")
    elif damage == "approval":
        review["approved"] = False
    elif damage == "admission":
        review["original_admission"]["sha256"] = "wrong"
    elif damage == "template-hash":
        review["corrected_template_sha256"] = "wrong"
    elif damage in {"parent", "name", "provider-version", "extra-template-change"}:
        create = packet["execution_grants"]["TF"][1]["call"]["params"]
        before = {
            "parent": "offline-parent",
            "name": "offline_tf",
            "provider-version": "1.140.0",
            "extra-template-change": "String",
        }[damage]
        create["TemplateBody"] = create["TemplateBody"].replace(before, "WRONG")
    elif damage == "quota":
        packet["execution_grants"]["TF"][1]["quota"] = 2
    elif damage == "delete-call":
        packet["execution_grants"]["TF"][2]["call"]["params"]["StackId"] = "existing"
    elif damage == "open-scene":
        packet["scenes"]["R0043-TF-1-open"] = next(iter(packet["scenes"].values()))
    elif damage == "failure-id":
        review["original_failure_attempt_id"] = "missing"
    elif damage in {"real-apply", "different-error", "identity", "unknown", "unused-write"}:
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as c:
            if damage in {"real-apply", "different-error"}:
                body = json.loads(c.execute("SELECT response FROM attempts WHERE id='stack-source'").fetchone()[0])
                if damage == "real-apply":
                    body["Log"]["TerraformLogs"].append({"Command": "apply", "Content": "actual apply"})
                else:
                    body["OperationInfo"]["Message"] = "some other error"
                c.execute("UPDATE attempts SET response=? WHERE id='stack-source'", (canonical(body),))
            elif damage == "identity":
                c.execute("UPDATE identities SET account='other-account'")
            elif damage == "unknown":
                c.execute("UPDATE attempts SET state='unknown' WHERE id='create'")
            else:
                c.execute("UPDATE authority_limits SET used=0 WHERE id='create'")
    elif damage in {"human", "cost"}:
        json_write(store.root / f"inputs/{damage}.json", {})
    else:
        # The already-reviewed correction cannot be cloned under a fresh request ID.
        saved = {
            "task_id": packet["task_id"],
            "authorization_evidence": "inputs/corrected-review.json",
            "additional_source_requests": copy.deepcopy(packet["additional_source_requests"]),
        }
        saved["additional_source_requests"][0]["request_id"] = "another-operation-id"
        json_write(store.root / "creation-authorizations/previous.json", saved)
    # Validate shape/history directly, independent of the execution digest drift.
    from scripts.web.e2e.resource_link_workflow.additional_source import _validate_original_operation

    with pytest.raises(WorkflowError):
        if damage in {"human", "cost"}:
            validate_additional_source(store.root, packet, proof)
        else:
            _validate_original_operation(store.root, packet, packet["additional_source_requests"][0], proof)
