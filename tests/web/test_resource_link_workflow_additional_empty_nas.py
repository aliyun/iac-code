"""A new explicit NAS generation never replenishes the original operation."""

import copy
import hashlib
import json

import pytest

from scripts.web.e2e.resource_link_workflow.creation import execution_digest
from scripts.web.e2e.resource_link_workflow.execution_gate import canonical
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.store import Store
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_workflow import resource
from tests.web.test_resource_link_workflow_empty_general_nas import candidate  # noqa: F401


@pytest.fixture
def additional(candidate, tmp_path, monkeypatch):  # noqa: F811
    candidate[0]()
    store = Store.initialize(tmp_path)
    spec = resource(39)
    spec["key"] = "ALIYUN::NAS::MountTarget"
    spec["dimensions"]["QUERY"]["contracts"][0]["historical"] = {"iframe": "pass", "open": "pass"}
    spec["dimensions"]["TF"].update(
        support="supported",
        contracts=[{"id": "R0039-TF-1", "source_type": spec["key"], "historical": {"iframe": "fail", "open": "fail"}}],
    )
    store.import_plan({"schema_version": 1, "plan_id": "test", "resources": [spec]})
    with store.transaction() as c:
        spec["retest_scope"] = {
            "kind": "failed_navigation_managed",
            "scene_ids": ["R0039-TF-1-iframe", "R0039-TF-1-open"],
        }
        c.execute("UPDATE tasks SET spec=?", (json.dumps(spec),))
        frozen = c.execute("SELECT plan FROM runs").fetchone()[0]
    directory = tmp_path / "inputs/R0039"

    def save(name, value):
        path = directory / name
        json_write(path, value)
        return {"path": path.relative_to(tmp_path).as_posix(), "sha256": digest(path)}

    initial = read_json(directory / "admission.json")
    initial["frozen_plan_sha256"] = hashlib.sha256(frozen.encode()).hexdigest()
    save("admission.json", initial)
    store.authorize_temporary_samples("test:R0039", "inputs/R0039/admission.json")
    old = read_json(directory / "proposal.json")
    packet = copy.deepcopy(old)
    packet["scenes"] = {key: {"account": "1234567890123456"} for key in spec["retest_scope"]["scene_ids"]}
    packet["additional_source_requests"] = [
        {
            "request_id": "offline-extra-empty-nas",
            "dimension": "TF",
            "prior_scope": "a" * 32 + ":TF",
            "predecessor_proposal": initial["proposal"]["path"],
            "predecessor_sha256": initial["proposal"]["sha256"],
        }
    ]
    human = read_json(directory / "human.json")
    human["human_question_verbatim"] = "one additional empty NAS generation after cleanup?"
    human["scope"]["additional_generation_after_original_cleanup"] = True
    proof = copy.deepcopy(initial)
    proof.update(
        kind="ops_additional_empty_general_nas_admission",
        original_admission={"path": "inputs/R0039/admission.json", "sha256": digest(directory / "admission.json")},
    )
    proof["human_authorization"] = save("extra-human.json", human)
    proof["proposal"] = save("extra-proposal.json", packet)
    proof["execution_sha256"] = execution_digest(packet)
    fee = read_json(directory / "pricing.json")
    fee.update(human_authorization=proof["human_authorization"], execution_sha256=execution_digest(packet))
    proof["pricing_review"] = save("extra-pricing.json", fee)
    operation = {
        "kind": "ops_additional_sources_review",
        "approved": True,
        "proposal_sha256": proof["proposal"]["sha256"],
        "additional_sources_approved": packet["additional_source_requests"],
        "carry_forward_dimensions": {},
    }
    proof["operation_review"] = save("extra-operation.json", operation)
    save("extra-admission.json", proof)
    with store.transaction() as c:
        c.execute("UPDATE tasks SET state='blocked',attempt=4")
    # Original signed wire/cleanup provenance has an independent real SQLite
    # suite; this admission must call it and propagate every refusal.
    checked = []
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.additional_source.validate_additional_source",
        lambda *args: checked.append(args[1:]),
    )
    return store, packet, proof, checked, save


def test_extra_nas_retains_original_grants_passes_and_operation_count(additional):
    store, packet, _, checked, _ = additional
    with store.transaction() as c:
        tables = ("tasks", "runs", "jobs", "owned", "verdicts", "budget_reservations")
        before = {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table}")] for table in tables}
        original = c.execute("SELECT value FROM meta WHERE key LIKE 'temporary_samples:%'").fetchone()[0]
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")[
        "operation_authorization_required"
    ]
    assert len(checked) == 1
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")["already_recorded"]
    assert len(checked) == 1
    with store.transaction() as c:
        operation = {"task_id": packet["task_id"], "operation": packet["additional_source_requests"][0]}
        authority = hashlib.sha256(canonical(operation).encode()).hexdigest()
        assert admitted(store.root, c, c.execute("SELECT * FROM tasks").fetchone(), packet) == {"TF": authority + ":TF"}
        assert c.execute("SELECT value FROM meta WHERE key LIKE 'temporary_samples:%'").fetchone()[0] == original
        for table in tables:
            assert before[table] == [tuple(row) for row in c.execute(f"SELECT * FROM {table}")]
    assert not list((store.root / "creation-authorizations").glob("*.json"))


