"""MSE corrective generation: synthetic offline evidence, no cloud or user config."""

import copy
import hashlib
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.additional_source import _validate_original_operation
from scripts.web.e2e.resource_link_workflow.creation import execution_digest
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, initialize, install
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.mse_correction import CLEANUP_KIND, KIND, corrected_grants
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow_hourly_samples import reviewed  # noqa: F401


@pytest.fixture
def correction(reviewed):  # noqa: F811
    store, old, admission = reviewed
    store.authorize_temporary_samples(old["task_id"], "inputs/admission.json")
    Runtime(store).authorize_creation(store.root / "inputs/proposal.json", "inputs/operation.json", 0)
    task, scope, session = old["task_id"], "a" * 32 + ":TF", "offline~mse"
    authority, account = execution_digest(old) + ":TF", "1234567890123456"
    entries = old["execution_grants"]["TF"]
    create = next(e for e in entries if e["kind"] == "create")
    delete = next(e for e in entries if e["kind"] == "delete")
    status_id = next(
        e["id"] for e in entries if e["call"]["action"] == "GetStack" and e["read_policy"]["phase"] == "execute"
    )
    status = {
        "RequestId": "fake-status",
        "StackId": "fake-stack",
        "StackName": create["call"]["params"]["StackName"],
        "StackType": "Terraform",
        "RegionId": "cn-hangzhou",
        "Status": "CREATE_ROLLBACK_COMPLETE",
        "OperationInfo": {"Code": "IllegalRequest", "ResourceType": "alicloud_mse_cluster"},
        "Log": {
            "TerraformLogs": [
                {
                    "Content": "alicloud_vpc.sample_vpc: Creation complete [id=vpc-fake]\n"
                    "alicloud_vswitch.sample_vswitch: Creation complete [id=vsw-fake]"
                }
            ]
        },
    }
    initialize(store.root / "execution-gate.sqlite3")
    install(store.root / "execution-gate.sqlite3", "0", scope, session, account, None, entries, authority=authority)
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as db:
        db.execute("INSERT INTO identities VALUES(?,?,?,?)", (scope, session, "fake-credential", account))
        db.execute("UPDATE grants SET used=1 WHERE scope=? AND id=?", (scope, create["id"]))
        db.execute("UPDATE authority_limits SET used=1 WHERE authority=? AND id=?", (authority, create["id"]))
        for index, (grant, body) in enumerate(
            [(create["id"], {"RequestId": "fake-create", "StackId": "fake-stack"}), (status_id, status)]
        ):
            db.execute(
                "INSERT INTO attempts(id,scope,grant_id,state,response,created,session,credential) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("attempt-" + grant, scope, grant, "succeeded", canonical(body), index + 1, session, "fake-credential"),
            )

    def save(name, value):
        path = "inputs/" + name + ".json"
        json_write(store.root / path, value)
        return {"path": path, "sha256": digest(store.root / path)}

    def call(product, action, version, params):
        return {"product": product, "action": action, "version": version, "region_id": "cn-hangzhou", "params": params}

    identity = call("sts", "GetCallerIdentity", "2015-04-01", {})
    get = call(
        "ros", "GetStack", "2019-09-10", {"RegionId": "cn-hangzhou", "StackId": "fake-stack", "OutputOption": "Enabled"}
    )

    def event(index, operation, body):
        body = {"RequestId": "fake-request-" + str(index), **body}
        return {
            **save("raw-" + str(index), body),
            "call": operation,
            "error": False,
            "request_id": body["RequestId"],
            "started_at": index,
            "completed_at": index + 0.5,
        }

    deletion = {
        "task_id": task,
        "original_job_id": "a" * 32,
        "maximum_delete_calls": 1,
        "original_wire_unchanged": True,
        "events": [
            event(10, identity, {"AccountId": account}),
            event(11, get, status),
            event(
                12,
                call("ros", "ListStackResources", "2019-09-10", {"RegionId": "cn-hangzhou", "StackId": "fake-stack"}),
                {},
            ),
            event(
                13,
                call(
                    "ros",
                    "DeleteStack",
                    "2019-09-10",
                    {"RegionId": "cn-hangzhou", "StackId": "fake-stack", "RetainAllResources": False},
                ),
                {},
            ),
            event(14, get, {"StackId": "fake-stack", "Status": "DELETE_COMPLETE"}),
        ],
    }
    post = {
        "task_id": task,
        "cloud_writes": 0,
        "events": [
            event(20, identity, {"AccountId": account}),
            event(
                21,
                call(
                    "vpc",
                    "DescribeVpcs",
                    "2016-04-28",
                    {"RegionId": "cn-hangzhou", "VpcId": "vpc-fake", "PageNumber": 1, "PageSize": 10},
                ),
                {"TotalCount": 0, "Vpcs": {"Vpc": []}},
            ),
            event(
                22,
                call(
                    "vpc",
                    "DescribeVSwitches",
                    "2016-04-28",
                    {"RegionId": "cn-hangzhou", "VSwitchId": "vsw-fake", "PageNumber": 1, "PageSize": 10},
                ),
                {"TotalCount": 0, "VSwitches": {"VSwitch": []}},
            ),
            event(
                23,
                call(
                    "mse",
                    "ListClusters",
                    "2019-05-31",
                    {
                        "RegionId": "cn-hangzhou",
                        "ClusterAliasName": create["call"]["params"]["StackName"],
                        "PageNum": 1,
                        "PageSize": 10,
                    },
                ),
                {"TotalCount": 0, "Data": [], "Success": True},
            ),
        ],
    }
    resource = {"id": "fake-stack", "account": account, "region": "cn-hangzhou"}
    audit = save(
        "audit",
        {
            "task_id": task,
            "original_job_id": "a" * 32,
            "account": account,
            "region": "cn-hangzhou",
            "original_wire_unchanged": True,
            "pass_added": False,
            "sources": [],
            "findings": {"vpc": {"id": "vpc-fake"}, "vswitch": {"id": "vsw-fake"}},
        },
    )
    cleanup = save(
        "cleanup",
        {
            "ownership_id": "fake-owner",
            "resource": resource,
            "absent": True,
            "failed": False,
            "source_evidence": audit["path"],
            "source_sha256": audit["sha256"],
        },
    )
    external = save(
        "external",
        {
            "kind": CLEANUP_KIND,
            "approved": True,
            "task_id": task,
            "scope": scope,
            "authority": authority,
            "model_wire_unchanged": True,
            "pass_added": False,
            "ownership_id": "fake-owner",
            "owner_cleanup": cleanup,
            "audit": audit,
            "delete_result": save("delete-result", deletion),
            "absence_result": save("post", post),
        },
    )
    with store.transaction() as db:
        db.execute(
            "INSERT INTO jobs(id,task_id,role,purpose,state,turn_id,capability,submission,created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("a" * 32, task, "glm", "execute", "complete", "fake-turn", "fake", "confirmed", 1),
        )
        db.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            ("fake-owner", task, "TF", canonical(resource), "deleted", canonical(cleanup)),
        )
    packet = copy.deepcopy(old)
    expected, before, after = corrected_grants(entries)
    packet["execution_grants"]["TF"] = expected
    request = {
        "request_id": "offline-mse-professional-correction",
        "dimension": "TF",
        "prior_scope": scope,
        "predecessor_proposal": "inputs/proposal.json",
        "predecessor_sha256": digest(store.root / "inputs/proposal.json"),
        "remediation_evidence": audit["path"],
        "remediation_sha256": audit["sha256"],
        "external_cleanup": external,
    }
    packet["additional_source_requests"] = [request]
    cost = json.loads((store.root / "inputs/R0067-cost.json").read_text(encoding="utf-8"))
    cost.update(
        max_hourly_cents=110,
        execution_grants_sha256=hashlib.sha256(canonical(packet["execution_grants"]).encode()).hexdigest(),
    )
    cost["components"][0]["max_hourly_cents"] = 110
    packet["hourly_cost"] = {"max_hourly_cents": 110, "cost_evidence": save("new-cost", cost)}
    operation = json.loads((store.root / "inputs/operation.json").read_text(encoding="utf-8"))
    operation.update(
        kind="ops_additional_sources_review", additional_sources_approved=[request], carry_forward_dimensions={}
    )
    operation["hourly_operation_review"].update(
        execution_sha256=execution_digest(packet), hourly_cost=packet["hourly_cost"]
    )
    operation["temporary_template_corrections"] = {
        request["request_id"]: {
            "kind": KIND,
            "approved": True,
            "task_id": task,
            "human_scope_interpretation": "offline fake human scope",
            "original_admission": {
                "path": "inputs/admission.json",
                "sha256": digest(store.root / "inputs/admission.json"),
            },
            "frozen_plan_sha256": admission["frozen_plan_sha256"],
            "external_cleanup": external,
            "original_authority": authority,
            "original_template_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "corrected_template_sha256": hashlib.sha256(after.encode()).hexdigest(),
        }
    }
    save("corrected-proposal", packet)
    operation["proposal_sha256"] = digest(store.root / "inputs/corrected-proposal.json")
    save("corrected-review", operation)
    return store, packet, operation, authority, delete["id"]


