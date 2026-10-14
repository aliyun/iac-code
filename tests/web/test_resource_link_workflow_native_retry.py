"""Offline transport retry; no cloud grants, calls, or historical PASS changes."""
# ruff: noqa: F811

import json

import pytest

from scripts.web.e2e.resource_link_workflow import native_continuation as continuation
from scripts.web.e2e.resource_link_workflow import native_retry as retry
from scripts.web.e2e.resource_link_workflow.files import WorkflowError, digest, json_write, read_json
from tests.web.test_resource_link_execution_gate import native, native_renew, rotated_native  # noqa: F401
from tests.web.test_resource_link_workflow import reserve, token
from tests.web.test_resource_link_workflow_native_continuation import approved, retained, unchanged  # noqa: F401


@pytest.fixture
def failed(retained, monkeypatch):
    runtime, task_id, native_case = retained
    approved(runtime)
    continuation.queue(runtime, "native", "approved.json")
    runtime.store.set_review_only("Original generation only")
    runtime.store.pause(False)
    job = reserve(runtime.store, "continuation")
    runtime.store.pause(True)
    native_renew(rotated_native(native_case))
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE jobs SET state='complete',submission='confirmed',turn_id='failed-turn' WHERE id=?", (job["job_id"],)
        )
        db.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task_id,))
        binding = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone())
    folder = runtime.root / "jobs" / job["job_id"]
    folder.mkdir(parents=True, exist_ok=True)
    event = {
        "method": "turn/completed",
        "params": {"threadId": "glm-thread", "turn": {"id": "failed-turn", "status": "completed"}},
    }
    (folder / "raw-rollout.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    json_write(
        folder / "ops-thread-read-0001.json",
        {
            "method": "thread/read",
            "job_binding": binding,
            "transport_evidence": {"path": f"jobs/{job['job_id']}/raw-rollout.jsonl", "state": "present"},
            "thread": {
                "id": "glm-thread",
                "turns": [
                    {
                        "id": "failed-turn",
                        "status": "completed",
                        "items": [
                            {
                                "type": "dynamicToolCall",
                                "id": "failed-cleanup",
                                "namespace": None,
                                "arguments": {"operation": "cleanup"},
                                "status": "failed",
                                "success": False,
                                "contentItems": [
                                    {"type": "inputText", "text": "Unsupported dynamic tool namespace: null"}
                                ],
                            }
                        ],
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(runtime, "browser", lambda _: {"session_id": "ws~native", "busy": False, "unlocked": True})
    return runtime, task_id, job["job_id"]


def test_script_retry_preserves_wire_owner_and_existing_thread(failed):
    runtime, task_id, failed_job = failed
    before = unchanged(runtime)
    proposal = retry.inspect(runtime, failed_job, "repair.json")
    assert proposal["cloud_quota_added"] == 0 and proposal["attempt"] == 3
    json_write(runtime.root / "inputs/retry.json", {**proposal, "approved": True}, private=True)
    assert retry.queue(runtime, failed_job, "retry.json")["no_glm_dispatched"]
    assert unchanged(runtime) == before
    runtime.store.pause(False)
    job = reserve(runtime.store, "continuation")
    with runtime.store.transaction() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
        assert row["thread_id"] == "glm-thread"
        assert runtime.store.reviewed_continuation(db, row)
        assert db.execute("SELECT attempt FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == 4
    assert unchanged(runtime) == before


@pytest.mark.parametrize(
    "damage",
    [
        "not_paused",
        "active_job",
        "wrong_thread",
        "wrong_slot",
        "wrong_attempt",
        "owner_deleted",
        "packet_changed",
        "observed",
        "raw_changed",
        "busy_web",
        "wrong_session",
        "unapproved",
        "used_approval",
        "grant_changed",
    ],
)
def test_script_retry_refuses_drift_without_wire_mutation(failed, damage, monkeypatch):
    runtime, task_id, failed_job = failed
    proposal = retry.inspect(runtime, failed_job, "repair.json")
    json_write(runtime.root / "inputs/retry.json", {**proposal, "approved": True}, private=True)
    with runtime.store.transaction() as db:
        if damage == "not_paused":
            db.execute("UPDATE meta SET value='false' WHERE key='paused'")
        elif damage == "active_job":
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (failed_job,))
        elif damage == "wrong_thread":
            db.execute("UPDATE jobs SET thread_id='wrong' WHERE id=?", (failed_job,))
        elif damage == "wrong_slot":
            db.execute("UPDATE slots SET target='wrong' WHERE id=0")
        elif damage == "wrong_attempt":
            db.execute("UPDATE tasks SET attempt=4 WHERE id=?", (task_id,))
        elif damage == "owner_deleted":
            db.execute("UPDATE owned SET state='deleted'")
        elif damage == "observed":
            db.execute("INSERT INTO observations VALUES('observed',?,3,'scene','{}')", (task_id,))
        elif damage == "used_approval":
            db.execute("INSERT INTO meta VALUES(?, '{}')", ("native_script_retry:" + task_id,))
    if damage == "packet_changed":
        (runtime.root / "packets/original/packet.json").write_text("{}", encoding="utf-8")
    elif damage == "raw_changed":
        path = runtime.root / proposal["failed_terminal"]["path"]
        raw = read_json(path)
        raw["thread"]["turns"][0]["items"][0]["success"] = True
        json_write(path, raw)
    elif damage in {"busy_web", "wrong_session"}:
        monkeypatch.setattr(
            runtime,
            "browser",
            lambda _: {
                "session_id": "wrong" if damage == "wrong_session" else "ws~native",
                "busy": damage == "busy_web",
                "unlocked": True,
            },
        )
    elif damage == "unapproved":
        json_write(runtime.root / "inputs/retry.json", proposal, private=True)
    elif damage == "grant_changed":
        from scripts.web.e2e.resource_link_workflow import execution_gate as gate

        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            db.execute("UPDATE grants SET quota=quota+1 WHERE id='read-filesystem-ready'")
    before = unchanged(runtime)
    with pytest.raises(WorkflowError):
        retry.queue(runtime, failed_job, "retry.json")
    assert unchanged(runtime) == before


@pytest.fixture
def visually_reviewed(failed):
    runtime, task_id, failed_job = failed
    proposal = retry.inspect(runtime, failed_job, "repair.json")
    json_write(runtime.root / "inputs/retry.json", {**proposal, "approved": True}, private=True)
    retry.queue(runtime, failed_job, "retry.json")
    runtime.store.pause(False)
    source_job = reserve(runtime.store, "continuation")["job_id"]
    runtime.store.pause(True)
    image_path = f"jobs/{source_job}/output/QUERY-iframe.png"
    image = runtime.root / image_path
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"offline immutable screenshot fixture")
    scene = "R0036-QUERY-1-iframe"
    visual = {
        "kind": "native_screenshot_review",
        "task_id": task_id,
        "job_id": "visual-review",
        "source_job_id": source_job,
        "navigation_verified": True,
        "verified_scenes": [{"scene": scene, "screenshot": image_path, "sha256": digest(image)}],
    }
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE jobs SET state='complete',submission='confirmed',turn_id='click-turn' WHERE id=?", (source_job,)
        )
        db.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task_id,))
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "visual-review",
                task_id,
                "sol",
                "review",
                "complete",
                "sol-thread",
                "visual-turn",
                "fake",
                "confirmed",
                9999999999999,
            ),
        )
        db.execute(
            "INSERT INTO observations VALUES(?,?,?,?,?)",
            (
                "click",
                task_id,
                4,
                scene,
                json.dumps({"actual_ui_click": True, "screenshot": image_path}),
            ),
        )
        db.execute("INSERT INTO inspected VALUES(?,?,?)", ("visual-review", image_path, digest(image)))
        bindings = [
            dict(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone())
            for ident in (source_job, "visual-review")
        ]
    for row in bindings:
        directory = runtime.root / "jobs" / row["id"]
        trace = directory / "raw-rollout.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": row["thread_id"],
                        "turn": {"id": row["turn_id"], "status": "completed"},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        json_write(
            directory / "ops-thread-read-0001.json",
            {
                "method": "thread/read",
                "job_binding": row,
                "transport_evidence": {"path": str(trace.relative_to(runtime.root)), "state": "present"},
                "thread": {"id": row["thread_id"], "turns": [{"id": row["turn_id"], "status": "completed"}]},
            },
        )
    json_write(runtime.root / "inputs/visual-review.json", visual)
    return runtime, task_id, source_job


