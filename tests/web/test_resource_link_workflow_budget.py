"""Offline OPS fee amendments preserve frozen plans and all execution state."""

import hashlib
import json

import pytest

from scripts.web.e2e.resource_link_workflow.__main__ import parser
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from tests.web.test_resource_link_workflow import resource


@pytest.fixture
def reviewed(tmp_path):
    store = Store.initialize(tmp_path / "state")
    plan = {"schema_version": 1, "plan_id": "fee-fixture", "resources": [resource(33)]}
    store.import_plan(plan)
    human = {
        "kind": "human_fee_authorization",
        "approved": True,
        "plan_id": plan["plan_id"],
        "task_id": "fee-fixture:R0033",
        "max_budget_cents": 100,
        "verbatim_user_message": "OFFLINE FAKE HUMAN AUTHORIZATION ONLY",
    }
    json_write(store.root / "inputs/human.json", human, private=True)
    proof = {
        "schema_version": 1,
        "kind": "ops_run_budget_review",
        "approved": True,
        "plan_id": plan["plan_id"],
        "task_id": human["task_id"],
        "frozen_plan_sha256": hashlib.sha256(dumps(plan).encode()).hexdigest(),
        "old_budget_cents": 0,
        "new_budget_cents": 100,
        "human_authorization": "inputs/human.json",
        "human_authorization_sha256": digest(store.root / "inputs/human.json"),
    }
    json_write(store.root / "inputs/review.json", proof, private=True)
    return store, proof


def test_fee_amendment_is_audited_idempotent_and_preserves_all_other_rows(reviewed):
    store, _ = reviewed
    with store.transaction() as connection:
        connection.execute("INSERT INTO budget_reservations VALUES(?,?)", ("fee-fixture:R0033", 0))
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("tasks", "slots", "owned", "verdicts", "jobs", "budget_reservations")
        }
        frozen = connection.execute("SELECT plan FROM runs").fetchone()[0]
    result = store.amend_budget("fee-fixture", 100, "inputs/review.json")
    assert result["no_cloud_call"] and not result["already_recorded"]
    revision = store.status()["revision"]
    assert store.amend_budget("fee-fixture", 100, "inputs/review.json")["already_recorded"]
    assert store.status()["revision"] == revision
    with store.transaction() as connection:
        assert connection.execute("SELECT plan,budget FROM runs").fetchone()[:] == (frozen, 100)
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        events = connection.execute("SELECT data FROM events WHERE kind='run_budget_amended'").fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0])["evidence_sha256"] == digest(store.root / "inputs/review.json")
    args = parser().parse_args(
        ["amend-budget", "fee-fixture", "--budget-cents", "100", "--evidence", "inputs/review.json"]
    )
    assert args.budget_cents == 100


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved", False),
        ("kind", "worker_summary"),
        ("plan_id", "other"),
        ("frozen_plan_sha256", "wrong"),
        ("old_budget_cents", 99),
        ("task_id", "other:R0033"),
        ("human_authorization_sha256", "wrong"),
    ],
)
def test_fee_amendment_rejects_unreviewed_or_drifting_inputs(reviewed, field, value):
    store, proof = reviewed
    proof[field] = value
    json_write(store.root / "inputs/review.json", proof)
    revision = store.status()["revision"]
    with pytest.raises(WorkflowError):
        store.amend_budget("fee-fixture", 100, "inputs/review.json")
    assert store.status()["revision"] == revision
    assert store.status()["active_plans"][0]["budget_cents"] == 0


@pytest.mark.parametrize("cents", [True, -1, 0, 101, "100"])
def test_fee_amendment_rejects_bad_or_unapproved_amounts(reviewed, cents):
    store, _ = reviewed
    with pytest.raises(WorkflowError):
        store.amend_budget("fee-fixture", cents, "inputs/review.json")


def test_fee_amendment_requires_drained_pause_and_incomplete_task(reviewed):
    store, _ = reviewed
    store.pause(False)
    with pytest.raises(WorkflowError, match="pause"):
        store.amend_budget("fee-fixture", 100, "inputs/review.json")
    store.pause(True)
    with store.transaction() as connection:
        connection.execute("INSERT INTO actions VALUES('fixture-action',NULL,'prepare','issued',0)")
    with pytest.raises(WorkflowError, match="drain"):
        store.amend_budget("fee-fixture", 100, "inputs/review.json")
    store.pause(True)
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='complete'")
    with pytest.raises(WorkflowError, match="incomplete"):
        store.amend_budget("fee-fixture", 100, "inputs/review.json")


def test_fee_amendment_checks_original_human_record_sha(reviewed):
    store, _ = reviewed
    json_write(store.root / "inputs/human.json", {"kind": "worker_summary", "approved": True})
    with pytest.raises(WorkflowError, match="human"):
        store.amend_budget("fee-fixture", 100, "inputs/review.json")


def test_fee_amendment_cannot_read_authorization_outside_inputs(reviewed):
    store, proof = reviewed
    json_write(store.root / "review.json", proof)
    with pytest.raises(WorkflowError, match="escapes"):
        store.amend_budget("fee-fixture", 100, "../review.json")
