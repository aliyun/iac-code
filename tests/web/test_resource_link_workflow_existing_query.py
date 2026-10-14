"""Existing user-created samples are read-only provenance, not fabricated creates."""

# ruff: noqa: F811
import copy
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow import execution_gate as gate
from scripts.web.e2e.resource_link_workflow import existing_query
from scripts.web.e2e.resource_link_workflow.existing_query import references, snapshot_wire, validate_flow
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from tests.web.test_resource_link_execution_gate import active, grants, identity  # noqa: F401
from tests.web.test_resource_link_workflow_native_continuation import (  # noqa: F401
    native,
    native_filesystem,
    retained,
)


def review(active):
    path, credential = active
    identity(active)
    ticket = gate.reserve(grants()[2]["call"], "ws~complete", credential, read_only=False)
    gate.finish(ticket, {"VSwitchId": "vsw-own"}, True)
    root = path.parent
    # snapshot_wire uses the standard workflow filename.
    standard = root / "execution-gate.sqlite3"
    gate.initialize(standard)
    with sqlite3.connect(path) as source, sqlite3.connect(standard) as target:
        source.backup(target)
    reads = [
        {"id": "identity", "kind": "identity", "quota": 1, "call": grants()[0]["call"]},
        {
            "id": "existing-source",
            "kind": "read",
            "quota": 1,
            "call": grants()[1]["call"],
            "read_policy": {"phase": "execute"},
        },
    ]
    packet = root / "inputs/sample/packet.json"
    json_write(packet, {"requests": {"QUERY": "read existing sample"}, "execution_grants": {"QUERY": reads}})
    proof = {
        "kind": "ops_existing_query_review",
        "approved": True,
        "new_creation_quota": 0,
        "pass_added": False,
        "account": "123",
        "slot": 0,
        "old_scope": "job:CREATE",
        "session_id": "ws~complete",
        "retained_wire": snapshot_wire(root, "job:CREATE"),
        "packet": {"path": "inputs/sample/packet.json", "sha256": digest(packet)},
    }
    evidence = root / "inputs/sample/review.json"
    json_write(evidence, proof, private=True)
    return standard, reads, proof, evidence


def install_review(path, reads, evidence, *, session="ws~new", scope="new:QUERY"):
    sha = digest(evidence)
    gate.install(
        path,
        "0",
        scope,
        session,
        "123",
        None,
        reads,
        authority="existing-query:" + sha,
        existing_review=(str(evidence), sha),
    )


def test_existing_read_preserves_original_creates_counters_and_cannot_replenish(active):
    path, reads, proof, evidence = review(active)
    with pytest.raises(gate.GateDeniedError):
        gate.install(path, "0", "new:QUERY", "ws~new", "123", None, reads)
    install_review(path, reads, evidence)
    with gate.database(path) as db:
        for name in ("grants", "attempts"):
            assert [
                dict(r) for r in db.execute(f"SELECT * FROM {name} WHERE scope=? ORDER BY rowid", ("job:CREATE",))
            ] == proof["retained_wire"]["tables"][name]
        assert db.execute("SELECT scope FROM active WHERE slot='0'").fetchone()[0] == "new:QUERY"
    with pytest.raises(gate.GateDeniedError):
        install_review(path, reads, evidence, scope="another:QUERY")


@pytest.mark.parametrize("damage", [None, "grant", "attempt", "authority"])
def test_retained_cleanup_pointer_never_reinstalls_or_refills_original_wire(active, damage):
    from scripts.web.e2e.resource_link_workflow.existing_cleanup import retained_wire

    path, reads, proof, evidence = review(active)
    install_review(path, reads, evidence)
    with gate.database(path) as db:
        if damage == "grant":
            db.execute("UPDATE grants SET used=used+1 WHERE scope='job:CREATE' AND id='create'")
            # Fixture grant names are intentionally independent of a live product.
            db.execute("UPDATE grants SET quota=quota+1 WHERE scope='job:CREATE'")
        elif damage == "attempt":
            db.execute("UPDATE attempts SET state='unknown' WHERE scope='job:CREATE'")
        elif damage == "authority":
            db.execute("UPDATE authority_limits SET used=used+1 WHERE authority='job:CREATE'")
        if damage:
            with pytest.raises(WorkflowError):
                retained_wire(db, proof)
        else:
            assert retained_wire(db, proof) == proof["retained_wire"]["active"]
            assert db.execute("SELECT scope FROM active WHERE slot='0'").fetchone()[0] == "new:QUERY"


