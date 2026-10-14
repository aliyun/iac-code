"""Human-approved extra generation is immutable and never refills old grants."""

import copy
import hashlib
import json

import pytest

from scripts.web.e2e.resource_link_workflow.creation import execution_digest
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow_temporary_samples import sample  # noqa: F401


@pytest.fixture
def additional(sample, monkeypatch):  # noqa: F811
    store, initial, original_review = sample
    store.authorize_temporary_samples(initial["task_id"], "inputs/review.json")
    packet = copy.deepcopy(initial)
    packet["execution_grants"] = {"TF": packet["execution_grants"]["TF"]}
    packet["scenes"] = {"R0043-TF-1-iframe": {"account": "1234567890123456"}}
    old = copy.deepcopy(packet)
    json_write(store.root / "inputs/predecessor.json", old)
    request = {
        "request_id": "offline-additional-table",
        "dimension": "TF",
        "prior_scope": "a" * 32 + ":TF",
        "predecessor_proposal": "inputs/predecessor.json",
        "predecessor_sha256": digest(store.root / "inputs/predecessor.json"),
    }
    packet["additional_source_requests"] = [request]
    json_write(store.root / "inputs/additional-proposal.json", packet)
    human = json.loads((store.root / "inputs/human.json").read_text(encoding="utf-8"))
    human["scope"].pop("native_ros_empty_tables_max")
    human["scope"].pop("ros_managed_terraform_empty_tables_max")
    human["scope"].update(
        additional_ros_managed_terraform_empty_tables_max=1, includes_previous_operations_in_budget=True
    )
    json_write(store.root / "inputs/additional-human.json", human)
    json_write(store.root / "inputs/official.json", {"offline": True})
    cost = {
        "kind": "ops_cumulative_fee_review",
        "approved": True,
        "task_id": packet["task_id"],
        "account": "1234567890123456",
        "execution_sha256": execution_digest(packet),
        "additional_operations": [request],
        "human_authorization": "inputs/additional-human.json",
        "human_sha256": digest(store.root / "inputs/additional-human.json"),
        "budget_cents": 100,
        "includes_previous_operations": True,
        "estimate_basis": "OFFLINE ONLY",
        "limitations": ["offline"],
        "official_sources": [{"path": "inputs/official.json", "sha256": digest(store.root / "inputs/official.json")}],
    }
    json_write(store.root / "inputs/cost.json", cost)
    operation = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "proposal_sha256": digest(store.root / "inputs/additional-proposal.json"),
        "additional_sources_approved": [request],
        "carry_forward_dimensions": {},
        "paid_operation_review": {
            "human_authorization": "inputs/additional-human.json",
            "human_sha256": cost["human_sha256"],
            "cost_evidence": "inputs/cost.json",
            "cost_sha256": digest(store.root / "inputs/cost.json"),
        },
    }
    json_write(store.root / "inputs/operation.json", operation)
    review = {
        "schema_version": 1,
        "kind": "ops_additional_empty_table_admission",
        "approved": True,
        "task_id": packet["task_id"],
        "plan_id": "sample-fixture",
        "frozen_plan_sha256": original_review["frozen_plan_sha256"],
        "human_authorization": "inputs/additional-human.json",
        "human_authorization_sha256": cost["human_sha256"],
        "proposal": "inputs/additional-proposal.json",
        "proposal_sha256": digest(store.root / "inputs/additional-proposal.json"),
        "operation_review": "inputs/operation.json",
        "operation_review_sha256": digest(store.root / "inputs/operation.json"),
        "execution_sha256": execution_digest(packet),
        "sample_binding": original_review["sample_bindings"]["TF"],
    }
    json_write(store.root / "inputs/additional-review.json", review)
    with store.transaction() as c:
        spec = json.loads(c.execute("SELECT spec FROM tasks").fetchone()[0])
        spec["dimensions"]["TF"]["support"] = "supported"
        spec["dimensions"]["ROS"]["support"] = "supported"
        spec["dimensions"]["ROS"]["contracts"][0]["historical"]["iframe"] = "pass"
        spec["dimensions"]["ROS"]["contracts"][0]["historical"]["open"] = "pass"
        c.execute("UPDATE tasks SET spec=?,attempt=4,state='blocked'", (json.dumps(spec),))
    # Provenance validator has its own real SQLite/wire suite. Here ensure this
    # new admission invokes it, does not replace it, and propagates every refusal.
    checked = []
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.additional_source.validate_additional_source",
        lambda *args: checked.append(args[1:]),
    )
    return store, packet, review, checked


