"""Offline original CPFS rejection reconciliation; no new cloud authority or calls."""

import copy
import json
import sqlite3
import time

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.nas_rejection import KIND
from scripts.web.e2e.resource_link_workflow.ownership import owner_key
from scripts.web.e2e.resource_link_workflow.store import Store, dumps
from tests.web.test_resource_link_execution_gate import NATIVE_NAME, native_call, native_wire


@pytest.fixture
def rejected(tmp_path, monkeypatch):
    store = Store.initialize(tmp_path / "state")
    task, job, account = "fixture:R0036", "a" * 32, "offline-account"
    return build_rejected(store, task, job, account, monkeypatch)


def build_rejected(store, task, job, account, monkeypatch, authority=None):
    with store.transaction() as db:
        if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task,)).fetchone():
            db.execute(
                "INSERT INTO tasks(id,run_id,resource_id,spec,state) VALUES(?,?,?,?,?)",
                (task, "fixture", "R0036", dumps({"key": "ALIYUN::NAS::DataFlow"}), "blocked"),
            )
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job, task, "glm", "dispatch", "complete", "offline-thread", "offline-turn", "t", "confirmed", 1),
        )
    path = store.root / "execution-gate.sqlite3"
    gate.initialize(path)
    monkeypatch.setenv(gate.ENV, str(path))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    grants = gate.nas_native_plan(NATIVE_NAME)
    gate.install(path, "0", job + ":QUERY", "ws~native", account, None, grants, authority=authority)
    signer = AliyunCredential(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    native = path, signer, {g["id"]: g for g in grants}, account
    for ident in (
        "identity",
        "precheck-bucket-absent",
        "create-filesystem-vpc",
        "read-filesystem-vpc",
        "create-filesystem-vswitch",
        "read-filesystem-vswitch",
        "read-filesystem-implicit-members",
        "precheck-filesystem",
    ):
        native_wire(native, ident)
    body = {
        "Code": "COMMODITY.INVALID_COMPONENT",
        "statusCode": 400,
        "RequestId": "offline-reject",
        "HostId": "nas.cn-hangzhou.aliyuncs.com",
        "Message": json.dumps(
            {
                "code": "COMMODITY.INVALID_COMPONENT",
                "requestId": "offline-reject",
                "success": False,
                "message": "offline order rejected",
            }
        ),
    }
    ticket = gate.reserve(
        native_call(native, "create-filesystem"),
        "ws~native",
        signer,
        read_only=False,
        tool_use_id="offline-create-tool",
    )
    gate.finish(ticket, body, False, status=400, headers={"x-acs-request-id": "offline-reject"})

    def save(name, value):
        filename = "inputs/" + name + ".json"
        json_write(store.root / filename, value)
        return {"path": filename, "sha256": digest(store.root / filename)}

    originals = gate.creations(path, [job])
    for record in originals:
        if record["state"] != "succeeded":
            continue
        resource = record["resource"]
        source = save(resource["action"] + "-absence", {"offline": True, "TotalCount": 0})
        cleanup = save(
            resource["action"] + "-cleanup",
            {
                "resource": resource,
                "absent": True,
                "failed": False,
                "source_evidence": source["path"],
                "source_sha256": source["sha256"],
            },
        )
        with store.transaction() as db:
            owned = owner_key(db, store.root, task, "QUERY", resource, job)
            db.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (owned, task, "QUERY", dumps(resource), "deleted", dumps(cleanup)),
            )
    original = next(r for r in originals if r["resource"]["action"] == "CreateFileSystem")
    now = time.time() + 1
    events = []
    for index, (call, response) in enumerate(
        [
            (
                {"product": "sts", "action": "GetCallerIdentity", "version": "2015-04-01", "params": {}},
                {"RequestId": "offline-identity", "AccountId": account},
            ),
            (
                {
                    "product": "nas",
                    "action": "DescribeFileSystems",
                    "version": "2017-06-26",
                    "params": {"VpcId": "vpc-fs", "FileSystemType": "cpfs", "PageNumber": 1, "PageSize": 100},
                },
                {
                    "RequestId": "offline-empty",
                    "FileSystems": {"FileSystem": []},
                    "TotalCount": 0,
                    "PageNumber": 1,
                    "PageSize": 100,
                },
            ),
        ]
    ):
        event = save(
            "independent-" + str(index),
            {
                "call": call,
                "body": response,
                "exit_code": 0,
                "started_at": now + index * 2,
                "completed_at": now + index * 2 + 1,
            },
        )
        event.update(exit_code=0, request_id=response["RequestId"])
        events.append(event)
    absence = save(
        "absence",
        {
            "kind": "ops_rejected_native_filesystem_absence_result",
            "task_id": task,
            "original_job_id": job,
            "account": account,
            "region": "cn-hangzhou",
            "vpc_id": "vpc-fs",
            "cloud_writes": 0,
            "formal_test": False,
            "pass_added": False,
            "failure_type": None,
            "events": events,
        },
    )
    audit = {
        "schema_version": 1,
        "kind": KIND,
        "approved": True,
        "task_id": task,
        "original_job": job,
        "actual_account": account,
        "attempt_id": original["attempt_id"],
        "confirmed_no_residual": True,
        "no_journal_mutation": True,
        "new_creation_authorized": False,
        "pass_added": False,
        "wire_source": save("wire", original),
        "independent_absence": absence,
    }
    save("rejection", audit)
    return store, task, audit, save


