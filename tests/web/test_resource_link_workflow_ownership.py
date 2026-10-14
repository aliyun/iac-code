"""Offline same-name generation regression; no cloud, models or user credentials."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write
from scripts.web.e2e.resource_link_workflow.ownership import evidence_generation, owner_key, register_generation


@pytest.fixture
def owners(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE jobs(id TEXT,task_id TEXT,role TEXT)")
    connection.execute(
        "CREATE TABLE owned(id TEXT PRIMARY KEY,task_id TEXT,dimension TEXT,resource TEXT,state TEXT,evidence TEXT)"
    )
    for ident, task, role in (
        ("a" * 32, "run:R1", "glm"),
        ("b" * 32, "run:R1", "glm"),
        ("c" * 32, "run:R1", "sol"),
        ("d" * 32, "other:R1", "glm"),
    ):
        connection.execute("INSERT INTO jobs VALUES(?,?,?)", (ident, task, role))
    yield connection, tmp_path
    connection.close()


def original(root, worker="a" * 32):
    relative = f"jobs/{worker}/output/source.json"
    json_write(root / relative, {"scope": worker + ":CREATE", "raw": "offline"})
    return {"path": relative, "sha256": digest(root / relative)}


def test_same_name_recreation_does_not_inherit_deleted_owner(owners):
    db, root = owners
    resource = {"id": "same-policy", "region": "cn-hangzhou", "account": "fake-account"}
    first = register_generation(db, root, "run:R1", "CREATE", resource, "a" * 32, original(root))
    db.execute("UPDATE owned SET state='deleted' WHERE id=?", (first,))
    old = db.execute("SELECT * FROM owned WHERE id=?", (first,)).fetchone()
    second = register_generation(db, root, "run:R1", "CREATE", resource, "b" * 32, original(root, "b" * 32))
    assert first != second
    assert db.execute("SELECT * FROM owned WHERE id=?", (first,)).fetchone() == old
    assert db.execute("SELECT state FROM owned WHERE id=?", (second,)).fetchone() == ("present",)
    assert register_generation(db, root, "run:R1", "CREATE", resource, "b" * 32, original(root, "b" * 32)) == second
    assert db.execute("SELECT count(*) FROM owned").fetchone() == (2,)


def test_query_sample_dependency_has_its_own_original_creation_generation(owners):
    db, root = owners
    resource = {"id": "own-vpc", "region": "cn-hangzhou", "account": "fake-account"}
    source = original(root)
    json_write(root / source["path"], {"scope": "a" * 32 + ":QUERY", "raw": "offline"})
    source["sha256"] = digest(root / source["path"])
    first = register_generation(db, root, "run:R1", "QUERY", resource, "a" * 32, source)
    assert db.execute("SELECT dimension,state FROM owned WHERE id=?", (first,)).fetchone() == ("QUERY", "present")
    assert register_generation(db, root, "run:R1", "QUERY", resource, "a" * 32, source) == first
    with pytest.raises(WorkflowError, match="original GLM"):
        register_generation(db, root, "other:R1", "QUERY", resource, "a" * 32, source)
    with pytest.raises(WorkflowError, match="exact original"):
        register_generation(db, root, "run:R1", "QUERY", resource, "b" * 32, source)


def test_legacy_cleanup_id_is_reused_only_for_its_original_generation(owners):
    db, root = owners
    resource = {"id": "same-policy", "region": "cn-hangzhou"}
    source = original(root)
    proof = "jobs/" + "c" * 32 + "/output/cleanup.json"
    json_write(root / proof, {"source_evidence": source["path"], "source_sha256": source["sha256"]})
    binding = {"path": proof, "sha256": digest(root / proof)}
    legacy = hashlib.sha256(json.dumps(["run:R1", "CREATE", resource], sort_keys=True).encode()).hexdigest()
    db.execute(
        "INSERT INTO owned VALUES(?,?,?,?,?,?)",
        (legacy, "run:R1", "CREATE", json.dumps(resource), "deleted", json.dumps(binding)),
    )
    old = db.execute("SELECT * FROM owned").fetchone()
    assert owner_key(db, root, "run:R1", "CREATE", resource, "a" * 32) == legacy
    second = register_generation(db, root, "run:R1", "CREATE", resource, "b" * 32, original(root, "b" * 32))
    assert second != legacy
    assert db.execute("SELECT * FROM owned WHERE id=?", (legacy,)).fetchone() == old


@pytest.mark.parametrize("failure", ["sha", "cross_task", "sol_only", "cycle", "missing", "scope_only"])
def test_unproven_legacy_evidence_never_aliases_a_new_generation(owners, failure):
    db, root = owners
    if failure == "scope_only":
        path = "inputs/proof.json"
        json_write(root / path, {"scope": "a" * 32 + ":CREATE"})
        binding = {"path": path, "sha256": digest(root / path)}
        assert evidence_generation(db, root, "run:R1", binding) == "a" * 32
        return
    binding = original(root, "d" * 32 if failure == "cross_task" else "c" * 32 if failure == "sol_only" else "a" * 32)
    if failure == "sha":
        binding["sha256"] = "0" * 64
    if failure == "missing":
        binding["path"] = "inputs/missing.json"
    if failure == "cycle":
        path = "inputs/cycle.json"
        json_write(root / path, {"source_evidence": path, "source_sha256": "0" * 64})
        binding = {"path": path, "sha256": digest(root / path)}
    assert evidence_generation(db, root, "run:R1", binding) is None


@pytest.mark.parametrize("generation", ["c" * 32, "d" * 32, "missing"])
def test_generation_cannot_be_a_sol_other_task_or_missing_job(owners, generation):
    db, root = owners
    with pytest.raises(WorkflowError, match="original GLM"):
        register_generation(db, root, "run:R1", "CREATE", {"id": "policy"}, generation, {})
    assert db.execute("SELECT count(*) FROM owned").fetchone() == (0,)


@pytest.mark.parametrize("mismatch", [None, "session", "tool", "resource", "unknown", "ambiguous", "sha"])
def test_legacy_raw_cleanup_binds_only_exact_successful_wire_creator(owners, monkeypatch, mismatch):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    db, root = owners
    # Placeholder presence only; wire reads are isolated below, not real configuration.
    (root / "execution-gate.sqlite3").touch()
    resource = {"id": "same-policy", "region": "cn-hangzhou"}
    path = "inputs/original-cleanup.json"
    json_write(
        root / path,
        {
            "session_id": "full-session",
            "messages": {
                "tools": {
                    "original-create": {"toolUseId": "original-create"},
                    "other-create": {"toolUseId": "other-create"},
                }
            },
        },
    )
    binding = {"path": path, "sha256": digest(root / path)}
    legacy = hashlib.sha256(json.dumps(["run:R1", "CREATE", resource], sort_keys=True).encode()).hexdigest()
    db.execute(
        "INSERT INTO owned VALUES(?,?,?,?,?,?)",
        (legacy, "run:R1", "CREATE", json.dumps(resource), "deleted", json.dumps(binding)),
    )
    record = {
        "job_id": "a" * 32,
        "dimension": "CREATE",
        "resource": resource,
        "state": "succeeded",
        "session_id": "full-session",
        "tool_use_id": "original-create",
    }
    records = [record]
    if mismatch in {"session", "tool", "unknown", "resource"}:
        key, value = {
            "session": ("session_id", "different"),
            "tool": ("tool_use_id", "missing"),
            "unknown": ("state", "unknown"),
            "resource": ("resource", {"id": "other"}),
        }[mismatch]
        record[key] = value
    if mismatch == "ambiguous":
        records.append({**record, "job_id": "b" * 32, "tool_use_id": "other-create"})
    if mismatch == "sha":
        json_write(root / path, {"changed": True})
    monkeypatch.setattr(gate, "creations", lambda *_: records)
    original_row = db.execute("SELECT * FROM owned").fetchone()
    key = owner_key(db, root, "run:R1", "CREATE", resource, "a" * 32)
    assert (key == legacy) == (mismatch is None)
    assert db.execute("SELECT * FROM owned").fetchone() == original_row
    if mismatch is None:
        assert owner_key(db, root, "run:R1", "CREATE", resource, "b" * 32) != legacy