def test_native_cleanup_requires_sol_pixels_then_reuses_same_attempt_and_thread(visually_reviewed, monkeypatch):
    runtime, task_id, source_job = visually_reviewed
    monkeypatch.setattr(
        "scripts.web.e2e.resource_link_workflow.temporary_samples.admitted", lambda *a, **kw: {"QUERY": {}}
    )
    before = unchanged(runtime)
    proposal = retry.inspect_cleanup(runtime, source_job, "visual-review.json")
    assert proposal["pass_added"] is False and proposal["cloud_quota_added"] == 0
    json_write(runtime.root / "inputs/cleanup.json", {**proposal, "approved": True}, private=True)
    assert retry.queue_cleanup(runtime, source_job, "cleanup.json")["only_luna_dispatches"]
    assert unchanged(runtime) == before
    runtime.store.pause(False)
    job = reserve(runtime.store, "cleanup")
    context = runtime.store.context(job["job_id"], token(runtime.store, job))
    assert context["attempt"] == 4 and context["expected_scenes"] == []
    assert context["cleanup_recovery"]["native"] is True
    assert context["native_continuation"]["original_job_id"] == "native"
    assert runtime.store.status()["jobs"][0]["thread_id"] == "glm-thread"


@pytest.mark.parametrize("damage", [None, "kind", "job", "source", "scene", "uninspected", "wrong_path", "handoff"])
def test_review_can_append_only_own_inspected_screenshot_handoff(visually_reviewed, damage):
    from scripts.web.e2e.resource_link_workflow.screenshot_review import validate

    runtime, task_id, _ = visually_reviewed
    value = read_json(runtime.root / "inputs/visual-review.json")
    with runtime.store.transaction() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    context = {
        "purpose": "review",
        "role": "sol",
        "job_id": "visual-review",
        "task_id": task_id,
        "resource": {"id": task["resource_id"]},
        "attempt": 4,
        "native_cleanup_order": True,
    }
    relative = task["resource_id"] + "/new-review.json"
    if damage == "kind":
        value["kind"] = "arbitrary_source"
    elif damage == "job":
        value["job_id"] = "another-sol"
    elif damage == "source":
        value["source_job_id"] = "native"
    elif damage == "scene":
        value["verified_scenes"] = []
    elif damage == "uninspected":
        with runtime.store.transaction() as db:
            db.execute("DELETE FROM inspected")
    elif damage == "wrong_path":
        relative = "other/new-review.json"
    elif damage == "handoff":
        context["native_cleanup_order"] = None
    if damage:
        with pytest.raises(WorkflowError):
            validate(runtime.root, runtime.store, context, relative, json.dumps(value))
    else:
        assert validate(runtime.root, runtime.store, context, relative, json.dumps(value)) == value
        with runtime.store.transaction() as db:
            assert validate(runtime.root, runtime.store, context, relative, json.dumps(value), connection=db) == value