def test_additional_admission_preserves_original_authorities_and_state(additional):
    store, packet, _, checked = additional
    with store.transaction() as c:
        before = {
            table: [tuple(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in ("tasks", "runs", "owned", "jobs", "verdicts", "budget_reservations")
        }
        original = c.execute("SELECT value FROM meta WHERE key LIKE 'temporary_samples:%'").fetchone()[0]
    result = store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")
    assert result["operation_authorization_required"] and len(checked) == 1
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")["already_recorded"]
    assert len(checked) == 1
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        request = packet["additional_source_requests"][0]
        authority = hashlib.sha256(canonical({"task_id": packet["task_id"], "operation": request}).encode()).hexdigest()
        assert admitted(store.root, c, task, packet) == {"TF": authority + ":TF"}
        assert c.execute("SELECT value FROM meta WHERE key LIKE 'temporary_samples:%'").fetchone()[0] == original
        for table, rows in before.items():
            assert [tuple(row) for row in c.execute(f"SELECT * FROM {table}")] == rows
    assert not list((store.root / "creation-authorizations").glob("*.json"))


@pytest.mark.parametrize("damage", ["permission", "scope", "cost", "proposal", "operation", "binding", "plan"])
def test_rejects_authority_drift(additional, damage):
    store, packet, review, _ = additional
    if damage in {"permission", "scope"}:
        path = store.root / "inputs/additional-human.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["approved"] = damage != "permission"
        value["scope"]["additional_ros_managed_terraform_empty_tables_max"] = 2
        json_write(path, value)
        review["human_authorization_sha256"] = digest(path)
    elif damage in {"cost", "operation", "proposal"}:
        path = (
            store.root
            / "inputs"
            / {"cost": "cost.json", "operation": "operation.json", "proposal": "additional-proposal.json"}[damage]
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["changed"] = True
        json_write(path, value)
    elif damage == "binding":
        review["sample_binding"]["table_name"] = "other_parent_or_table"
    else:
        review["frozen_plan_sha256"] = "changed"
    json_write(store.root / "inputs/additional-review.json", review)
    with pytest.raises(WorkflowError):
        store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")


def test_cannot_replace_permission_or_change_packet_after_admission(additional):
    store, packet, review, _ = additional
    store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")
    with store.transaction() as c:
        task = c.execute("SELECT * FROM tasks").fetchone()
        changed = copy.deepcopy(packet)
        changed["requests"] = {"TF": "different prompt"}
        with pytest.raises(WorkflowError, match="full reviewed proposal"):
            admitted(store.root, c, task, changed)
    review["unused_note"] = "a different review cannot duplicate the one human permission"
    json_write(store.root / "inputs/additional-review.json", review)
    with pytest.raises(WorkflowError, match="duplicate"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")


def test_real_original_provenance_refusal_is_not_bypassed(additional, monkeypatch):
    store, packet, _, _ = additional

    def refuse(*args):
        raise WorkflowError("original owner not deleted or unknown original result")

    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.additional_source.validate_additional_source", refuse)
    with pytest.raises(WorkflowError, match="original owner"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")
    with store.transaction() as c:
        assert not c.execute("SELECT 1 FROM meta WHERE key LIKE 'additional_empty_table:%'").fetchone()


def test_copying_human_material_cannot_multiply_its_one_operation(additional):
    store, packet, review, _ = additional
    store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")
    human_path = store.root / "inputs/additional-human.json"
    human = json.loads(human_path.read_text(encoding="utf-8"))
    human["comment"] = "A copied document is not a new human approval"
    json_write(human_path, human)
    review["human_authorization_sha256"] = digest(human_path)
    cost_path = store.root / "inputs/cost.json"
    cost = json.loads(cost_path.read_text(encoding="utf-8"))
    cost["human_sha256"] = digest(human_path)
    json_write(cost_path, cost)
    operation_path = store.root / "inputs/operation.json"
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    operation["paid_operation_review"].update(human_sha256=digest(human_path), cost_sha256=digest(cost_path))
    json_write(operation_path, operation)
    review["operation_review_sha256"] = digest(operation_path)
    json_write(store.root / "inputs/copied-review.json", review)
    with pytest.raises(WorkflowError, match="duplicate"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/copied-review.json")


def test_requires_maintenance_pause_and_revalidates_old_permission(additional):
    store, packet, _, _ = additional
    store.pause(False)
    with pytest.raises(WorkflowError, match="pause"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")
    store.pause(True)
    store.authorize_temporary_samples(packet["task_id"], "inputs/additional-review.json")
    json_write(store.root / "inputs/human.json", {})
    with store.transaction() as c, pytest.raises(WorkflowError):
        admitted(store.root, c, c.execute("SELECT * FROM tasks").fetchone(), packet)
