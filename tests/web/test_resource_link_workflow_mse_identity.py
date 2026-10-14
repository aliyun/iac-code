"""Original hosted MSE post-delete signer rotation, without resetting any quota."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from scripts.web.e2e.resource_link_workflow import cleanup_recovery
from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.phases import (
    assert_stack_source_ready,
    known_original_absence_read,
    original_stack_read_body,
)
from tests.web.test_resource_link_cleanup_identity import original_rows
from tests.web.test_resource_link_workflow_mse_phases import absent, deleted, rows
from tests.web.test_resource_link_workflow_read_policy import counts, mse_actual, mse_perform


def fixture(tmp_path, monkeypatch):
    setup = deleted(tmp_path, monkeypatch)
    path, credential, clock, grants = setup
    dest = tmp_path / "execution-gate.sqlite3"
    path.rename(dest)
    monkeypatch.setenv(gate.ENV, str(dest))
    setup = dest, credential, clock, grants
    args = dict(
        slot="0",
        scope="offline-mse:TF",
        session="ws~mse-full",
        account="123",
        grants=grants,
        authority="offline-mse-authority",
    )
    candidate = cleanup_recovery.inspect_candidate(dest, **args)
    fields = cleanup_recovery.readonly_review_fields(candidate)
    review = {
        "kind": "ops_cleanup_identity_review",
        "approved": True,
        **{key: candidate[key] for key in ("scope", "authority", "account", "owned_from_wire")},
        "session": candidate["session_id"],
        "additional_identity_reads": 1,
        "identity_call": next(g["call"] for g in grants if g["kind"] == "identity"),
        "post_delete_readonly": fields,
        "reason": "Original signed deletion succeeded; rotated signer must prove the same account.",
    }
    json_write(tmp_path / "inputs/identity.json", review, private=True)
    cleanup = {
        "kind": "ops_cleanup_readonly_review",
        "approved": True,
        "original_job_id": "offline-mse",
        "dimension": "TF",
        **{k: candidate[k] for k in ("scope", "authority", "account", "session_id", "owned_from_wire")},
        **fields,
    }
    json_write(tmp_path / "inputs/cleanup.json", cleanup, private=True)
    recovery = {
        "original_job_id": "offline-mse",
        "dimension": "TF",
        "authority": candidate["authority"],
        "evidence": "inputs/cleanup.json",
        "evidence_sha256": digest(tmp_path / "inputs/cleanup.json"),
    }
    return setup, args, candidate, review, recovery


def browser_accepts(text):
    if not shutil.which("node"):
        pytest.skip("Node is needed for the real browser delivery validator")
    module = (Path(__file__).resolve().parents[2] / "scripts/web/e2e/resource_link_workflow/browser.mjs").as_uri()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            f"import {{allowedOriginalReadContinuation as ok}} from '{module}';"
            "let text='';for await (const chunk of process.stdin) text+=chunk;"
            "process.stdout.write(String(ok(text)));",
        ],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout == "true"


def test_real_exact_readonly_delivery_has_one_machine_payload(tmp_path, monkeypatch):
    setup, args, _, _, recovery = fixture(tmp_path, monkeypatch)
    before = original_rows(tmp_path)
    text = cleanup_recovery.exact_readonly_request(tmp_path, recovery, "0", args["session"], setup[3])
    assert json.loads(text.rsplit("\n", 1)[1])["kind"] == "original_missing_cleanup_reads"
    assert browser_accepts(text)
    assert original_rows(tmp_path) == before


def rotate(setup, candidate, tmp_path):
    before = original_rows(tmp_path)
    result = cleanup_recovery.authorize_identity(tmp_path, candidate, "inputs/identity.json")
    after = original_rows(tmp_path)
    for table in before:
        assert all(row in after[table] for row in before[table])
    credential = AliyunCredential(mode="AK", access_key_id="fake-rotated", access_key_secret="fake-new-secret")
    grants = [
        *setup[3],
        {
            "id": result["grant_id"],
            "kind": "identity",
            "quota": 1,
            "call": next(g["call"] for g in setup[3] if g["kind"] == "identity"),
        },
    ]
    return (setup[0], credential, setup[2], grants), result["grant_id"]


def stack_rows(db):
    return db.execute(
        "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param FROM attempts a JOIN grants g "
        "ON a.scope=g.scope AND a.grant_id=g.id ORDER BY a.created"
    ).fetchall()


@pytest.mark.parametrize("child", ["cluster", "vpc", "vswitch", "stack"])
def test_reviewed_rotation_only_finishes_original_reads(tmp_path, monkeypatch, child):
    setup, args, candidate, _, recovery = fixture(tmp_path, monkeypatch)
    old_counts = counts(setup[0])
    rotated, identity = rotate(setup, candidate, tmp_path)
    request = cleanup_recovery.exact_readonly_request(tmp_path, recovery, "0", args["session"], setup[3])
    assert "GetCallerIdentity" in request and "DescribeVpcAttribute" not in request and browser_accepts(request)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(
            mse_actual(setup[3], "cluster-cleanup"),
            args["session"],
            rotated[1],
            read_only=True,
            tool_use_id="no-identity-yet",
        )
    mse_perform(rotated, identity, {"AccountId": "123", "RequestId": "rotated-identity"})
    request = cleanup_recovery.exact_readonly_request(tmp_path, recovery, "0", args["session"], setup[3])
    assert "original_missing_cleanup_reads" in request and browser_accepts(request)
    if child == "stack":
        mse_perform(
            rotated,
            "mse-status-cleanup",
            {
                "RequestId": "own-stack-absent",
                "Code": "StackNotFound",
                "Message": "The Stack (mse-stack-own) could not be found.",
            },
            status=404,
            success=False,
        )
        grant_id = "mse-status-cleanup"
    else:
        absent(rotated, child)
        grant_id = child + "-cleanup"
    with gate.database(setup[0]) as db:
        row = next(r for r in rows(db) if r["grant_id"] == grant_id)
        before = db.total_changes
        assert known_original_absence_read(db, row)
        assert db.total_changes == before and row["state"] == "unknown"
    assert counts(setup[0])[0]["mse-create"] == old_counts[0]["mse-create"] == 1
    assert counts(setup[0])[0]["mse-delete"] == old_counts[0]["mse-delete"] == 1
    assert counts(setup[0])[0]["mse-identity"] == old_counts[0]["mse-identity"] == 1
    for grant_id in ("mse-create", "mse-delete", "cluster-resource"):
        before = counts(setup[0])
        with pytest.raises(gate.GateDeniedError):
            gate.reserve(
                mse_actual(setup[3], grant_id),
                args["session"],
                rotated[1],
                read_only=grant_id == "cluster-resource",
                tool_use_id="forbidden",
            )
        assert counts(setup[0]) == before


@pytest.mark.parametrize("status", ["DELETE_IN_PROGRESS", "DELETE_COMPLETE", "DELETE_FAILED"])
def test_original_stack_readiness_uses_reviewed_rotated_terminal_response(tmp_path, monkeypatch, status):
    setup, _, candidate, _, _ = fixture(tmp_path, monkeypatch)
    rotated, identity = rotate(setup, candidate, tmp_path)
    mse_perform(rotated, identity, {"AccountId": "123", "RequestId": "rotated-identity"})
    mse_perform(
        rotated,
        "mse-status-cleanup",
        {
            "RequestId": "rotated-stack-status",
            "Stack": {"StackId": "mse-stack-own", "Status": status},
        },
    )
    with gate.database(setup[0]) as db:
        current = stack_rows(db)
        create = next(r for r in current if r["grant_id"] == "mse-create")
        row = next(r for r in current if r["grant_id"] == "mse-status-cleanup")
        before = db.total_changes
        assert original_stack_read_body(db, row, create)["Status"] == status
        assert original_stack_read_body(db, row, create, require_active_identity=False)["Status"] == status
        assert db.total_changes == before
    before = original_rows(tmp_path)
    if status.endswith("_IN_PROGRESS"):
        with pytest.raises(WorkflowError, match="still pending"):
            assert_stack_source_ready(setup[0], "offline-mse:TF", cleanup=True)
    else:
        assert_stack_source_ready(setup[0], "offline-mse:TF", cleanup=True)
    assert original_rows(tmp_path) == before  # Readiness is not a verdict or journal mutation.


@pytest.mark.parametrize("damage", ["review", "account", "order", "delete-state", "response", "region"])
def test_original_stack_projection_cannot_bypass_rotated_cleanup_proof(tmp_path, monkeypatch, damage):
    setup, _, candidate, _, _ = fixture(tmp_path, monkeypatch)
    rotated, identity = rotate(setup, candidate, tmp_path)
    mse_perform(rotated, identity, {"AccountId": "123", "RequestId": "rotated-identity"})
    mse_perform(
        rotated,
        "mse-status-cleanup",
        {
            "RequestId": "rotated-stack-status",
            "Stack": {"StackId": "mse-stack-own", "Status": "DELETE_COMPLETE"},
        },
    )
    with gate.database(setup[0]) as db:
        if damage == "review":
            review = json.loads((tmp_path / "inputs/identity.json").read_text(encoding="utf-8"))
            review["reason"] = "tampered"
            json_write(tmp_path / "inputs/identity.json", review, private=True)
        elif damage == "account":
            db.execute(
                "UPDATE identities SET account='foreign' WHERE credential=(SELECT credential FROM attempts "
                "WHERE grant_id=?)",
                (identity,),
            )
        elif damage == "order":
            db.execute("UPDATE attempts SET created=0 WHERE grant_id=?", (identity,))
        elif damage == "delete-state":
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='mse-delete'")
        current = stack_rows(db)
        create = next(r for r in current if r["grant_id"] == "mse-create")
        row = dict(next(r for r in current if r["grant_id"] == "mse-status-cleanup"))
        if damage == "response":
            value = json.loads(row["response"])
            value["body"]["Stack"]["StackId"] = "foreign-stack"
            row["response"] = json.dumps(value)
        elif damage == "region":
            value = json.loads(row["call"])
            value["region_id"] = "cn-shanghai"
            row["call"] = json.dumps(value)
        before = db.total_changes
        assert original_stack_read_body(db, row, create) is None
        assert db.total_changes == before


@pytest.mark.parametrize("damage", ["proof", "reads", "scope", "account", "owned", "identity", "extra"])
def test_postdelete_identity_review_drift_never_appends_quota(tmp_path, monkeypatch, damage):
    _, _, candidate, review, _ = fixture(tmp_path, monkeypatch)
    review = copy.deepcopy(review)
    if damage == "proof":
        review["post_delete_readonly"]["successful_deletions"][0]["response_sha256"] = "wrong"
    elif damage == "reads":
        review["post_delete_readonly"]["cleanup_read_calls"] = []
    elif damage == "extra":
        review["additional_identity_reads"] = 2
    elif damage == "owned":
        review["owned_from_wire"] = []
    elif damage == "identity":
        review["identity_call"]["action"] = "OtherCall"
    else:
        review[damage] = "foreign"
    json_write(tmp_path / "inputs/identity.json", review, private=True)
    before = original_rows(tmp_path)
    with pytest.raises(WorkflowError):
        cleanup_recovery.authorize_identity(tmp_path, candidate, "inputs/identity.json")
    assert original_rows(tmp_path) == before


@pytest.mark.parametrize(
    "damage",
    [
        "review",
        "identity-row",
        "account",
        "identity-error",
        "tool",
        "reference",
        "order",
        "counter",
        "delete-state",
        "delete-counter",
        "parent",
        "members",
    ],
)
def test_rotated_read_bridge_rechecks_original_and_new_signed_proofs(tmp_path, monkeypatch, damage):
    setup, args, candidate, _, _ = fixture(tmp_path, monkeypatch)
    rotated, identity = rotate(setup, candidate, tmp_path)
    mse_perform(rotated, identity, {"AccountId": "123", "RequestId": "rotated-identity"})
    absent(rotated, "cluster")
    with gate.database(setup[0]) as db:
        row = next(r for r in rows(db) if r["grant_id"] == "cluster-cleanup")
        assert known_original_absence_read(db, row)
        if damage == "review":
            value = json.loads((tmp_path / "inputs/identity.json").read_text(encoding="utf-8"))
            value["reason"] = "changed"
            json_write(tmp_path / "inputs/identity.json", value, private=True)
        elif damage == "identity-row":
            db.execute(
                "DELETE FROM identities WHERE credential=(SELECT credential FROM attempts WHERE grant_id=?)",
                (identity,),
            )
        elif damage in {"account", "identity-error"}:
            body = {"AccountId": "other" if damage == "account" else "123", "RequestId": "rotated-identity"}
            if damage == "identity-error":
                body["Code"] = "NoPermission"
            db.execute("UPDATE attempts SET response=? WHERE grant_id=?", (json.dumps(body), identity))
        elif damage in {"tool", "reference", "order"}:
            column = {"tool": "tool_use_id", "reference": "reference_scope", "order": "created"}[damage]
            db.execute(f"UPDATE attempts SET {column}=? WHERE grant_id=?", (0 if damage == "order" else "", identity))
        elif damage in {"counter", "delete-counter"}:
            db.execute(
                "UPDATE authority_limits SET used=0 WHERE id=?", (identity if damage == "counter" else "mse-delete",)
            )
        elif damage in {"delete-state", "parent"}:
            db.execute(
                "UPDATE attempts SET state='unknown' WHERE grant_id=?",
                ("mse-delete" if damage == "delete-state" else "mse-create",),
            )
        elif damage == "members":
            db.execute("UPDATE attempts SET response='{}' WHERE grant_id='mse-members'")
        before = db.total_changes
        assert not known_original_absence_read(db, row)
        assert db.total_changes == before
    before = original_rows(tmp_path)
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(
            mse_actual(setup[3], "vpc-cleanup"),
            args["session"],
            rotated[1],
            read_only=True,
            tool_use_id="cannot-bypass",
        )
    assert original_rows(tmp_path) == before