def test_new_professional_authority_preserves_original_delete_zero_and_context(correction):
    store, packet, proof, old_authority, delete = correction
    before = (store.root / "execution-gate.sqlite3").read_bytes()
    Runtime(store).authorize_creation(store.root / "inputs/corrected-proposal.json", "inputs/corrected-review.json", 0)
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        assert admitted(store.root, db, task, packet)["TF"] != old_authority
        db.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)", ("new-owner", packet["task_id"], "TF", "{}", "present", "{}")
        )
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        assert admitted(store.root, db, task, packet)["TF"]
        with pytest.raises(WorkflowError, match="verified cleanup"):
            admitted(store.root, db, task, packet, review=proof)
    assert (store.root / "execution-gate.sqlite3").read_bytes() == before
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as db:
        assert db.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?", (old_authority, delete)
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "damage",
    [
        "quota",
        "template",
        "cost",
        "cost-source",
        "approval",
        "owner",
        "unknown",
        "delete-used",
        "identity",
        "failure",
        "cleanup-raw",
        "clone",
    ],
)
def test_correction_rejects_changed_cost_calls_identity_and_cleanup(correction, damage):
    store, packet, proof, _, delete = correction
    request = packet["additional_source_requests"][0]
    review = proof["temporary_template_corrections"][request["request_id"]]
    if damage == "quota":
        packet["execution_grants"]["TF"][0]["quota"] += 1
    elif damage == "template":
        next(e for e in packet["execution_grants"]["TF"] if e["kind"] == "create")["call"]["params"][
            "TemplateBody"
        ] += " "
    elif damage == "cost":
        proof.pop("hourly_operation_review")
    elif damage == "cost-source":
        json_write(store.root / "inputs/new-cost.json", {})
    elif damage == "approval":
        review["approved"] = False
    elif damage == "owner":
        with store.transaction() as db:
            db.execute("UPDATE owned SET state='present'")
    elif damage in {"unknown", "delete-used", "identity", "failure"}:
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as db:
            if damage == "unknown":
                db.execute("UPDATE attempts SET state='unknown'")
            elif damage == "delete-used":
                db.execute("UPDATE grants SET used=1 WHERE id=?", (delete,))
            elif damage == "identity":
                db.execute("UPDATE identities SET account='foreign'")
            else:
                db.execute("UPDATE attempts SET response='{}' WHERE grant_id='mse-status'")
    elif damage == "cleanup-raw":
        json_write(store.root / "inputs/raw-21.json", {"TotalCount": 1})
    else:
        json_write(
            store.root / "creation-authorizations/clone.json",
            {
                "task_id": packet["task_id"],
                "authorization_evidence": "inputs/corrected-review.json",
                "authorization_sha256": digest(store.root / "inputs/corrected-review.json"),
                "additional_source_requests": [{**request, "request_id": "renamed-copy"}],
            },
        )
    with pytest.raises(WorkflowError):
        _validate_original_operation(store.root, packet, request, proof)