@pytest.mark.parametrize("damage", ["same_session", "write", "unapproved", "counter", "sha", "account"])
def test_existing_admission_fails_closed(active, damage):
    path, reads, proof, evidence = review(active)
    session = "ws~new"
    if damage == "same_session":
        session = "ws~complete"
    elif damage == "write":
        reads.append(grants()[2])
    elif damage == "counter":
        with gate.database(path) as db:
            db.execute("UPDATE authority_limits SET used=used+1")
    else:
        proof[{"unapproved": "approved", "sha": "packet", "account": "account"}[damage]] = (
            False
            if damage == "unapproved"
            else {"path": "inputs/sample/packet.json", "sha256": "bad"}
            if damage == "sha"
            else "456"
        )
        json_write(evidence, proof, private=True)
    with pytest.raises(gate.GateDeniedError):
        install_review(path, reads, evidence, session=session)


def test_exact_existing_flow_health_and_signed_response(tmp_path):
    binding = {
        "dataflow_id": "df-own",
        "file_system_id": "cpfs-own",
        "fileset_id": "fset-own",
        "file_system_path": "/sample/",
        "source_storage": "oss://sample",
    }
    row = {
        "DataFlowId": "df-own",
        "FileSystemId": "cpfs-own",
        "FsetId": "fset-own",
        "FileSystemPath": "/sample/",
        "SourceStorage": "oss://sample",
        "Status": "Running",
        "ErrorMessage": "",
    }
    validate_flow(binding, row)
    for field in row:
        broken = copy.deepcopy(row)
        broken[field] = "wrong"
        with pytest.raises(WorkflowError):
            validate_flow(binding, broken)
    gate.initialize(tmp_path / "execution-gate.sqlite3")
    with gate.database(tmp_path / "execution-gate.sqlite3") as db:
        db.execute(
            "INSERT INTO attempts(id,scope,session,grant_id,state,response) VALUES(?,?,?,?,?,?)",
            (
                "a",
                "job:QUERY",
                "ws~new",
                "existing-source",
                "succeeded",
                json.dumps({"DataFlowInfo": {"DataFlow": [row]}}),
            ),
        )
    refs = [{"identifier": "df-own"}, {"identifier": "df-other"}]
    assert references(tmp_path, {"binding": binding}, "job", "ws~new", refs) == refs[:1]
    with pytest.raises(WorkflowError):
        references(tmp_path, {"binding": binding}, "job", "ws~old", refs)


def test_reviewed_existing_query_queue_tracks_manual_owners_without_cloud_write(retained):
    runtime, task_id, _ = retained
    root = runtime.root
    old_packet = root / "packets/original/packet.json"
    old = json.loads(old_packet.read_text(encoding="utf-8"))
    old.update(ui={}, scenes={"R0036-QUERY-1-iframe": {"account": "123", "region": "cn-hangzhou"}})
    json_write(old_packet, old)
    with runtime.store.transaction() as db:
        db.execute("UPDATE jobs SET purpose='review' WHERE id='repair'")
    bodies = [
        {"AccountId": "123", "RequestId": "sts"},
        {"FileSystems": {"FileSystem": [{"FileSystemId": "cpfs-own", "Status": "Running"}]}, "RequestId": "fs"},
        {
            "DataFlowInfo": {
                "DataFlow": [
                    {
                        "DataFlowId": "df-own",
                        "FileSystemId": "cpfs-own",
                        "FsetId": "fset-own",
                        "FileSystemPath": "/sample/",
                        "SourceStorage": "oss://sample",
                        "Status": "Running",
                        "ErrorMessage": "",
                    }
                ]
            },
            "RequestId": "flow",
        },
        {
            "Entries": {"Entrie": [{"FsetId": "fset-own", "FileSystemPath": "/sample/", "Status": "CREATED"}]},
            "RequestId": "fset",
        },
    ]
    sources = []
    query = {
        "product": "nas",
        "action": "DescribeDataFlows",
        "version": "2017-06-26",
        "params": {
            "FileSystemId": "cpfs-own",
            "MaxResults": 20,
            "Filters.1.Key": "DataFlowIds",
            "Filters.1.Value": "df-own",
        },
    }
    for i, body in enumerate(bodies):
        file = root / f"inputs/raw-{i}.json"
        json_write(file, {"exit_code": 0, "body": body, "call": query if i == 2 else {}})
        sources.append({"path": str(file.relative_to(root)), "sha256": digest(file)})
    json_write(root / "inputs/human.json", {"origin": "user_manual_console_creation_not_glm_wire"})
    seed = {
        "kind": "ops_user_created_nas_sample_handoff",
        "task_id": task_id,
        "account": "123",
        "region": "cn-hangzhou",
        "file_system_id": "cpfs-own",
        "dataflow_id": "df-own",
        "fileset_id": "fset-own",
        "file_system_path": "/sample/",
        "source_storage": "oss://sample",
        "independent_reads": sources,
        "human_declaration": "inputs/human.json",
        "origin": "user_manual_console_creation_not_glm_wire",
    }
    json_write(root / "inputs/handoff.json", seed)
    before = snapshot_wire(root, "native:QUERY")
    proof = existing_query.propose(runtime, task_id, "handoff.json")
    proof["approved"] = True
    json_write(root / "inputs/review.json", proof, private=True)
    assert existing_query.queue(runtime, "review.json")["no_cloud_calls"]
    assert snapshot_wire(root, "native:QUERY") == before
    with runtime.store.transaction() as db:
        assert existing_query.load(root, db, task_id)["binding"] == proof["binding"]
        assert db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "continuation_pending"
        owners = [json.loads(r[0]) for r in db.execute("SELECT resource FROM owned WHERE task_id=?", (task_id,))]
        assert {r["id"] for r in owners} == {"owner", "implicit", "df-own", "fset-own"}
        assert len([r for r in owners if r.get("origin") == "user_manual_console_creation_not_glm_wire"]) == 2
    with pytest.raises(Exception):
        existing_query.queue(runtime, "review.json")