@pytest.mark.parametrize(
    "damage",
    [
        "uninspected",
        "png_changed",
        "not_actual",
        "wrong_source",
        "wrong_sol",
        "incomplete_review",
        "unapproved",
        "active",
        "phase",
    ],
)
def test_native_cleanup_rejects_unreviewed_or_changed_evidence(visually_reviewed, damage):
    runtime, task_id, source_job = visually_reviewed
    proposal = retry.inspect_cleanup(runtime, source_job, "visual-review.json")
    json_write(runtime.root / "inputs/cleanup.json", {**proposal, "approved": True}, private=True)
    with runtime.store.transaction() as db:
        if damage == "uninspected":
            db.execute("DELETE FROM inspected")
        elif damage == "not_actual":
            row = db.execute("SELECT data FROM observations WHERE task_id=?", (task_id,)).fetchone()
            data = {**json.loads(row[0]), "actual_ui_click": False}
            db.execute("UPDATE observations SET data=? WHERE task_id=?", (json.dumps(data), task_id))
        elif damage == "active":
            db.execute("UPDATE jobs SET state='running' WHERE id='visual-review'")
    if damage == "png_changed":
        (runtime.root / proposal["verified_scenes"][0]["screenshot"]).write_bytes(b"changed")
    elif damage in {"wrong_source", "wrong_sol", "incomplete_review"}:
        path = runtime.root / "inputs/visual-review.json"
        value = read_json(path)
        value.update(
            {
                "wrong_source": {"source_job_id": "native"},
                "wrong_sol": {"job_id": "repair"},
                "incomplete_review": {"verified_scenes": []},
            }[damage]
        )
        json_write(path, value)
    elif damage == "unapproved":
        json_write(runtime.root / "inputs/cleanup.json", proposal, private=True)
    elif damage == "phase":
        from scripts.web.e2e.resource_link_workflow import execution_gate as gate

        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            db.execute("UPDATE active SET phase='cleanup'")
    before = unchanged(runtime)
    with pytest.raises(WorkflowError):
        retry.queue_cleanup(runtime, source_job, "cleanup.json")
    assert unchanged(runtime) == before


