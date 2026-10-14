"""Offline explicit, immutable temporary samples; no cloud or model calls."""

import copy
import hashlib
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.creation import authorization_path, execution_digest, validate_execution
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow import resource


@pytest.fixture
def sample(tmp_path):
    store = Store.initialize(tmp_path / "state")
    spec = resource(43)
    spec["key"] = "ALIYUN::OTS::Table"
    plan = {"schema_version": 1, "plan_id": "sample-fixture", "resources": [spec]}
    store.import_plan(plan, budget_cents=100)
    frozen_plan_sha256 = hashlib.sha256(dumps(plan).encode()).hexdigest()
    spec["retest_scope"] = {"kind": "failed_navigation_managed", "scene_ids": []}
    for dim in ("ROS", "TF"):
        spec["dimensions"][dim]["contracts"] = [
            {"id": f"R0043-{dim}-1", "source_type": "offline", "historical": {"iframe": "fail", "open": "fail"}}
        ]
        spec["retest_scope"]["scene_ids"].append(f"R0043-{dim}-1-iframe")
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET spec=?", (dumps(spec),))
    human = {
        "kind": "human_fee_authorization",
        "approved": True,
        "plan_id": plan["plan_id"],
        "task_id": "sample-fixture:R0043",
        "max_budget_cents": 100,
        "verbatim_user_message": "FAKE OFFLINE AUTHORIZATION ONLY",
        "scope": dict.fromkeys(
            [
                "existing_verified_accessible_parent_only",
                "delete_created_tables_after_test",
                "no_data_rows",
                "no_indexes",
                "no_new_instance",
                "no_existing_network_or_iam_changes",
            ],
            True,
        ),
    }
    human["scope"].update(native_ros_empty_tables_max=1, ros_managed_terraform_empty_tables_max=1)
    packet = {
        "task_id": human["task_id"],
        "budget_cents": 100,
        "execution_grants": {},
        "scenes": {"offline": {"account": "1234567890123456"}},
    }
    bindings = {}
    for dim in ("ROS", "TF"):
        name = "offline_" + dim.lower()
        template = {"ROSTemplateFormatVersion": "2015-09-01"}
        if dim == "ROS":
            template["Resources"] = {
                "Table": {
                    "Type": "ALIYUN::OTS::Table",
                    "Properties": {
                        "InstanceName": "offline-parent",
                        "TableName": name,
                        "PrimaryKey": [{"Name": "pk", "Type": "STRING"}],
                        "ReservedThroughput": {"Read": 0, "Write": 0},
                    },
                }
            }
        else:
            template.update(
                Transform="Aliyun::Terraform-v1.5",
                Workspace={
                    "main.tf": 'resource "alicloud_ots_table" "table" {\n instance_name = "offline-parent"\n'
                    f' table_name = "{name}"\n}}'
                },
            )
        body = json.dumps(template)
        create = {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": {
                "product": "ros",
                "action": "CreateStack",
                "region_id": "cn-hangzhou",
                "params": {"RegionId": "cn-hangzhou", "StackName": "offline-" + dim.lower(), "TemplateBody": body},
            },
        }
        delete = {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "parent": "create",
            "response_path": ["StackId"],
            "target_param": "StackId",
            "call": {
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "$created_id"},
            },
        }
        read = {
            "id": "absence",
            "kind": "read",
            "quota": 1,
            "parent": "create",
            "response_path": ["StackId"],
            "target_param": "StackId",
            "read_policy": {
                "phase": "cleanup",
                "min_interval_seconds": 30,
                "required_cleanup": True,
                "after_delete": "create",
            },
            "call": {
                "product": "ros",
                "action": "GetStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "$created_id"},
            },
        }
        identity = {
            "id": "identity",
            "kind": "identity",
            "quota": 1,
            "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "cn-hangzhou", "params": {}},
        }
        packet["execution_grants"][dim] = [identity, create, delete, read]
        bindings[dim] = {
            "instance_name": "offline-parent",
            "table_name": name,
            "region": "cn-hangzhou",
            "empty_table_only": True,
            "template_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
    json_write(store.root / "inputs/human.json", human)
    json_write(store.root / "inputs/proposal.json", packet)
    review = {
        "schema_version": 1,
        "kind": "ops_temporary_sample_review",
        "approved": True,
        "task_id": human["task_id"],
        "plan_id": plan["plan_id"],
        "frozen_plan_sha256": frozen_plan_sha256,
        "human_authorization": "inputs/human.json",
        "human_authorization_sha256": digest(store.root / "inputs/human.json"),
        "proposal": "inputs/proposal.json",
        "proposal_sha256": digest(store.root / "inputs/proposal.json"),
        "execution_sha256": execution_digest(packet),
        "sample_bindings": bindings,
    }
    json_write(store.root / "inputs/review.json", review)
    return store, packet, review


def test_admission_is_immutable_idempotent_and_not_operation_authority(sample):
    store, packet, _ = sample
    with store.transaction() as c:
        before = {
            table: [tuple(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in ("tasks", "runs", "verdicts", "jobs", "owned", "budget_reservations")
        }
        task = c.execute("SELECT * FROM tasks").fetchone()
        assert store.readonly_dimensions(c, task, packet) == {"ROS", "TF"}
    result = store.authorize_temporary_samples(packet["task_id"], "inputs/review.json")
    assert result["operation_authorization_required"] and result["no_cloud_call"]
    assert not list((store.root / "creation-authorizations").glob("*.json"))
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/review.json")["already_recorded"]
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        assert store.readonly_dimensions(c, task, packet) == set()
        assert admitted(store.root, c, task, packet) == {
            dim: execution_digest(packet) + ":" + dim for dim in ("ROS", "TF")
        }
        for table, rows in before.items():
            assert [tuple(row) for row in c.execute(f"SELECT * FROM {table}")] == rows
        changed = copy.deepcopy(packet)
        changed["execution_grants"]["ROS"][1]["quota"] = 2
        assert store.readonly_dimensions(c, task, changed) == {"ROS"}
        with pytest.raises(WorkflowError):
            validate_execution(changed, {"ROS", "TF"}, readonly_dimensions=store.readonly_dimensions(c, task, changed))


@pytest.mark.parametrize("damage", ["human", "proposal", "plan", "approval", "scope", "budget"])
def test_review_or_original_authority_drift_is_rejected(sample, damage):
    store, packet, review = sample
    if damage in {"human", "scope"}:
        path = store.root / "inputs/human.json"
        human = json.loads(path.read_text(encoding="utf-8"))
        human["approved"] = damage != "human"
        human["scope"]["no_new_instance"] = False
        json_write(path, human)
        review["human_authorization_sha256"] = digest(path)
    elif damage == "proposal":
        review["proposal_sha256"] = "wrong"
    elif damage == "plan":
        review["frozen_plan_sha256"] = "wrong"
    elif damage == "approval":
        review["approved"] = False
    else:
        packet["budget_cents"] = 101
        json_write(store.root / "inputs/proposal.json", packet)
        review["proposal_sha256"] = digest(store.root / "inputs/proposal.json")
    json_write(store.root / "inputs/review.json", review)
    with pytest.raises(WorkflowError):
        store.authorize_temporary_samples(packet["task_id"], "inputs/review.json")


def test_policy_cannot_be_replaced_to_refill_and_requires_drained_pause(sample):
    store, packet, review = sample
    store.pause(False)
    with pytest.raises(WorkflowError, match="pause"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/review.json")
    store.pause(True)
    store.authorize_temporary_samples(packet["task_id"], "inputs/review.json")
    review["other"] = "changed"
    json_write(store.root / "inputs/review.json", review)
    with pytest.raises(WorkflowError, match="replaced"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/review.json")


def test_saved_policy_is_revalidated_at_every_boundary(sample):
    store, packet, _ = sample
    store.authorize_temporary_samples(packet["task_id"], "inputs/review.json")
    json_write(store.root / "inputs/human.json", {})
    with store.transaction() as c, pytest.raises(WorkflowError):
        store.readonly_dimensions(c, c.execute("SELECT * FROM tasks").fetchone(), packet)


@pytest.fixture
def continued_sample(sample):
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, initialize, install

    store, old, _ = sample
    store.authorize_temporary_samples(old["task_id"], "inputs/review.json")
    old["cleanup_recipe"] = {"TF": "response-bound delete and exact stack absence"}
    # The immutable source has already been reviewed; cleanup text does not
    # change its execution digest or any inline template.
    original = copy.deepcopy(old)
    json_write(store.root / "inputs/original-execution.json", original)
    json_write(store.root / "inputs/original-operation-review.json", {"approved": True})
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
        "authorization_evidence": "inputs/original-operation-review.json",
        "authorization_sha256": digest(store.root / "inputs/original-operation-review.json"),
    }
    json_write(authorization_path(store.root, original), approval)
    scope = "a" * 32 + ":TF"
    authority = execution_digest(original) + ":TF"
    initialize(store.root / "execution-gate.sqlite3")
    install(
        store.root / "execution-gate.sqlite3",
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
            ("a" * 32, old["task_id"], "glm", "execute", "complete", "turn", "offline", "confirmed", 1),
        )
        c.execute("INSERT INTO budget_reservations VALUES(?,?)", (old["task_id"], 100))
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as c:
        c.execute("UPDATE grants SET used=1 WHERE id='identity'")
        c.execute("UPDATE authority_limits SET used=1 WHERE id='identity'")
        c.execute(
            "INSERT INTO attempts(id,scope,grant_id,state,response) VALUES(?,?,?,?,?)",
            ("original-identity", scope, "identity", "succeeded", canonical({"AccountId": approval["account"]})),
        )
    new = copy.deepcopy(original)
    new["execution_grants"] = {"TF": new["execution_grants"]["TF"]}
    new["execution_grants"]["TF"][0]["quota"] = 2
    json_write(store.root / "inputs/remediation.json", {"diagnosis": "offline serialization correction"})
    continuation = {
        "request_id": "offline-sample-read-continuation",
        "prior_scope": scope,
        "grant_ids": ["identity"],
        "remediation_evidence": "inputs/remediation.json",
        "remediation_sha256": digest(store.root / "inputs/remediation.json"),
    }
    proof = {
        "kind": "ops_read_continuation_review",
        "approved": True,
        "proposal_execution_sha256": execution_digest(new),
        "read_continuation_approved": continuation,
        "carry_forward_dimensions": {
            "TF": {
                "proposal": "inputs/original-execution.json",
                "proposal_sha256": digest(store.root / "inputs/original-execution.json"),
                "execution_sha256": execution_digest(original),
                "read_continuation": continuation,
            }
        },
    }
    json_write(store.root / "inputs/official.json", {"offline": "fake fee basis"})
    cost = {
        "kind": "ops_cumulative_fee_review",
        "approved": True,
        "task_id": old["task_id"],
        "account": approval["account"],
        "execution_sha256": execution_digest(new),
        "additional_operations": [],
        "human_authorization": "inputs/human.json",
        "human_sha256": digest(store.root / "inputs/human.json"),
        "budget_cents": 100,
        "includes_previous_operations": True,
        "estimate_basis": "offline estimate only",
        "limitations": "not billing or cloud cap",
        "official_sources": [{"path": "inputs/official.json", "sha256": digest(store.root / "inputs/official.json")}],
    }
    json_write(store.root / "inputs/cost.json", cost)
    proof["paid_operation_review"] = {
        "human_authorization": cost["human_authorization"],
        "human_sha256": cost["human_sha256"],
        "cost_evidence": "inputs/cost.json",
        "cost_sha256": digest(store.root / "inputs/cost.json"),
    }
    proof["budget_reservation_reuse"] = {
        "task_id": old["task_id"],
        "reserved_cents": 100,
        "cost_evidence": "inputs/cost.json",
        "cost_sha256": digest(store.root / "inputs/cost.json"),
    }
    json_write(store.root / "inputs/continued.json", new)
    proof["proposal_sha256"] = digest(store.root / "inputs/continued.json")
    json_write(store.root / "inputs/continued-review.json", proof)
    return store, new, proof, authority


def test_sample_read_continuation_requires_ops_and_preserves_original_authority(continued_sample):
    store, packet, proof, authority = continued_sample
    before = (store.root / "execution-gate.sqlite3").read_bytes()
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        assert store.readonly_dimensions(c, task, packet) == {"ROS", "TF"}
        assert admitted(store.root, c, task, packet, review=proof) == {"TF": authority}
        assert store.readonly_dimensions(c, task, packet, review=proof) == {"ROS"}
    assert (store.root / "execution-gate.sqlite3").read_bytes() == before


def test_authorize_and_active_successor_reuse_original_sample_and_budget(continued_sample, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.additional_source import dispatch_reservation_increment
    from scripts.web.e2e.resource_link_workflow.execution_gate import canonical, install
    from scripts.web.e2e.resource_link_workflow.runtime import Runtime

    store, packet, _, authority = continued_sample
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.selection.selected_scenes", lambda *_: [{"dimension": "TF"}]
    )
    assert (
        Runtime(store).authorize_creation(
            store.root / "inputs/continued.json",
            "inputs/continued-review.json",
            100,
        )["cloud_write_performed"]
        is False
    )
    assert dispatch_reservation_increment(store.root, packet) == 0
    install(
        store.root / "execution-gate.sqlite3",
        "0",
        "b" * 32 + ":TF",
        "offline~new",
        "1234567890123456",
        None,
        packet["execution_grants"]["TF"],
        authority=authority,
    )
    with store.transaction() as c:
        c.execute(
            "INSERT INTO jobs(id,task_id,role,purpose,state,turn_id,capability,submission,created) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("b" * 32, packet["task_id"], "glm", "execute", "running", "new-turn", "offline", "confirmed", 2),
        )
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as c:
        c.execute("UPDATE grants SET used=1 WHERE scope=? AND id='identity'", ("b" * 32 + ":TF",))
        c.execute("UPDATE authority_limits SET used=2 WHERE id='identity'")
        c.execute(
            "INSERT INTO attempts(id,scope,grant_id,state,response) VALUES(?,?,?,?,?)",
            ("new-identity", "b" * 32 + ":TF", "identity", "succeeded", canonical({"AccountId": "1234567890123456"})),
        )
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        assert admitted(store.root, c, task, packet) == {"TF": authority}
        assert c.execute("SELECT amount FROM budget_reservations").fetchone()[0] == 100
    assert dispatch_reservation_increment(store.root, packet) == 0


@pytest.mark.parametrize(
    "damage",
    [
        "template",
        "write-quota",
        "read-call",
        "identity-quota",
        "approval",
        "authority",
        "human",
        "cost",
        "reservation",
        "unknown",
        "used-write",
    ],
)
def test_sample_continuation_cannot_expand_or_bypass_review(continued_sample, damage):
    store, packet, proof, _ = continued_sample
    if damage == "template":
        packet["execution_grants"]["TF"][1]["call"]["params"]["TemplateBody"] = "different"
    elif damage == "write-quota":
        packet["execution_grants"]["TF"][1]["quota"] = 2
    elif damage == "read-call":
        packet["execution_grants"]["TF"][0]["call"]["region_id"] = "cn-beijing"
    elif damage == "identity-quota":
        packet["execution_grants"]["TF"][0]["quota"] = 3
    elif damage == "approval":
        proof["approved"] = False
    elif damage == "authority":
        original = json.loads((store.root / "inputs/original-execution.json").read_text(encoding="utf-8"))
        approval_path = authorization_path(store.root, original)
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        approval["dimension_authorities"] = {"TF": "wrong:TF"}
        json_write(approval_path, approval)
    elif damage in {"human", "cost"}:
        json_write(store.root / f"inputs/{damage}.json", {})
    elif damage == "reservation":
        with store.transaction() as c:
            c.execute("UPDATE budget_reservations SET amount=99")
    else:
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as c:
            if damage == "unknown":
                c.execute("UPDATE attempts SET state='unknown'")
            else:
                c.execute("UPDATE grants SET used=1 WHERE id='create'")
                c.execute("UPDATE authority_limits SET used=1 WHERE id='create'")
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        try:
            result = admitted(store.root, c, task, packet, review=proof)
        except WorkflowError:
            pass
        else:
            assert "TF" not in result


@pytest.mark.parametrize("damage", ["review-sha", "approval", "authority", "human", "cost", "reservation"])
def test_installed_continuation_revalidates_evidence_and_reserve(continued_sample, monkeypatch, damage):
    from scripts.web.e2e.resource_link_workflow.additional_source import dispatch_reservation_increment
    from scripts.web.e2e.resource_link_workflow.runtime import Runtime

    store, packet, _, _ = continued_sample
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.selection.selected_scenes", lambda *_: [{"dimension": "TF"}]
    )
    Runtime(store).authorize_creation(store.root / "inputs/continued.json", "inputs/continued-review.json", 100)
    if damage in {"review-sha", "human", "cost"}:
        name = "continued-review" if damage == "review-sha" else damage
        json_write(store.root / f"inputs/{name}.json", {})
    elif damage == "reservation":
        with store.transaction() as c:
            c.execute("UPDATE budget_reservations SET amount=99")
    else:
        path = authorization_path(store.root, packet)
        approval = json.loads(path.read_text(encoding="utf-8"))
        if damage == "approval":
            approval["authorized"] = False
        else:
            approval["dimension_authorities"]["TF"] = "wrong:TF"
        json_write(path, approval)
    with pytest.raises(WorkflowError):
        dispatch_reservation_increment(store.root, packet)
    with store.transaction() as c, pytest.raises(WorkflowError):
        admitted(store.root, c, c.execute("SELECT * FROM tasks").fetchone(), packet)


def test_continuation_wrong_final_proposal_sha_cannot_authorize(continued_sample, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.runtime import Runtime

    store, packet, proof, _ = continued_sample
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.selection.selected_scenes", lambda *_: [{"dimension": "TF"}]
    )
    proof["proposal_sha256"] = "0" * 64
    json_write(store.root / "inputs/continued-review.json", proof)
    with pytest.raises(WorkflowError, match="final exact proposal SHA"):
        Runtime(store).authorize_creation(store.root / "inputs/continued.json", "inputs/continued-review.json", 100)
    assert not authorization_path(store.root, packet).exists()