@pytest.mark.parametrize("damage", [None, "hash", "task", "approved", "cloud", "pass", "sampler", "source"])
def test_existing_capture_review_is_immutable_and_zero_cloud(tmp_path, damage):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
    source = tmp_path / "inputs/original.json"
    json_write(source, {"actual_ui_click": True})
    proof = {
        "kind": "ops_existing_query_capture_review",
        "task_id": "task",
        "approved": True,
        "cloud_calls": 0,
        "pass_added": False,
        "capture_recovery": {"console_inspection": "nas_dataflow_metadata"},
        "sources": [{"path": "inputs/original.json", "sha256": digest(source)}],
    }
    if damage in {"task", "approved", "cloud", "pass", "sampler"}:
        proof[
            {
                "task": "task_id",
                "approved": "approved",
                "cloud": "cloud_calls",
                "pass": "pass_added",
                "sampler": "capture_recovery",
            }[damage]
        ] = {
            "task": "other",
            "approved": False,
            "cloud": 1,
            "pass": True,
            "sampler": {"console_inspection": "arbitrary"},
        }[damage]
    file = tmp_path / "inputs/capture.json"
    json_write(file, proof, private=True)
    db.execute(
        "INSERT INTO meta VALUES(?,?)",
        (
            "existing_query_capture:task",
            json.dumps(
                {"evidence": "inputs/capture.json", "evidence_sha256": "wrong" if damage == "hash" else digest(file)}
            ),
        ),
    )
    if damage == "source":
        json_write(source, {"changed": True})
    if damage:
        with pytest.raises(WorkflowError):
            existing_query.load_capture(tmp_path, db, "task")
    else:
        assert existing_query.load_capture(tmp_path, db, "task")["cloud_calls"] == 0


@pytest.mark.parametrize(
    "operation",
    ["new-session", "request", "source", "cleanup", "cleanup-source", "continue-read", "continue-execution"],
)
def test_existing_capture_cannot_send_query_create_or_cleanup(retained, monkeypatch, operation):
    runtime, _, _ = retained
    json_write(runtime.root / "inputs/readonly.json", {"inputs": []})
    context = {
        "role": "glm",
        "purpose": "continuation",
        "slot": {"target": "own"},
        "packet_path": "inputs/readonly.json",
        "output_dir": str(runtime.root / "jobs/new/output"),
        "existing_query_recovery": {"capture_recovery": {}},
        "navigation_recovery": {"original_job_id": "original"},
    }
    monkeypatch.setattr(runtime.store, "context", lambda *_: context)
    monkeypatch.setattr(runtime, "browser", lambda *_: pytest.fail("no browser/cloud operation permitted"))
    with pytest.raises(WorkflowError):
        runtime.tool("new", "offline", operation, {"dimension": "QUERY"})