def test_native_source_turn_cannot_start_cleanup_before_sol_review(failed, monkeypatch):
    runtime, _, failed_job = failed
    context = {
        "role": "glm",
        "purpose": "continuation",
        "packet_path": "packets/original/packet.json",
        "slot": {"target": "target-0"},
        "native_continuation": {"original_job_id": "native"},
    }
    monkeypatch.setattr(runtime.store, "context", lambda *_: context)
    before = unchanged(runtime)
    with pytest.raises(WorkflowError, match="Sol must inspect"):
        runtime.tool(failed_job, "offline", "cleanup", {"dimension": "QUERY"})
    assert unchanged(runtime) == before


@pytest.fixture
def repaired_gap(failed):
    runtime, task_id, failed_job = failed
    proposal = retry.inspect(runtime, failed_job, "repair.json")
    json_write(runtime.root / "inputs/retry.json", {**proposal, "approved": True}, private=True)
    retry.queue(runtime, failed_job, "retry.json")
    runtime.store.pause(False)
    job_id = reserve(runtime.store, "continuation")["job_id"]
    runtime.store.pause(True)
    folder = runtime.root / "jobs" / job_id
    image = folder / "output/gap.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"offline diagnostic only")
    data = {"actual_ui_click": False, "session_id": "ws~native", "screenshot": str(image.relative_to(runtime.root))}
    json_write(folder / "output/gap.json", data)
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE jobs SET state='complete',submission='result_staged',turn_id='gap-turn' WHERE id=?", (job_id,)
        )
        db.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task_id,))
        db.execute("INSERT INTO observations VALUES('gap',?,4,'gap',?)", (task_id, json.dumps(data)))
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "fresh-repair",
                task_id,
                "sol",
                "repair",
                "complete",
                "sol-thread",
                "fresh-turn",
                "fake",
                "result_staged",
                9999999999999,
            ),
        )
        bindings = [
            dict(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()) for ident in (job_id, "fresh-repair")
        ]
    for row in bindings:
        directory = runtime.root / "jobs" / row["id"]
        directory.mkdir(parents=True, exist_ok=True)
        params = {"jobId": row["id"], "threadId": row["thread_id"], "turnId": row["turn_id"]}
        events = [
            {"method": "script/request", "params": {**params, "operation": "submit", "arguments": {}}},
            {
                "method": "turn/completed",
                "params": {"threadId": row["thread_id"], "turn": {"id": row["turn_id"], "status": "completed"}},
            },
        ]
        trace = directory / "raw-rollout.jsonl"
        trace.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        json_write(
            directory / "ops-thread-read-0001.json",
            {
                "method": "thread/read",
                "job_binding": row,
                "transport_evidence": {"path": str(trace.relative_to(runtime.root)), "state": "present"},
                "thread": {"id": row["thread_id"], "turns": [{"id": row["turn_id"], "status": "completed"}]},
            },
        )
    original = {k: bindings[0][k] for k in ("id", "thread_id", "turn_id")}
    diagnosis = runtime.root / "inputs/gap-diagnosis.json"
    json_write(diagnosis, {"kind": "ops_preserved_product_gap", "original_job": original})
    repair = read_json(runtime.root / "inputs/repair.json")
    json_write(
        runtime.root / "inputs/fresh-repair.json",
        {
            **repair,
            "job_id": "fresh-repair",
            "repair_request": {"path": str(diagnosis.relative_to(runtime.root)), "sha256": digest(diagnosis)},
        },
    )
    return runtime, task_id, job_id


