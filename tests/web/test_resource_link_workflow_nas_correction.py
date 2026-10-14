"""Offline native NAS correction: distinct authority, immutable history, own cleanup."""

import copy
import hashlib
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.additional_source import validate_additional_source
from scripts.web.e2e.resource_link_workflow.creation import authorization_path, execution_digest
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, read_json
from scripts.web.e2e.resource_link_workflow.nas_correction import KIND
from scripts.web.e2e.resource_link_workflow.runtime import Runtime
from scripts.web.e2e.resource_link_workflow.store import dumps
from scripts.web.e2e.resource_link_workflow.temporary_samples import admitted
from tests.web.test_resource_link_execution_gate import NATIVE_NAME
from tests.web.test_resource_link_workflow_nas_rejection import build_rejected, wire_snapshot
from tests.web.test_resource_link_workflow_native_nas_query import reviewed  # noqa: F401

pytestmark = pytest.mark.parametrize("reviewed", ["legacy"], indirect=True)


@pytest.fixture
def correction(reviewed, monkeypatch):  # noqa: F811
    store, old, _ = reviewed
    task_id, original_job, sol_job = old["task_id"], "a" * 32, "b" * 32
    store.authorize_temporary_samples(task_id, "inputs/admission.json")
    Runtime(store).authorize_creation(store.root / "inputs/proposal.json", "inputs/operation.json", 0)
    _, _, audit, save = build_rejected(
        store, task_id, original_job, "1234567890123456", monkeypatch, authority=execution_digest(old) + ":QUERY"
    )
    with store.transaction() as db:
        db.execute("UPDATE tasks SET state='blocked'")
    store.reconcile_rejected_creation(task_id, "inputs/rejection.json")
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as db:
        db.execute("DELETE FROM active")  # Real terminal dispatch revokes its capability, not historical evidence.
    with store.transaction() as db:
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                sol_job,
                task_id,
                "sol",
                "repair",
                "complete",
                "offline-sol",
                "offline-sol-turn",
                "t",
                "confirmed",
                1,
            ),
        )
        db.execute("UPDATE tasks SET state='blocked'")
        saved = json.loads(
            db.execute("SELECT value FROM meta WHERE key=?", ("temporary_samples:" + task_id,)).fetchone()[0]
        )
    diagnosis = save("diagnosis", {"job_id": sol_job, "task_id": task_id, "offline": True})
    packet = copy.deepcopy(old)
    request = {
        "request_id": "native-nas-bwo1-offline",
        "dimension": "QUERY",
        "prior_scope": original_job + ":QUERY",
        "predecessor_proposal": "inputs/proposal.json",
        "predecessor_sha256": digest(store.root / "inputs/proposal.json"),
        "remediation_evidence": diagnosis["path"],
        "remediation_sha256": diagnosis["sha256"],
    }
    packet["additional_source_requests"] = [request]
    packet["execution_grants"]["QUERY"] = gate.nas_native_plan(NATIVE_NAME, variant="bandwidth_omission_bwo1")
    cost = read_json(store.root / "inputs/R0036-cost.json")
    cost["execution_grants_sha256"] = hashlib.sha256(gate.canonical(packet["execution_grants"]).encode()).hexdigest()
    packet["hourly_cost"]["cost_evidence"] = save("corrected-cost", cost)
    proposal = save("corrected-proposal", packet)
    source = read_json(store.root / "inputs/source-review.json")
    source["execution_sha256"] = execution_digest(packet)
    source["sample_binding"]["execution_grants_sha256"] = cost["execution_grants_sha256"]
    proof = read_json(store.root / "inputs/operation.json")
    proof.update(
        kind="ops_additional_sources_review",
        additional_sources_approved=[request],
        carry_forward_dimensions={},
        proposal_sha256=proposal["sha256"],
        native_nas_corrections={
            request["request_id"]: {
                "kind": KIND,
                "approved": True,
                "task_id": task_id,
                "human_scope_interpretation": "offline fake task-scoped hourly authorization",
                "no_journal_mutation": True,
                "pass_added": False,
                "original_admission": {"path": saved["evidence"], "sha256": saved["evidence_sha256"]},
                "original_authority": saved["execution_sha256"] + ":QUERY",
                "frozen_plan_sha256": saved["frozen_plan_sha256"],
                "reconciliation": save("rejection", audit),
                "source_review": save("corrected-source", source),
                "official_sources": source["official_sources"],
            }
        },
    )
    proof["hourly_operation_review"].update(
        execution_sha256=execution_digest(packet), hourly_cost=packet["hourly_cost"]
    )
    save("corrected-review", proof)
    return store, old, packet, proof, save