@pytest.mark.parametrize("damage", ["permission", "network", "pricing", "operation", "initial", "scenes", "grants"])
def test_extra_nas_rejects_expansion_or_evidence_drift(additional, damage):
    store, packet, proof, _, save = additional
    if damage == "permission":
        human = read_json(store.root / proof["human_authorization"]["path"])
        human["scope"]["additional_generation_after_original_cleanup"] = False
        proof["human_authorization"] = save("extra-human.json", human)
        fee = read_json(store.root / proof["pricing_review"]["path"])
        fee["human_authorization"] = proof["human_authorization"]
        proof["pricing_review"] = save("extra-pricing.json", fee)
    elif damage in {"network", "pricing", "operation", "initial"}:
        field = {
            "network": "network_review",
            "pricing": "pricing_review",
            "operation": "operation_review",
            "initial": "original_admission",
        }[damage]
        value = read_json(store.root / proof[field]["path"])
        value["changed"] = True
        json_write(store.root / proof[field]["path"], value)
    else:
        if damage == "scenes":
            packet["scenes"]["R0039-QUERY-1-open"] = {"account": "1234567890123456"}
        else:
            packet["execution_grants"]["TF"][0]["quota"] = 2
        proof["proposal"] = save("extra-proposal.json", packet)
    save("extra-admission.json", proof)
    with pytest.raises(WorkflowError):
        store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")