def test_fresh_repaired_gap_keeps_original_history_and_wire(repaired_gap):
    runtime, task_id, job_id = repaired_gap
    before = unchanged(runtime)
    prior = read_json(runtime.root / "inputs/retry.json")
    proposal = retry.inspect(runtime, job_id, "fresh-repair.json")
    assert proposal["transport_failure"]["kind"] == "repaired_execution_gap"
    json_write(runtime.root / "inputs/retry-gap.json", {**proposal, "approved": True}, private=True)
    retry.queue(runtime, job_id, "retry-gap.json")
    assert read_json(runtime.root / "inputs/retry.json") == prior
    assert unchanged(runtime) == before
    with pytest.raises(WorkflowError):
        retry.queue(runtime, job_id, "retry-gap.json")
    runtime.store.pause(False)
    job = reserve(runtime.store, "continuation")
    with runtime.store.transaction() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone()
        assert row["thread_id"] == "glm-thread" and runtime.store.reviewed_continuation(db, row)
        assert db.execute("SELECT attempt FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM meta WHERE key LIKE 'native_script_retry_history:%'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "actual",
        "raw_changed",
        "wrong_session",
        "png_changed",
        "click_intent",
        "cleanup_intent",
        "unknown_script",
        "wrong_job",
        "old_repair",
        "wrong_diagnosis",
        "missing_original_owner",
        "unbound_extra_owner",
        "old_history_changed",
    ],
)
def test_repaired_gap_refuses_replay_or_unbound_repair(repaired_gap, damage):
    runtime, task_id, job_id = repaired_gap
    proposal = retry.inspect(runtime, job_id, "fresh-repair.json")
    json_write(runtime.root / "inputs/retry-gap.json", {**proposal, "approved": True}, private=True)
    with runtime.store.transaction() as db:
        if damage in {"actual", "wrong_session"}:
            row = db.execute("SELECT data FROM observations WHERE id='gap'").fetchone()
            data = json.loads(row[0])
            data.update({"actual_ui_click": True} if damage == "actual" else {"session_id": "foreign"})
            db.execute("UPDATE observations SET data=? WHERE id='gap'", (json.dumps(data),))
        elif damage == "missing_original_owner":
            db.execute("UPDATE owned SET state='deleted' WHERE id='owner'")
        elif damage == "unbound_extra_owner":
            db.execute("INSERT INTO owned VALUES('foreign',?,'QUERY','{}','present','{}')", (task_id,))
    if damage == "raw_changed":
        json_write(runtime.root / f"jobs/{job_id}/output/gap.json", {})
    elif damage == "png_changed":
        (runtime.root / f"jobs/{job_id}/output/gap.png").write_bytes(b"changed")
    elif damage in {"click_intent", "cleanup_intent", "unknown_script", "wrong_job"}:
        trace = runtime.root / f"jobs/{job_id}/raw-rollout.jsonl"
        events = [json.loads(s) for s in trace.read_text(encoding="utf-8").splitlines()]
        params = events[0]["params"].copy()
        if damage == "wrong_job":
            events[0]["params"]["jobId"] = "foreign"
        elif damage == "unknown_script":
            events.insert(0, {"method": "script/rejected", "params": {"error": "unknown transport"}})
        else:
            events.insert(
                0,
                {
                    "method": "script/request",
                    "params": {**params, "operation": "click" if damage == "click_intent" else "cleanup"},
                },
            )
        trace.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    elif damage in {"old_repair", "wrong_diagnosis"}:
        path = runtime.root / "inputs/fresh-repair.json"
        value = read_json(path)
        if damage == "old_repair":
            value["job_id"] = "repair"
        else:
            value["repair_request"]["sha256"] = "changed"
        json_write(path, value)
    elif damage == "old_history_changed":
        json_write(runtime.root / "inputs/retry.json", {})
    before = unchanged(runtime)
    with pytest.raises(WorkflowError):
        retry.queue(runtime, job_id, "retry-gap.json")
    assert unchanged(runtime) == before