def wire_snapshot(store):
    with sqlite3.connect(store.root / "execution-gate.sqlite3") as db:
        return {
            name: db.execute("SELECT * FROM " + name + " ORDER BY rowid").fetchall()
            for name in ("attempts", "grants", "identities", "authority_limits", "active")
        }


def test_exact_rejection_records_one_append_only_event_without_refilling_or_pass(rejected):
    store, task, audit, _ = rejected
    before = wire_snapshot(store)
    result = store.reconcile_rejected_creation(task, "inputs/rejection.json")
    assert result == {"reconciled_attempts": [audit["attempt_id"]], "wire_unchanged": True, "new_pass": 0}
    assert store.reconcile_rejected_creation(task, "inputs/rejection.json")["already_recorded"] is True
    assert wire_snapshot(store) == before
    with store.transaction() as db:
        assert store.reconciled_creations(db, task) == {audit["attempt_id"]}
        assert db.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events WHERE kind='ops_creation_reconciled'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "approval",
        "job",
        "live",
        "attempt",
        "sha",
        "wire",
        "http500",
        "wrong_code",
        "has_id",
        "account",
        "grant_call",
        "credential",
        "allowance",
        "owner",
        "owner_source",
        "parent_id",
        "inventory_account",
        "inventory_nonempty",
        "inventory_pagination",
        "inventory_call",
        "chronology",
        "error_request",
        "request_header",
        "dryrun",
        "region",
        "no_residual",
    ],
)
def test_reconciliation_rejects_ambiguous_or_drifted_evidence(rejected, damage):
    store, task, original, save = rejected
    audit = copy.deepcopy(original)
    if damage in {"approval", "job", "attempt", "account", "no_residual"}:
        key = {
            "approval": "approved",
            "job": "original_job",
            "attempt": "attempt_id",
            "account": "actual_account",
            "no_residual": "confirmed_no_residual",
        }[damage]
        audit[key] = False
    elif damage == "sha":
        audit["wire_source"]["sha256"] = "0" * 64
    elif damage == "wire":
        value = read_json(store.root / audit["wire_source"]["path"])
        value["state"] = "succeeded"
        audit["wire_source"] = save("wire", value)
    elif damage in {"live", "owner", "owner_source"}:
        with store.transaction() as db:
            if damage == "live":
                db.execute("UPDATE jobs SET state='running'")
            elif damage == "owner":
                db.execute("UPDATE owned SET state='created'")
            else:
                evidence = json.loads(db.execute("SELECT evidence FROM owned LIMIT 1").fetchone()[0])
                cleanup = read_json(store.root / evidence["path"])
                save(cleanup["source_evidence"].removeprefix("inputs/").removesuffix(".json"), {"changed": True})
    elif damage.startswith("inventory_") or damage == "chronology":
        report = read_json(store.root / audit["independent_absence"]["path"])
        index = 0 if damage == "inventory_account" else 1
        raw = read_json(store.root / report["events"][index]["path"])
        if damage == "inventory_account":
            raw["body"]["AccountId"] = "foreign"
        elif damage == "inventory_nonempty":
            raw["body"]["FileSystems"]["FileSystem"] = [{"FileSystemId": "business"}]
        elif damage == "inventory_pagination":
            raw["body"]["NextToken"] = "more"
        elif damage == "inventory_call":
            raw["call"]["params"]["VpcId"] = "business"
        else:
            raw["started_at"] = 0
        ref = save("independent-" + str(index), raw)
        report["events"][index].update(ref)
        audit["independent_absence"] = save("absence", report)
    else:
        with gate.database(store.root / "execution-gate.sqlite3") as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (audit["attempt_id"],)).fetchone()
            raw = json.loads(row["response"])
            if damage == "grant_call":
                value = json.loads(db.execute("SELECT call FROM grants WHERE id='create-filesystem'").fetchone()[0])
                value["params"]["Description"] = "changed"
                db.execute("UPDATE grants SET call=? WHERE id='create-filesystem'", (dumps(value),))
            elif damage == "credential":
                db.execute("UPDATE attempts SET credential='other' WHERE id=?", (row["id"],))
            elif damage == "allowance":
                db.execute("UPDATE authority_limits SET used=0 WHERE id='create-filesystem'")
            elif damage == "http500":
                raw["status"] = 500
            elif damage == "wrong_code":
                raw["body"]["Code"] = "InternalError"
            elif damage == "has_id":
                raw["body"]["FileSystemId"] = "cpfs-maybe"
            elif damage == "error_request":
                message = json.loads(raw["body"]["Message"])
                message["requestId"] = "other"
                raw["body"]["Message"] = dumps(message)
            elif damage == "request_header":
                raw["headers"]["x-acs-request-id"] = "other"
            elif damage == "parent_id":
                raw["call"]["params"]["VpcId"] = "business"
            elif damage == "dryrun":
                raw["call"]["params"]["DryRun"] = True
            elif damage == "region":
                raw["call"]["region_id"] = "cn-beijing"
            db.execute("UPDATE attempts SET response=? WHERE id=?", (dumps(raw), row["id"]))
        current = next(
            r
            for r in gate.creations(store.root / "execution-gate.sqlite3", [audit["original_job"]])
            if r["attempt_id"] == audit["attempt_id"]
        )
        audit["wire_source"] = save("wire", current)
    save("rejection", audit)
    before = wire_snapshot(store)
    with pytest.raises(WorkflowError):
        store.reconcile_rejected_creation(task, "inputs/rejection.json")
    assert wire_snapshot(store) == before