def test_correction_authorizes_one_distinct_operation_and_allows_its_own_cleanup_review(correction):
    store, old, packet, proof, _ = correction
    before = wire_snapshot(store)
    validate_additional_source(store.root, packet, proof)
    Runtime(store).authorize_creation(store.root / "inputs/corrected-proposal.json", "inputs/corrected-review.json", 0)
    operation = {"task_id": packet["task_id"], "operation": packet["additional_source_requests"][0]}
    authority = hashlib.sha256(gate.canonical(operation).encode()).hexdigest()
    approval = read_json(authorization_path(store.root, packet))
    assert approval["dimension_authorities"] == {"QUERY": authority + ":QUERY"}
    assert authority != execution_digest(old)
    with store.transaction() as db:
        task = db.execute("SELECT * FROM tasks").fetchone()
        db.execute(
            "INSERT INTO owned VALUES('new-owner',?,'QUERY',?,'awaiting_review','{}')",
            (packet["task_id"], dumps({"id": "offline-new-file-system"})),
        )
        assert admitted(store.root, db, task, packet) == approval["dimension_authorities"]
        assert admitted(store.root, db, task, old) == {"QUERY": execution_digest(old) + ":QUERY"}
        with pytest.raises(WorkflowError, match="owners cleaned"):
            admitted(store.root, db, task, packet, review=proof)
    assert wire_snapshot(store) == before


@pytest.mark.parametrize(
    "damage",
    [
        "quota",
        "token",
        "bandwidth",
        "capacity",
        "name",
        "cleanup",
        "request",
        "scene",
        "hourly_increase",
        "unapproved",
        "reconciliation",
        "original_admission",
        "official",
        "source",
        "original_authority",
        "frozen",
        "original_proposal",
        "diagnosis",
        "old_counter",
        "old_attempt",
        "uncommitted",
        "extra_attempt",
        "bucket_unknown",
        "second_operation",
        "missing_review",
    ],
)
def test_correction_rejects_drift_unknowns_and_copied_allowances(correction, damage):
    store, old, packet, proof, save = correction
    item = proof["native_nas_corrections"][packet["additional_source_requests"][0]["request_id"]]
    entries = {g["id"]: g for g in packet["execution_grants"]["QUERY"]}
    if damage in {"quota", "token", "bandwidth", "capacity", "name"}:
        if damage == "quota":
            entries["create-filesystem"]["quota"] = 2
        elif damage == "token":
            entries["create-filesystem"]["call"]["params"]["ClientToken"] += "-copy"
        elif damage == "bandwidth":
            entries["create-filesystem"]["call"]["params"]["Bandwidth"] = 720
        elif damage == "capacity":
            entries["create-filesystem"]["call"]["params"]["Capacity"] = 10800
        else:
            entries["create-bucket"]["call"]["params"]["bucket"] += "-copy"
    elif damage in {"cleanup", "request", "scene"}:
        key = {"cleanup": "cleanup_recipe", "request": "requests", "scene": "scenes"}[damage]
        packet[key] = {} if key != "cleanup_recipe" else "changed"
    elif damage == "hourly_increase":
        packet["hourly_cost"]["max_hourly_cents"] += 1
    elif damage in {
        "unapproved",
        "reconciliation",
        "original_admission",
        "official",
        "source",
        "original_authority",
        "frozen",
    }:
        key = {
            "unapproved": "approved",
            "official": "official_sources",
            "source": "source_review",
            "frozen": "frozen_plan_sha256",
        }.get(damage, damage)
        item[key] = False if key == "approved" else [] if key == "official_sources" else "changed"
    elif damage == "original_proposal":
        changed = copy.deepcopy(old)
        changed["task_id"] = "different"
        save("proposal", changed)
    elif damage == "diagnosis":
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='running' WHERE role='sol'")
    elif damage in {"old_counter", "old_attempt", "extra_attempt", "bucket_unknown"}:
        with sqlite3.connect(store.root / "execution-gate.sqlite3") as db:
            if damage == "old_counter":
                db.execute("UPDATE authority_limits SET used=0 WHERE id='identity'")
            elif damage == "old_attempt":
                db.execute("UPDATE attempts SET state='succeeded' WHERE grant_id='create-filesystem'")
            elif damage == "bucket_unknown":
                db.execute("UPDATE attempts SET response='{}' WHERE grant_id='precheck-bucket-absent'")
            else:
                db.execute("UPDATE attempts SET grant_id='orphan' WHERE grant_id='read-filesystem-vpc'")
    elif damage == "uncommitted":
        with store.transaction() as db:
            db.execute("DELETE FROM events WHERE kind='ops_creation_reconciled'")
    elif damage == "missing_review":
        proof.pop("native_nas_corrections")
    else:
        Runtime(store).authorize_creation(
            store.root / "inputs/corrected-proposal.json",
            "inputs/corrected-review.json",
            0,
        )
        # A different request ID/file/attempt cannot authorize a second correction.
        prior_id = packet["additional_source_requests"][0]["request_id"]
        packet["additional_source_requests"][0]["request_id"] = "another-native-nas-operation"
        proof["native_nas_corrections"] = {"another-native-nas-operation": proof["native_nas_corrections"][prior_id]}
        source = read_json(store.root / item["source_review"]["path"])
        source["execution_sha256"] = execution_digest(packet)
        item["source_review"] = save("second-source", source)
        proof["hourly_operation_review"]["execution_sha256"] = execution_digest(packet)
    with pytest.raises(WorkflowError, match="already has" if damage == "second_operation" else None):
        validate_additional_source(store.root, packet, proof)