@pytest.mark.parametrize("damage", [None, "source", "session", "resource", "unknown_write"])
def test_repaired_gap_retains_only_own_signed_additional_creation(repaired_gap, retained, damage):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate
    from tests.web.test_resource_link_execution_gate import native_wire

    runtime, task_id, job_id = repaired_gap
    native_wire(rotated_native(retained[2]), "create-fileset")
    record = next(
        r
        for r in gate.creations(runtime.root / "execution-gate.sqlite3", ["native"])
        if r["state"] == "succeeded" and r["response"]["call"]["action"] == "CreateFileset"
    )
    path = runtime.root / "jobs/native/output/fileset.json"
    json_write(path, record)
    binding = {"path": str(path.relative_to(runtime.root)), "sha256": digest(path)}
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO owned VALUES('fileset',?,'QUERY',?,'present',?)",
            (task_id, json.dumps(record["resource"]), json.dumps(binding)),
        )
    proposal = retry.inspect(runtime, job_id, "fresh-repair.json")
    json_write(runtime.root / "inputs/retry-gap.json", {**proposal, "approved": True}, private=True)
    if damage in {"source", "session", "resource"}:
        altered = dict(record)
        if damage == "source":
            altered["attempt_id"] = "foreign"
        elif damage == "session":
            altered["session_id"] = "foreign"
        else:
            altered["resource"] = {"id": "foreign"}
        json_write(path, altered)
    elif damage == "unknown_write":
        with gate.database(runtime.root / "execution-gate.sqlite3") as db:
            db.execute("UPDATE attempts SET state='unknown' WHERE grant_id='create-fileset'")
    before = unchanged(runtime)
    if damage:
        with pytest.raises(WorkflowError):
            retry.queue(runtime, job_id, "retry-gap.json")
    else:
        assert retry.queue(runtime, job_id, "retry-gap.json")["no_glm_dispatched"]
    assert unchanged(runtime) == before


@pytest.mark.parametrize("phase", ["source", "cleanup"])
def test_native_recovery_classifies_original_negative_without_mutation(visually_reviewed, retained, phase):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate
    from tests.web.test_resource_link_execution_gate import native_wire

    runtime, _, source_job = visually_reviewed
    original_native = retained[2]
    current = rotated_native(original_native)
    native_wire(current, "precheck-bucket-absent")
    before = unchanged(runtime)
    with gate.database(runtime.root / "execution-gate.sqlite3") as db:
        rows = list(db.execute("SELECT * FROM attempts"))
        assert retry.resolved_wire(db, rows)
        assert next(r for r in rows if r["grant_id"] == "precheck-bucket-absent")["state"] == "unknown"
    if phase == "cleanup":
        assert retry.inspect_cleanup(runtime, source_job, "visual-review.json")["cloud_quota_added"] == 0
    assert unchanged(runtime) == before


@pytest.mark.parametrize("damage", [None, "execute_phase", "dry_run", "code", "request_id", "host", "unknown_create"])
def test_original_rejected_dry_run_allows_only_cleanup_not_creation(native, damage):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate
    from tests.web.test_resource_link_execution_gate import native_empty_export, native_wire

    native_empty_export(native)
    body = {
        "Code": "OperationDenied.FsetAlreadyInUse",
        "RequestId": "offline-rejected",
        "HostId": "nas.cn-hangzhou.aliyuncs.com",
    }
    native_wire(native, "precheck-dataflow", body=body, status=403, succeed=False)
    gate.cleanup(native[0], "0", "native:QUERY", "ws~native")
    with gate.database(native[0]) as db:
        row = db.execute("SELECT * FROM attempts WHERE grant_id='precheck-dataflow'").fetchone()
        value = json.loads(row["response"])
        value["headers"]["x-acs-request-id"] = "offline-rejected"
        if damage == "dry_run":
            value["call"]["params"]["DryRun"] = False
        elif damage == "code":
            value["body"]["Code"] = "AccessDenied"
        elif damage == "request_id":
            value["headers"]["x-acs-request-id"] = "wrong"
        elif damage == "host":
            value["body"]["HostId"] = "foreign"
        elif damage == "execute_phase":
            db.execute("UPDATE active SET phase='execute'")
        elif damage == "unknown_create":
            db.execute(
                "INSERT INTO attempts(id,scope,session,grant_id,state) VALUES(?,?,?,?,?)",
                ("unknown-write", "native:QUERY", "ws~native", "create-dataflow", "unknown"),
            )
        db.execute("UPDATE attempts SET response=? WHERE id=?", (json.dumps(value), row["id"]))
        joined = db.execute(
            "SELECT a.*,g.kind FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id WHERE a.id=?",
            (row["id"],),
        ).fetchone()
        assert retry.rejected_dry_run_cleanup(db, joined) is (damage is None)
        assert joined["state"] == "unknown"  # Never turn the error into a passing precheck.
