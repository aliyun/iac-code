"""First hourly hosted-source admission: offline permission, templates and costs."""

import copy
import hashlib
import json

import pytest

from scripts.web.e2e.resource_link_workflow.creation import execution_digest
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.hourly_budget import register
from scripts.web.e2e.resource_link_workflow.plans import expected_scenes
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow import resource
from tests.web.test_resource_link_workflow_hourly_budget import operation_fixture
from tests.web.test_resource_link_workflow_read_policy import mse_entries


@pytest.fixture
def reviewed(tmp_path):
    store = Store.initialize(tmp_path / "state")
    spec = resource(67)
    spec["key"] = "ALIYUN::MSE::Cluster"
    spec["dimensions"]["QUERY"].update(support="not_applicable", contracts=[], official_basis="offline fixture")
    spec["dimensions"]["TF"].update(
        support="supported",
        contracts=[
            {
                "id": "R0067-TF-1",
                "source_type": "ALIYUN::MSE::Cluster",
                "historical": {"iframe": "fail", "open": "pass"},
            }
        ],
    )
    plan = {"schema_version": 1, "plan_id": "hourly-sample-offline", "resources": [spec]}
    store.import_plan(plan)
    # Install a synthetic reviewed scope directly; import-lineage contracts are
    # covered separately. This fixture never reads a user's real plan or cloud.
    spec["retest_scope"] = {"kind": "failed_navigation_managed", "scene_ids": ["R0067-TF-1-iframe"]}
    with store.transaction() as db:
        db.execute("UPDATE tasks SET spec=?", (dumps(spec),))
        db.execute("UPDATE runs SET plan=?", (dumps(plan),))
    task_id = plan["plan_id"] + ":R0067"
    frozen_sha = hashlib.sha256(dumps(plan).encode()).hexdigest()
    human = {
        "schema_version": 1,
        "kind": "human_hourly_fee_authorization",
        "approved": True,
        "plan_id": plan["plan_id"],
        "currency": "CNY",
        "resource_ids": ["R0067"],
        "scene_ids": ["R0067-TF-1-iframe"],
        "max_concurrent_hourly_cents": 1000,
        "total_budget_cents": None,
        "verbatim_user_message": "OFFLINE FAKE AUTHORIZATION",
    }
    json_write(store.root / "inputs/human.json", human)
    allowance = {
        "schema_version": 1,
        "kind": "ops_run_hourly_fee_review",
        "approved": True,
        "plan_id": plan["plan_id"],
        "frozen_plan_sha256": frozen_sha,
        "currency": "CNY",
        "max_concurrent_hourly_cents": 1000,
        "resource_ids": ["R0067"],
        "scene_ids": ["R0067-TF-1-iframe"],
        "human_authorization": {"path": "inputs/human.json", "sha256": digest(store.root / "inputs/human.json")},
    }
    json_write(store.root / "inputs/review.json", allowance)
    register(store, plan["plan_id"], "inputs/review.json")
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    value, cost, operation = operation_fixture(store, task, 50)
    value.update(
        execution_grants={"TF": mse_entries()},
        requests={"TF": "Create approved hosted sample"},
        cleanup_requests={"TF": "Delete only original stack and confirm absence"},
        cleanup_recipe="Original response-bound stack, cluster, VPC and switch checks",
    )
    for scene in value["scenes"].values():
        scene["account"] = "1234567890123456"
    cost.update(
        account="1234567890123456",
        execution_grants_sha256=hashlib.sha256(canonical(value["execution_grants"]).encode()).hexdigest(),
    )
    json_write(store.root / "inputs/R0067-cost.json", cost)
    value["hourly_cost"]["cost_evidence"]["sha256"] = digest(store.root / "inputs/R0067-cost.json")
    proposal = "inputs/proposal.json"
    json_write(store.root / proposal, value)
    operation["hourly_operation_review"].update(
        execution_sha256=execution_digest(value), hourly_cost=value["hourly_cost"]
    )
    operation["proposal_sha256"] = digest(store.root / proposal)
    json_write(store.root / "inputs/operation.json", operation)
    body = next(g for g in mse_entries() if g["kind"] == "create")["call"]["params"]["TemplateBody"]
    source = {
        "kind": "ops_hourly_managed_tf_source_review",
        "approved": True,
        "task_id": task_id,
        "account": "1234567890123456",
        "execution_sha256": execution_digest(value),
        "sample_binding": {
            "template_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "region": "cn-hangzhou",
            "stack_name": "offline-mse",
            "resources": [
                {"type": t, "name": n}
                for t, n in (
                    ("alicloud_vpc", "sample_vpc"),
                    ("alicloud_vswitch", "sample_vswitch"),
                    ("alicloud_mse_cluster", "sample"),
                )
            ],
        },
        "full_inline_template_reviewed": True,
        "implicit_dependencies_reviewed": True,
        "no_existing_resource_mutation": True,
        "all_new_resources_response_bound_cleanup": True,
        "estimate_basis": "offline fake source",
        "limitations": "not cloud evidence",
        "official_sources": cost["components"][0]["official_sources"],
    }
    json_write(store.root / "inputs/source-review.json", source)
    proof = {
        "schema_version": 1,
        "kind": "ops_hourly_managed_tf_sample_admission",
        "approved": True,
        "task_id": task_id,
        "plan_id": plan["plan_id"],
        "frozen_plan_sha256": frozen_sha,
        "execution_sha256": execution_digest(value),
    }
    for field, name in (
        ("proposal", proposal),
        ("source_review", "inputs/source-review.json"),
        ("operation_review", "inputs/operation.json"),
    ):
        proof[field] = {"path": name, "sha256": digest(store.root / name)}
    json_write(store.root / "inputs/admission.json", proof)
    return store, value, proof