def member_reconciliation_input(correction):
    from scripts.web.e2e.resource_link_workflow.execution_gate import creations
    from scripts.web.e2e.resource_link_workflow.mse_correction import RECONCILIATION_KIND

    store, packet, _, _, _ = correction
    task = packet["task_id"]
    request = packet["additional_source_requests"][0]
    audit = {
        "schema_version": 1,
        "kind": RECONCILIATION_KIND,
        "task_id": task,
        "original_job_id": "a" * 32,
        "model_wire_unchanged": True,
        "pass_added": False,
        "predecessor": {"path": "inputs/proposal.json", "sha256": digest(store.root / "inputs/proposal.json")},
        "cleanup_request": request,
        "original_authority": execution_digest(
            json.loads((store.root / "inputs/proposal.json").read_text(encoding="utf-8"))
        ) + ":TF",
        "original_records": creations(store.root / "execution-gate.sqlite3", ["a" * 32]),
    }
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task,))
    assert len(audit["original_records"]) == 2
    return store, task, audit


def test_failed_mse_member_reconciliation_preserves_wire_and_later_owners(correction):
    from scripts.web.e2e.resource_link_workflow.execution_gate import creations

    store, task, audit = member_reconciliation_input(correction)
    later = "b" * 32
    path = f"jobs/{later}/output/original.json"
    json_write(store.root / path, {"scope": later + ":TF"})
    with store.transaction() as db:
        db.execute(
            "INSERT INTO jobs(id,task_id,role,purpose,state,turn_id,capability,submission,created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (later, task, "glm", "execute", "complete", "later-turn", "fake", "confirmed", 2),
        )
        db.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            (
                "later-owner",
                task,
                "TF",
                canonical({"id": "later-stack"}),
                "present",
                canonical({"path": path, "sha256": digest(store.root / path)}),
            ),
        )
    json_write(store.root / "inputs/member-reconciliation.json", audit)
    before = digest(store.root / "execution-gate.sqlite3")
    result = store.reconcile_rejected_creation(task, "inputs/member-reconciliation.json")
    expected = {audit["original_records"][1]["attempt_id"]}
    assert set(result["reconciled_attempts"]) == expected
    assert result["wire_unchanged"] and result["new_pass"] == 0
    assert store.reconcile_rejected_creation(task, "inputs/member-reconciliation.json")["already_recorded"]
    assert digest(store.root / "execution-gate.sqlite3") == before
    assert creations(store.root / "execution-gate.sqlite3", ["a" * 32]) == audit["original_records"]
    with store.transaction() as db:
        assert store.reconciled_creations(db, task) == expected
        assert db.execute("SELECT state FROM owned WHERE id='later-owner'").fetchone()[0] == "present"
        assert db.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "damage",
    [
        "task",
        "scope",
        "authority",
        "records",
        "parent",
        "old-owner",
        "unbound-owner",
        "raw",
        "live-job",
    ],
)
def test_failed_mse_member_reconciliation_refuses_unproved_cleanup(correction, damage):
    store, task, audit = member_reconciliation_input(correction)
    if damage == "task":
        audit["task_id"] = "foreign:R0067"
    elif damage == "scope":
        audit["cleanup_request"]["prior_scope"] = "b" * 32 + ":TF"
    elif damage == "authority":
        audit["original_authority"] = "foreign:TF"
    elif damage == "records":
        audit["original_records"][1]["state"] = "succeeded"
    elif damage == "parent":
        audit["original_records"][1]["parent_attempt_id"] = "unrelated"
    elif damage == "raw":
        json_write(store.root / "inputs/raw-21.json", {"TotalCount": 1})
    else:
        with store.transaction() as db:
            if damage == "old-owner":
                db.execute("UPDATE owned SET state='present'")
            elif damage == "live-job":
                db.execute("UPDATE jobs SET state='running'")
            else:
                db.execute(
                    "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                    (
                        "unbound",
                        task,
                        "TF",
                        canonical({"id": "other"}),
                        "present",
                        "{}",
                    ),
                )
    json_write(store.root / "inputs/member-reconciliation.json", audit)
    with pytest.raises(WorkflowError):
        store.reconcile_rejected_creation(task, "inputs/member-reconciliation.json")
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='ops_creation_reconciled'").fetchone()[0] == 0