def test_extra_nas_requires_original_cleanup_and_no_unknown_writes(additional, monkeypatch):
    store, packet, _, _, _ = additional

    def refuse(*args):
        raise WorkflowError("original owner remains or original write result unknown")

    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.additional_source.validate_additional_source", refuse)
    with pytest.raises(WorkflowError, match="original owner"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")
    with store.transaction() as c:
        assert not c.execute("SELECT 1 FROM meta WHERE key LIKE 'additional_empty_nas:%'").fetchone()


def test_extra_nas_cannot_change_packet_or_duplicate_human_permission(additional):
    store, packet, proof, _, save = additional
    store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")
    changed = copy.deepcopy(packet)
    changed["requests"] = {"TF": "different"}
    with store.transaction() as c, pytest.raises(WorkflowError, match="full reviewed proposal"):
        admitted(store.root, c, c.execute("SELECT * FROM tasks").fetchone(), changed)
    proof["comment"] = "same human permission, not a new operation"
    save("extra-admission.json", proof)
    with pytest.raises(WorkflowError, match="duplicate"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")


def test_extra_nas_requires_maintenance_pause(additional):
    store, packet, _, _, _ = additional
    store.pause(False)
    with pytest.raises(WorkflowError, match="pause"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")


def test_new_request_cannot_borrow_first_generation_permission(additional):
    store, packet, _, _, _ = additional
    with store.transaction() as c, pytest.raises(WorkflowError, match="separate exact human admission"):
        admitted(store.root, c, c.execute("SELECT * FROM tasks").fetchone(), packet)
    store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")
    changed = copy.deepcopy(packet)
    changed["additional_source_requests"][0]["request_id"] = "renamed-not-new-human-permission"
    with store.transaction() as c, pytest.raises(WorkflowError, match="separate exact human admission"):
        admitted(store.root, c, c.execute("SELECT * FROM tasks").fetchone(), changed)


def test_copied_human_file_cannot_multiply_additional_generation(additional):
    store, packet, proof, _, save = additional
    store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/extra-admission.json")
    human = read_json(store.root / proof["human_authorization"]["path"])
    human["comment"] = "a copy is not another human approval"
    proof["human_authorization"] = save("copied-human.json", human)
    fee = read_json(store.root / proof["pricing_review"]["path"])
    fee["human_authorization"] = proof["human_authorization"]
    proof["pricing_review"] = save("copied-pricing.json", fee)
    save("copied-admission.json", proof)
    with pytest.raises(WorkflowError, match="duplicate"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/copied-admission.json")


def standing_candidate(additional):
    store, packet, proof, checked, save = additional
    human = read_json(store.root / proof["human_authorization"]["path"])
    human.pop("human_question_verbatim")
    human.pop("human_reply_verbatim")
    human["verbatim_user_message"] = "Complete these tests without asking me to authorize each rebuild."
    human["scope"].update(
        authorization_lifetime="task",
        sample_limit_basis="per_generation",
        ops_reviewed_rebuilds_until_remaining_scenes_complete=True,
        cleanup_before_next_generation=True,
        no_new_cost_allowance=True,
    )
    proof["human_authorization"] = save("standing-human.json", human)
    fee = read_json(store.root / proof["pricing_review"]["path"])
    fee["human_authorization"] = proof["human_authorization"]
    proof["pricing_review"] = save("standing-pricing.json", fee)
    save("standing-admission.json", proof)
    with store.transaction() as c:
        c.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "a" * 32,
                packet["task_id"],
                "glm",
                "execute",
                "complete",
                "thread",
                "turn",
                "",
                "",
                1,
            ),
        )
    return store, packet, proof, checked, save


def test_task_lifetime_permission_keeps_independent_review_and_single_successor(additional):
    store, packet, proof, checked, save = standing_candidate(additional)
    store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/standing-admission.json")
    assert len(checked) == 1
    assert store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/standing-admission.json")[
        "already_recorded"
    ]
    with store.transaction() as c:
        saved = json.loads(c.execute("SELECT value FROM meta WHERE key LIKE 'additional_empty_nas:%'").fetchone()[0])
        assert saved["authorization_lifetime"] == "task"
        assert admitted(store.root, c, c.execute("SELECT * FROM tasks").fetchone(), packet)
    packet["additional_source_requests"][0]["request_id"] = "renamed-same-predecessor-not-new-permission"
    proof["proposal"] = save("renamed-proposal.json", packet)
    proof["execution_sha256"] = execution_digest(packet)
    fee = read_json(store.root / proof["pricing_review"]["path"])
    fee["execution_sha256"] = execution_digest(packet)
    proof["pricing_review"] = save("renamed-pricing.json", fee)
    operation = read_json(store.root / proof["operation_review"]["path"])
    operation.update(
        proposal_sha256=proof["proposal"]["sha256"], additional_sources_approved=packet["additional_source_requests"]
    )
    proof["operation_review"] = save("renamed-operation.json", operation)
    save("renamed-admission.json", proof)
    with pytest.raises(WorkflowError, match="duplicate"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/renamed-admission.json")


def test_same_task_permission_can_review_next_generation_without_refilling_old_one(additional):
    store, packet, proof, checked, save = standing_candidate(additional)
    store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/standing-admission.json")
    predecessor = copy.deepcopy(proof["proposal"])
    packet["additional_source_requests"][0].update(
        request_id="next-independent-operation",
        prior_scope="b" * 32 + ":TF",
        predecessor_proposal=predecessor["path"],
        predecessor_sha256=predecessor["sha256"],
    )
    proof["proposal"] = save("next-proposal.json", packet)
    proof["execution_sha256"] = execution_digest(packet)
    fee = read_json(store.root / proof["pricing_review"]["path"])
    fee["execution_sha256"] = execution_digest(packet)
    proof["pricing_review"] = save("next-pricing.json", fee)
    operation = read_json(store.root / proof["operation_review"]["path"])
    operation.update(
        proposal_sha256=proof["proposal"]["sha256"], additional_sources_approved=packet["additional_source_requests"]
    )
    proof["operation_review"] = save("next-operation.json", operation)
    save("next-admission.json", proof)
    with store.transaction() as c:
        c.execute("UPDATE jobs SET id=?", ("b" * 32,))
    store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/next-admission.json")
    assert len(checked) == 2  # Each successor still goes through the real operation/cleanup gate.
    with store.transaction() as c:
        records = [
            json.loads(row[0]) for row in c.execute("SELECT value FROM meta WHERE key LIKE 'additional_empty_nas:%'")
        ]
        assert len({row["standing_permission_id"] for row in records}) == 1
        assert len({row["human_permission_id"] for row in records}) == 2


def test_task_lifetime_permission_requires_latest_predecessor(additional):
    store, packet, _, _, _ = standing_candidate(additional)
    with store.transaction() as c:
        c.execute("UPDATE jobs SET id=?", ("b" * 32,))
    with pytest.raises(WorkflowError, match="latest real worker"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/standing-admission.json")


@pytest.mark.parametrize(
    "field",
    [
        "cleanup_before_next_generation",
        "no_new_cost_allowance",
        "ops_reviewed_rebuilds_until_remaining_scenes_complete",
    ],
)
def test_task_lifetime_permission_cannot_drop_review_cleanup_or_cost_boundary(additional, field):
    store, packet, proof, _, save = standing_candidate(additional)
    human = read_json(store.root / proof["human_authorization"]["path"])
    human["scope"][field] = False
    proof["human_authorization"] = save("invalid-standing-human.json", human)
    save("invalid-standing-admission.json", proof)
    with pytest.raises(WorkflowError, match="same-scope human delegation"):
        store.authorize_temporary_samples(packet["task_id"], "inputs/R0039/invalid-standing-admission.json")