def test_hourly_source_admission_is_immutable_not_cloud_authority_and_retains_old_pass(reviewed):
    store, packet, _ = reviewed
    with store.transaction() as db:
        before = {
            t: list(map(tuple, db.execute(f"SELECT * FROM {t}")))
            for t in ("runs", "tasks", "jobs", "owned", "verdicts", "budget_reservations")
        }
        task = db.execute("SELECT * FROM tasks").fetchone()
        assert store.readonly_dimensions(db, task, packet) == {"TF"}
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")["no_cloud_call"]
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")["already_recorded"]
    assert not list((store.root / "creation-authorizations").glob("*.json"))
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        assert store.readonly_dimensions(db, task, packet) == set()
        assert admitted(store.root, db, task, packet) == {"TF": execution_digest(packet) + ":TF"}
        for table, data in before.items():
            assert list(map(tuple, db.execute(f"SELECT * FROM {table}"))) == data
        assert expected_scenes(json.loads(task["spec"])) == expected_scenes(json.loads(before["tasks"][0][3]))
        changed = copy.deepcopy(packet)
        changed["hourly_cost"]["max_hourly_cents"] = 49
        with pytest.raises(WorkflowError, match="hourly cost"):
            admitted(store.root, db, task, changed)


def test_hourly_admitted_source_can_receive_exact_runtime_operation_authority(reviewed):
    store, packet, _ = reviewed
    store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")
    result = Runtime(store).authorize_creation(
        store.root / "inputs/proposal.json",
        "inputs/operation.json",
        0,
    )
    assert result["execution_reviewed"] is True
    assert result["cloud_write_performed"] is False
    authorizations = list((store.root / "creation-authorizations").glob("*.json"))
    assert len(authorizations) == 1
    authorization = json.loads(authorizations[0].read_text(encoding="utf-8"))
    assert authorization["dimension_authorities"] == {"TF": execution_digest(packet) + ":TF"}
    assert authorization["proposal_sha256"] == digest(store.root / "inputs/proposal.json")
    assert authorization["account"] == "1234567890123456"


@pytest.mark.parametrize(
    "damage",
    [
        "human",
        "proposal",
        "source",
        "operation",
        "approval",
        "frozen",
        "after_worker",
        "active",
        "pause",
        "cost",
        "scene",
        "counter",
    ],
)
def test_hourly_sample_rejects_drift_or_retroactive_create_allowance(reviewed, damage):
    store, packet, proof = reviewed
    if damage in {"proposal", "human", "source", "operation", "cost"}:
        path = {
            "proposal": "inputs/proposal.json",
            "human": "inputs/human.json",
            "source": "inputs/source-review.json",
            "operation": "inputs/operation.json",
            "cost": "inputs/R0067-cost.json",
        }[damage]
        value = json.loads((store.root / path).read_text(encoding="utf-8"))
        value["tampered"] = True
        json_write(store.root / path, value)
    elif damage in {"approval", "frozen", "scene"}:
        if damage == "approval":
            proof["approved"] = False
        elif damage == "frozen":
            proof["frozen_plan_sha256"] = "wrong"
        else:
            packet["scenes"]["R0067-TF-1-open"] = packet["scenes"]["R0067-TF-1-iframe"]
            json_write(store.root / "inputs/proposal.json", packet)
            proof["proposal"]["sha256"] = digest(store.root / "inputs/proposal.json")
        json_write(store.root / "inputs/admission.json", proof)
    elif damage == "pause":
        store.pause(False)
    else:
        with store.transaction() as db:
            if damage in {"after_worker", "counter"}:
                db.execute("UPDATE tasks SET attempt=1")
            else:
                db.execute("INSERT INTO actions VALUES('pending',NULL,'prepare','issued',1)")
    with pytest.raises(WorkflowError):
        store.authorize_temporary_samples(packet["task_id"], "inputs/admission.json")
