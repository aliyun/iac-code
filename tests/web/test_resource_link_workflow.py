"""Offline state-machine contracts; no actual LLM, cloud, Chrome or user config."""

from __future__ import annotations

import base64
import copy
import json
import socket
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.web.e2e.resource_link_workflow.files import WorkflowError, contained, digest, json_write, read_json
from scripts.web.e2e.resource_link_workflow.plans import validate_plan
from scripts.web.e2e.resource_link_workflow.reports import make_plan, render_matrix, render_results
from scripts.web.e2e.resource_link_workflow.runtime import Runtime, exclusive, navigation_identifier
from scripts.web.e2e.resource_link_workflow.server import Service, reconcile
from scripts.web.e2e.resource_link_workflow.store import Store

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jN1sAAAAASUVORK5CYII=")
REPO = Path(__file__).resolve().parents[2]
WORKFLOW_DATA = Path("synthetic-workflow-data-created-by-fixture")


@pytest.mark.parametrize("purpose", ["prepare", "repair"])
@pytest.mark.parametrize("created_before", [False, True])
def test_official_resolution_cannot_skip_unverified_creation(tmp_path, purpose, created_before):
    store = Store.initialize(tmp_path / "state")
    spec = resource()
    spec["dimensions"]["ROS"] = {
        "support": "supported", "creation_verified": created_before,
        "contracts": [{"id": "R0001-ROS-1", "source_type": spec["key"],
                       "historical": {"iframe": "pass", "open": "pass"}}],
    }
    store.import_plan({"schema_version": 1, "plan_id": "offline-checkpoint", "resources": [spec]})
    store.pause(False)
    job = reserve(store, "prepare")
    token, _, _ = bound(store, job)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET purpose=? WHERE id=?", (purpose, job["job_id"]))
    json_write(store.root / "inputs/official.json", {"fixture": "applicability, not creation proof"})
    dimensions = copy.deepcopy(spec["dimensions"])
    for source in dimensions.values():
        source["official_evidence"] = "inputs/official.json"
    dimensions["ROS"]["creation_verified"] = not created_before
    with pytest.raises(WorkflowError, match="creation checkpoint"):
        Runtime(store).tool(job["job_id"], token, "resolve", {"dimensions": dimensions})
    with store.transaction() as db:
        actual = json.loads(db.execute("SELECT spec FROM tasks").fetchone()[0])
    assert actual["dimensions"]["ROS"]["creation_verified"] is created_before
    from scripts.web.e2e.resource_link_workflow.plans import expected_scenes

    assert ("R0001-ROS-creation" in {scene["id"] for scene in expected_scenes(actual)}) is not created_before


def test_reusable_assets_and_cli_defaults_do_not_depend_on_docs():
    from scripts.web.e2e.resource_link_workflow.__main__ import parser

    directory = REPO / "scripts/web/e2e/resource_link_workflow"
    for name in (
        "README.zh-CN.md",
        "TEST_GUIDE.zh-CN.md",
        "LUNA.zh-CN.md",
        "SOL.zh-CN.md",
        "GLM.zh-CN.md",
        "OPS.zh-CN.md",
        "PLAN_GENERATION.zh-CN.md",
        "catalog.example.json",
        "environment.example.json",
        "roles.json",
    ):
        assert (directory / name).is_file()
    assert read_json(directory / "roles.json") == {
        "luna": {"model": "gpt-6-luna", "effort": "high"},
        "sol": {"model": "gpt-6.1-sol", "effort": "high"},
        "glm": {"model": "glm-5.3-prime", "effort": "xhigh"},
    }
    plan_args = parser().parse_args(["plan", "--id", "fixture", "--resources", "R0001", "--output", "fixture.json"])
    assert (REPO / plan_args.catalog) == directory / "catalog.json"
    assert (REPO / parser().parse_args(["report"]).output) == directory / "resource-link-results.html"


def test_catalog_example_contains_no_acceptance_or_creation_claims():
    from tests.web.resource_link_data import EXAMPLE

    example = read_json(EXAMPLE)
    validate_plan(example)
    assert len(example["resources"]) == 87
    for resource in example["resources"]:
        for source in resource["dimensions"].values():
            assert source["support"] == "unknown"
            assert source["creation_verified"] is False
            for contract in source["contracts"]:
                assert contract["historical"] == {"iframe": "untested", "open": "untested"}


def test_machine_acceptance_data_is_not_part_of_the_committed_tree():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO, encoding="utf-8").split("\0")
    tracked = set(paths)
    private = {
        "resource_link_backfill_gate.json", "resource_link_e2e_matrix.json",
        "resource_link_e2e_applicability.json", "resource_link_e2e_create_fixtures.json",
        "resource_link_e2e_history.json", "resource_link_e2e_query_replay.json",
        "resource_link_e2e_route_cloud_sources.json", "resource_link_e2e_status.json",
        "resource_link_e2e_targeted_query_sources.json", "resource_link_e2e_web_source_registry.json",
        "resource_link_workflow/catalog.json", "resource_link_workflow/historical-results.json",
        "resource_link_workflow/resource-link-results.html", "resource_link_workflow/resource-link-test-matrix.html",
    }
    assert not {"scripts/web/e2e/" + path for path in private} & tracked


def test_port_probe_does_not_adopt_or_collide_with_another_listener():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with pytest.raises(WorkflowError, match="occupied"):
            Runtime.require_free_port(listener.getsockname()[1])


def test_new_state_is_wal_before_first_concurrent_slot_request(tmp_path):
    root = tmp_path / "fresh-state"
    store = Store.initialize(root)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT count(*) FROM slots").fetchone()[0] == 4


def test_four_browser_targets_do_not_contend_on_one_global_lock(store, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    runtime = Runtime(store)
    barrier = threading.Barrier(4)

    def operation(*_, input, **kwargs):
        value = json.loads(input)
        assert value["foreground_lock"] == str(store.root / "browser-locks" / "foreground.lock")
        barrier.wait(timeout=5)
        return subprocess.CompletedProcess([], 0, stdout=json.dumps({"target": value["target"]}))

    monkeypatch.setattr(subprocess, "run", operation)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda index: runtime.browser(
                    {
                        "operation": "snapshot",
                        "origin": f"http://127.0.0.1:{16100 + index}",
                        "target": f"target-{index}",
                        "foreground_lock": "untrusted-lock-path",
                    }
                ),
                range(4),
            )
        )
    assert [value["target"] for value in results] == [f"target-{index}" for index in range(4)]
    assert len(list((store.root / "browser-locks").glob("*.lock"))) == 4


def test_same_browser_target_remains_exclusive(store, monkeypatch):
    import hashlib

    runtime = Runtime(store)
    request = {"operation": "snapshot", "origin": "http://127.0.0.1:16100", "target": "original-target"}
    page_key = hashlib.sha256(json.dumps([request["origin"], request["target"]], sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("same target already owned"))
    with exclusive(store.root / "browser-locks" / f"{page_key}.lock"):
        with pytest.raises(WorkflowError, match="already owned"):
            runtime.browser(request)


def test_control_attestation_is_import_time_not_disk_only(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import runtime as module

    runtime = Runtime(store)
    proof = runtime.attest_control()
    assert proof["loaded_sha256"] == digest(Path(module.__file__))
    assert proof["disk_matches_loaded"] is True
    monkeypatch.setattr(module, "RUNTIME_LOADED_SHA", "stale-import")
    with pytest.raises(WorkflowError, match="restart the same Server"):
        runtime.attest_control()


@pytest.mark.parametrize("component", ["runtime", "server"])
def test_code_reload_is_observable_wait_not_false_server_outage(store, monkeypatch, component):
    from scripts.web.e2e.resource_link_workflow import runtime, server

    service = Service(store)
    if component == "runtime":
        monkeypatch.setattr(runtime, "RUNTIME_LOADED_SHA", "stale-import")
    else:
        monkeypatch.setattr(server, "SERVER_LOADED_SHA", "stale-import")
    status = service.request({"operation": "status"})
    assert status["control_runtime"]["ready"] is False
    assert status["control_runtime"]["requires_ops"] is True
    assert "restart the same Server" in status["control_runtime"]["reason"]
    before = store.status()["revision"]
    result = service.request({"operation": "next"})
    assert result["type"] == "wait" and result["actions"] == [] and result["maintenance"] is True
    assert store.status()["revision"] == before
    assert service.request({"operation": "pause", "arguments": {"paused": True}}) == {"paused": True}
    for operation, arguments in (("pause", {"paused": False}), ("execute", {"action_id": "unused"})):
        with pytest.raises(WorkflowError, match="restart the same Server"):
            service.request({"operation": operation, "arguments": arguments})
    with pytest.raises(WorkflowError, match="restart the same Server"):
        service.launch("unused")


@pytest.mark.parametrize("http_status", [401, 409, 500])
def test_authenticated_http_rejection_never_starts_second_server(tmp_path, monkeypatch, http_status):
    import urllib.error

    from scripts.web.e2e.resource_link_workflow import __main__ as cli

    def rejected(*_):
        raise urllib.error.HTTPError("http://127.0.0.1/control", http_status, "fixture", {}, None)

    monkeypatch.setattr(cli, "call", rejected)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("no duplicate launch"))
    with pytest.raises(WorkflowError, match="Existing Server rejected status"):
        cli.ensure(tmp_path / "missing-state", 16099)


def test_control_attestation_includes_loaded_creation_review(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import creation

    runtime = Runtime(store)
    assert runtime.attest_control()["creation_review"]["loaded_sha256"] == digest(Path(creation.__file__))
    monkeypatch.setattr(creation, "CREATION_LOADED_SHA", "stale-import")
    with pytest.raises(WorkflowError, match="creation review code changed"):
        runtime.attest_control()


def test_control_attestation_includes_loaded_scheduler(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import store as module

    runtime = Runtime(store)
    assert runtime.attest_control()["scheduler"]["loaded_sha256"] == digest(Path(module.__file__))
    monkeypatch.setattr(module, "STORE_LOADED_SHA", "stale-import")
    with pytest.raises(WorkflowError, match="scheduler code changed"):
        runtime.attest_control()


def test_control_attestation_includes_loaded_execution_phases(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import phases

    runtime = Runtime(store)
    assert runtime.attest_control()["execution_phases"]["loaded_sha256"] == digest(Path(phases.__file__))
    monkeypatch.setattr(phases, "LOADED_SHA", "stale-import")
    with pytest.raises(WorkflowError, match="execution phase code changed"):
        runtime.attest_control()


def test_control_attestation_includes_loaded_worker_wait(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import delays

    runtime = Runtime(store)
    assert runtime.attest_control()["worker_wait"]["loaded_sha256"] == digest(Path(delays.__file__))
    monkeypatch.setattr(delays, "LOADED_SHA", "stale-import")
    with pytest.raises(WorkflowError, match="worker wait code changed"):
        runtime.attest_control()


def test_cleanup_inspection_is_ops_only_and_rejects_missing_original_owned_slot(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.__main__ import parser

    parsed = parser().parse_args(["inspect-cleanup", "a" * 32, "--dimension", "ROS"])
    assert parsed.job_id == "a" * 32 and parsed.dimension == "ROS"
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    with pytest.raises(WorkflowError, match="GLM operation is not allowed"):
        Runtime(store).tool(worker["job_id"], key, "inspect-cleanup", {})
    with pytest.raises(WorkflowError, match="same task model jobs terminal"):
        Runtime(store).inspect_cleanup(worker["job_id"], "ROS")
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET state='complete' WHERE id=?", (worker["job_id"],))
        connection.execute("UPDATE tasks SET slot=NULL")
    monkeypatch.setattr(Runtime, "browser", lambda *_: pytest.fail("no slot means no UI operation"))
    with pytest.raises(WorkflowError, match="retain its owned execution slot"):
        Runtime(store).inspect_cleanup(worker["job_id"], "ROS")


@pytest.mark.parametrize("condition", ["not_paused", "live_models", "busy", "locked", "changed_session"])
def test_same_web_restart_keeps_retained_lease_and_rejects_unsafe_recovery(store, monkeypatch, condition):
    worker, context, _ = cleanup_queue_fixture(store)
    store.pause(condition != "not_paused")
    original = store.status()
    if condition == "live_models":
        with store.transaction() as connection:
            connection.execute("UPDATE jobs SET state='queued' WHERE id=?", (worker["job_id"],))
    calls = []
    snapshots = [
        {"unlocked": condition != "locked", "busy": condition == "busy", "session_id": "ws~original"},
        {
            "unlocked": True,
            "busy": False,
            "session_id": "ws~other" if condition == "changed_session" else "ws~original",
        },
    ]
    monkeypatch.setattr(Runtime, "browser", lambda *_: snapshots.pop(0))
    monkeypatch.setattr(Runtime, "stop_web", lambda *a, **kw: calls.append(("stop", kw)))
    monkeypatch.setattr(Runtime, "start_web", lambda *a: calls.append(("start", {})) or {"slot": 0})
    with pytest.raises(WorkflowError):
        Runtime(store).restart_web(0)
    assert calls == (
        [] if condition != "changed_session" else [("stop", {"_retained_maintenance": True}), ("start", {})]
    )
    assert store.status()["slots"] == original["slots"]
    assert store.status()["tasks"][0]["attempt"] == context["attempt"]


def test_same_web_restart_preserves_drained_retained_owned_lease(store, monkeypatch):
    _, context, _ = cleanup_queue_fixture(store)
    store.pause(True)
    before = store.status()
    browser_calls = []

    def browser(_self, request):
        browser_calls.append(request)
        return {
            "unlocked": True,
            "busy": False,
            "session_id": "ws~original",
            "original_page_reloaded": request["operation"] == "refresh-session",
        }

    monkeypatch.setattr(Runtime, "browser", browser)
    calls = []
    monkeypatch.setattr(Runtime, "stop_web", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(Runtime, "start_web", lambda *a: {"slot": 0})
    result = Runtime(store).restart_web(0)
    assert result["ownership_lease_retained"] and result["no_cloud_calls"] and result["same_session_preserved"]
    assert result["original_page_reloaded"]
    assert [call["operation"] for call in browser_calls] == ["snapshot", "refresh-session"]
    assert browser_calls[1]["session_id"] == "ws~original"
    assert calls == [{"_retained_maintenance": True}]
    assert store.status()["slots"] == before["slots"]
    assert store.status()["tasks"][0]["attempt"] == context["attempt"]


def test_glm_cannot_refresh_or_restore_browser_conversation(store, monkeypatch):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    monkeypatch.setattr(Runtime, "browser", lambda *_: pytest.fail("worker cannot refresh OPS session"))
    with pytest.raises(WorkflowError, match="GLM operation is not allowed"):
        Runtime(store).tool(worker["job_id"], key, "refresh-session", {"session_id": "ws~original"})


def cleanup_queue_fixture(store, count=1):
    plan(store, count=count)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    context = store.context(worker["job_id"], key)
    store.submit(worker["job_id"], key, "Offline original worker stopped before deletion")
    store.finish_job(worker["job_id"], thread, turn, "completed")
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (context["task_id"],))
    return worker, context, thread


@pytest.mark.parametrize("same_task", [False, True])
def test_cleanup_recovery_isolates_other_resources_but_requires_own_jobs_terminal(store, monkeypatch, same_task):
    from scripts.web.e2e.resource_link_workflow import cleanup_recovery

    worker, context, _ = cleanup_queue_fixture(store, count=2)
    other = reserve(store, "prepare")
    if same_task:
        with store.transaction() as connection:
            connection.execute("UPDATE jobs SET task_id=? WHERE id=?", (context["task_id"], other["job_id"]))
    with store.transaction() as connection:
        connection.execute("UPDATE slots SET target='original-target' WHERE id=?", (context["slot"]["id"],))
    json_write(
        Path(context["output_dir"]) / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "origin": context["slot"]["origin"],
            "target": "original-target",
            "session_id": "ws~original",
        },
    )
    proposal = read_json(store.root / context["packet_path"])
    proposal["execution_grants"]["ROS"] = [{"fixture": "projection mocked, not a cloud allowance"}]
    json_write(store.root / context["packet_path"], proposal)
    runtime = Runtime(store)
    monkeypatch.setattr(runtime, "assert_process", lambda *_: None)
    monkeypatch.setattr(
        runtime,
        "browser",
        lambda *_: {
            "session_id": "ws~original",
            "unlocked": True,
            "busy": False,
        },
    )
    monkeypatch.setattr(cleanup_recovery, "inspect_candidate", lambda *_, **kwargs: {"scope": kwargs["scope"]})
    recovery = {"original_job_id": worker["job_id"], "dimension": "ROS"}
    if same_task:
        with pytest.raises(WorkflowError, match="same task model jobs terminal"):
            runtime.inspect_cleanup(worker["job_id"], "ROS")
        with pytest.raises(WorkflowError, match="same task model jobs terminal"):
            store.queue_cleanup(context["task_id"], recovery)
    else:
        assert runtime.inspect_cleanup(worker["job_id"], "ROS")["scope"] == worker["job_id"] + ":ROS"
        store.queue_cleanup(context["task_id"], recovery)
        assert next(t for t in store.status()["tasks"] if t["id"] == context["task_id"])["state"] == "cleanup_pending"
        assert next(j for j in store.status()["jobs"] if j["id"] == other["job_id"])["state"] == "queued"


def test_cleanup_queue_preserves_attempt_slot_thread_and_luna_only_allocation(store):
    worker, before, thread = cleanup_queue_fixture(store)
    recovery = {
        "original_job_id": worker["job_id"],
        "dimension": "ROS",
        "evidence": "inputs/review.json",
        "evidence_sha256": "offline",
        "authority": "offline:ROS",
    }
    store.set_review_only("offline cleanup recovery")
    store.queue_cleanup(before["task_id"], recovery)
    assert store.status()["jobs"] == []  # Queueing is not model dispatch.
    assert store.status()["tasks"][0]["attempt"] == before["attempt"]
    with pytest.raises(WorkflowError, match="original slot|unresolved task"):
        store.queue_cleanup(before["task_id"], recovery)
    job = reserve(store, "cleanup")  # Only the scheduler consumes the action.
    key = token(store, job)
    context = store.context(job["job_id"], key)
    assert context["role"] == "glm" and context["purpose"] == "cleanup"
    assert context["slot"] == before["slot"] and context["attempt"] == before["attempt"]
    assert context["cleanup_recovery"] == recovery and context["expected_scenes"] == []
    assert store.status()["jobs"][0]["thread_id"] == thread
    store.mark_submission(job["job_id"], key)  # Cleanup remains legal under review-only hold.


@pytest.mark.parametrize("remaining,alter", [(0, False), (1, True), (1, False)])
def test_ops_cleanup_queue_requires_unused_exact_response_bound_delete(store, monkeypatch, remaining, alter):
    worker, before, _ = cleanup_queue_fixture(store)
    delete = {
        "product": "ros",
        "action": "DeleteStack",
        "params": {"StackId": "offline-original-id", "RetainAllResources": False},
    }
    candidate = {
        "scope": worker["job_id"] + ":ROS",
        "authority": "offline:ROS",
        "account": "offline",
        "owned_from_wire": [{"identifier": "offline-original-id"}],
        "remaining_calls": [{"kind": "delete", "remaining": remaining, "call": delete}],
    }
    monkeypatch.setattr(Runtime, "inspect_cleanup", lambda *_: candidate)
    review = {
        "kind": "ops_cleanup_only_review",
        "approved": True,
        "original_job_id": worker["job_id"],
        "dimension": "ROS",
        **{k: candidate[k] for k in ["scope", "authority", "account", "owned_from_wire"]},
        "delete_call": copy.deepcopy(delete),
    }
    if alter:
        review["delete_call"]["params"]["StackId"] = "another-id"
    json_write(store.root / "inputs/review.json", review)
    if remaining == 0 or alter:
        with pytest.raises(WorkflowError):
            Runtime(store).queue_cleanup(worker["job_id"], "ROS", "inputs/review.json")
        assert store.status()["tasks"][0]["state"] == "blocked"
    else:
        assert Runtime(store).queue_cleanup(worker["job_id"], "ROS", "inputs/review.json")["only_luna_dispatches"]
        assert store.status()["tasks"][0]["state"] == "cleanup_pending"
    assert store.status()["jobs"] == []
    assert store.status()["tasks"][0]["attempt"] == before["attempt"]


@pytest.mark.parametrize(
    "damage",
    [None, "mode", "session", "unused_delete", "missing_delete", "read_target", "source_stage", "reads_exhausted"],
)
def test_ops_readonly_cleanup_queue_requires_exact_original_delete_proof_and_remaining_reads(
    store,
    monkeypatch,
    damage,
):
    from scripts.web.e2e.resource_link_workflow.cleanup_recovery import readonly_review_fields

    worker, before, _ = cleanup_queue_fixture(store)
    candidate = {
        "scope": worker["job_id"] + ":ROS",
        "authority": "offline:ROS",
        "account": "offline",
        "session_id": "ws~original",
        "original_phase": "cleanup",
        "owned_from_wire": [{"identifier": "offline-original-id", "create_grant_id": "create"}],
        "successful_deletions": [{"grant_id": "delete", "parent": "create", "attempt_id": "original-delete"}],
        "remaining_calls": [
            {"id": "delete", "kind": "delete", "remaining": 0, "call": {"action": "DeleteStack"}},
            {
                "id": "read",
                "kind": "read",
                "remaining": 2,
                "read_policy": {"phase": "cleanup"},
                "call": {"action": "GetStack", "params": {"StackId": "offline-original-id"}},
            },
        ],
    }
    review = {
        "kind": "ops_cleanup_readonly_review",
        "approved": True,
        "original_job_id": worker["job_id"],
        "dimension": "ROS",
        **{
            k: candidate[k]
            for k in (
                "scope",
                "authority",
                "account",
                "session_id",
                "owned_from_wire",
            )
        },
        **copy.deepcopy(readonly_review_fields(candidate)),
    }
    if damage == "mode":
        review["kind"] = "ops_cleanup_only_review"
    elif damage == "session":
        review["session_id"] = "other"
    elif damage == "unused_delete":
        candidate["remaining_calls"][0]["remaining"] = 1
    elif damage == "missing_delete":
        candidate["successful_deletions"] = []
    elif damage == "read_target":
        review["cleanup_read_calls"][0]["call"]["params"]["StackId"] = "another-stack"
    elif damage == "source_stage":
        candidate["remaining_calls"][1]["read_policy"]["phase"] = "execute"
    elif damage == "reads_exhausted":
        candidate["remaining_calls"][1]["remaining"] = 0
    monkeypatch.setattr(Runtime, "inspect_cleanup", lambda *_: candidate)
    json_write(store.root / "inputs/review.json", review)
    if damage:
        with pytest.raises(WorkflowError):
            Runtime(store).queue_cleanup(worker["job_id"], "ROS", "inputs/review.json", read_only=True)
        assert store.status()["tasks"][0]["state"] == "blocked"
    else:
        assert Runtime(store).queue_cleanup(
            worker["job_id"],
            "ROS",
            "inputs/review.json",
            read_only=True,
        )["only_luna_dispatches"]
        job = reserve(store, "cleanup")
        context = store.context(job["job_id"], token(store, job))
        assert context["cleanup_recovery"]["read_only"] is True
        assert context["cleanup_recovery"]["original_job_id"] == worker["job_id"]
    assert store.status()["tasks"][0]["attempt"] == before["attempt"]


@pytest.mark.parametrize(
    "terminal,source,early,wrong_id",
    [
        (False, True, False, False),
        (True, False, False, False),
        (True, True, False, False),
        (True, True, True, False),
        (True, True, False, True),
    ],
)
def test_previous_dimension_delete_acceptance_is_not_terminal_absence(store, terminal, source, early, wrong_id):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize

    path = store.root / "execution-gate.sqlite3"
    initialize(path)
    scope = "offline-worker:TF"
    with sqlite3.connect(path) as connection:
        for index, (ident, kind, action, response) in enumerate(
            [
                ("create", "create", "CreateStack", {"StackId": "offline-stack"}),
                ("delete", "delete", "DeleteStack", {"RequestId": "offline-delete"}),
                (
                    "status",
                    "read",
                    "GetStack",
                    {
                        "Status": "DELETE_COMPLETE" if terminal else "DELETE_IN_PROGRESS",
                        "StackId": "other-stack" if wrong_id else "offline-stack",
                    },
                ),
            ]
        ):
            connection.execute(
                "INSERT INTO grants(scope,id,kind,call,quota) VALUES(?,?,?,?,?)",
                (scope, ident, kind, json.dumps({"action": action}), 1),
            )
            connection.execute(
                "INSERT INTO attempts(id,scope,grant_id,state,response,created) VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    scope,
                    ident,
                    "succeeded",
                    json.dumps(response),
                    0 if early and ident == "status" else index + 1,
                ),
            )
    if source:
        json_write(store.root / "jobs/offline-worker/output/TF-cleanup-source.json", {"offline": True})
    before = path.read_bytes()
    transition = Runtime(store).dimension_transition_admission("offline-worker", {"requests": {"CREATE": "approved"}})
    assert transition["remaining_dimensions"]["CREATE"]["previous_cleanup_execution_complete"] is (
        terminal and source and not early and not wrong_id
    )
    assert transition["physical_absence_verified"] is False
    assert transition["cleanup_accepted"] is False
    assert transition["independent_ops_sol_review_required"] is True
    assert path.read_bytes() == before
    if terminal and source and not early and not wrong_id:
        Runtime(store).assert_previous_cleanup("offline-worker", "CREATE")
    else:
        with pytest.raises(WorkflowError):
            Runtime(store).assert_previous_cleanup("offline-worker", "CREATE")


def test_dimension_transition_does_not_offer_replaying_started_dimensions(store):
    json_write(store.root / "jobs/offline-worker/output/ROS-request-intent.json", {"state": "may_have_submitted"})
    transition = Runtime(store).dimension_transition_admission(
        "offline-worker",
        {"requests": {"ROS": "old", "QUERY": "approved"}},
    )
    assert set(transition["remaining_dimensions"]) == {"QUERY"}
    assert transition["remaining_dimensions"]["QUERY"]["previous_cleanup_execution_complete"] is True
    assert transition["no_call_or_quota_added"] is True
    assert transition["immutable_cleanup_source_must_not_be_reopened"] is True


def test_dimension_transition_reports_exact_gate_rejection_without_mutation(store, monkeypatch):
    runtime = Runtime(store)

    def reject(job_id, dimension):
        assert (job_id, dimension) == ("offline-worker", "CREATE")
        raise WorkflowError("required post-delete read group-id is missing")

    monkeypatch.setattr(runtime, "assert_previous_cleanup", reject)
    transition = runtime.dimension_transition_admission("offline-worker", {"requests": {"CREATE": "approved"}})
    assert transition["remaining_dimensions"]["CREATE"] == {
        "previous_cleanup_execution_complete": False,
        "blocker": "required post-delete read group-id is missing",
    }


@pytest.mark.parametrize("operation", ["new-session", "request", "click", "source", "close-preview"])
def test_cleanup_recovery_forbids_new_tests_and_creation(store, monkeypatch, operation):
    worker, before, _ = cleanup_queue_fixture(store)
    json_write(store.root / "inputs/review.json", {"offline": True})
    recovery = {
        "original_job_id": worker["job_id"],
        "dimension": "ROS",
        "evidence": "inputs/review.json",
        "evidence_sha256": digest(store.root / "inputs/review.json"),
    }
    store.queue_cleanup(before["task_id"], recovery)
    job = reserve(store, "cleanup")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    context["slot"]["target"] = "original-target"
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(Runtime, "browser", lambda *_: pytest.fail("forbidden action must not reach Chrome"))
    with pytest.raises(WorkflowError, match="cleanup-only recovery forbids"):
        Runtime(store).tool(job["job_id"], key, operation, {})


@pytest.mark.parametrize("proof_failure", [False, True])
def test_readonly_cleanup_bootstrap_never_activates_or_sends_delete(store, monkeypatch, proof_failure):
    from scripts.web.e2e.resource_link_workflow import cleanup_recovery
    from scripts.web.e2e.resource_link_workflow import execution_gate as test_execution_gate

    worker, before, _ = cleanup_queue_fixture(store)
    json_write(store.root / "inputs/review.json", {"offline": True})
    recovery = {
        "original_job_id": worker["job_id"],
        "dimension": "ROS",
        "read_only": True,
        "evidence": "inputs/review.json",
        "evidence_sha256": digest(store.root / "inputs/review.json"),
    }
    store.queue_cleanup(before["task_id"], recovery)
    job = reserve(store, "cleanup")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    context["slot"]["target"] = "original-target"
    json_write(
        Path(before["output_dir"]) / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "origin": context["slot"]["origin"],
            "target": "original-target",
            "session_id": "ws~original",
        },
    )
    packet_path = store.root / context["packet_path"]
    packet = read_json(packet_path)
    packet["cleanup_requests"] = {"ROS": "OLD DELETE TEXT MUST NOT REACH PRODUCT"}
    packet["execution_grants"]["ROS"] = []  # Offline helper spy checks the exact original list.
    json_write(packet_path, packet)
    output = Path(context["output_dir"])
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(test_execution_gate, "cleanup", lambda *_: pytest.fail("never mutate original phase"))
    monkeypatch.setattr(test_execution_gate, "install", lambda *_a, **_kw: pytest.fail("never reinstall grants"))
    monkeypatch.setattr(cleanup_recovery, "exact_delete_request", lambda *_: pytest.fail("never send deletion"))
    operations = []

    def original_reads(root, current_recovery, slot, session, grants):
        assert root == store.root and current_recovery == recovery
        assert slot == str(context["slot"]["id"]) and session == "ws~original" and grants == []
        assert operations == ["snapshot", "close-preview", "snapshot"]
        assert not (output / "ROS-cleanup-intent.json").exists()
        if proof_failure:
            raise WorkflowError("original readonly proof changed")
        return "EXACT REMAINING READS ONLY"

    monkeypatch.setattr(cleanup_recovery, "exact_readonly_request", original_reads)
    runtime = Runtime(store)

    def browser(request):
        assert request["target"] == "original-target" and request["origin"] == context["slot"]["origin"]
        operations.append(request["operation"])
        if request["operation"] == "snapshot":
            return {"session_id": "ws~original", "unlocked": True, "busy": False, "preview_open": len(operations) == 1}
        if request["operation"] == "close-preview":
            assert request["session_id"] == "ws~original"
            return {"closed_preview": True}
        assert request["operation"] == "original-read-continuation" and request["text"] == "EXACT REMAINING READS ONLY"
        assert (output / "ROS-cleanup-intent.json").exists()
        return {"submitted": True}

    monkeypatch.setattr(runtime, "browser", browser)
    if proof_failure:
        with pytest.raises(WorkflowError, match="readonly proof changed"):
            runtime.tool(job["job_id"], key, "cleanup", {"dimension": "ROS"})
        assert "original-read-continuation" not in operations and not (output / "ROS-cleanup-intent.json").exists()
    else:
        assert runtime.tool(job["job_id"], key, "cleanup", {"dimension": "ROS"})["submitted"]
        with pytest.raises(WorkflowError, match="cleanup already submitted or unknown"):
            runtime.tool(job["job_id"], key, "cleanup", {"dimension": "ROS"})
        assert operations == ["snapshot", "close-preview", "snapshot", "original-read-continuation"]


@pytest.mark.parametrize("dimension,stage", [("ROS", "cleanup"), ("TF", "cleanup"), ("ROS", "source")])
def test_cleanup_recovery_continuation_keeps_original_scope_and_stage(store, monkeypatch, dimension, stage):
    from scripts.web.e2e.resource_link_workflow import execution_gate as test_execution_gate
    from scripts.web.e2e.resource_link_workflow import runtime as runtime_module

    worker, before, _ = cleanup_queue_fixture(store)
    json_write(store.root / "inputs/review.json", {"offline": True})
    store.queue_cleanup(
        before["task_id"],
        {
            "original_job_id": worker["job_id"],
            "dimension": "ROS",
            "evidence": "inputs/review.json",
            "evidence_sha256": digest(store.root / "inputs/review.json"),
        },
    )
    job = reserve(store, "cleanup")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    context["slot"]["target"] = "original-target"
    original_output = Path(before["output_dir"])
    json_write(
        original_output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "origin": context["slot"]["origin"],
            "target": "original-target",
            "session_id": "ws~original",
        },
    )
    output = Path(context["output_dir"])
    json_write(output / "ROS-cleanup-intent.json", {"state": "submitted"})
    packet_path = store.root / context["packet_path"]
    packet = read_json(packet_path)
    packet["requests"]["ROS"] = "offline original approved ROS request"
    json_write(packet_path, packet)
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(test_execution_gate, "install", lambda *_a, **_kw: pytest.fail("never reinstall grants"))

    def continuation(path, slot, scope, session, current_stage):
        assert (scope, session, current_stage) == (worker["job_id"] + ":ROS", "ws~original", "cleanup")
        return "remaining original reads only"

    monkeypatch.setattr(runtime_module, "readonly_continuation", continuation)
    sent = []

    def browser(request):
        if request["operation"] == "snapshot":
            return {"session_id": "ws~original", "unlocked": True, "busy": False}
        assert request["operation"] == "original-read-continuation"
        assert request["text"] == "remaining original reads only"
        sent.append(request)
        return {"submitted": True}

    runtime = Runtime(store)
    monkeypatch.setattr(runtime, "browser", browser)
    if (dimension, stage) != ("ROS", "cleanup"):
        with pytest.raises(WorkflowError, match="original dimension|post-delete reads"):
            runtime.tool(job["job_id"], key, "continue-read", {"dimension": dimension, "stage": stage})
        assert not sent
    else:
        runtime.tool(job["job_id"], key, "continue-read", {"dimension": dimension, "stage": stage})
        assert len(sent) == 1
        marker = read_json(output / "ROS-cleanup-continue-0001.json")
        assert marker["scope"] == worker["job_id"] + ":ROS" and marker["state"] == "submitted"


@pytest.mark.parametrize("damage", [None, "same_evidence", "dimension", "live", "wrong_job"])
def test_cleanup_retry_requires_fresh_terminal_review_without_new_attempt(store, damage):
    worker, before, _ = cleanup_queue_fixture(store)
    original = {
        "original_job_id": worker["job_id"],
        "dimension": "ROS",
        "authority": "offline:ROS",
        "evidence": "inputs/original.json",
        "evidence_sha256": "old-review",
    }
    store.queue_cleanup(before["task_id"], original)
    retry = reserve(store, "cleanup")
    key, thread, turn = bound(store, retry)
    store.submit(retry["job_id"], key, "Offline explicit framework failure, no deletion")
    if damage != "live":
        store.finish_job(retry["job_id"], thread, turn, "completed")
    reviewed = {
        **original,
        "evidence": "inputs/fresh.json",
        "evidence_sha256": "fresh-review",
        "retry_job_id": retry["job_id"],
    }
    if damage == "same_evidence":
        reviewed["evidence_sha256"] = "old-review"
    elif damage == "dimension":
        reviewed["dimension"] = "TF"
    elif damage == "wrong_job":
        reviewed["retry_job_id"] = worker["job_id"]
    if damage:
        with pytest.raises(WorkflowError):
            store.queue_cleanup(before["task_id"], reviewed)
    else:
        store.queue_cleanup(before["task_id"], reviewed)
        assert store.status()["jobs"] == []
        task = store.status()["tasks"][0]
        assert task["state"] == "cleanup_pending" and task["attempt"] == before["attempt"]
        assert task["slot"] == before["slot"]["id"]
        new_job = reserve(store, "cleanup")
        new_key, new_thread, new_turn = bound(store, new_job)
        store.submit(new_job["job_id"], new_key, "Offline terminal")
        store.finish_job(new_job["job_id"], new_thread, new_turn, "completed")
        with pytest.raises(WorkflowError, match="fresh exact terminal"):
            store.queue_cleanup(before["task_id"], {**reviewed, "evidence_sha256": "different-again"})


@pytest.mark.parametrize("new_generation", [False, True])
@pytest.mark.parametrize("legacy_pointer", [False, True])
def test_cleanup_recovery_tracks_each_binding_without_erasing_previous_review(store, new_generation, legacy_pointer):
    worker, before, _ = cleanup_queue_fixture(store)
    first = {
        "original_job_id": worker["job_id"],
        "dimension": "ROS",
        "authority": "offline:ROS",
        "evidence": "inputs/original.json",
        "evidence_sha256": "original-review",
    }
    store.queue_cleanup(before["task_id"], first)
    with store.transaction() as connection:
        # Model terminal proof is still a prerequisite for every new recovery.
        connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (before["task_id"],))
        if legacy_pointer:
            connection.execute("DELETE FROM meta WHERE key LIKE 'cleanup_binding:%'")
        new_id = "b" * 32 if new_generation else worker["job_id"]
        if new_generation:
            connection.execute(
                "INSERT INTO jobs SELECT ?,task_id,role,purpose,state,thread_id,turn_id,capability,submission,created "
                "FROM jobs WHERE id=?",
                (new_id, worker["job_id"]),
            )
    second = {
        "original_job_id": new_id,
        "dimension": "TF",
        "authority": "offline:TF",
        "evidence": "inputs/current.json",
        "evidence_sha256": "current-review",
    }
    store.queue_cleanup(before["task_id"], second)
    with store.transaction() as connection:
        history = [
            json.loads(row[0])
            for row in connection.execute("SELECT value FROM meta WHERE key LIKE 'cleanup_binding:%'")
        ]
        assert first in history and second in history and len(history) == 2
        assert (
            json.loads(
                connection.execute(
                    "SELECT value FROM meta WHERE key=?", ("cleanup_recovery:" + before["task_id"],)
                ).fetchone()[0]
            )
            == second
        )
        connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (before["task_id"],))
    # Switching away and back must not copy an already-consumed recovery.
    with pytest.raises(WorkflowError, match="fresh exact terminal retry"):
        store.queue_cleanup(before["task_id"], first)
    assert store.status()["tasks"][0]["attempt"] == before["attempt"]
    assert store.status()["tasks"][0]["slot"] == before["slot"]["id"]
    assert store.status()["jobs"] == []


def test_cleanup_source_requires_explicit_cleanup_and_original_binding(store, monkeypatch):
    worker, before, _ = cleanup_queue_fixture(store)
    json_write(store.root / "inputs/review.json", {"offline": True})
    recovery = {
        "original_job_id": worker["job_id"],
        "dimension": "QUERY",
        "evidence": "inputs/review.json",
        "evidence_sha256": digest(store.root / "inputs/review.json"),
    }
    store.queue_cleanup(before["task_id"], recovery)
    job = reserve(store, "cleanup")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    context["slot"]["target"] = "original-target"
    original_output = store.root / "jobs" / worker["job_id"] / "output"
    json_write(
        original_output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "original-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~original-key",
        },
    )
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(Runtime, "browser", lambda *_: pytest.fail("missing explicit cleanup cannot save source"))
    with pytest.raises(WorkflowError, match="explicit cleanup phase"):
        Runtime(store).tool(job["job_id"], key, "cleanup-source", {"dimension": "QUERY"})
    with pytest.raises(WorkflowError, match="original dimension"):
        Runtime(store).tool(job["job_id"], key, "cleanup-source", {"dimension": "ROS"})
    json_write(store.root / "inputs/review.json", {"mutated": True})
    with pytest.raises(WorkflowError, match="cleanup review changed"):
        Runtime(store).tool(job["job_id"], key, "snapshot", {})


@pytest.mark.parametrize("ui_failure", [None, "busy", "locked", "other_session"])
def test_cleanup_inspection_retains_full_session_target_and_original_authority(store, monkeypatch, ui_failure):
    from scripts.web.e2e.resource_link_workflow import cleanup_recovery, creation

    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    context = store.context(worker["job_id"], key)
    packet_path = store.root / context["packet_path"]
    value = read_json(packet_path)
    value["execution_grants"]["CREATE"] = copy.deepcopy(value["execution_grants"]["QUERY"])
    json_write(packet_path, value)  # Isolated fixture, never rewrite a real approved packet.
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET state='complete' WHERE id=?", (worker["job_id"],))
        connection.execute(
            "UPDATE slots SET target='original-target',web_pid=42,web_identity='original-process' WHERE id=0"
        )
    binding = {
        "job_id": worker["job_id"],
        "target": "original-target",
        "origin": "http://127.0.0.1:16100",
        "session_id": "ws~original-complete-key",
    }
    json_write(store.root / "jobs" / worker["job_id"] / "output/self-created-session.json", binding)
    monkeypatch.setattr(Runtime, "assert_process", lambda *_: None)

    def snapshot(_self, request):
        assert request == {"operation": "snapshot", "origin": binding["origin"], "target": binding["target"]}
        return {
            "busy": ui_failure == "busy",
            "unlocked": ui_failure != "locked",
            "session_id": "other" if ui_failure == "other_session" else binding["session_id"],
        }

    monkeypatch.setattr(Runtime, "browser", snapshot)
    captured = []

    def inspect(path, **arguments):
        captured.append(arguments)
        assert path == store.root / "execution-gate.sqlite3"
        return {"candidate_only": True, "activation_allowed": False}

    monkeypatch.setattr(cleanup_recovery, "inspect_candidate", inspect)
    if ui_failure:
        with pytest.raises(WorkflowError, match="original exact Web session"):
            Runtime(store).inspect_cleanup(worker["job_id"], "CREATE")
        assert not captured
    else:
        assert Runtime(store).inspect_cleanup(worker["job_id"], "CREATE")["activation_allowed"] is False
        assert captured[0] == {
            "slot": "0",
            "scope": worker["job_id"] + ":CREATE",
            "session": binding["session_id"],
            "account": next(iter(value["scenes"].values()))["account"],
            "grants": value["execution_grants"]["CREATE"],
            "authority": creation.execution_digest(value) + ":CREATE",
        }


@pytest.mark.parametrize("timeouts", [[None], [True], ["10"], [9], [1441]])
def test_stack_timeout_validates_only_invalid_api_parameters(timeouts):
    from scripts.web.e2e.resource_link_workflow.creation import execution_digest, validate_stack_timeouts

    value = {
        "task_id": "offline:R0001",
        "budget_cents": 0,
        "scenes": {"fixture": {"account": "offline"}},
        "execution_grants": {
            str(i): [
                {
                    "kind": "create",
                    "call": {"product": "ros", "action": "CreateStack", "params": {"TimeoutInMinutes": timeout}},
                }
            ]
            for i, timeout in enumerate(timeouts)
        },
    }
    before = copy.deepcopy(value)
    original_digest = execution_digest(value)
    with pytest.raises(WorkflowError, match="timeout"):
        validate_stack_timeouts(value)
    assert value == before
    assert execution_digest(value) == original_digest


@pytest.mark.parametrize("timeouts", [[20, 20], [60], [10, 10], [15], [1440]])
def test_long_stack_timeouts_do_not_impose_model_deadline(timeouts):
    from scripts.web.e2e.resource_link_workflow.creation import validate_stack_timeouts

    value = {
        "execution_grants": {
            str(i): [
                {
                    "kind": "create",
                    "call": {"product": "ros", "action": "CreateStack", "params": {"TimeoutInMinutes": timeout}},
                }
            ]
            for i, timeout in enumerate(timeouts)
        }
    }
    before = copy.deepcopy(value)
    validate_stack_timeouts(value)
    assert value == before


def test_stack_timeout_default_is_allowed_without_execution_deadline():
    from scripts.web.e2e.resource_link_workflow.creation import validate_stack_timeouts

    value = {
        "execution_grants": {
            "ROS": [{"kind": "create", "call": {"product": "ROS", "action": "CreateStack", "params": {}}}]
        }
    }
    validate_stack_timeouts(value)
    validate_stack_timeouts(
        {
            "execution_grants": {
                "CREATE": [{"kind": "create", "call": {"product": "vpc", "action": "CreateVpc", "params": {}}}]
            }
        }
    )


def test_task_authorization_allows_long_stack_wait_and_preserves_grants(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow.creation import authorization_path, validate_authorization

    spec = plan(store)["resources"][0]
    spec["dimensions"]["ROS"] = copy.deepcopy(spec["dimensions"]["QUERY"])
    spec["dimensions"]["ROS"]["contracts"][0]["id"] = "R0001-ROS-1"
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET spec=?", (json.dumps(spec),))
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    value = packet(store, store.context(job["job_id"], key))
    value["execution_grants"]["ROS"] = [
        copy.deepcopy(value["execution_grants"]["QUERY"][0]),
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": {
                "product": "ros",
                "action": "CreateStack",
                "region_id": "test",
                "params": {"TimeoutInMinutes": 20, "TemplateBody": "offline"},
            },
        },
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "parent": "create",
            "response_path": ["StackId"],
            "target_param": "StackId",
            "call": {
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "test",
                "params": {"StackId": "$created_id"},
            },
        },
    ]
    value["cleanup_recipe"] = "Delete only the stack returned by this offline fixture; verify absence."
    json_write(store.root / "inputs/proposal.json", value)
    json_write(store.root / "inputs/review.json", {"offline": True})
    original = authorization_path(store.root, value)
    json_write(original, {"original": "expired approval must never be overwritten on rejected renewal"})
    Runtime(store).authorize_creation(Path("inputs/proposal.json"), "inputs/review.json", 0)
    approved = read_json(original)
    assert approved["schema_version"] == 3 and approved["lifetime"] == "task"
    assert approved["expires_at"] is None
    monkeypatch.setattr(time, "time", lambda: 9999999999)
    validate_authorization(store.root, "R0001", value)
    assert read_json(store.root / "inputs/proposal.json") == value
    assert not (store.root / "execution-gate.sqlite3").exists()


def test_safe_slot_release_closes_task_capability_without_resetting_history(store):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize, install

    plan(store)
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    value = packet(store, store.context(job["job_id"], key))
    gate = store.root / "execution-gate.sqlite3"
    initialize(gate)
    install(gate, "0", "original-worker:QUERY", "ws~fixture", "offline", None, value["execution_grants"]["QUERY"])
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET id='original-worker',role='glm' WHERE id=?", (job["job_id"],))
        store.revoke_task_capabilities(connection, value["task_id"])
    with sqlite3.connect(gate) as wire:
        assert wire.execute("SELECT phase,expires FROM active").fetchone() == ("closed", None)
        assert wire.execute("SELECT count(*) FROM grants").fetchone()[0] > 0
        assert wire.execute("SELECT count(*) FROM authority_limits").fetchone()[0] > 0


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "module_sha",
        "signin_sha",
        "signin_missing",
        "readonly_policy_sha",
        "readonly_policy_missing",
        "asset_sha",
        "asset_missing",
        "asset_path",
        "style_sha",
        "style_missing",
    ],
)
def test_web_attestation_binds_actual_process_and_product_mapping_manifest(store, monkeypatch, invalid):
    from scripts.web.e2e.resource_link_workflow.execution_gate import ATTEST_ASSETS, ATTEST_MODULES

    assert "web/static/styles.css" in ATTEST_ASSETS
    assert "iac_code.web.console_readonly_policy" in ATTEST_MODULES

    runtime = Runtime(store)
    runtime.config = {"repo": str(REPO)}
    with store.transaction() as connection:
        connection.execute("UPDATE slots SET web_pid=111,web_identity='owned-parent' WHERE id=0")
    value = {"slot": "0", "parent_pid": 111, "pid": 222, "modules": [], "assets": []}
    for name in ATTEST_MODULES:
        module_path = name.replace(".", "/") + ".py"
        path = REPO / module_path if name.startswith("scripts.") else REPO / "src" / module_path
        value["modules"].append({"module": name, "path": str(path), "sha256": digest(path)})
    for name in ATTEST_ASSETS:
        path = REPO / "src/iac_code" / name
        value["assets"].append({"relative_path": name, "path": str(path), "sha256": digest(path)})
    if invalid == "module_sha":
        value["modules"][2]["sha256"] = "stale-loaded-product"
    elif invalid == "signin_sha":
        next(item for item in value["modules"] if item["module"] == "iac_code.web.resource_signin")["sha256"] = (
            "stale-loaded-signin"
        )
    elif invalid == "signin_missing":
        value["modules"] = [item for item in value["modules"] if item["module"] != "iac_code.web.token_transport"]
    elif invalid == "readonly_policy_sha":
        policy = next(item for item in value["modules"] if item["module"] == "iac_code.web.console_readonly_policy")
        policy["sha256"] = "stale-loaded-readonly-policy"
    elif invalid == "readonly_policy_missing":
        value["modules"] = [
            item for item in value["modules"] if item["module"] != "iac_code.web.console_readonly_policy"
        ]
    elif invalid == "asset_sha":
        value["assets"][0]["sha256"] = "stale-startup-catalog"
    elif invalid == "asset_missing":
        value["assets"].pop()
    elif invalid == "asset_path":
        value["assets"][0]["path"] = str(store.root / "another-catalog.json")
    elif invalid == "style_sha":
        next(item for item in value["assets"] if item["relative_path"] == "web/static/styles.css")["sha256"] = (
            "stale-console-width"
        )
    elif invalid == "style_missing":
        value["assets"] = [item for item in value["assets"] if item["relative_path"] != "web/static/styles.css"]
    json_write(store.root / "runtime-attestations/slot-0.json", value)
    monkeypatch.setattr(runtime, "assert_process", lambda *_: None)
    monkeypatch.setattr(
        runtime,
        "process_identity",
        lambda *_: "python -m scripts.web.e2e.resource_link_workflow.web_entry --port 16100",
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: "111\n")
    if invalid:
        with pytest.raises(WorkflowError):
            runtime.attest_web(0)
    else:
        proof = runtime.attest_web(0)
        assert proof["same_slot_state_preserved"] is True
        assert proof["modules"] == value["modules"] and proof["assets"] == value["assets"]


@pytest.mark.parametrize("error", ["source session mismatch", "http://127.0.0.1/#web_token=fake-private"])
def test_browser_error_exposes_only_fixed_validation_literals(store, monkeypatch, error):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, json.dumps({"workflow_error": error}), "private stderr never forwarded"
        ),
    )
    with pytest.raises(WorkflowError) as failure:
        Runtime(store).browser({"operation": "source"})
    if error == "source session mismatch":
        assert error in str(failure.value)
    else:
        assert "web_token" not in str(failure.value)
        assert "fake-private" not in str(failure.value)


def resource(number=1, locks=None):
    ident = f"R{number:04}"
    dimensions = {
        dim: {"support": "not_applicable", "official_basis": "offline fixture", "contracts": []}
        for dim in ("ROS", "TF", "CREATE")
    }
    dimensions["TF"]["execution"] = "ros_managed_terraform"
    dimensions["QUERY"] = {
        "support": "supported",
        "contracts": [
            {
                "id": ident + "-QUERY-1",
                "source_type": "ALIYUN::TEST::Example",
                "historical": {"iframe": "untested", "open": "untested"},
            }
        ],
    }
    return {
        "id": ident,
        "key": f"ALIYUN::TEST::Example{number}",
        "service": "Test",
        "name": f"Example{number}",
        "group": "identity-application",
        "dependencies": [],
        "locks": locks or [],
        "dimensions": dimensions,
    }


@pytest.fixture
def store(tmp_path):
    result = Store.initialize(tmp_path / "state")
    json_write(result.root / "inputs" / "cloud.json", {"fixture": "not a real cloud observation"})
    return result


def plan(store, count=1, **kwargs):
    value = {"schema_version": 1, "plan_id": "offline", "resources": [resource(i + 1, **kwargs) for i in range(count)]}
    store.import_plan(value)
    store.pause(False)
    return value


def test_current_plan_scope_recovers_from_frozen_database_not_historical_run_state(store):
    old = {"schema_version": 1, "plan_id": "old-six", "resources": [resource(1)]}
    store.import_plan(old)
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='complete'")
    current = {
        "schema_version": 1,
        "plan_id": "new-six",
        "initial_wave": 4,
        "resources": [resource(i) for i in (21, 22, 33, 52, 53, 76)],
    }
    store.import_plan(current)
    before = store.status()["revision"]
    result = Store(store.root).status()
    assert result["revision"] == before
    assert result["tasks"][0]["run_id"] == "old-six"
    assert [p["plan_id"] for p in result["active_plans"]] == ["new-six"]
    scope = result["active_plans"][0]
    assert scope["resource_ids"] == ["R0021", "R0022", "R0033", "R0052", "R0053", "R0076"]
    assert scope["remaining_resource_ids"] == scope["resource_ids"]
    assert scope["initial_wave"] == 4 and scope["budget_cents"] == 0
    assert not scope["initial_wave_eligible"] and not scope["glm_reservation_started"]
    assert len(scope["frozen_plan_sha256"]) == 64
    assert Store(store.root).next_action()["active_plans"] == result["active_plans"]
    with store.transaction() as connection:
        assert connection.execute("SELECT state FROM runs WHERE id='old-six'").fetchone()[0] == "active"
        assert not connection.execute("SELECT 1 FROM actions").fetchone()


def test_plan_scope_keeps_frozen_members_after_completion_and_uses_real_wave_gate(store):
    store.import_plan(
        {"schema_version": 1, "plan_id": "wave", "initial_wave": 4, "resources": [resource(i) for i in range(1, 7)]}
    )
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='ready' WHERE resource_id IN ('R0001','R0002','R0003','R0004')")
        connection.execute("UPDATE tasks SET state='complete' WHERE resource_id='R0006'")
    scope = store.status()["active_plans"][0]
    assert scope["resource_ids"] == [f"R{i:04}" for i in range(1, 7)]
    assert scope["remaining_resource_ids"] == [f"R{i:04}" for i in range(1, 6)]
    assert scope["initial_wave_eligible"] is True
    assert scope["glm_reservation_started"] is False  # Eligible is not actual concurrency.
    with store.transaction() as connection:
        connection.execute("INSERT INTO locks VALUES('account-preference','wave:R0005')")
        spec = json.loads(connection.execute("SELECT spec FROM tasks WHERE id='wave:R0001'").fetchone()[0])
        spec["locks"] = ["account-preference"]
        connection.execute("UPDATE tasks SET spec=? WHERE id='wave:R0001'", (json.dumps(spec),))
    assert store.status()["active_plans"][0]["initial_wave_eligible"] is False


@pytest.mark.parametrize("job_state", ["running", "unknown", "submitted", "complete"])
def test_plan_scope_does_not_drop_active_terminal_task_job_or_invent_closed_plan(store, job_state):
    plan(store)
    job = store.execute(store.next_action()["actions"][0]["id"])
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='complete'")
        connection.execute("UPDATE jobs SET state=? WHERE id=?", (job_state, job["job_id"]))
    result = store.status()
    assert bool(result["active_plans"]) == (job_state != "complete")
    if result["active_plans"]:
        assert result["active_plans"][0]["remaining_resource_ids"] == []
    else:
        assert store.next_action()["type"] == "finished"
        assert store.next_action()["active_plans"] == []


def token(store, job):
    return read_json(store.root / "jobs" / job["job_id"] / "capability.json")["token"]


def test_sol_context_lists_only_resource_seed_inputs(store):
    plan(store)
    job = reserve(store, "prepare")
    json_write(store.root / "inputs/R0001/official.json", {"url": "offline fixture"})
    json_write(store.root / "inputs/R0002/other.json", {"other": True})
    (store.root / "inputs/R0001/login.png").write_bytes(PNG)
    context = Runtime(store).tool(job["job_id"], token(store, job), "context", {})
    assert context["preparation_inputs"] == ["R0001/official.json"]
    assert context["creation_approval_present"] is False
    assert context["dispatch_state"] == {
        "revision": store.status()["revision"],
        "paused": False,
        "review_only": None,
        "pause_scope": "new_dispatch_only; existing jobs retain response-bound cleanup obligations",
        "worker_host": "local",
        "web_host": "local",
    }
    assert context["cloud_source_readiness"]["status"] == "not_adopted"
    assert context["cloud_source_readiness"]["signed_identity_verified"] is False


def test_glm_context_uses_program_approval_not_obsolete_proposal_flags(store):
    plan(store)
    prepare_job = reserve(store, "prepare")
    key, thread, turn = bound(store, prepare_job)
    value = packet(store, store.context(prepare_job["job_id"], key))
    value.update(proposal_status="pending_global_hold", executable=False, dispatch_allowed=False)
    value["ops_review_required"] = {"global_review_only_remains": True, "sol_new_packet_approval": False}
    store.approve_packet(prepare_job["job_id"], key, value)
    store.finish_job(prepare_job["job_id"], thread, turn, "completed")
    worker = reserve(store, "dispatch")
    worker_key, _, _ = bound(store, worker)
    runtime = Runtime(store)
    ctx = runtime.tool(worker["job_id"], worker_key, "context", {})
    original_path = store.root / ctx["packet_path"]
    original_sha = digest(original_path)
    assert read_json(original_path) == value  # Never rewrite historical proposals or grants.
    assert ctx["execution_admission"]["packet_sha256"] == original_sha
    assert ctx["execution_admission"]["dispatch_allowed"] is True
    assert ctx["packet"]["executable"] is True and ctx["packet"]["dispatch_allowed"] is True
    assert ctx["packet"]["execution_grants"] == value["execution_grants"]
    assert "ops_review_required" not in ctx["packet"]
    store.pause(True)
    assert runtime.tool(worker["job_id"], worker_key, "context", {})["execution_admission"]["dispatch_allowed"] is True
    store.set_review_only("offline recovery")
    held = runtime.tool(worker["job_id"], worker_key, "context", {})
    assert held["dispatch_state"]["review_only"] == "offline recovery"
    assert held["execution_admission"]["dispatch_allowed"] is False
    assert held["packet"]["executable"] is False
    store.set_review_only(None)
    json_write(store.root / "inputs/cloud.json", {"changed": True})
    altered = runtime.tool(worker["job_id"], worker_key, "context", {})
    assert altered["execution_admission"]["inputs_intact"] is False
    assert altered["packet"]["dispatch_allowed"] is False
    assert digest(original_path) == original_sha


def test_creation_reference_selection_never_borrows_another_dimension_or_session(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    monkeypatch.setattr(
        gate,
        "creations",
        lambda *_: [
            {
                "dimension": "ROS",
                "session_id": "ws~own",
                "state": "succeeded",
                "resource": {"id": "stack-actual", "account": "123", "region": "cn-hangzhou", "product": "ros"},
            }
        ],
    )
    matching = {"identifier": "vsw-new", "stackId": "stack-actual", "accountScope": "123", "regionId": "cn-hangzhou"}
    references = [matching, {**matching, "stackId": "stack-old"}, {**matching, "accountScope": "other"}]
    runtime = Runtime(store)
    assert runtime.created_references("job", "ROS", "ws~own", references) == [matching]
    with pytest.raises(WorkflowError, match="wire creation"):
        runtime.created_references("job", "TF", "ws~own", references)
    with pytest.raises(WorkflowError, match="wire creation"):
        runtime.created_references("job", "ROS", "ws~other", references)
    monkeypatch.setattr(
        gate,
        "creations",
        lambda *_: [
            {
                "dimension": "CREATE",
                "session_id": "ws~own",
                "state": "succeeded",
                "resource": {"id": "vsw-new", "account": "123", "region": "cn-hangzhou", "product": "vpc"},
            }
        ],
    )
    assert runtime.created_references("job", "CREATE", "ws~own", [matching, {**matching, "identifier": "vsw-old"}]) == [
        matching
    ]


def test_opaque_reference_scope_requires_exact_successful_wire_proof(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    monkeypatch.setattr(
        gate,
        "reference_observations",
        lambda *_: [
            {"account": "123", "reference_scope": "credential:actual", "region": "cn-hangzhou", "tool_use_id": "real"}
        ],
    )
    reference = {
        "accountScope": "credential:actual",
        "regionId": "cn-hangzhou",
        "sources": [{"toolUseId": "real", "evidence": "api_response"}],
    }
    runtime = Runtime(store)
    assert runtime.proven_references("job", "QUERY", "ws~own", "123", [reference]) == [reference]
    assert runtime.proven_references("job", "QUERY", "ws~own", "other", [reference]) == []
    for changed in (
        {"accountScope": "credential:forged"},
        {"regionId": "cn-beijing"},
        {"sources": [{"toolUseId": "other", "evidence": "api_response"}]},
        {"sources": [{"toolUseId": "real", "evidence": "tool_input"}]},
    ):
        assert runtime.proven_references("job", "QUERY", "ws~own", "123", [{**reference, **changed}]) == []


@pytest.mark.parametrize("dimension", ["ROS", "TF", "CREATE"])
@pytest.mark.parametrize("mode", ["iframe", "open"])
def test_account_reference_click_keeps_signed_creation_binding_and_scene_request_region(
    store,
    monkeypatch,
    dimension,
    mode,
):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    resource_type = "alicloud_ram_policy" if dimension == "TF" else "ALIYUN::RAM::ManagedPolicy"
    scene = context["expected_scenes"][0]
    scene.update(dimension=dimension, source_type=resource_type, mode=mode)
    context["slot"]["target"] = "offline-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "offline-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~offline",
        },
    )
    packet = read_json(store.root / context["packet_path"])
    packet["scenes"][scene["id"]].update(
        account="123",
        region="cn-hangzhou",
        expected_type=resource_type,
        selector='a.resource-link[data-resource-link][aria-label="{identifier}"]',
        name_prefix="offline-policy",
        render_text=["offline-policy"],
    )
    json_write(store.root / context["packet_path"], packet)
    reference = {
        "resourceType": resource_type,
        "identifier": "offline-policy",
        "scopeKind": "account",
        "accountScope": "credential:offline",
        "stackId": "offline-stack",
        "sources": [
            {
                "toolUseId": "offline-read",
                "evidence": "api_response",
                "requestRegionId": "cn-hangzhou",
            }
        ],
    }
    original = {"session_id": "ws~offline", "references": {"items": [reference]}}
    json_write(output / f"{dimension}-product-source.json", original)
    monkeypatch.setattr(
        gate,
        "reference_observations",
        lambda *_: [
            {
                "account": "123",
                "reference_scope": "credential:offline",
                "region": "cn-hangzhou",
                "tool_use_id": "offline-read",
            }
        ],
    )
    monkeypatch.setattr(
        gate,
        "creations",
        lambda *_: [
            {
                "dimension": dimension,
                "session_id": "ws~offline",
                "state": "succeeded",
                "reference_scope": "credential:offline",
                "resource": {
                    "id": "offline-policy" if dimension == "CREATE" else "offline-stack",
                    "account": "123",
                    "region": "cn-hangzhou",
                    "product": "ram" if dimension == "CREATE" else "ros",
                },
            }
        ],
    )
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)

    def browser(request):
        assert request["selector"] == 'a.resource-link[data-resource-link][aria-label="offline-policy"]'
        assert request["source_identifier"] == request["render_identifier"] == "offline-policy"
        assert request["render_text"] == ["offline-policy"] and request["mode"] == mode
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~offline"}

    monkeypatch.setattr(runtime, "browser", browser)
    result = runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
    assert result["source_identifier"] == "offline-policy"
    assert read_json(output / f"{dimension}-product-source.json") == original
    assert not store.status()["completed"]  # Offline browser exercise is not acceptance.


@pytest.mark.parametrize("dimension", ["ROS", "TF", "CREATE"])
@pytest.mark.parametrize("mode", ["iframe", "open"])
@pytest.mark.parametrize("display_name_filter", [False, True])
def test_acl_click_filters_response_bound_identifier_not_display_name(
    store,
    monkeypatch,
    dimension,
    mode,
    display_name_filter,
):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    resource_type = "alicloud_slb_acl" if dimension == "TF" else "ALIYUN::SLB::AccessControl"
    scene = context["expected_scenes"][0]
    scene.update(dimension=dimension, source_type=resource_type, mode=mode)
    context["slot"]["target"] = "offline-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "offline-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~offline",
        },
    )
    packet = read_json(store.root / context["packet_path"])
    packet["scenes"][scene["id"]].update(
        account="123",
        region="cn-hangzhou",
        expected_type=resource_type,
        selector='a.resource-link[data-resource-link][aria-label="{identifier}"]',
        reference_types=[resource_type],
        render_text=["offline-acl", "IPv4"],
    )
    if display_name_filter:
        packet["scenes"][scene["id"]]["name_prefix"] = "offline-acl"
    else:
        packet["scenes"][scene["id"]].pop("name_prefix", None)
    json_write(store.root / context["packet_path"], packet)
    reference = {
        "resourceType": resource_type,
        "identifier": "acl-fixture",
        "scopeKind": "regional",
        "regionId": "cn-hangzhou",
        "attributes": {"AclName": "offline-acl", "AddressIPVersion": "ipv4"},
        "accountScope": "credential:offline",
        "stackId": "offline-stack",
        "sources": [
            {
                "toolUseId": "offline-read",
                "evidence": "api_response",
                "requestRegionId": "cn-hangzhou",
            }
        ],
    }
    original = {"session_id": "ws~offline", "references": {"items": [reference]}}
    json_write(output / f"{dimension}-product-source.json", original)
    monkeypatch.setattr(
        gate,
        "reference_observations",
        lambda *_: [
            {
                "account": "123",
                "reference_scope": "credential:offline",
                "region": "cn-hangzhou",
                "tool_use_id": "offline-read",
            }
        ],
    )
    monkeypatch.setattr(
        gate,
        "creations",
        lambda *_: [
            {
                "dimension": dimension,
                "session_id": "ws~offline",
                "state": "succeeded",
                "reference_scope": "credential:offline",
                "resource": {
                    "id": "acl-fixture" if dimension == "CREATE" else "offline-stack",
                    "account": "123",
                    "region": "cn-hangzhou",
                    "product": "slb" if dimension == "CREATE" else "ros",
                },
            }
        ],
    )
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)

    def browser(request):
        assert not display_name_filter
        assert request["selector"] == 'a.resource-link[data-resource-link][aria-label="acl-fixture"]'
        assert request["source_identifier"] == request["render_identifier"] == "acl-fixture"
        assert request["render_text"] == ["offline-acl", "IPv4"] and request["mode"] == mode
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~offline"}

    monkeypatch.setattr(runtime, "browser", browser)
    if display_name_filter:
        with pytest.raises(WorkflowError, match="newly generated precise reference must be unique"):
            runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
        assert not (output / f"{scene['id']}.png").exists()
    else:
        result = runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
        assert result["source_identifier"] == "acl-fixture"
    assert read_json(output / f"{dimension}-product-source.json") == original
    assert not store.status()["completed"]


def reserve(store, kind):
    action = store.next_action()["actions"][0]
    assert action["kind"] == kind
    return store.execute(action["id"])


def preparation_repair_fixture(store, tmp_path):
    plan(store)
    task_id = store.status()["tasks"][0]["id"]
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task_id,))
    store.pause(True)
    repo = tmp_path / "repo"
    code = repo / "src" / "mapping.json"
    json_write(code, {"fixture": "missing alias"})
    json_write(store.root / "environment.json", {"repo": str(repo)})
    proof = {
        "schema_version": 1,
        "kind": "preparation_product_gap",
        "task_id": task_id,
        "diagnosis": "Offline fixture alias gap, not actual cloud evidence",
        "sources": [{"path": "cloud.json", "sha256": digest(store.root / "inputs/cloud.json")}],
        "code_evidence": [{"path": "src/mapping.json", "sha256": digest(code), "fact": "fixture gap"}],
    }
    json_write(store.root / "inputs" / "repair.json", proof)
    return task_id, proof, code


def input_repair_fixture(store):
    """Real state transitions with deliberately fake offline cloud/PNG inputs."""
    plan(store)
    review, key, thread, turn, receipt, values = review_fixture(store, ("pass", "execution_gap"))
    store.accept(review["job_id"], key, receipt)
    store.finish_job(review["job_id"], thread, turn, "completed")
    task_id = receipt["task_id"]
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task_id,))
        worker = connection.execute("SELECT id FROM jobs WHERE role='glm'").fetchone()[0]
    store.pause(True)
    image = "inputs/R0001/ops-original.png"
    (store.root / image).parent.mkdir(parents=True, exist_ok=True)
    (store.root / image).write_bytes(PNG)
    diagnostic = "inputs/R0001/ops-diagnostic.json"
    json_write(
        store.root / diagnostic,
        {
            "formal_test": False,
            "pass_added": False,
            "cloud_writes": 0,
            "original_job": worker,
            "diagnostic_source": "inputs/cloud.json",
            "diagnostic_source_sha256": digest(store.root / "inputs/cloud.json"),
            "original_screenshot": image,
            "original_screenshot_sha256": digest(store.root / image),
        },
    )
    proof = {
        "schema_version": 1,
        "kind": "ops_input_repair",
        "task_id": task_id,
        "diagnosis": "Offline diagnostic image handoff; not a formal test",
        "formal_test": False,
        "pass_added": False,
        "sources": [{"path": "inputs/cloud.json", "sha256": digest(store.root / "inputs/cloud.json")}],
        "diagnostic_images": [
            {
                "path": image,
                "sha256": digest(store.root / image),
                "original_job_id": worker,
                "diagnostic_evidence": diagnostic,
                "diagnostic_sha256": digest(store.root / diagnostic),
                "source_evidence": "inputs/cloud.json",
                "source_sha256": digest(store.root / "inputs/cloud.json"),
            }
        ],
    }
    json_write(store.root / "inputs/R0001/repair.json", proof)
    return task_id, proof, values


def environment_diagnostic_fixture(store):
    task_id, proof, _ = input_repair_fixture(store)
    image = proof["diagnostic_images"][0]
    pixel_path = "inputs/ops-domestic-fixture/original.png"
    (store.root / pixel_path).parent.mkdir(parents=True)
    (store.root / pixel_path).write_bytes(PNG)
    source_path = "inputs/ops-domestic-fixture/observation.json"
    source = {
        "kind": "ops_domestic_account_menu_snapshot",
        "host": "home.console.aliyun.com",
        "target": "fixture-target",
        "screenshot": pixel_path,
        "screenshot_sha256": digest(store.root / pixel_path),
        "cloud_api_calls_performed": False,
        "resource_link_clicks_performed": False,
        "pass_added": False,
    }
    json_write(store.root / source_path, source)
    image.update(
        path=pixel_path,
        sha256=digest(store.root / pixel_path),
        source_evidence=source_path,
        source_sha256=digest(store.root / source_path),
        evidence_role="current_console_environment",
    )
    diagnostic = read_json(store.root / image["diagnostic_evidence"])
    diagnostic.update(
        kind="ops_current_console_environment_diagnostic",
        approved=True,
        task_id=task_id,
        evidence_role="current_console_environment",
        eligible_for_verdict=False,
        captured_by="ops_not_original_glm",
        limitations="Current login only, never past navigation/PASS",
        host=source["host"],
        target=source["target"],
        diagnostic_source=source_path,
        diagnostic_source_sha256=image["source_sha256"],
        original_screenshot=pixel_path,
        original_screenshot_sha256=image["sha256"],
    )
    json_write(store.root / image["diagnostic_evidence"], diagnostic)
    image["diagnostic_sha256"] = digest(store.root / image["diagnostic_evidence"])
    json_write(store.root / "inputs/R0001/repair.json", proof)
    return task_id, proof, diagnostic, source


def test_environment_original_pixels_are_scoped_repair_only_without_copy_or_formal_observation(store):
    task_id, proof, _, _ = environment_diagnostic_fixture(store)
    image = proof["diagnostic_images"][0]
    before = (store.root / image["path"]).read_bytes()
    store.queue_input_repair(task_id, "R0001/repair.json", "OPS exact current environment diagnosis")
    store.pause(False)
    repair = reserve(store, "repair")
    key, _, _ = bound(store, repair)
    result = store.inspected(repair["job_id"], key, image["path"])
    assert result["evidence_role"] == "diagnostic_only"
    assert result["eligible_for_verdict"] is False
    assert (store.root / image["path"]).read_bytes() == before
    with store.transaction() as connection:
        assert not any(image["path"] in row[0] for row in connection.execute("SELECT data FROM observations"))


@pytest.mark.parametrize("damage", ["task", "approved", "verdict", "origin", "capture", "target", "write", "click"])
def test_environment_diagnostic_rejects_unbound_or_formal_capture(store, damage):
    task_id, proof, diagnostic, source = environment_diagnostic_fixture(store)
    image = proof["diagnostic_images"][0]
    if damage == "task":
        diagnostic["task_id"] = "other:R0002"
    elif damage == "approved":
        diagnostic["approved"] = False
    elif damage == "verdict":
        diagnostic["eligible_for_verdict"] = True
    elif damage == "origin":
        diagnostic["host"] = "ram.console.aliyun.com"
    elif damage == "capture":
        diagnostic["captured_by"] = "glm"
    elif damage == "target":
        source["target"] = "other"
    elif damage == "write":
        source["cloud_api_calls_performed"] = True
    else:
        source["resource_link_clicks_performed"] = True
    json_write(store.root / image["source_evidence"], source)
    image["source_sha256"] = digest(store.root / image["source_evidence"])
    diagnostic["diagnostic_source_sha256"] = image["source_sha256"]
    json_write(store.root / image["diagnostic_evidence"], diagnostic)
    image["diagnostic_sha256"] = digest(store.root / image["diagnostic_evidence"])
    json_write(store.root / "inputs/R0001/repair.json", proof)
    with pytest.raises(WorkflowError):
        store.input_repair_proof(task_id, "R0001/repair.json")


def test_ops_input_repair_preserves_history_wire_and_pass_and_delivers_original_image(store):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize

    task_id, proof, _ = input_repair_fixture(store)
    gate = store.root / "execution-gate.sqlite3"
    initialize(gate)
    wire_before = gate.read_bytes()
    tables = ("slots", "jobs", "owned", "verdicts", "observations", "budget_reservations", "locks")
    with store.transaction() as connection:
        before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] for table in tables}
        spec = connection.execute("SELECT spec,attempt FROM tasks WHERE id=?", (task_id,)).fetchone()
    store.queue_input_repair(task_id, "R0001/repair.json", "OPS original same-resource input gap")
    assert store.status()["tasks"][0]["state"] == "repair"
    with store.transaction() as connection:
        assert {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] for table in tables
        } == before
        assert tuple(
            connection.execute(
                "SELECT spec,attempt FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        ) == tuple(spec)
    assert gate.read_bytes() == wire_before
    store.pause(False)
    repair = reserve(store, "repair")
    key, _, _ = bound(store, repair)
    context = store.context(repair["job_id"], key)
    assert context["repair_request"]["input_path"] == "R0001/repair.json"
    assert context["repair_request"]["diagnostic_only"] is True
    image = proof["diagnostic_images"][0]
    result = Runtime(store).sol_tool(context, key, "inspect", {"path": image["path"]})
    assert base64.b64decode(result["image_url"].split(",", 1)[1]) == PNG
    assert result["sha256"] == image["sha256"]
    assert result["evidence_role"] == "diagnostic_only" and result["eligible_for_verdict"] is False
    with pytest.raises(WorkflowError, match="exact bound Sol review"):
        store.accept(repair["job_id"], key, {})


@pytest.mark.parametrize("accepted", [True, False])
def test_ops_input_diagnosis_can_reclassify_an_independently_accepted_retry_not_arbitrary_repair(store, accepted):
    task_id, _, _ = input_repair_fixture(store)
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='repair' WHERE id=?", (task_id,))
        store.set_repair_scope(connection, task_id, "product")
        if not accepted:
            connection.execute("DELETE FROM events WHERE kind='acceptance_committed'")
        preserved = [tuple(row) for row in connection.execute("SELECT * FROM verdicts")]
    if not accepted:
        with pytest.raises(WorkflowError, match="accepted repair"):
            store.queue_input_repair(task_id, "R0001/repair.json", "OPS diagnoses native readonly capture gap")
        return
    store.queue_input_repair(task_id, "R0001/repair.json", "OPS diagnoses native readonly capture gap")
    with store.transaction() as connection:
        assert store.repair_scope(connection, task_id) == "input"
        assert [tuple(row) for row in connection.execute("SELECT * FROM verdicts")] == preserved


def test_repair_context_new_acceptance_supersedes_old_ops_diagnosis_without_changing_history(store):
    task_id, proof, _ = input_repair_fixture(store)
    store.queue_input_repair(task_id, "R0001/repair.json", "offline old diagnosis")
    with store.transaction() as connection:
        old_request, _ = store.repair_context(connection, task_id)
        original = [tuple(row) for row in connection.execute("SELECT * FROM verdicts ORDER BY id")]
        current = {"scene": "R0001-CREATE-creation", "status": "execution_gap", "reason": "current input shape"}
        connection.execute(
            "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
            (task_id, current["scene"], current["status"], json.dumps(current), time.time()),
        )
        store.event(connection, "acceptance_committed", {"task_id": task_id, "retry": True})
        # Another resource's newer event must not supersede this task's cause.
        store.event(connection, "ops_input_repair", {"task_id": "other:R0002", "input_path": "other.json"})
        request, trigger = store.repair_context(connection, task_id)
        assert request is None
        assert trigger["kind"] == "acceptance_committed" and trigger["decisions"] == [current]
        assert [tuple(row) for row in connection.execute("SELECT * FROM verdicts ORDER BY id")][:-1] == original
    store.pause(False)
    repair = reserve(store, "repair")
    key, _, _ = bound(store, repair)
    context = store.context(repair["job_id"], key)
    assert context["repair_request"] is None and context["repair_trigger"] == trigger
    # Superseding is not deleting: the immutable old original PNG is still inspectable.
    assert old_request["input_path"] == "R0001/repair.json"
    image = proof["diagnostic_images"][0]["path"]
    assert store.inspected(repair["job_id"], key, image)["eligible_for_verdict"] is False


def test_repair_context_receipt_binding_and_new_ops_request(store):
    task_id, _, _ = input_repair_fixture(store)
    current = {"scene": "R0001-CREATE-creation", "status": "product_gap", "reason": "current evidence"}
    path = store.root / "receipts/current-offline.json"
    json_write(path, {"task_id": task_id, "decisions": [current]})
    with store.transaction() as connection:
        store.event(
            connection,
            "acceptance_committed",
            {
                "task_id": task_id,
                "receipt_path": "receipts/current-offline.json",
                "receipt_sha256": digest(path),
            },
        )
        assert store.repair_context(connection, task_id)[1]["decisions"] == [current]
    json_write(path, {"task_id": task_id, "decisions": []})
    with store.transaction() as connection, pytest.raises(WorkflowError, match="receipt changed"):
        store.repair_context(connection, task_id)
    # An explicit newer OPS input repair remains authoritative, including its original proof.
    store.queue_input_repair(task_id, "R0001/repair.json", "offline newer diagnosis")
    with store.transaction() as connection:
        request, trigger = store.repair_context(connection, task_id)
        assert trigger["kind"] == "ops_input_repair" and request["diagnostic_only"] is True


@pytest.mark.parametrize(
    "damage",
    [
        "task",
        "formal",
        "pass",
        "source",
        "pixels",
        "diagnostic",
        "cross-resource",
        "escape",
        "duplicate",
        "job",
        "writes",
        "binding",
    ],
)
def test_ops_input_repair_rejects_unbound_changed_or_cross_resource_inputs(store, damage):
    task_id, proof, _ = input_repair_fixture(store)
    image = proof["diagnostic_images"][0]
    if damage == "task":
        proof["task_id"] = "offline:R0002"
    elif damage == "formal":
        proof["formal_test"] = True
    elif damage == "pass":
        proof["pass_added"] = True
    elif damage == "source":
        json_write(store.root / "inputs/cloud.json", {"changed": True})
    elif damage == "pixels":
        (store.root / image["path"]).write_bytes(PNG + b"changed")
    elif damage == "diagnostic":
        json_write(store.root / image["diagnostic_evidence"], {"changed": True})
    elif damage == "cross-resource":
        image["path"] = "inputs/R0002/other.png"
    elif damage == "escape":
        image["path"] = "inputs/R0001/../../jobs/other.png"
    elif damage == "duplicate":
        proof["diagnostic_images"].append(copy.deepcopy(image))
    elif damage == "job":
        image["original_job_id"] = "not-the-original"
    else:
        diagnostic = read_json(store.root / image["diagnostic_evidence"])
        field = "cloud_writes" if damage == "writes" else "original_screenshot"
        diagnostic[field] = 1 if damage == "writes" else "other.png"
        json_write(store.root / image["diagnostic_evidence"], diagnostic)
        image["diagnostic_sha256"] = digest(store.root / image["diagnostic_evidence"])
    json_write(store.root / "inputs/R0001/repair.json", proof)
    before = store.status()
    with pytest.raises(WorkflowError):
        store.queue_input_repair(task_id, "R0001/repair.json", "must refuse")
    assert store.status() == before


@pytest.mark.parametrize(
    "damage",
    [
        "unpaused",
        "active",
        "slot",
        "owned",
        "original-nonterminal",
        "original-cross-task",
    ],
)
def test_ops_input_repair_requires_drained_terminal_original_and_verified_cleanup(store, damage):
    task_id, proof, _ = input_repair_fixture(store)
    with store.transaction() as connection:
        if damage == "unpaused":
            connection.execute("UPDATE meta SET value='false' WHERE key='paused'")
        elif damage in {"active", "original-nonterminal"}:
            connection.execute(
                "UPDATE jobs SET state='running' WHERE id=?", (proof["diagnostic_images"][0]["original_job_id"],)
            )
        elif damage == "slot":
            connection.execute("UPDATE tasks SET slot=0 WHERE id=?", (task_id,))
        elif damage == "owned":
            connection.execute("INSERT INTO owned VALUES('unverified',?,'CREATE','{}','present','{}')", (task_id,))
        else:
            connection.execute(
                "UPDATE jobs SET task_id='another:R0001' WHERE id=?",
                (proof["diagnostic_images"][0]["original_job_id"],),
            )
    before = store.status()
    with pytest.raises(WorkflowError):
        store.queue_input_repair(task_id, "R0001/repair.json", "must refuse")
    assert store.status() == before


def test_ops_input_repair_is_immutable_on_reinspection_and_not_a_formal_verdict(store):
    task_id, proof, _ = input_repair_fixture(store)
    store.queue_input_repair(task_id, "R0001/repair.json", "exact original")
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task_id,))
    with pytest.raises(WorkflowError, match="already consumed"):
        store.queue_input_repair(task_id, "R0001/repair.json", "duplicate")
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='repair' WHERE id=?", (task_id,))
    store.pause(False)
    repair = reserve(store, "repair")
    key, _, _ = bound(store, repair)
    artifact = proof["diagnostic_images"][0]["path"]
    store.inspected(repair["job_id"], key, artifact)
    scene = store.context(repair["job_id"], key)["expected_scenes"][0]["id"]
    # Even a corrupt formal observation referencing inspected diagnostic pixels cannot yield PASS.
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET purpose='review' WHERE id=?", (repair["job_id"],))
        connection.execute(
            "UPDATE observations SET data=? WHERE task_id=? AND scene=?",
            (
                json.dumps({"screenshot": artifact}),
                task_id,
                scene,
            ),
        )
    with pytest.raises(WorkflowError, match="diagnostic-only"):
        store.accept(
            repair["job_id"],
            key,
            {
                "task_id": task_id,
                "attempt": 1,
                "decisions": [{"scene": scene, "status": "pass", "reason": "must refuse"}],
                "cleanup": [],
            },
        )
    with pytest.raises(WorkflowError, match="diagnostic-only"):
        store.inspected(repair["job_id"], key, artifact)
    # Repair-only inspection cannot be used in a review, even before the original files change.
    with pytest.raises(WorkflowError, match="does not belong"):
        other = "inputs/R0001/unregistered.png"
        (store.root / other).write_bytes(PNG)
        store.inspected(repair["job_id"], key, other)
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET purpose='repair' WHERE id=?", (repair["job_id"],))
    (store.root / artifact).write_bytes(PNG + b"changed")
    with pytest.raises(WorkflowError, match="changed"):
        store.context(repair["job_id"], key)
    with pytest.raises(WorkflowError, match="changed"):
        store.inspected(repair["job_id"], key, artifact)


@pytest.mark.parametrize("outcome", ["unknown", "succeeded"])
def test_ops_input_repair_cannot_bypass_unknown_or_unregistered_creation_generation(store, monkeypatch, outcome):
    from scripts.web.e2e.resource_link_workflow import execution_gate as test_execution_gate

    task_id, _, _ = input_repair_fixture(store)
    test_execution_gate.initialize(store.root / "execution-gate.sqlite3")
    monkeypatch.setattr(
        test_execution_gate,
        "creations",
        lambda *_: [
            {
                "attempt_id": "original-create",
                "state": outcome,
                "resource": {"id": "new-generation"},
                "dimension": "CREATE",
                "job_id": "original",
            }
        ],
    )
    monkeypatch.setattr("scripts.web.e2e.resource_link_workflow.store.owner_key", lambda *_: "new-generation")
    with pytest.raises(WorkflowError, match="unknown creation|original generation"):
        store.queue_input_repair(task_id, "R0001/repair.json", "must not bypass cleanup")


def test_inspect_delivers_exact_inspected_bytes_not_later_changed_file(store, monkeypatch):
    task_id, proof, _ = input_repair_fixture(store)
    store.queue_input_repair(task_id, "R0001/repair.json", "original")
    store.pause(False)
    repair = reserve(store, "repair")
    key, _, _ = bound(store, repair)
    context = store.context(repair["job_id"], key)
    image = proof["diagnostic_images"][0]
    inspected = store.inspected

    def changed_during_delivery(*args):
        result = inspected(*args)
        (store.root / image["path"]).write_bytes(PNG + b"changed-after-inspection")
        return result

    monkeypatch.setattr(store, "inspected", changed_during_delivery)
    with pytest.raises(WorkflowError, match="pixels changed before delivery"):
        Runtime(store).sol_tool(context, key, "inspect", {"path": image["path"]})


def test_preparation_repair_queues_single_sol_without_dispatch_or_pass(store, tmp_path):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize

    task_id, _, code = preparation_repair_fixture(store, tmp_path)
    gate = store.root / "execution-gate.sqlite3"
    initialize(gate)
    with sqlite3.connect(gate) as wire:
        wire.execute("INSERT INTO authority_limits(authority,id,used) VALUES('prior','create',1)")
        wire.execute("INSERT INTO active(slot,scope,expires) VALUES('0','prior:QUERY',1)")
    wire_before = gate.read_bytes()
    with store.transaction() as connection:
        tables = ("slots", "jobs", "owned", "verdicts", "observations", "budget_reservations", "locks")
        immutable_before = {
            name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name}")] for name in tables
        }
        spec_before = connection.execute("SELECT spec FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    before = store.status()
    result = Runtime(store).queue_preparation_repair(task_id, "repair.json", "verified fixture")
    assert result["no_glm_dispatched"] is True
    after = store.status()
    assert after["tasks"][0]["state"] == "repair"
    assert after["tasks"][0]["attempt"] == 0
    assert after["jobs"] == before["jobs"] == []
    assert after["completed"] == before["completed"]
    assert after["slots"] == before["slots"]
    with store.transaction() as connection:
        assert {
            name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name}")] for name in tables
        } == immutable_before
        assert connection.execute("SELECT spec FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == spec_before
    assert gate.read_bytes() == wire_before
    with pytest.raises(WorkflowError, match="never-executed"):
        Runtime(store).queue_preparation_repair(task_id, "repair.json", "duplicate")
    store.pause(False)
    job = reserve(store, "repair")
    assert job["role"] == "sol"
    key, thread, turn = bound(store, job)
    context = store.context(job["job_id"], key)
    assert context["purpose"] == "repair" and context["slot"] is None
    assert context["repair_request"]["evidence"] == "inputs/repair.json"
    assert context["repair_request"]["input_path"] == "repair.json"
    assert context["repair_request"]["sha256"] == digest(store.root / "inputs/repair.json")
    safety = context["repair_safety"]
    assert safety["job_id"] == job["job_id"]
    assert (safety["thread_id"], safety["turn_id"]) == (thread, turn)
    assert safety["repair_barrier_active"] and safety["new_execution_dispatch_blocked"]
    assert safety["glm_drained"] and safety["sole_bound_sol_repair"] and safety["product_write_allowed"]
    assert safety["nonterminal_glm_jobs"] == []
    assert safety["global_pause_required_for_bound_repair"] is False
    assert store.status()["paused"] is False
    assert store.next_action()["actions"] == []
    repaired = Runtime(store).sol_tool(
        context,
        key,
        "repair_file",
        {
            "path": "src/mapping.json",
            "before_sha256": digest(code),
            "text": '{"fixture":"alias fixed"}\n',
        },
    )
    assert repaired["restart_required"] is True
    assert read_json(code) == {"fixture": "alias fixed"}


@pytest.mark.parametrize("state", ["queued", "running", "unknown", "submitted", "complete"])
def test_repair_write_rechecks_glm_drain_after_context_read(store, tmp_path, state):
    task_id, _, code = preparation_repair_fixture(store, tmp_path)
    Runtime(store).queue_preparation_repair(task_id, "repair.json", "verified fixture")
    store.pause(False)
    job = reserve(store, "repair")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    assert context["repair_safety"]["product_write_allowed"] is True
    before = digest(code)
    # Offline fixture corruption simulates state changing after the model's read.
    # Never trust the stale context when deciding whether a source write is safe.
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO jobs SELECT 'other-glm','other-task','glm','dispatch',?,"
            "'other-thread','other-turn',capability,'confirmed',created FROM jobs WHERE id=?",
            (state, job["job_id"]),
        )
    arguments = {"path": "src/mapping.json", "before_sha256": before, "text": '{"fixture":"fixed"}\n'}
    if state == "complete":
        Runtime(store).sol_tool(context, key, "repair_file", arguments)
        assert read_json(code) == {"fixture": "fixed"}
    else:
        fresh = store.context(job["job_id"], key)["repair_safety"]
        assert fresh["nonterminal_glm_jobs"] == ["other-glm"]
        assert fresh["glm_drained"] is False and fresh["product_write_allowed"] is False
        with pytest.raises(WorkflowError, match="repair barrier and drained exact GLM"):
            Runtime(store).sol_tool(context, key, "repair_file", arguments)
        assert digest(code) == before


@pytest.mark.parametrize("changed", ["barrier", "turn", "purpose", "role"])
def test_repair_write_requires_fresh_bound_single_sol_barrier(store, tmp_path, changed):
    task_id, _, code = preparation_repair_fixture(store, tmp_path)
    Runtime(store).queue_preparation_repair(task_id, "repair.json", "verified fixture")
    store.pause(False)
    job = reserve(store, "repair")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    assert context["repair_safety"]["product_write_allowed"] is True
    before = digest(code)
    with store.transaction() as connection:
        if changed == "barrier":
            connection.execute("UPDATE tasks SET state='ready' WHERE id=?", (task_id,))
        elif changed == "turn":
            connection.execute("UPDATE jobs SET turn_id=NULL WHERE id=?", (job["job_id"],))
        elif changed == "purpose":
            connection.execute("UPDATE jobs SET purpose='prepare' WHERE id=?", (job["job_id"],))
        else:
            connection.execute("UPDATE jobs SET role='glm' WHERE id=?", (job["job_id"],))
    with pytest.raises(WorkflowError, match="repair barrier and drained exact GLM"):
        Runtime(store).sol_tool(
            context,
            key,
            "repair_file",
            {"path": "src/mapping.json", "before_sha256": before, "text": '{"fixture":"fixed"}\n'},
        )
    assert digest(code) == before


@pytest.mark.parametrize("changed", ["task", "kind", "source", "code", "controller", "no_sources"])
def test_preparation_repair_requires_current_original_evidence(store, tmp_path, changed):
    task_id, proof, code = preparation_repair_fixture(store, tmp_path)
    if changed == "task":
        proof["task_id"] = "another-task"
    elif changed == "kind":
        proof["kind"] = "cleanup"
    elif changed == "source":
        proof["sources"][0]["sha256"] = "stale"
    elif changed == "code":
        json_write(code, {"concurrent": "user change"})
    elif changed == "controller":
        proof["code_evidence"][0]["path"] = "scripts/controller.py"
    else:
        proof["sources"] = []
    json_write(store.root / "inputs/repair.json", proof)
    with pytest.raises(WorkflowError):
        Runtime(store).queue_preparation_repair(task_id, "repair.json", "fixture")
    assert store.status()["tasks"][0]["state"] == "blocked"


@pytest.mark.parametrize("guard", ["unpaused", "attempt", "slot", "job", "action", "wire_unknown", "wire_live"])
def test_preparation_repair_cannot_bypass_execution_or_unknown_state(store, tmp_path, guard):
    task_id, _, _ = preparation_repair_fixture(store, tmp_path)
    if guard == "unpaused":
        store.pause(False)
    elif guard in {"attempt", "slot"}:
        with store.transaction() as connection:
            connection.execute(f"UPDATE tasks SET {guard}=1 WHERE id=?", (task_id,))
    elif guard in {"job", "action"}:
        with store.transaction() as connection:
            connection.execute("UPDATE tasks SET state='pending' WHERE id=?", (task_id,))
        store.pause(False)
        if guard == "job":
            reserve(store, "prepare")
        else:
            store.next_action()
        store.pause(True)
        with store.transaction() as connection:
            connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task_id,))
            if guard == "action":
                # Inject a stale/racing issued action; normal pause itself invalidates old actions.
                connection.execute("UPDATE actions SET state='issued'")
    else:
        from scripts.web.e2e.resource_link_workflow.execution_gate import initialize

        gate = store.root / "execution-gate.sqlite3"
        initialize(gate)
        with sqlite3.connect(gate) as wire:
            if guard == "wire_live":
                wire.execute("INSERT INTO active(slot,scope,expires) VALUES('0','other:QUERY',9999999999)")
            else:
                wire.execute("INSERT INTO attempts(id,scope,state) VALUES('unknown','other:CREATE','unknown')")
    with pytest.raises(WorkflowError):
        Runtime(store).queue_preparation_repair(task_id, "repair.json", "fixture")
    assert store.status()["tasks"][0]["state"] == "blocked"


@pytest.mark.parametrize(
    "history",
    [
        "accepted",
        "unaccepted",
        "task_blocked",
        "outstanding_owner",
        "orphan",
        "own_task",
        "non_glm",
    ],
)
def test_preparation_repair_retains_only_independently_closed_unknown_history(store, tmp_path, history):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize

    task_id, _, _ = preparation_repair_fixture(store, tmp_path)
    old_task = "prior:R0002"
    store.import_plan({"schema_version": 1, "plan_id": "prior", "resources": [resource(2)]})
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='complete' WHERE id=?", (old_task,))
        connection.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "original",
                old_task,
                "glm",
                "dispatch",
                "complete",
                "original-thread",
                "original-turn",
                "offline-fixture",
                "result_staged",
                0,
            ),
        )
        if history != "unaccepted":
            store.event(connection, "acceptance_committed", {"task_id": old_task, "retry": False})
        if history == "task_blocked":
            connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (old_task,))
        if history == "outstanding_owner":
            connection.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (
                    "old-owner",
                    old_task,
                    "CREATE",
                    "{}",
                    "present",
                    "{}",
                ),
            )
        if history == "own_task":
            connection.execute("UPDATE jobs SET task_id=?,role='sol',purpose='prepare'", (task_id,))
        if history == "non_glm":
            connection.execute("UPDATE jobs SET role='sol'")
    gate = store.root / "execution-gate.sqlite3"
    initialize(gate)
    with sqlite3.connect(gate) as wire:
        wire.execute(
            "INSERT INTO attempts(id,scope,state) VALUES(?,?,?)",
            (
                "original-negative",
                "missing:CREATE" if history == "orphan" else "original:CREATE",
                "unknown",
            ),
        )
    before = gate.read_bytes()
    if history == "accepted":
        Runtime(store).queue_preparation_repair(task_id, "repair.json", "separate preparation gap")
        assert next(t for t in store.status()["tasks"] if t["id"] == task_id)["state"] == "repair"
        assert next(t for t in store.status()["tasks"] if t["id"] == old_task)["state"] == "complete"
    else:
        with pytest.raises(WorkflowError, match="cloud calls"):
            Runtime(store).queue_preparation_repair(task_id, "repair.json", "must not bypass history")
        assert next(t for t in store.status()["tasks"] if t["id"] == task_id)["state"] == "blocked"
    assert gate.read_bytes() == before
    with sqlite3.connect(gate) as wire:
        assert wire.execute("SELECT state FROM attempts WHERE id='original-negative'").fetchone() == ("unknown",)


def bound(store, job):
    key = token(store, job)
    previous = next(item for item in store.status()["jobs"] if item["id"] == job["job_id"])
    thread = previous["thread_id"] or ("single-sol" if job["role"] == "sol" else job["job_id"])
    turn = job["job_id"] + "-turn"
    store.bind(job["job_id"], key, thread, turn)
    return key, thread, turn


def packet(store, context):
    source = "inputs/cloud.json"
    return {
        "task_id": context["task_id"],
        "profile": "resource-link-test",
        "terraform": "ros_managed_terraform",
        "requests": {"QUERY": "请只读列出测试地域的示例资源。"},
        "execution_grants": {
            "QUERY": [
                {
                    "id": "identity",
                    "kind": "identity",
                    "quota": 2,
                    "call": {"product": "sts", "action": "GetCallerIdentity", "region_id": "test", "params": {}},
                },
                {
                    "id": "query",
                    "kind": "read",
                    "quota": 2,
                    "call": {"product": "test", "action": "DescribeExamples", "region_id": "test", "params": {}},
                },
            ]
        },
        "budget_cents": 0,
        "inputs": [{"path": source, "sha256": digest(store.root / source)}],
        "scenes": {
            scene["id"]: {
                "selector": "a.resource-link",
                "source_evidence": source,
                "source_sha256": digest(store.root / source),
                "account": "fixture",
                "region": "test",
            }
            for scene in context["expected_scenes"]
        },
        "ui": {
            "new_session_selector": '[data-app-shell="new-session"]',
            "input_selector": '[data-app-shell="composer-input"]',
            "send_selector": '[data-app-shell="composer-send"]',
            "busy_selector": ".fixture-busy",
        },
    }


def prepare(store):
    job = reserve(store, "prepare")
    key, thread, turn = bound(store, job)
    store.approve_packet(job["job_id"], key, packet(store, store.context(job["job_id"], key)))
    assert next(item for item in store.status()["jobs"] if item["id"] == job["job_id"])["state"] == "submitted"
    store.finish_job(job["job_id"], thread, turn, "completed")
    return job


def execute_worker(store, report=True):
    prepare(store)
    job = reserve(store, "dispatch")
    key, thread, turn = bound(store, job)
    if report:
        store.submit(job["job_id"], key, "Offline fixture report, not actual product PASS")
    store.finish_job(job["job_id"], thread, turn, "completed")
    return job, key


@pytest.mark.parametrize("explicit_session", [None, "ws~new", "mistyped"])
def test_failed_creation_can_capture_screenshot_without_claiming_a_created_reference(
    store, monkeypatch, explicit_session
):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene["dimension"] = "ROS"
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": context["slot"]["target"],
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(runtime, "created_references", lambda *_: pytest.fail("failure evidence is not a click"))

    def browser(request):
        assert request["operation"] == "evidence"
        assert request["session_id"] == "ws~new"
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": False, "session_id": "ws~new"}

    monkeypatch.setattr(runtime, "browser", browser)
    arguments = {"scene": scene["id"]}
    if explicit_session is not None:
        arguments["session_id"] = explicit_session
    if explicit_session == "mistyped":
        with pytest.raises(WorkflowError, match="explicit session differs"):
            runtime.tool(worker["job_id"], key, "evidence", arguments)
        return
    result = runtime.tool(worker["job_id"], key, "evidence", arguments)
    assert result["actual_ui_click"] is False
    assert read_json(output / f"{scene['id']}.json")["actual_ui_click"] is False


@pytest.mark.parametrize("source_case", ["valid", "missing", "empty", "wrong_session", "wrong_generation"])
def test_creation_evidence_binds_current_product_generation_not_preparation_input(store, monkeypatch, source_case):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension="ROS", mode="creation")
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": context["slot"]["target"],
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    source = output / "ROS-product-source.json"
    refs = [{"identifier": "fixture-created-id"}] if source_case != "empty" else []
    if source_case != "missing":
        json_write(
            source,
            {"session_id": "ws~wrong" if source_case == "wrong_session" else "ws~new", "references": {"items": refs}},
        )
    original_input_sha = digest(store.root / "inputs/cloud.json")
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    calls = []

    def proven(job, dimension, session, account, references):
        assert (job, dimension, session, account) == (worker["job_id"], "ROS", "ws~new", "fixture")
        calls.append("signed-source")
        return references

    def created(job, dimension, session, references):
        assert (job, dimension, session) == (worker["job_id"], "ROS", "ws~new")
        calls.append("wire-generation")
        return [] if source_case == "wrong_generation" else references

    monkeypatch.setattr(runtime, "proven_references", proven)
    monkeypatch.setattr(runtime, "created_references", created)

    def browser(request):
        assert request["operation"] == "evidence" and request["mode"] == "creation"
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": False, "session_id": "ws~new"}

    monkeypatch.setattr(runtime, "browser", browser)
    if source_case == "wrong_session":
        with pytest.raises(WorkflowError, match="creation source/session mismatch"):
            runtime.tool(worker["job_id"], key, "evidence", {"scene": scene["id"]})
        assert not (output / f"{scene['id']}.png").exists()
        return
    result = runtime.tool(worker["job_id"], key, "evidence", {"scene": scene["id"]})
    expected_source = source if source_case != "missing" else store.root / "inputs/cloud.json"
    assert result["source_evidence"] == expected_source.relative_to(store.root).as_posix()
    assert result["source_sha256"] == digest(expected_source)
    assert result["creation_source_verified"] is (source_case == "valid")
    assert not result["actual_ui_click"] and not result["eligible_for_automatic_verdict"]
    assert digest(store.root / "inputs/cloud.json") == original_input_sha
    assert calls == (
        []
        if source_case == "missing"
        else ["signed-source"]
        if source_case == "empty"
        else ["signed-source", "wire-generation"]
    )


@pytest.mark.parametrize("cleanup_recovery", [False, True])
def test_worker_wait_never_uses_browser_or_installs_cloud_capabilities(store, monkeypatch, cleanup_recovery):
    from scripts.web.e2e.resource_link_workflow import delays
    from scripts.web.e2e.resource_link_workflow import execution_gate as test_execution_gate

    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    context["slot"]["target"] = "fixture-target"
    if cleanup_recovery:
        evidence = store.root / "inputs/wait-cleanup-fixture.json"
        json_write(evidence, {"fixture": True})
        context["cleanup_recovery"] = {
            "original_job_id": worker["job_id"],
            "dimension": "ROS",
            "evidence": evidence.relative_to(store.root).as_posix(),
            "evidence_sha256": digest(evidence),
        }
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(runtime, "browser", lambda *_: pytest.fail("waiting never locks a browser"))
    monkeypatch.setattr(test_execution_gate, "install", lambda *_a, **_kw: pytest.fail("no new capabilities"))
    waited = []
    monkeypatch.setattr(delays.time, "sleep", waited.append)
    result = runtime.tool(worker["job_id"], key, "wait", {})
    assert waited == [30]
    assert result["cloud_calls"] == 0 and result["resource_deadline"] is None


@pytest.mark.parametrize(
    "stage,damage",
    [
        ("source", None),
        ("cleanup", None),
        ("source", "session"),
        ("source", "frozen"),
        ("cleanup", "unknown"),
        ("source", "cleanup_started"),
    ],
)
def test_read_continuation_is_bound_and_never_reinstalls_capabilities(store, monkeypatch, stage, damage):
    from scripts.web.e2e.resource_link_workflow import execution_gate as test_execution_gate
    from scripts.web.e2e.resource_link_workflow import runtime as runtime_module

    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "fixture-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    dimension = next(iter(read_json(store.root / context["packet_path"])["requests"]))
    suffix = "cleanup" if stage == "cleanup" else "request"
    json_write(output / f"{dimension}-{suffix}-intent.json", {"state": "may_have_submitted"})
    if damage == "frozen":
        json_write(output / f"{dimension}-product-source.json", {"original": True})
    elif damage == "unknown":
        json_write(output / f"{dimension}-cleanup-continue-0001.json", {"state": "may_have_submitted"})
    elif damage == "cleanup_started":
        json_write(output / f"{dimension}-cleanup-intent.json", {"state": "may_have_submitted"})
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(test_execution_gate, "install", lambda *_a, **_kw: pytest.fail("no new capabilities"))

    def continuation(path, slot, scope, session, current_stage):
        assert (scope, session, current_stage) == (f"{worker['job_id']}:{dimension}", "ws~new", stage)
        return "original read only"

    monkeypatch.setattr(runtime_module, "readonly_continuation", continuation)
    sent = []

    def browser(request):
        if request["operation"] == "snapshot":
            return {"session_id": "ws~new", "unlocked": True, "busy": False}
        assert request["operation"] == "original-read-continuation" and request["text"] == "original read only"
        sent.append(request)
        return {"submitted": True}

    monkeypatch.setattr(runtime, "browser", browser)
    arguments = {"dimension": dimension, "stage": stage}
    if damage == "session":
        arguments["session_id"] = "wrong"
    if damage:
        with pytest.raises(WorkflowError):
            runtime.tool(worker["job_id"], key, "continue-read", arguments)
        assert not sent
    else:
        runtime.tool(worker["job_id"], key, "continue-read", arguments)
        assert len(sent) == 1
        assert read_json(output / f"{dimension}-{stage}-continue-0001.json")["state"] == "submitted"


def test_session_binding_only_from_current_job_real_new_ui_result(store, monkeypatch):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    context["slot"]["target"] = "fixture-target"
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(
        runtime,
        "browser",
        lambda request: {"session_id": "previous" if request["operation"] == "snapshot" else "ws~actual-full-key"},
    )
    runtime.tool(worker["job_id"], key, "new-session", {})
    sealed = read_json(Path(context["output_dir"]) / "self-created-session.json")
    assert sealed == {
        "job_id": worker["job_id"],
        "target": "fixture-target",
        "origin": context["slot"]["origin"],
        "session_id": "ws~actual-full-key",
    }
    with pytest.raises(WorkflowError, match="one self-created"):
        runtime.tool(worker["job_id"], key, "new-session", {})
    context["slot"]["target"] = "different-worker-target"
    with pytest.raises(WorkflowError, match="lease mismatch"):
        runtime.tool(worker["job_id"], key, "close-preview", {})


@pytest.mark.parametrize("failure", [None, "busy", "locked", "session", "close", "still_open"])
def test_cleanup_closes_only_original_preview_before_phase_and_intent(store, monkeypatch, failure):
    from scripts.web.e2e.resource_link_workflow import execution_gate as test_execution_gate

    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    context["slot"]["target"] = "original-target"
    output = Path(context["output_dir"])
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "origin": context["slot"]["origin"],
            "target": "original-target",
            "session_id": "ws~original",
        },
    )
    proposal = read_json(store.root / context["packet_path"])
    proposal["cleanup_requests"] = {"ROS": "删除本会话创建的示例栈并核实消失。"}
    json_write(store.root / context["packet_path"], proposal)
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    operations = []

    def browser(request):
        assert request["target"] == "original-target"
        assert request["origin"] == context["slot"]["origin"]
        operations.append(request["operation"])
        if request["operation"] == "snapshot":
            return {
                "session_id": "different" if failure == "session" else "ws~original",
                "busy": failure == "busy",
                "unlocked": failure != "locked",
                "preview_open": len(operations) == 1 or failure == "still_open",
            }
        if request["operation"] == "close-preview":
            assert request["session_id"] == "ws~original"
            assert not (output / "ROS-cleanup-intent.json").exists()
            if failure == "close":
                raise WorkflowError("offline preview close failure")
            return {"closed_preview": True}
        assert request["operation"] == "request"
        assert (output / "ROS-cleanup-intent.json").exists()
        return {"submitted": True}

    monkeypatch.setattr(runtime, "browser", browser)

    def activate(*args):
        assert operations == ["snapshot", "close-preview", "snapshot"]
        assert args[2:4] == (worker["job_id"] + ":ROS", "ws~original")
        assert not (output / "ROS-cleanup-intent.json").exists()

    monkeypatch.setattr(test_execution_gate, "cleanup", activate)
    if failure:
        with pytest.raises(WorkflowError):
            runtime.tool(worker["job_id"], key, "cleanup", {"dimension": "ROS"})
        assert not (output / "ROS-cleanup-intent.json").exists()
        assert "request" not in operations
    else:
        assert runtime.tool(worker["job_id"], key, "cleanup", {"dimension": "ROS"})["submitted"]
        assert operations == ["snapshot", "close-preview", "snapshot", "request"]
        with pytest.raises(WorkflowError, match="cleanup already submitted or unknown"):
            runtime.tool(worker["job_id"], key, "cleanup", {"dimension": "ROS"})


@pytest.mark.parametrize("identifiers", [["vsw-new", "vsw-new"], ["vsw-new", "vsw-other"], []])
def test_creation_click_deduplicates_only_same_proven_identifier(store, monkeypatch, identifiers):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension="ROS", source_type="fixture-type")
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": context["slot"]["target"],
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    proposal = read_json(store.root / context["packet_path"])
    proposal["scenes"][scene["id"]]["selector"] = 'a[data-id="{identifier}"]'
    json_write(store.root / context["packet_path"], proposal)
    references = [{"identifier": ident, "resourceType": "fixture-type", "regionId": "test"} for ident in identifiers]
    json_write(output / "ROS-product-source.json", {"session_id": "ws~new", "references": {"items": references}})
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(runtime, "proven_references", lambda *args: args[-1])
    monkeypatch.setattr(runtime, "created_references", lambda *args: args[-1])

    def browser(request):
        assert request["selector"] == 'a[data-id="vsw-new"]'
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~new"}

    monkeypatch.setattr(runtime, "browser", browser)
    if identifiers == ["vsw-new", "vsw-new"]:
        assert runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"], "session_id": "ws~new"})
    else:
        with pytest.raises(WorkflowError, match="precise reference"):
            runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"], "session_id": "ws~new"})


@pytest.mark.parametrize("templated", [False, True])
@pytest.mark.parametrize("dimension", ["QUERY", "TF"])
def test_click_reveals_only_current_signed_exact_source_card(store, monkeypatch, templated, dimension):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension=dimension, source_type="fixture-type")
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "fixture-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    proposal = read_json(store.root / context["packet_path"])
    proposal["scenes"][scene["id"]].update(
        identifier="fixture-user",
        expected_type=scene["source_type"],
        selector='a.resource-link[aria-label="' + ("{identifier}" if templated else "fixture-user") + '"]',
    )
    json_write(store.root / context["packet_path"], proposal)
    reference = {
        "identifier": "fixture-user",
        "resourceType": scene["source_type"],
        "regionId": "test",
        "stackId": "fixture-stack",
        "logicalResourceId": "member",
        "sources": [
            {
                "toolUseId": "call_own",
                "turnId": "own-turn",
                "product": "ros",
                "action": "ListStackResources",
                "stackId": "fixture-stack",
                "logicalResourceId": "member",
                "evidence": "api_response",
                "sourceKind": "tool_result",
            }
        ],
    }
    json_write(
        output / f"{dimension}-product-source.json",
        {
            "session_id": "ws~new",
            "references": {"items": [reference]},
        },
    )
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)

    def proven(job, source_dimension, session, account, refs):
        assert (job, source_dimension, session, account) == (worker["job_id"], dimension, "ws~new", "fixture")
        return refs

    monkeypatch.setattr(runtime, "proven_references", proven)
    if dimension == "TF":

        def created(job, source_dimension, session, refs):
            assert (job, source_dimension, session) == (worker["job_id"], "TF", "ws~new")
            return refs

        monkeypatch.setattr(runtime, "created_references", created)

    def browser(request):
        assert request["selector"] == 'details[data-tool-use-id="call_own"] a.resource-link[aria-label="fixture-user"]'
        assert request["source_card_tool_use_id"] == "call_own" and request["source_card_turn_id"] == "own-turn"
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~new"}

    monkeypatch.setattr(runtime, "browser", browser)
    assert runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"], "session_id": "ws~new"})


def test_runtime_refuses_navigation_after_cleanup_without_touching_browser(store, monkeypatch):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension="ROS")
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": context["slot"]["target"],
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    json_write(output / "ROS-cleanup-intent.json", {"state": "may_have_submitted"})
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(runtime, "browser", lambda *_: pytest.fail("deleted sample must not be clicked"))
    with pytest.raises(WorkflowError, match="navigation must precede explicit cleanup"):
        runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
    assert (output / "ROS-cleanup-intent.json").is_file()
    assert not (output / f"{scene['id']}.png").exists()


def nas_composite_reference():
    return {
        "identifier": "group-fixture:standard",
        "identifierField": "PhysicalResourceId",
        "resourceType": "alicloud_nas_access_group",
        "regionId": "test",
        "accountScope": "fake-scope",
        "stackId": "stack-fixture",
        "evidence": "api_response",
        "attributes": {"AccessGroupName": "group-fixture", "FileSystemType": "standard", "AccessGroupType": "Vpc"},
        "sources": [
            {"product": "ros", "action": "ListStackResources", "evidence": "api_response", "toolUseId": "tool-fixture"}
        ],
        "aliases": [],
    }


def test_navigation_identity_preserves_source_and_other_composite_ids():
    reference = nas_composite_reference()
    before = copy.deepcopy(reference)
    assert navigation_identifier(reference) == "group-fixture"
    assert reference == before
    assert navigation_identifier({"resourceType": "other-type", "identifier": "opaque:keep:entire"}) == (
        "opaque:keep:entire"
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"identifier": "group-fixture"},
        {"identifier": "group-fixture:standard:extra"},
        {"identifier": "group-fixture:unknown"},
        {"identifier": "https://evil.invalid:standard"},
        {"identifierField": "AccessGroupName"},
        {"evidence": "tool_input"},
        {"accountScope": ""},
        {"regionId": ""},
        {"stackId": ""},
        {"sources": []},
        {
            "sources": [
                {
                    "product": "nas",
                    "action": "ListStackResources",
                    "evidence": "api_response",
                    "toolUseId": "tool-fixture",
                }
            ]
        },
        {
            "sources": [
                {"product": "ros", "action": "CreateStack", "evidence": "api_response", "toolUseId": "tool-fixture"}
            ]
        },
        {
            "sources": [
                {
                    "product": "ros",
                    "action": "ListStackResources",
                    "evidence": "tool_input",
                    "toolUseId": "tool-fixture",
                }
            ]
        },
        {"sources": [{"product": "ros", "action": "ListStackResources", "evidence": "api_response"}]},
        {"attributes": {"AccessGroupType": "Vpc", "AccessGroupName": "other-group"}},
        {"attributes": {"AccessGroupType": "Vpc", "FileSystemType": "extreme"}},
        {"attributes": {"AccessGroupType": "invented"}},
        {"attributes": {}},
        {"aliases": [{"identifierField": "AccessGroupName", "identifier": "other-group"}]},
    ],
)
def test_navigation_identity_rejects_unproven_or_conflicting_nas_projection(patch):
    reference = {**nas_composite_reference(), **patch}
    before = copy.deepcopy(reference)
    with pytest.raises(WorkflowError, match="precise NAS composite"):
        navigation_identifier(reference)
    assert reference == before


@pytest.mark.parametrize("mode", ["iframe", "open"])
@pytest.mark.parametrize("inspection", [None, "nas_access_group_metadata"])
def test_tf_composite_click_projects_only_after_proven_created_reference(store, monkeypatch, mode, inspection):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension="TF", source_type="alicloud_nas_access_group", mode=mode)
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "fixture-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    proposal = read_json(store.root / context["packet_path"])
    binding = proposal["scenes"][scene["id"]]
    binding.update(
        selector='a.resource-link[data-resource-link][href*="{identifier}"]',
        reference_types=["alicloud_nas_access_group"],
        name_prefix="group-fixture",
        render_text=["group-fixture", "group-fixture-empty-unbound"],
    )
    if inspection:
        binding.update(console_inspection=inspection, region="cn-hangzhou")
    json_write(store.root / context["packet_path"], proposal)
    source = {"session_id": "ws~new", "references": {"items": [nas_composite_reference()]}}
    if inspection:
        source["references"]["items"][0]["regionId"] = "cn-hangzhou"
    json_write(output / "TF-product-source.json", source)
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    bound_calls = []

    def proven(*args):
        bound_calls.append("signed")
        return args[-1]

    def created(*args):
        assert bound_calls == ["signed"]
        bound_calls.append("owned")
        return args[-1]

    monkeypatch.setattr(runtime, "proven_references", proven)
    monkeypatch.setattr(runtime, "created_references", created)

    def browser(request):
        assert bound_calls == ["signed", "owned"]
        assert request["selector"] == 'a.resource-link[data-resource-link][href*="group-fixture"]'
        assert request["render_identifier"] == "group-fixture"
        assert request["source_identifier"] == "group-fixture:standard"
        assert request["render_text"] == ["group-fixture", "group-fixture-empty-unbound"]
        assert request["mode"] == mode
        if inspection:
            assert request["console_inspection"] == {
                "kind": inspection,
                "identifier": "group-fixture",
                "region": "cn-hangzhou",
                "description": "group-fixture-empty-unbound",
            }
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        result = {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~new"}
        if inspection:
            supplemental = output / f"{scene['id']}-metadata.png"
            supplemental.write_bytes(PNG)
            result["console_inspection"] = {"screenshot": str(supplemental), "identity_visible": False}
        return result

    monkeypatch.setattr(runtime, "browser", browser)
    result = runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
    assert result["source_identifier"] == "group-fixture:standard"
    assert result["navigation_identifier"] == "group-fixture"
    assert read_json(output / "TF-product-source.json") == source
    assert read_json(output / f"{scene['id']}.json")["source_identifier"] == "group-fixture:standard"
    assert not store.status()["completed"]
    if inspection:
        assert result["console_inspection"]["identity_visible"] is False
        assert result["console_inspection"]["screenshot"].startswith("jobs/")


@pytest.mark.parametrize("damage", [None, "kind", "type", "name", "markers", "region", "selector"])
def test_nas_metadata_inspection_is_fixed_source_bound_and_not_a_generic_browser_capability(damage):
    from scripts.web.e2e.resource_link_workflow.runtime import console_inspection

    binding = {
        "console_inspection": "nas_access_group_metadata",
        "name_prefix": "group-fixture",
        "render_text": ["group-fixture", "original-description"],
        "region": "cn-hangzhou",
    }
    source_type, identifier = "ALIYUN::NAS::AccessGroup", "group-fixture"
    if damage == "kind":
        binding["console_inspection"] = {"selector": "delete"}
    elif damage == "type":
        source_type = "unrelated"
    elif damage == "name":
        identifier = "other-group"
    elif damage == "markers":
        binding["render_text"] = [identifier]
    elif damage == "region":
        binding["region"] = "global"
    elif damage == "selector":
        identifier = 'group"] a[href]'
    if damage is None:
        assert console_inspection(binding, source_type, identifier)["description"] == "original-description"
    else:
        with pytest.raises(WorkflowError, match="NAS metadata inspection"):
            console_inspection(binding, source_type, identifier)


@pytest.mark.parametrize("damage", [None, "alias", "type", "id", "name_prefix", "name", "family", "region", "extra"])
def test_slb_metadata_inspection_binds_original_id_name_family_and_region(damage):
    from scripts.web.e2e.resource_link_workflow.runtime import console_inspection

    binding = {
        "console_inspection": "slb_acl_metadata",
        "render_text": ["fixture-acl", "IPv4"],
        "region": "cn-hangzhou",
    }
    source_type, identifier = "ALIYUN::SLB::AccessControl", "acl-fixture123"
    if damage == "alias":
        source_type = "alicloud_slb_acl"
    elif damage == "type":
        source_type = "ALIYUN::VPC::VSwitch"
    elif damage == "id":
        identifier = 'acl-fixture"] a[href]'
    elif damage == "name_prefix":
        binding["name_prefix"] = "fixture-acl"
    elif damage == "name":
        binding["render_text"][0] = identifier
    elif damage == "family":
        binding["render_text"][1] = {"selector": "delete"}
    elif damage == "region":
        binding["region"] = "global"
    elif damage == "extra":
        binding["render_text"].append("extra")
    if damage in (None, "alias"):
        assert console_inspection(binding, source_type, identifier) == {
            "kind": "slb_acl_metadata",
            "identifier": identifier,
            "name": "fixture-acl",
            "address_family": "IPv4",
            "region": "cn-hangzhou",
        }
    else:
        with pytest.raises(WorkflowError, match="SLB metadata inspection"):
            console_inspection(binding, source_type, identifier)


@pytest.mark.parametrize(
    "damage", [None, "alias", "type", "id", "prefix", "name", "fingerprint", "region", "extra", "markers"]
)
def test_ecs_keypair_metadata_inspection_binds_exact_source_fields(damage):
    from scripts.web.e2e.resource_link_workflow.runtime import console_inspection

    fingerprint = "0123456789abcdef0123456789abcdef"
    binding = {
        "console_inspection": "ecs_keypair_metadata",
        "render_text": ["fixture-key", fingerprint],
        "region": "cn-hangzhou",
    }
    source_type, identifier = "ALIYUN::ECS::SSHKeyPair", "fixture-key"
    if damage == "alias":
        source_type = "alicloud_ecs_key_pair"
    elif damage == "type":
        source_type = "ALIYUN::VPC::VSwitch"
    elif damage == "id":
        identifier = 'fixture-key"] a[href]'
    elif damage == "prefix":
        binding["name_prefix"] = "unbound"
    elif damage == "name":
        binding["render_text"][0] = "unbound"
    elif damage == "fingerprint":
        binding["render_text"][1] = {"selector": "delete"}
    elif damage == "region":
        binding["region"] = None
    elif damage == "extra":
        binding["render_text"].append("extra")
    elif damage == "markers":
        binding["render_text"] = None
    if damage in (None, "alias"):
        assert console_inspection(binding, source_type, identifier) == {
            "kind": "ecs_keypair_metadata",
            "identifier": identifier,
            "fingerprint": fingerprint,
            "region": "cn-hangzhou",
        }
    else:
        with pytest.raises(WorkflowError, match="ECS KeyPair metadata inspection"):
            console_inspection(binding, source_type, identifier)


@pytest.mark.parametrize(
    "damage", [None, "alias", "type", "id", "prefix", "region", "api_region", "name", "display", "markers"]
)
def test_resource_group_metadata_inspection_binds_account_scope_and_approved_fields(damage):
    from scripts.web.e2e.resource_link_workflow.runtime import console_inspection

    binding = {
        "console_inspection": "resource_group_metadata",
        "render_text": ["s76fixture", "fixture display"],
        "region": "global",
        "api_region": "cn-hangzhou",
        "name_prefix": "rg-",
    }
    source_type, identifier = "ALIYUN::ResourceManager::ResourceGroup", "rg-fixture123"
    if damage == "alias":
        source_type = "alicloud_resource_manager_resource_group"
    elif damage == "type":
        source_type = "ALIYUN::VPC::VSwitch"
    elif damage == "id":
        identifier = 'rg-fixture"] a[href]'
    elif damage == "prefix":
        binding["name_prefix"] = "s76fixture"
    elif damage == "region":
        binding["region"] = "cn-hangzhou"
    elif damage == "api_region":
        binding["api_region"] = "us-west-1"
    elif damage == "name":
        binding["render_text"][0] = "bad/name"
    elif damage == "display":
        binding["render_text"][1] = identifier
    elif damage == "markers":
        binding["render_text"] = None
    if damage in (None, "alias"):
        assert console_inspection(binding, source_type, identifier) == {
            "kind": "resource_group_metadata",
            "identifier": identifier,
            "name": "s76fixture",
            "display_name": "fixture display",
            "region": "global",
            "api_region": "cn-hangzhou",
        }
    else:
        with pytest.raises(WorkflowError, match="ResourceGroup metadata inspection"):
            console_inspection(binding, source_type, identifier)


@pytest.mark.parametrize("damage", [None, "Name", "DisplayName", "Id", "missing_attributes"])
def test_resource_group_click_rejects_metadata_not_from_own_signed_source(store, monkeypatch, damage):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension="CREATE", source_type="ALIYUN::ResourceManager::ResourceGroup", mode="open")
    context["slot"]["target"] = "fixture-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "fixture-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~new",
        },
    )
    proposal = read_json(store.root / context["packet_path"])
    proposal["scenes"][scene["id"]].update(
        selector='a.resource-link[data-resource-link][href*="{identifier}"]',
        reference_types=[scene["source_type"]],
        name_prefix="rg-",
        region="global",
        api_region="cn-hangzhou",
        render_text=["s76fixture", "fixture display"],
        console_inspection="resource_group_metadata",
    )
    json_write(store.root / context["packet_path"], proposal)
    attributes = {"Id": "rg-fixture123", "Name": "s76fixture", "DisplayName": "fixture display"}
    if damage in attributes:
        attributes[damage] = "not-own-source"
    reference = {
        "resourceType": scene["source_type"],
        "identifier": "rg-fixture123",
        "scopeKind": "account",
        "attributes": None if damage == "missing_attributes" else attributes,
    }
    source = {"session_id": "ws~new", "references": {"items": [reference]}}
    json_write(output / "CREATE-product-source.json", source)
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    checks = []

    def signed(*args):
        checks.append("signed")
        return args[-1]

    def created(*args):
        checks.append("created")
        return args[-1]

    def browser(request):
        assert checks == ["signed", "created"]
        assert request["console_inspection"]["name"] == attributes["Name"]
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~new"}

    monkeypatch.setattr(runtime, "proven_references", signed)
    monkeypatch.setattr(runtime, "created_references", created)
    monkeypatch.setattr(runtime, "browser", browser)
    if damage is None:
        runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
    else:
        with pytest.raises(WorkflowError, match="own signed product source"):
            runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
    assert checks == ["signed", "created"]
    assert read_json(output / "CREATE-product-source.json") == source
    assert store.status()["completed"] == 0


def observation(store, worker, scene):
    artifact = f"jobs/{worker['job_id']}/output/{scene}.png"
    path = store.root / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    value = {
        "scene": scene,
        "screenshot": artifact,
        "source_evidence": "inputs/cloud.json",
        "source_sha256": digest(store.root / "inputs/cloud.json"),
        "session_id": "ws~full-key",
        "actual_ui_click": True,
    }
    return value


def test_sol_reads_exact_worker_raw_json_and_writes_new_verification(store):
    plan(store)
    worker, _ = execute_worker(store)
    review = reserve(store, "review")
    key, _, _ = bound(store, review)
    runtime = Runtime(store)
    original = f"jobs/{worker['job_id']}/output/cleanup-source.json"
    json_write(store.root / original, {"fixture": "original raw cleanup response"})
    result = runtime.tool(review["job_id"], key, "read_artifact", {"path": original})
    assert "original raw cleanup" in result["text"]
    with pytest.raises(WorkflowError, match="original output"):
        runtime.tool(review["job_id"], key, "read_artifact", {"path": f"jobs/{worker['job_id']}/capability.json"})
    with pytest.raises(WorkflowError, match="original output"):
        runtime.tool(review["job_id"], key, "read_artifact", {"path": "jobs/other/output/source.json"})
    value = {"source_evidence": original, "source_sha256": result["sha256"], "fixture": True}
    written = runtime.tool(review["job_id"], key, "write_verification", {"id": "cleanup-1", "value": value})
    assert read_json(store.root / written["path"]) == value
    with pytest.raises(WorkflowError, match="immutable"):
        runtime.tool(review["job_id"], key, "write_verification", {"id": "cleanup-1", "value": value})


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("credential_source", ["legacy", "shared", "unavailable"])
def test_bounded_cloud_read_preserves_once_and_timeout_blindspot(store, monkeypatch, timeout, credential_source):
    from scripts.web.e2e.resource_link_workflow import credentials

    spec = resource()
    spec["queries"] = [{"id": "hz-1", "product": "vpc", "action": "DescribeVSwitches", "parameters": {}}]
    store.import_plan({"schema_version": 1, "plan_id": "query-once", "resources": [spec]})
    store.pause(False)
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    calls = []

    @contextmanager
    def snapshot(_root):
        if credential_source == "unavailable":
            raise WorkflowError("canonical credential refresh failed")
        yield store.root / "private/fake-sts.json" if credential_source == "shared" else None

    monkeypatch.setattr(credentials, "cli_snapshot", snapshot)

    def invoke(argv, **kwargs):
        assert argv[argv.index("--profile") + 1] == "resource-link-test"
        if credential_source == "shared":
            assert argv[-2:] == ["--config-path", str(store.root / "private/fake-sts.json")]
        else:
            assert "--config-path" not in argv
        calls.append(argv)
        if timeout:
            raise subprocess.TimeoutExpired(argv, 40)
        return subprocess.CompletedProcess(argv, 0, '{"VSwitches":[]}', "")

    monkeypatch.setattr(subprocess, "run", invoke)
    runtime = Runtime(store)
    result = runtime.tool(job["job_id"], key, "cloud_read", {"id": "hz-1"})
    original = read_json(store.root / result["path"])
    assert original["failure_blind_spot"] is (timeout or credential_source == "unavailable")
    with pytest.raises(WorkflowError, match="already executed or unknown"):
        runtime.tool(job["job_id"], key, "cloud_read", {"id": "hz-1"})
    assert len(calls) == (0 if credential_source == "unavailable" else 1)


@pytest.mark.parametrize(
    "product,action,candidate,allowed",
    [
        ("ons", "OnsInstanceInServiceList", True, True),
        ("ons", "OnsInstanceInServiceList", False, False),
        ("other", "OnsInstanceInServiceList", True, False),
        ("ons", "OnsInstanceCreate", True, False),
        ("ons", "OnsInstanceDelete", True, False),
        ("ons", "OnsInstanceInServiceListExtra", True, False),
    ],
)
def test_ons_preparation_registration_and_read_keep_candidate_official_basis_and_once(
    store,
    monkeypatch,
    product,
    action,
    candidate,
    allowed,
):
    from scripts.web.e2e.resource_link_workflow import credentials

    spec = resource(83)
    spec["query_candidates"] = [{"product": product, "action": action}] if candidate else []
    store.import_plan({"schema_version": 1, "plan_id": "ons-preparation", "resources": [spec]})
    store.pause(False)
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    runtime = Runtime(store)
    query = {
        "id": "ons-hz",
        "product": product,
        "action": action,
        "parameters": {"RegionId": "cn-hangzhou"},
        "official_evidence": "inputs/cloud.json",
    }
    if not allowed:
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("rejected before cloud call"))
        with pytest.raises(WorkflowError, match="resource-specific reviewed candidate"):
            runtime.tool(job["job_id"], key, "register_query", {"query": query})
        return
    assert runtime.tool(job["job_id"], key, "register_query", {"query": query}) == {"registered": "ons-hz"}
    with pytest.raises(WorkflowError, match="immutable"):
        runtime.tool(job["job_id"], key, "register_query", {"query": {**query, "parameters": {"RegionId": "other"}}})

    @contextmanager
    def snapshot(_root):
        yield None

    monkeypatch.setattr(credentials, "cli_snapshot", snapshot)
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        assert argv == [
            "aliyun",
            "ons",
            "OnsInstanceInServiceList",
            "--profile",
            "resource-link-test",
            "--RegionId",
            "cn-hangzhou",
        ]
        return subprocess.CompletedProcess(argv, 0, '{"Data":{"InstanceVO":[]}}', "")

    monkeypatch.setattr(subprocess, "run", invoke)
    result = runtime.tool(job["job_id"], key, "cloud_read", {"id": "ons-hz"})
    assert read_json(store.root / result["path"])["response"] == {"Data": {"InstanceVO": []}}
    with pytest.raises(WorkflowError, match="already executed or unknown"):
        runtime.tool(job["job_id"], key, "cloud_read", {"id": "ons-hz"})
    assert len(calls) == 1


def test_exact_terminal_requires_own_job_capability(store):
    plan(store)
    job = reserve(store, "prepare")
    key, thread, turn = bound(store, job)
    service = Service(store)
    payload = {
        "job_id": job["job_id"],
        "token": "wrong-job-token",
        "operation": "_terminal",
        "arguments": {"thread": thread, "turn": turn, "status": "completed"},
    }
    with pytest.raises(WorkflowError, match="terminal job capability"):
        service.request(payload)
    assert store.status()["jobs"][0]["state"] == "running"
    payload["token"] = key
    assert service.request(payload) == {"finished": True}


def test_ops_binding_reads_completed_job_without_mutation_or_capabilities(store):
    plan(store)
    job = reserve(store, "prepare")
    _, thread, turn = bound(store, job)
    store.finish_job(job["job_id"], thread, turn, "completed")
    before = store.status()
    service = Service(store)
    binding = service.request({"operation": "job-binding", "arguments": {"job_id": job["job_id"]}})
    runtime = binding.pop("reader_runtime")
    assert runtime["loaded_sha256"] == digest(Path(runtime["path"]))
    assert binding == {
        "id": job["job_id"],
        "task_id": before["tasks"][0]["id"],
        "role": "sol",
        "state": "complete",
        "thread_id": thread,
        "turn_id": turn,
        "submission": "confirmed",
    }
    assert store.status() == before
    with pytest.raises(WorkflowError, match="unknown job binding"):
        service.request({"operation": "job-binding", "arguments": {"job_id": "unknown"}})


def review_fixture(store, decisions=("pass", "pass")):
    prepare(store)
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    scenes = store.context(worker["job_id"], key)["expected_scenes"]
    values = [observation(store, worker, scene["id"]) for scene in scenes]
    for value in values:
        store.record_observation(worker["job_id"], key, value)
    store.submit(worker["job_id"], key, "fixture")
    store.finish_job(worker["job_id"], thread, turn, "completed")
    review = reserve(store, "review")
    review_key, review_thread, review_turn = bound(store, review)
    for value in values:
        store.inspected(review["job_id"], review_key, value["screenshot"])
    receipt = {
        "task_id": store.context(review["job_id"], review_key)["task_id"],
        "attempt": 1,
        "decisions": [
            {
                "scene": scene["id"],
                "status": verdict,
                "reason": "offline screenshot test only",
                "identity_verified": True,
                "login_verified": True,
                "render_verified": True,
            }
            for scene, verdict in zip(scenes, decisions)
        ],
        "cleanup": [],
    }
    return review, review_key, review_thread, review_turn, receipt, values


@pytest.mark.parametrize("dimension", ["ROS", "TF", "CREATE"])
@pytest.mark.parametrize("structured", [False, True])
def test_sol_register_owned_accepts_both_original_evidence_forms_without_verdict(store, dimension, structured):
    plan(store)
    job, key, _, _, _, values = review_fixture(store)
    context = store.context(job["job_id"], key)
    with store.transaction() as connection:
        worker_id = connection.execute("SELECT id FROM jobs WHERE role='glm'").fetchone()[0]
    path = f"jobs/{worker_id}/output/creation-source.json"
    json_write(store.root / path, {"fixture": "original GLM raw response, offline only"})
    sha = digest(store.root / path)
    evidence = {"path": path, "sha256": sha} if structured else path
    resource_value = {"id": "fixture-child", "region": "fixture-region", "stack_id": "fixture-stack"}
    original = copy.deepcopy(resource_value)
    result = Runtime(store).sol_tool(
        context,
        key,
        "register_owned",
        {
            "dimension": dimension,
            "resource": resource_value,
            "evidence": evidence,
        },
    )
    assert resource_value == original
    with store.transaction() as connection:
        owned = connection.execute("SELECT * FROM owned WHERE id=?", (result["ownership_id"],)).fetchone()
        assert owned["dimension"] == dimension and owned["state"] == "present"
        assert json.loads(owned["resource"]) == original
        assert json.loads(owned["evidence"]) == {"path": path, "sha256": sha}
        assert not connection.execute("SELECT 1 FROM verdicts").fetchone()
    assert (
        Runtime(store).sol_tool(
            context,
            key,
            "register_owned",
            {
                "dimension": dimension,
                "resource": resource_value,
                "evidence": path,
            },
        )
        == result
    )
    assert digest(store.root / path) == sha
    assert store.context(job["job_id"], key)["register_owned_contract"]["operation"] == "register_owned"


def test_generation_code_requires_same_server_import_attestation(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import ownership

    runtime = Runtime(store)
    proof = runtime.attest_control()["ownership_generation"]
    assert proof["loaded_sha256"] == digest(Path(ownership.__file__))
    monkeypatch.setattr(ownership, "LOADED_SHA", "stale-generation-code")
    with pytest.raises(WorkflowError, match="restart the same Server"):
        runtime.attest_control()


def test_store_registration_does_not_reuse_same_name_deleted_prior_generation(store):
    from scripts.web.e2e.resource_link_workflow.ownership import register_generation

    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    task_id = store.context(worker["job_id"], key)["task_id"]
    prior = "a" * 32
    resource_value = {"id": "same-policy", "region": "fixture-region"}
    source = f"jobs/{worker['job_id']}/output/creation.json"
    json_write(store.root / source, {"raw": "new-generation"})
    prior_source = f"jobs/{prior}/output/creation.json"
    json_write(store.root / prior_source, {"raw": "prior-generation"})
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (prior, task_id, "glm", "dispatch", "complete", "old-thread", "old-turn", "unused", "confirmed", 0),
        )
        old = register_generation(
            connection,
            store.root,
            task_id,
            "CREATE",
            resource_value,
            prior,
            {"path": prior_source, "sha256": digest(store.root / prior_source)},
        )
        connection.execute("UPDATE owned SET state='deleted' WHERE id=?", (old,))
        original = tuple(connection.execute("SELECT * FROM owned WHERE id=?", (old,)).fetchone())
    current = store.register_owned(worker["job_id"], key, "CREATE", resource_value, source)
    assert current != old
    assert store.register_owned(worker["job_id"], key, "CREATE", resource_value, source) == current
    with store.transaction() as connection:
        assert tuple(connection.execute("SELECT * FROM owned WHERE id=?", (old,)).fetchone()) == original
        assert connection.execute("SELECT state FROM owned WHERE id=?", (current,)).fetchone()[0] == "present"


@pytest.mark.parametrize("register_current", [False, True])
def test_accept_cannot_use_old_deleted_owner_for_new_same_name_wire_creation(store, monkeypatch, register_current):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate
    from scripts.web.e2e.resource_link_workflow import phases
    from scripts.web.e2e.resource_link_workflow.ownership import register_generation

    plan(store)
    review, key, _, _, receipt, _ = review_fixture(store)
    task_id = receipt["task_id"]
    with store.transaction() as connection:
        worker = connection.execute("SELECT id FROM jobs WHERE role='glm'").fetchone()[0]
        prior = "a" * 32
        connection.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (prior, task_id, "glm", "dispatch", "complete", "old-thread", "old-turn", "unused", "confirmed", 0),
        )
        resource_value = {"id": "same-policy", "region": "fixture-region"}
        for generation in (prior, worker):
            json_write(store.root / f"jobs/{generation}/output/creation.json", {"raw": generation})
        path = f"jobs/{prior}/output/creation.json"
        old = register_generation(
            connection,
            store.root,
            task_id,
            "CREATE",
            resource_value,
            prior,
            {"path": path, "sha256": digest(store.root / path)},
        )
        connection.execute("UPDATE owned SET state='deleted' WHERE id=?", (old,))
    records = [
        {
            "attempt_id": "new-create",
            "state": "succeeded",
            "resource": resource_value,
            "dimension": "CREATE",
            "job_id": worker,
        }
    ]
    monkeypatch.setattr(gate, "creations", lambda *_: records)
    monkeypatch.setattr(phases, "assert_cleanup_reads_complete", lambda *_: None)
    if register_current:
        Runtime(store).tool(review["job_id"], key, "context", {})
        message = "cleanup must account for every owned resource"
    else:
        message = "wire-created generation missing"
    with pytest.raises(WorkflowError, match=message):
        store.accept(review["job_id"], key, receipt)
    with store.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM verdicts").fetchone()
        if register_current:
            assert connection.execute("SELECT count(*) FROM owned WHERE state='present'").fetchone()[0] == 1


def test_sol_registration_rejects_ambiguous_inputs_without_original_generation(store):
    plan(store)
    review, key, _, _, _, values = review_fixture(store)
    context = store.context(review["job_id"], key)
    with pytest.raises(WorkflowError, match="exact GLM generation"):
        Runtime(store).sol_tool(
            context,
            key,
            "register_owned",
            {
                "dimension": "CREATE",
                "resource": {"id": "same-policy", "region": "fixture-region"},
                "evidence": values[0]["source_evidence"],
            },
        )
    with store.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM owned").fetchone()


@pytest.mark.parametrize("external_verified", [False, True])
def test_accept_keeps_current_cleanup_gate_when_historical_external_cleanup_is_reviewed(
    store,
    monkeypatch,
    external_verified,
):
    from scripts.web.e2e.resource_link_workflow import additional_source, phases
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate
    from scripts.web.e2e.resource_link_workflow.ownership import register_generation

    plan(store)
    review, key, _, _, receipt, _ = review_fixture(store)
    task = receipt["task_id"]
    records = []
    with store.transaction() as connection:
        worker = connection.execute("SELECT id FROM jobs WHERE role='glm'").fetchone()[0]
        for dimension in ("ROS", "TF"):
            resource = {"id": "offline-" + dimension, "region": "fixture-region"}
            path = f"jobs/{worker}/output/{dimension}-original.json"
            json_write(store.root / path, {"offline": dimension})
            owner = register_generation(
                connection,
                store.root,
                task,
                dimension,
                resource,
                worker,
                {"path": path, "sha256": digest(store.root / path)},
            )
            connection.execute("UPDATE owned SET state='deleted' WHERE id=?", (owner,))
            records.append(
                {
                    "attempt_id": dimension,
                    "state": "succeeded",
                    "resource": resource,
                    "dimension": dimension,
                    "job_id": worker,
                }
            )
    monkeypatch.setattr(gate, "creations", lambda *_: records)
    checked = []

    def external(root, exact_task, scope, exact_review):
        assert (root, exact_task, exact_review) == (store.root, task, review["job_id"])
        return external_verified and scope == worker + ":ROS"

    def required(_path, scope):
        checked.append(scope)
        raise WorkflowError("required original cleanup: " + scope)

    monkeypatch.setattr(additional_source, "reviewed_external_cleanup", external)
    monkeypatch.setattr(phases, "assert_cleanup_reads_complete", required)
    expected_scope = worker + (":TF" if external_verified else ":ROS")
    with pytest.raises(WorkflowError, match="required original cleanup: " + expected_scope):
        store.accept(review["job_id"], key, receipt)
    assert checked == [expected_scope]
    with store.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM verdicts").fetchone()


@pytest.mark.parametrize("missing_generation", [True, False])
def test_pending_reopens_only_missing_creation_generation(store, monkeypatch, missing_generation):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    plan(store)
    review, key, thread, turn, _, _ = review_fixture(store, decisions=("execution_gap", "execution_gap"))
    store.block(review["job_id"], key, "original source evidence retained")
    store.finish_job(review["job_id"], thread, turn, "completed")
    task = store.status()["tasks"][0]
    # Offline fixture for a previously accepted/released lease, not a live recovery.
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET slot=NULL WHERE id=?", (task["id"],))
        connection.execute("UPDATE slots SET task_id=NULL WHERE task_id=?", (task["id"],))
    store.retry_blocked(task["id"], "legacy cleanup generation needs audit")
    with store.transaction() as connection:
        worker = connection.execute("SELECT id FROM jobs WHERE role='glm'").fetchone()[0]
    records = (
        [
            {
                "attempt_id": "original-create",
                "state": "succeeded",
                "resource": {"id": "same-policy", "region": "fixture-region"},
                "dimension": "CREATE",
                "job_id": worker,
            }
        ]
        if missing_generation
        else []
    )
    monkeypatch.setattr(gate, "creations", lambda *_: records)
    if missing_generation:
        store.reopen_review(task["id"], "independent generation cleanup audit only")
        current = store.status()["tasks"][0]
        assert current["state"] == "reported" and current["attempt"] == task["attempt"]
    else:
        with pytest.raises(WorkflowError, match="re-review requires"):
            store.reopen_review(task["id"], "no actual missing creation generation")
        assert store.status()["tasks"][0]["state"] == "pending"
    with store.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM owned").fetchone()
        assert not connection.execute("SELECT 1 FROM verdicts").fetchone()


@pytest.mark.parametrize(
    "patch",
    [
        {"evidence": None},
        {"evidence": []},
        {"evidence": 7},
        {"evidence": " "},
        {"evidence": {}},
        {"evidence": {"path": "fixture.json"}},
        {"evidence": {"path": "fixture.json", "sha256": 7}},
        {"evidence": {"path": "fixture.json", "sha256": "invalid"}},
        {"evidence": {"path": [], "sha256": "0" * 64}},
        {"evidence": {"path": "fixture.json", "sha256": "0" * 64, "extra": True}},
        {"evidence": "../escaped.json"},
        {"evidence": "jobs/missing/output/source.json"},
        {"resource": None},
        {"resource": []},
        {"resource": {"id": "", "region": "region"}},
        {"resource": {"id": 1, "region": "region"}},
        {"resource": {"id": "child", "region": {}}},
        {"dimension": []},
        {"dimension": "QUERY"},
        {"dimension": None},
    ],
)
def test_sol_register_owned_malformed_arguments_are_correctable_without_ledger_mutation(store, patch):
    plan(store)
    job, key, _, _, _, values = review_fixture(store)
    context = store.context(job["job_id"], key)
    arguments = {
        "dimension": "ROS",
        "resource": {"id": "child", "region": "region"},
        "evidence": values[0]["source_evidence"],
        **patch,
    }
    before = store.status()
    with pytest.raises(WorkflowError):
        Runtime(store).sol_tool(context, key, "register_owned", arguments)
    assert store.status() == before
    with store.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM owned").fetchone()


def test_sol_register_owned_stale_sha_rejects_before_ownership_and_preserves_original(store):
    plan(store)
    job, key, _, _, _, values = review_fixture(store)
    context = store.context(job["job_id"], key)
    path = values[0]["source_evidence"]
    original = (store.root / path).read_bytes()
    before = store.status()
    with pytest.raises(WorkflowError, match="original evidence SHA256 changed"):
        Runtime(store).sol_tool(
            context,
            key,
            "register_owned",
            {
                "dimension": "ROS",
                "resource": {"id": "child", "region": "region"},
                "evidence": {"path": path, "sha256": "0" * 64},
            },
        )
    assert store.status() == before
    assert (store.root / path).read_bytes() == original
    with store.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM owned").fetchone()


def test_sol_register_owned_rejects_non_review_and_contract_not_exposed_to_prepare(store):
    plan(store)
    job = reserve(store, "prepare")
    key = token(store, job)
    context = store.context(job["job_id"], key)
    assert context["register_owned_contract"] is None
    with pytest.raises(WorkflowError, match="ownership extraction is verified by Sol"):
        Runtime(store).sol_tool(context, key, "register_owned", {})


def test_init_paused_no_missing_state_replacement(tmp_path):
    with pytest.raises(WorkflowError):
        Store(tmp_path / "missing")
    store = Store.initialize(tmp_path / "state")
    assert store.next_action()["type"] == "paused"
    assert [slot["origin"] for slot in store.status()["slots"]] == [f"http://127.0.0.1:{16100 + i}" for i in range(4)]
    assert len({slot["cwd"] for slot in store.status()["slots"]}) == 4


def test_plan_frozen_and_unique_owner(store):
    value = plan(store)
    assert store.import_plan(value) == "offline"
    changed = copy.deepcopy(value)
    changed["resources"][0]["name"] = "other"
    with pytest.raises(WorkflowError, match="frozen"):
        store.import_plan(changed)
    changed = {**value, "plan_id": "other"}
    with pytest.raises(sqlite3.IntegrityError):
        store.import_plan(changed)


@pytest.mark.parametrize("mutation", ["dimensions", "terraform", "cycle", "duplicate", "nobasis"])
def test_invalid_plan(mutation):
    value = {"schema_version": 1, "plan_id": "p", "resources": [resource()]}
    item = value["resources"][0]
    if mutation == "dimensions":
        del item["dimensions"]["ROS"]
    elif mutation == "terraform":
        item["dimensions"]["TF"]["execution"] = "local_apply"
    elif mutation == "cycle":
        item["dependencies"] = [item["id"]]
    elif mutation == "duplicate":
        value["resources"].append(copy.deepcopy(item))
    else:
        del item["dimensions"]["ROS"]["official_basis"]
    with pytest.raises(WorkflowError):
        validate_plan(value)


def test_action_idempotent_and_stale(store):
    plan(store)
    action = store.next_action()["actions"][0]
    assert store.next_action()["actions"][0] == action
    store.execute(action["id"])
    assert store.execute(action["id"]) == {"stale_action_skipped": True}
    assert len(store.status()["jobs"]) == 1


@pytest.mark.parametrize("kind", ["prepare", "dispatch"])
@pytest.mark.parametrize("event", ["observation_recorded", "model_bound", "submission_boundary"])
def test_unrelated_worker_events_do_not_starve_refill(store, kind, event):
    plan(store, 2)
    prepare(store)
    sibling = reserve(store, "dispatch")
    bound(store, sibling)
    if kind == "dispatch":
        prepare(store)
    action = store.next_action()["actions"][0]
    assert action["kind"] == kind
    with store.transaction() as connection:
        store.event(connection, event, {"job_id": sibling["job_id"], "task_id": "offline:R0001"})
    assert store.status()["revision"] > action["revision"]
    result = store.execute(action["id"])
    assert result["role"] == ("sol" if kind == "prepare" else "glm")
    assert store.execute(action["id"]) == {"stale_action_skipped": True}
    assert len(store.status()["jobs"]) == 2


@pytest.mark.parametrize("change", ["paused", "task_state", "sol_lease", "repair_barrier", "dependency", "review"])
def test_prepare_rechecks_actual_scheduling_conditions(store, change):
    plan(store, 2)
    action = store.next_action()["actions"][0]
    with store.transaction() as connection:
        if change == "paused":
            # A racing old client is still refused even without normal invalidation.
            connection.execute("UPDATE meta SET value='true' WHERE key='paused'")
        elif change == "task_state":
            connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (action["task_id"],))
        elif change == "sol_lease":
            connection.execute(
                "INSERT INTO jobs VALUES('other','offline:R0002','sol','prepare','unknown',"
                "NULL,NULL,'fake','unknown',0)"
            )
        elif change == "repair_barrier":
            connection.execute("UPDATE tasks SET state='repair' WHERE id='offline:R0002'")
        elif change == "dependency":
            spec = json.loads(
                connection.execute("SELECT spec FROM tasks WHERE id=?", (action["task_id"],)).fetchone()[0]
            )
            spec["dependencies"] = ["R0002"]
            connection.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps(spec), action["task_id"]))
        else:
            connection.execute("UPDATE tasks SET state='reported' WHERE id='offline:R0002'")
    assert store.execute(action["id"]) == {"stale_action_skipped": True}
    assert not any(job["task_id"] == action["task_id"] for job in store.status()["jobs"])


@pytest.mark.parametrize("change", ["task_state", "repair_barrier", "slots", "lock", "first_wave"])
def test_dispatch_rechecks_actual_scheduling_conditions(store, change):
    plan(store, 2)
    prepare(store)
    action = store.next_action()["actions"][0]
    assert action["kind"] == "dispatch"
    with store.transaction() as connection:
        if change == "task_state":
            connection.execute("UPDATE tasks SET state='blocked' WHERE id=?", (action["task_id"],))
        elif change == "repair_barrier":
            connection.execute("UPDATE tasks SET state='repair' WHERE id='offline:R0002'")
        elif change == "slots":
            for slot in range(4):
                connection.execute("UPDATE slots SET task_id=? WHERE id=?", (f"fixture-{slot}", slot))
        elif change == "lock":
            spec = json.loads(
                connection.execute("SELECT spec FROM tasks WHERE id=?", (action["task_id"],)).fetchone()[0]
            )
            spec["locks"] = ["fixture-lock"]
            connection.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps(spec), action["task_id"]))
            connection.execute("INSERT INTO locks VALUES('fixture-lock','offline:R0002')")
        else:
            value = json.loads(connection.execute("SELECT plan FROM runs WHERE id='offline'").fetchone()[0])
            value["initial_wave"] = 4
            connection.execute("UPDATE runs SET plan=? WHERE id='offline'", (json.dumps(value),))
    assert store.execute(action["id"]) == {"stale_action_skipped": True}
    assert not any(job["role"] == "glm" for job in store.status()["jobs"])


def test_same_action_consumed_once_by_concurrent_clients(store):
    from concurrent.futures import ThreadPoolExecutor

    plan(store)
    action = store.next_action()["actions"][0]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(store.execute, [action["id"], action["id"]]))
    assert sum("job_id" in result for result in results) == 1
    assert sum(result.get("stale_action_skipped", False) for result in results) == 1
    assert len(store.status()["jobs"]) == 1


@pytest.mark.parametrize("rejected", [False, True])
def test_dispatch_retains_cumulative_budget_only_after_reviewed_increment(store, monkeypatch, rejected):
    from scripts.web.e2e.resource_link_workflow import additional_source

    plan(store)
    prepare(store)
    with store.transaction() as db:
        relative = db.execute("SELECT packet FROM tasks WHERE id='offline:R0001'").fetchone()[0]
        packet = read_json(store.root / relative)
        packet["budget_cents"] = 100
        json_write(store.root / relative, packet)
        db.execute("UPDATE runs SET budget=100 WHERE id='offline'")
        db.execute("INSERT INTO budget_reservations VALUES('offline:R0001',100)")

    def reviewed(root, actual):
        assert root == store.root and actual["budget_cents"] == 100
        if rejected:
            raise WorkflowError("fee evidence changed")
        return 0  # Full paid-review admission and drift rejection tested separately.

    monkeypatch.setattr(additional_source, "dispatch_reservation_increment", reviewed)
    action = store.next_action()["actions"][0]
    if rejected:
        with pytest.raises(WorkflowError, match="fee evidence changed"):
            store.execute(action["id"])
        assert not store.status()["jobs"]
        assert all(slot["task_id"] is None for slot in store.status()["slots"])
    else:
        assert store.execute(action["id"])["job_id"]
    with store.transaction() as db:
        assert db.execute("SELECT amount FROM budget_reservations").fetchone()[0] == 100


def test_sol_repair_can_view_original_pixels_without_acceptance_authority(store):
    plan(store, 2)
    worker, _ = execute_worker(store)
    task_id = "offline:R0001"
    relative = f"jobs/{worker['job_id']}/output/original.png"
    image = store.root / relative
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(PNG)
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET state='repair' WHERE id=?", (task_id,))
        connection.execute(
            "INSERT INTO observations VALUES('fixture',?,1,'scene',?)", (task_id, json.dumps({"screenshot": relative}))
        )
    repair = reserve(store, "repair")
    key, _, _ = bound(store, repair)
    assert store.inspected(repair["job_id"], key, relative)["sha256"] == digest(image)
    other = store.root / "inputs/R0002/unrelated.png"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(PNG)
    with pytest.raises(WorkflowError, match="does not belong"):
        store.inspected(repair["job_id"], key, other.relative_to(store.root).as_posix())
    with pytest.raises(WorkflowError, match="exact bound Sol review"):
        store.accept(repair["job_id"], key, {})
    with store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0


def test_pause_invalidates_action(store):
    plan(store)
    action = store.next_action()["actions"][0]
    store.pause(True)
    assert store.execute(action["id"])["stale_action_skipped"]


def test_review_only_blocks_new_prepare_dispatch_and_survives_resume_restart(store):
    plan(store)
    prepare(store)
    old = store.next_action()["actions"][0]
    assert old["kind"] == "dispatch"
    before = store.status()
    store.set_review_only("offline credential recovery")
    store.pause(True)
    store.pause(False)
    restored = Store(store.root)
    assert restored.status()["review_only"] == "offline credential recovery"
    waiting = restored.next_action()
    assert waiting["type"] == "wait" and waiting["actions"] == []
    assert waiting["review_only"] == "offline credential recovery"
    assert waiting["active_plans"][0]["plan_id"] == "offline"
    assert restored.execute(old["id"]) == {"stale_action_skipped": True}
    assert restored.status()["tasks"] == before["tasks"]
    assert restored.status()["jobs"] == before["jobs"]
    restored.set_review_only(None)
    assert restored.next_action()["actions"][0]["kind"] == "dispatch"


def test_review_only_allows_single_sol_review_and_repair_but_never_prepare(store):
    plan(store, 2)
    worker, _ = execute_worker(store)
    store.set_review_only("offline recovery")
    review = reserve(store, "review")
    key = token(store, review)
    store.mark_submission(review["job_id"], key)
    bound(store, review)
    assert store.next_action()["type"] == "wait"
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET state='complete' WHERE id=?", (review["job_id"],))
        connection.execute(
            "UPDATE tasks SET state='repair' WHERE id=(SELECT task_id FROM jobs WHERE id=?)", (worker["job_id"],)
        )
    repair = reserve(store, "repair")
    assert repair["role"] == "sol"
    assert store.next_action()["type"] == "wait"


def test_review_only_rejects_reserved_glm_launch_and_submission(store):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key = token(store, worker)
    store.set_review_only("offline recovery")
    with pytest.raises(WorkflowError, match="review-only"):
        Service(store).launch(worker["job_id"])
    with pytest.raises(WorkflowError, match="review-only"):
        store.mark_submission(worker["job_id"], key)
    assert not (store.root / "jobs" / worker["job_id"] / "launch.json").exists()


def test_review_only_execute_racing_action_fails_closed(store):
    plan(store)
    prepare(store)
    store.set_review_only("offline recovery")
    # Simulate an old client racing a stale action; execute rechecks the hold itself.
    with store.transaction() as connection:
        revision = int(connection.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
        task = connection.execute("SELECT id FROM tasks").fetchone()[0]
        connection.execute("INSERT INTO actions VALUES('race',?,'dispatch','issued',?)", (task, revision))
    assert store.execute("race") == {"stale_action_skipped": True, "review_only": True}
    assert not any(j["role"] == "glm" for j in store.status()["jobs"])


def test_four_slots_and_single_sol(store):
    plan(store, 5)
    for _ in range(4):
        prepare(store)
        job = reserve(store, "dispatch")
        bound(store, job)
    assert store.status()["running"] == 4
    prepare(store)  # Fifth resource may be prepared but cannot run in a fifth slot.
    assert store.next_action()["type"] == "wait"
    assert store.status()["reserved"] == 4


def test_initial_four_wave_waits_for_packets_then_rolls(store):
    value = {"schema_version": 1, "plan_id": "wave", "initial_wave": 4, "resources": [resource(i) for i in range(1, 7)]}
    store.import_plan(value)
    store.pause(False)
    for _ in range(4):
        prepare(store)
    jobs = []
    for _ in range(4):
        job = reserve(store, "dispatch")
        jobs.append((job, bound(store, job)))
    assert store.status()["running"] == 4
    prepare(store)
    prepare(store)
    assert store.next_action()["type"] == "wait"
    job, (_, thread, turn) = jobs[0]
    store.finish_job(job["job_id"], thread, turn, "completed")
    assert store.status()["reserved"] == 3
    assert store.next_action()["actions"][0]["kind"] == "review"
    with store.transaction() as connection:
        assert store.initial_wave_ready(connection, "wave")
    assert Store(store.root).status()["running"] == 3


@pytest.mark.parametrize("minimum", [True, 0, 5, "4"])
def test_initial_wave_invalid_threshold(minimum):
    with pytest.raises(WorkflowError, match="initial_wave"):
        validate_plan(
            {
                "schema_version": 1,
                "plan_id": "wave",
                "initial_wave": minimum,
                "resources": [resource(i) for i in range(1, 7)],
            }
        )


def test_initial_wave_rejects_dependency_or_shared_lock_deadlock():
    value = {
        "schema_version": 1,
        "plan_id": "wave",
        "initial_wave": 4,
        "resources": [resource(i, locks=["shared"]) for i in range(1, 7)],
    }
    with pytest.raises(WorkflowError, match="non-conflicting"):
        validate_plan(value)
    for item in value["resources"]:
        item["locks"] = []
        if item["id"] != "R0001":
            item["dependencies"] = ["R0001"]
    with pytest.raises(WorkflowError, match="independent"):
        validate_plan(value)


def test_shared_lock_does_not_starve_other_ready_resources(store):
    value = plan(store, 3)
    with store.transaction() as connection:
        for index, item in enumerate(value["resources"]):
            if index < 2:
                item["locks"] = ["shared"]
                connection.execute("UPDATE tasks SET spec=? WHERE resource_id=?", (json.dumps(item), item["id"]))
    prepare(store)
    bound(store, reserve(store, "dispatch"))
    prepare(store)
    prepare(store)
    action = store.next_action()["actions"][0]
    assert action["task_id"].endswith("R0003")


def test_self_report_does_not_release_model_lease(store):
    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    store.submit(worker["job_id"], key, "reported")
    assert store.next_action()["type"] == "wait"
    store.finish_job(worker["job_id"], thread, turn, "completed")
    assert store.next_action()["actions"][0]["kind"] == "review"


def test_accept_requires_original_view_then_freezes_pass(store):
    plan(store)
    job, key, thread, turn, receipt, values = review_fixture(store, ("pass", "execution_gap"))
    store.accept(job["job_id"], key, receipt)
    assert store.next_action()["type"] == "wait"  # Sol still in its exact turn.
    store.finish_job(job["job_id"], thread, turn, "completed")
    repair = reserve(store, "repair")
    repair_key, _, _ = bound(store, repair)
    scenes = store.context(repair["job_id"], repair_key)["expected_scenes"]
    assert [scene["mode"] for scene in scenes] == ["open"]


@pytest.mark.parametrize("damage", [None, "uninspected", "changed", "mismatch", "not_cancelled"])
def test_supplemental_metadata_cannot_replace_original_pixels_or_bypass_sol_inspection(store, damage):
    plan(store)
    job, key, _, _, receipt, values = review_fixture(store)
    artifact = values[0]["screenshot"].replace(".png", "-metadata.png")
    contained(store.root, artifact).write_bytes(PNG)
    supplemental = {
        "screenshot": artifact,
        "identity_visible": damage != "mismatch",
        "dialog_cancelled": damage != "not_cancelled",
    }
    with store.transaction() as connection:
        for row in connection.execute("SELECT id,data FROM observations WHERE task_id=?", (receipt["task_id"],)):
            value = json.loads(row["data"])
            if value["scene"] == values[0]["scene"]:
                connection.execute(
                    "UPDATE observations SET data=? WHERE id=?",
                    (json.dumps({**value, "console_inspection": supplemental}), row["id"]),
                )
    if damage != "uninspected":
        store.inspected(job["job_id"], key, artifact)
    if damage == "changed":
        contained(store.root, artifact).write_bytes(PNG + b"changed")
    if damage is None:
        store.accept(job["job_id"], key, receipt)
    else:
        with pytest.raises(WorkflowError, match="supplemental metadata screenshot"):
            store.accept(job["job_id"], key, receipt)


@pytest.mark.parametrize(
    "damage", [None, "uninspected", "changed", "mismatch", "not_completed", "writes", "kind", "original"]
)
@pytest.mark.parametrize(
    "kind",
    [
        "slb_acl_metadata",
        "ecs_keypair_metadata",
        "resource_group_metadata",
        "router_interface_vbr_child",
        "ess_scheduled_task_metadata",
        "oos_state_configuration_list",
    ],
)
def test_slb_supplemental_requires_readonly_completion_and_both_inspected_original_images(store, damage, kind):
    plan(store)
    job, key, _, _, receipt, values = review_fixture(store)
    artifact = values[0]["screenshot"].replace(".png", "-metadata.png")
    contained(store.root, artifact).write_bytes(PNG)
    supplemental = {
        "kind": "other" if damage == "kind" else kind,
        "screenshot": artifact,
        "identity_visible": damage != "mismatch",
        "readonly_completed": damage != "not_completed",
        "cloud_writes": 1 if damage == "writes" else 0,
        "original_landing_unchanged": damage != "original",
    }
    with store.transaction() as connection:
        for row in connection.execute("SELECT id,data FROM observations WHERE task_id=?", (receipt["task_id"],)):
            value = json.loads(row["data"])
            if value["scene"] == values[0]["scene"]:
                connection.execute(
                    "UPDATE observations SET data=? WHERE id=?",
                    (json.dumps({**value, "console_inspection": supplemental}), row["id"]),
                )
    if damage != "uninspected":
        store.inspected(job["job_id"], key, artifact)
    if damage == "changed":
        contained(store.root, artifact).write_bytes(PNG + b"changed")
    if damage is None:
        store.accept(job["job_id"], key, receipt)
    else:
        with pytest.raises(WorkflowError, match="supplemental metadata screenshot"):
            store.accept(job["job_id"], key, receipt)


def test_review_context_declares_exact_receipt_and_outstanding_cleanup_contract(store):
    plan(store)
    job, key, _, _, receipt, _ = review_fixture(store)
    with store.transaction() as connection:
        for identifier, state in (("historical-owner", "deleted"), ("current-owner", "present")):
            connection.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (identifier, receipt["task_id"], "ROS", "{}", state, "{}"),
            )
    contract = store.context(job["job_id"], key)["accept_contract"]
    assert contract["arguments_key"] == "receipt"
    assert contract["decisions_shape"] == "ARRAY of objects; not a scene-keyed object"
    assert "STRING" in contract["decision_entry"]["scene"]
    assert "ARRAY" in contract["cleanup_shape"]
    assert contract["cleanup_ownership_ids"] == ["current-owner"]
    assert "STRING" in contract["cleanup_entry"]["evidence"]
    assert contract["cleanup_entry"]["confirmed_absent"] is True
    assert contract["render_evidence"]["optional_decision_field"] == "render_evidence"
    assert contract["render_evidence"]["shape"]["basis"] == "original | supplemental"


@pytest.mark.parametrize(
    "field,value",
    [
        (None, None),
        (None, []),
        ("decisions", {}),
        ("decisions", "invalid"),
        ("decisions", None),
        ("decisions", ["invalid"]),
        ("decisions", [{"scene": []}]),
        ("decisions", [{"scene": None}]),
        ("decisions", [{"scene": ""}]),
        ("cleanup", {}),
        ("cleanup", "invalid"),
        ("cleanup", None),
        ("cleanup", ["invalid"]),
    ],
)
def test_malformed_receipt_is_correctable_without_ledger_mutation(store, field, value):
    plan(store)
    job, key, thread, turn, receipt, _ = review_fixture(store)
    invalid = {**receipt, field: value} if field else value
    before = store.status()
    with pytest.raises(WorkflowError, match="receipt"):
        store.accept(job["job_id"], key, invalid)
    assert store.status() == before
    with store.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0
    # The same bound review remains usable; no new turn, quota or automatic coercion.
    store.accept(job["job_id"], key, receipt)
    store.finish_job(job["job_id"], thread, turn, "completed")


@pytest.mark.parametrize("kind", ["slb_acl_metadata", "ecs_keypair_metadata", "resource_group_metadata"])
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "implicit",
        "supplemental_basis",
        "wrong_path",
        "wrong_sha",
        "no_reason",
        "wrong_basis",
        "uninspected",
        "changed",
        "writes",
        "original_changed",
        "unknown_kind",
        "source_changed",
        "unverified",
    ],
)
def test_explicit_original_visual_basis_retains_incomplete_metadata_without_bypassing_evidence(store, kind, damage):
    plan(store)
    job, key, _, _, receipt, values = review_fixture(store)
    original = values[0]["screenshot"]
    artifact = original.replace(".png", "-metadata.png")
    contained(store.root, artifact).write_bytes(PNG)
    supplemental = {
        "kind": "unknown" if damage == "unknown_kind" else kind,
        "screenshot": artifact,
        "identity_visible": False,
        "readonly_completed": True,
        "cloud_writes": 1 if damage == "writes" else 0,
        "original_landing_unchanged": damage != "original_changed",
    }
    with store.transaction() as connection:
        for row in connection.execute("SELECT id,data FROM observations WHERE task_id=?", (receipt["task_id"],)):
            value = json.loads(row["data"])
            if value["scene"] == values[0]["scene"]:
                connection.execute(
                    "UPDATE observations SET data=? WHERE id=?",
                    (json.dumps({**value, "console_inspection": supplemental}), row["id"]),
                )
    if damage != "uninspected":
        store.inspected(job["job_id"], key, artifact)
    if damage == "changed":
        contained(store.root, artifact).write_bytes(PNG + b"changed")
    if damage == "source_changed":
        contained(store.root, values[0]["source_evidence"]).write_text("{}", encoding="utf-8")
    selected = {
        "basis": "original",
        "path": original,
        "sha256": digest(contained(store.root, original)),
        "reason": "original pixels contain every own-source field; retained metadata clips its prefix",
    }
    if damage == "supplemental_basis":
        selected.update(basis="supplemental", path=artifact, sha256=digest(contained(store.root, artifact)))
    if damage == "wrong_path":
        selected["path"] = values[1]["screenshot"]
    if damage == "wrong_sha":
        selected["sha256"] = "0" * 64
    if damage == "no_reason":
        selected["reason"] = " "
    if damage == "wrong_basis":
        selected["basis"] = "dom"
    if damage != "implicit":
        receipt["decisions"][0]["render_evidence"] = selected
    if damage == "unverified":
        receipt["decisions"][0]["render_verified"] = False
    original_sha = digest(contained(store.root, original))
    if damage is None:
        store.accept(job["job_id"], key, receipt)
        assert digest(contained(store.root, original)) == original_sha
        with store.transaction() as connection:
            retained = json.loads(
                connection.execute(
                    "SELECT data FROM observations WHERE scene=?",
                    (values[0]["scene"],),
                ).fetchone()[0]
            )
            assert retained["console_inspection"] == supplemental
    else:
        with pytest.raises(WorkflowError):
            store.accept(job["job_id"], key, receipt)
        with store.transaction() as connection:
            assert connection.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0


def test_tampered_screenshot_rejects_whole_receipt(store):
    plan(store)
    job, key, _, _, receipt, values = review_fixture(store)
    (store.root / values[0]["screenshot"]).write_bytes(PNG + b"changed")
    with pytest.raises(WorkflowError, match="unchanged"):
        store.accept(job["job_id"], key, receipt)
    with store.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "proof",
        "source",
        "owner",
        "absent",
        "failed",
        "missing",
        "unverified_source",
    ],
)
def test_retained_creation_review_uses_immutable_deleted_owner_proof(store, damage):
    plan(store)
    job, key, _, _, receipt, values = review_fixture(store, ("execution_gap", "execution_gap"))
    creation_scene = "R0001-ROS-creation"
    with store.transaction() as connection:
        task = connection.execute("SELECT id,spec FROM tasks").fetchone()
        spec = json.loads(task["spec"])
        spec["dimensions"]["ROS"] = {
            "support": "supported",
            "creation_verified": False,
            "contracts": [
                {"id": "R0001-ROS-1", "source_type": spec["key"], "historical": {"iframe": "pass", "open": "pass"}}
            ],
        }
        spec["dimensions"]["QUERY"]["support"] = "not_applicable"
        connection.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps(spec), task["id"]))
        retained_observation = {**values[0], "scene": creation_scene, "actual_ui_click": False}
        if damage == "unverified_source":
            retained_observation["creation_source_verified"] = False
        connection.execute(
            "INSERT INTO observations VALUES(?,?,?,?,?)",
            (
                "retained-creation",
                receipt["task_id"],
                receipt["attempt"],
                creation_scene,
                json.dumps(retained_observation),
            ),
        )
    receipt["decisions"] = [{**receipt["decisions"][0], "scene": creation_scene, "status": "pass"}]
    owner_resource = {"id": "offline-created", "account": "offline"}
    raw = "inputs/retained-absence-source.json"
    proof = "inputs/retained-cleanup.json"
    json_write(store.root / raw, {"fixture": "offline successful bounded empty response"})
    verification = {
        "ownership_id": "retained-owner",
        "resource": owner_resource,
        "absent": True,
        "failed": False,
        "source_evidence": raw,
        "source_sha256": digest(store.root / raw),
    }
    if damage == "owner":
        verification["resource"] = {"id": "wrong-owner"}
    elif damage == "absent":
        verification["absent"] = False
    elif damage == "failed":
        verification["failed"] = True
    json_write(store.root / proof, verification)
    if damage != "missing":
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (
                    "retained-owner",
                    receipt["task_id"],
                    "ROS",
                    json.dumps(owner_resource),
                    "deleted",
                    json.dumps({"path": proof, "sha256": digest(store.root / proof)}),
                ),
            )
    if damage == "proof":
        json_write(store.root / proof, {**verification, "changed": True})
    elif damage == "source":
        json_write(store.root / raw, {"changed": True})
    if damage:
        with pytest.raises(WorkflowError, match="creation"):
            store.accept(job["job_id"], key, receipt)
        with store.transaction() as connection:
            assert connection.execute("SELECT count(*) FROM verdicts").fetchone()[0] == 0
    else:
        store.accept(job["job_id"], key, receipt)
        with store.transaction() as connection:
            spec = json.loads(connection.execute("SELECT spec FROM tasks").fetchone()[0])
            assert spec["dimensions"]["ROS"]["creation_verified"] is True
            assert connection.execute("SELECT state FROM owned").fetchone()[0] == "deleted"
        assert receipt["cleanup"] == []  # No duplicate deletion or fabricated cleanup receipt.


def test_unclicked_is_gap_not_fail(store):
    plan(store)
    job, key, _, _, receipt, _ = review_fixture(store)
    with store.transaction() as connection:
        row = connection.execute("SELECT id,data FROM observations LIMIT 1").fetchone()
        data = json.loads(row["data"])
        data["actual_ui_click"] = False
        connection.execute("UPDATE observations SET data=? WHERE id=?", (json.dumps(data), row["id"]))
    with pytest.raises(WorkflowError, match="unclicked"):
        store.accept(job["job_id"], key, receipt)


def test_open_cannot_be_unsupported_iframe(store):
    plan(store)
    job, key, _, _, receipt, _ = review_fixture(store, ("pass", "unsupported"))
    receipt["decisions"][1]["official_basis"] = "fixture policy"
    with pytest.raises(WorkflowError, match="unsupported iframe"):
        store.accept(job["job_id"], key, receipt)


def test_terminal_without_report_never_passes(store):
    plan(store)
    execute_worker(store, report=False)
    assert store.status()["tasks"][0]["state"] == "reported"
    assert store.status()["completed"] == 0


def test_restart_preserves_leases_and_exact_reconcile(store):
    plan(store)
    prepare(store)
    job = reserve(store, "dispatch")
    key, thread, turn = bound(store, job)
    store.submit(job["job_id"], key, "reported")
    store.park(job["job_id"], "Server restarted")
    restored = Store(store.root)
    assert restored.status()["jobs"][0]["state"] == "unknown"
    with pytest.raises(WorkflowError):
        reconcile(restored, job["job_id"], {"thread": {"id": "wrong", "turns": []}})
    reconcile(restored, job["job_id"], {"thread": {"id": thread, "turns": [{"id": turn, "status": "completed"}]}})
    assert restored.status()["tasks"][0]["state"] == "reported"


def test_no_second_server_lock(store):
    with exclusive(store.root / "server.lock"):
        with pytest.raises(WorkflowError):
            with exclusive(store.root / "server.lock"):
                pass


@pytest.mark.parametrize("policy_rejected", [False, True, "script"])
@pytest.mark.parametrize("paged", [False, True, "changed", "job_input", "incomplete"])
@pytest.mark.parametrize("trace_state", ["present", "missing", "empty"])
def test_dead_resume_before_turn_start_can_reconcile_without_fabricating_turn(
    store, monkeypatch, policy_rejected, paged, trace_state
):
    from datetime import datetime, timezone

    plan(store)
    first = reserve(store, "prepare")
    key, thread, turn = bound(store, first)
    store.finish_job(first["job_id"], thread, turn, "completed")
    store.retry_blocked("offline:R0001", "resume transport fixed")
    job = reserve(store, "prepare")
    if trace_state == "present" or policy_rejected:
        store.mark_submission(job["job_id"], token(store, job))
    if policy_rejected:
        store.bind(job["job_id"], token(store, job), thread, None)
    store.park(job["job_id"], "resume failed")
    folder = store.root / "jobs" / job["job_id"]
    records = [{"method": "account/updated", "params": {}}]
    if policy_rejected:
        records.extend(
            [
                {"method": "transport/thread_created", "params": {"threadId": thread}},
                {
                    "method": "transport/adapter_error",
                    "params": {
                        "message": "backend did not confirm job-workspace/script-network/never policy"
                        if policy_rejected == "script"
                        else "backend did not confirm read-only/no-network/never policy"
                    },
                },
            ]
        )
    if trace_state != "missing":
        (folder / "raw-rollout.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) if trace_state == "present" else "\n ", encoding="utf-8"
        )
    json_write(folder / "launch.json", {"pid": 123, "identity": "fixture-dead"})

    def absent(*_args):
        raise subprocess.CalledProcessError(1, ["ps"], output="")

    monkeypatch.setattr(Runtime, "assert_process", absent)
    raw = {
        "method": "thread/read",
        "read_at": datetime.now(timezone.utc).isoformat(),
        "thread": {"id": thread, "turns": [{"id": turn, "status": "completed", "items": []}]},
    }
    if trace_state != "present":
        raw["transport_evidence"] = {"path": f"jobs/{job['job_id']}/raw-rollout.jsonl", "state": trace_state}
        with store.transaction() as connection:
            raw["job_binding"] = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone())
    if paged:
        params = {"threadId": thread, "limit": 1, "sortDirection": "desc", "itemsView": "full"}
        item = {
            "id": turn,
            "status": "completed",
            "itemsView": "full",
            "items": [
                {"type": "userMessage", "text": job["job_id"] if paged == "job_input" else "original old input"},
            ],
        }
        page = folder / "ops-read-page-fixture.json"
        json_write(
            page, {"method": "thread/turns/list", "params": params, "result": {"data": [item], "nextCursor": None}}
        )
        raw["thread"]["turns"] = [{"id": turn, "status": "completed"}]
        raw["evidence_scope"] = "all_turns_full_paginated_files"
        raw["original_responses"] = {
            "metadata": {
                "method": "thread/read",
                "params": {"threadId": thread, "includeTurns": False},
                "result": {"thread": {"id": thread}},
            },
            "pages": [
                {
                    "method": "thread/turns/list",
                    "params": {**params, "itemsView": "notLoaded"},
                    "result": {"data": [{"id": turn}], "nextCursor": "missing" if paged == "incomplete" else None},
                },
                {
                    "path": page.relative_to(store.root).as_posix(),
                    "sha256": "changed" if paged == "changed" else digest(page),
                },
            ],
        }
        if paged in {"changed", "job_input", "incomplete"}:
            with pytest.raises(WorkflowError):
                reconcile(store, job["job_id"], raw)
            assert store.status()["jobs"][0]["state"] == "unknown"
            return
    if trace_state != "present" and (not paged or policy_rejected):
        with pytest.raises(WorkflowError, match="no transport trace"):
            reconcile(store, job["job_id"], raw)
        return
    assert reconcile(store, job["job_id"], raw)["terminal"] == "not_started"
    assert store.status()["completed"] == 0
    assert store.status()["tasks"][0]["state"] == "blocked"


def test_no_turn_recovery_rejects_live_adapter_and_started_transport(store, monkeypatch):
    from datetime import datetime, timezone

    plan(store)
    first = reserve(store, "prepare")
    key, thread, turn = bound(store, first)
    store.finish_job(first["job_id"], thread, turn, "completed")
    store.retry_blocked("offline:R0001", "retry")
    job = reserve(store, "prepare")
    folder = store.root / "jobs" / job["job_id"]
    trace = folder / "raw-rollout.jsonl"
    trace.write_text('{"method":"account/updated","params":{}}\n', encoding="utf-8")
    json_write(folder / "launch.json", {"pid": 123, "identity": "fixture"})
    raw = {
        "method": "thread/read",
        "read_at": datetime.now(timezone.utc).isoformat(),
        "thread": {"id": thread, "turns": [{"id": turn, "status": "completed"}]},
    }
    monkeypatch.setattr(Runtime, "assert_process", lambda *_args: None)
    with pytest.raises(WorkflowError, match="still live"):
        reconcile(store, job["job_id"], raw)
    trace.write_text(
        json.dumps({"method": "transport/thread_created", "params": {"threadId": thread}}), encoding="utf-8"
    )
    with pytest.raises(WorkflowError, match="unconfirmed submission"):
        reconcile(store, job["job_id"], raw)


@pytest.mark.parametrize("wire_touched", [False, True])
@pytest.mark.parametrize("script_policy", [False, True])
def test_policy_rejected_dispatch_releases_only_unused_wire_capability(store, monkeypatch, wire_touched, script_policy):
    from datetime import datetime, timezone

    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize

    plan(store)
    prepare(store)
    job = reserve(store, "dispatch")
    key = token(store, job)
    store.mark_submission(job["job_id"], key)
    store.bind(job["job_id"], key, "fixture-worker", None)
    store.park(job["job_id"], "policy rejected")
    folder = store.root / "jobs" / job["job_id"]
    records = [
        {"method": "transport/thread_created", "params": {"threadId": "fixture-worker"}},
        {
            "method": "transport/adapter_error",
            "params": {
                "message": (
                    "backend did not confirm job-workspace/script-network/never policy"
                    if script_policy
                    else "backend did not confirm read-only/no-network/never policy"
                )
            },
        },
    ]
    (folder / "raw-rollout.jsonl").write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    json_write(folder / "launch.json", {"pid": 123, "identity": "fixture-dead"})
    gate = store.root / "execution-gate.sqlite3"
    initialize(gate)
    if wire_touched:
        with sqlite3.connect(gate) as connection:
            connection.execute("INSERT INTO active(slot,scope) VALUES(?,?)", ("0", job["job_id"] + ":QUERY"))
    monkeypatch.setattr(Runtime, "assert_process", lambda *_: (_ for _ in ()).throw(WorkflowError("dead")))
    raw = {
        "method": "thread/read",
        "read_at": datetime.now(timezone.utc).isoformat(),
        "thread": {"id": "fixture-worker", "turns": [{"id": "prior-turn", "status": "completed"}]},
    }
    result = reconcile(store, job["job_id"], raw)
    assert result["not_accepted"] is True
    assert result["retry_safe"] is not wire_touched
    task = store.status()["tasks"][0]
    assert task["state"] == ("blocked" if wire_touched else "ready")
    assert task["slot"] == (0 if wire_touched else None)


def test_sol_can_audit_framework_read_only_and_run_one_exact_node_test(store, monkeypatch):
    plan(store)
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    runtime = Runtime(store)
    runtime.config = {"repo": str(REPO), "node": "/fixture/trusted-node"}
    result = runtime.sol_tool(context, key, "read_code", {"path": "scripts/web/e2e/resource_link_workflow/adapter.mjs"})
    assert result["sha256"] == digest(REPO / "scripts/web/e2e/resource_link_workflow/adapter.mjs")
    assert "verifyPolicy" in result["text"]
    with pytest.raises(WorkflowError, match="read_code only"):
        runtime.sol_tool(context, key, "read_code", {"path": "pyproject.toml"})
    context["purpose"] = "repair"
    with pytest.raises(WorkflowError, match="limited to product"):
        runtime.sol_tool(context, key, "repair_file", {"path": "scripts/web/e2e/resource_link_workflow/adapter.mjs"})
    argv = ["node", "--test", "tests/web/resource_link_workflow.test.mjs"]

    def offline(actual, **options):
        assert actual == ["/fixture/trusted-node", *argv[1:]]
        assert options["cwd"] == str(REPO)
        return subprocess.CompletedProcess(actual, 0, "fixture offline Node output", "")

    monkeypatch.setattr(subprocess, "run", offline)
    assert runtime.sol_tool(context, key, "offline_test", {"argv": argv})["exit_code"] == 0
    with pytest.raises(WorkflowError, match="exact workflow Node"):
        runtime.sol_tool(context, key, "offline_test", {"argv": ["node", "-e", "forbidden"]})


@pytest.mark.parametrize(
    "arguments",
    [
        ["format", "src/example.py"],
        ["check", "--fix", "src"],
        ["check", "--fix-only", "src"],
        ["check", "--unsafe-fixes", "src"],
        ["check", "--add-noqa", "src"],
        ["check", "--output-file", "src/example.py", "src"],
        ["check", "-osrc/example.py", "src"],
    ],
)
def test_offline_lint_cannot_write_during_input_repair(store, monkeypatch, arguments):
    plan(store)
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    context = {**store.context(job["job_id"], key), "purpose": "repair"}
    runtime = Runtime(store)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("must reject before executing a writer"))
    with pytest.raises(WorkflowError, match="read-only check"):
        runtime.sol_tool(context, key, "offline_test", {"argv": ["uv", "run", "ruff", *arguments]})


def test_offline_lint_overrides_configured_fix_mode(store, monkeypatch):
    plan(store)
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    context = {**store.context(job["job_id"], key), "purpose": "repair"}
    runtime = Runtime(store)
    runtime.config = {"repo": str(store.root)}

    def readonly(argv, **options):
        assert argv == ["uv", "run", "ruff", "check", "--no-fix", "--no-unsafe-fixes", "src"]
        assert options["cwd"] == str(store.root)
        return subprocess.CompletedProcess(argv, 0, "offline", "")

    monkeypatch.setattr(subprocess, "run", readonly)
    assert (
        runtime.sol_tool(context, key, "offline_test", {"argv": ["uv", "run", "ruff", "check", "src"]})["exit_code"]
        == 0
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--junitxml=src/example.py"],
        ["--junit-xml", "scripts/controller.py"],
        ["--override-ini", "addopts=--junitxml=src/example.py"],
        ["-oaddopts=--junitxml=src/example.py"],
        ["-p", "arbitrary_plugin"],
        ["-c", "outside.ini"],
        ["tests/../../outside.py"],
    ],
)
def test_offline_pytest_cannot_redirect_writes_or_configuration(store, tmp_path, monkeypatch, arguments):
    task_id, _, code = preparation_repair_fixture(store, tmp_path)
    Runtime(store).queue_preparation_repair(task_id, "repair.json", "offline fixture")
    store.pause(False)
    job = reserve(store, "repair")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    before = code.read_bytes()
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("must reject before subprocess"))
    with pytest.raises(WorkflowError):
        Runtime(store).sol_tool(context, key, "offline_test", {"argv": ["uv", "run", "pytest", *arguments]})
    assert code.read_bytes() == before


@pytest.mark.parametrize("changed", [None, "input", "glm", "turn", "barrier"])
def test_offline_pytest_rechecks_product_barrier_and_controls_addopts(store, tmp_path, monkeypatch, changed):
    task_id, _, _ = preparation_repair_fixture(store, tmp_path)
    Runtime(store).queue_preparation_repair(task_id, "repair.json", "offline fixture")
    store.pause(False)
    job = reserve(store, "repair")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    with store.transaction() as connection:
        if changed == "input":
            store.set_repair_scope(connection, task_id, "input")
        elif changed == "glm":
            connection.execute(
                "INSERT INTO jobs SELECT 'other-glm','other-task','glm','dispatch','running',"
                "'other-thread','other-turn',capability,'confirmed',created FROM jobs WHERE id=?", (job["job_id"],),
            )
        elif changed == "turn":
            connection.execute("UPDATE jobs SET turn_id=NULL WHERE id=?", (job["job_id"],))
        elif changed == "barrier":
            connection.execute("UPDATE tasks SET state='ready' WHERE id=?", (task_id,))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--junitxml=src/example.py")
    calls = []

    def offline(argv, **options):
        calls.append(argv)
        assert argv == [
            "uv", "run", "pytest", "-o", "addopts=", "tests/example.py::test_example", "-q", "-k", "example",
        ]
        assert options["env"]["PYTEST_ADDOPTS"] == ""
        # The write barrier cannot change while pytest runs.
        with sqlite3.connect(store.path, timeout=0) as concurrent:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                concurrent.execute("UPDATE tasks SET state='ready' WHERE id=?", (task_id,))
        return subprocess.CompletedProcess(argv, 0, "offline", "")

    monkeypatch.setattr(subprocess, "run", offline)
    arguments = {"argv": ["uv", "run", "pytest", "tests/example.py::test_example", "-q", "-k", "example"]}
    if changed:
        with pytest.raises(WorkflowError, match="product repair barrier"):
            Runtime(store).sol_tool(context, key, "offline_test", arguments)
        assert calls == []
    else:
        assert Runtime(store).sol_tool(context, key, "offline_test", arguments)["exit_code"] == 0
        assert len(calls) == 1


@pytest.mark.parametrize("change", ["order", "id", "quota", "budget", "call", "unchanged"])
def test_initial_query_repair_cannot_refill_cumulative_authority(store, monkeypatch, change):
    from types import SimpleNamespace

    from scripts.web.e2e.resource_link_workflow import execution_gate as gate
    from scripts.web.e2e.resource_link_workflow.creation import execution_authority

    plan(store)
    store.set_target(0, "offline-target")
    prepared = prepare(store)
    original = read_json(store.root / "packets" / prepared["job_id"] / "packet.json")
    worker = reserve(store, "dispatch")
    key, thread, turn = bound(store, worker)
    wire = store.root / "execution-gate.sqlite3"
    gate.initialize(wire)
    monkeypatch.setenv(gate.ENV, str(wire))
    monkeypatch.setenv(gate.SLOT_ENV, "0")
    authority = execution_authority(store.root, original, "QUERY")
    gate.install(
        wire,
        "0",
        worker["job_id"] + ":QUERY",
        "ws~old",
        "fixture",
        None,
        original["execution_grants"]["QUERY"],
        authority=authority,
    )
    credential = SimpleNamespace(mode="AK", access_key_id="fake", access_key_secret="fake-secret")
    for grant in original["execution_grants"]["QUERY"]:
        for _ in range(grant["quota"]):
            ticket = gate.reserve(grant["call"], "ws~old", credential, read_only=True)
            gate.finish(ticket, {"AccountId": "fixture"} if grant["kind"] == "identity" else {"Examples": []}, True)
    store.submit(worker["job_id"], key, "offline fixture")
    store.finish_job(worker["job_id"], thread, turn, "completed")
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    context = store.context(review["job_id"], key)
    store.accept(
        review["job_id"],
        key,
        {
            "task_id": context["task_id"],
            "attempt": 1,
            "cleanup": [],
            "decisions": [
                {"scene": scene["id"], "status": "execution_gap", "reason": "offline missing click"}
                for scene in context["expected_scenes"]
            ],
        },
    )
    store.finish_job(review["job_id"], thread, turn, "completed")
    repair = reserve(store, "repair")
    key, thread, turn = bound(store, repair)
    revised = copy.deepcopy(original)
    grants = revised["execution_grants"]["QUERY"]
    if change == "order":
        grants.reverse()
    elif change == "id":
        grants[0]["id"] = "another-identity"
    elif change == "quota":
        grants[0]["quota"] += 1
    elif change == "budget":
        revised["budget_cents"] += 1
    elif change == "call":
        grants[1]["call"]["params"] = {"PageSize": 1}
    if change != "unchanged":
        before = wire.read_bytes()
        with pytest.raises(WorkflowError, match="changed initial QUERY execution"):
            store.approve_packet(repair["job_id"], key, revised)
        assert wire.read_bytes() == before
    store.approve_packet(repair["job_id"], key, original)
    store.finish_job(repair["job_id"], thread, turn, "completed")
    following = reserve(store, "dispatch")
    next_key, _, _ = bound(store, following)
    runtime = Runtime(store)
    browser_session = "ws~old"

    def browser(request):
        nonlocal browser_session
        if request["operation"] == "snapshot":
            return {"session_id": browser_session, "busy": False, "unlocked": True}
        if request["operation"] == "new-session":
            browser_session = "ws~new"
            return {"session_id": browser_session}
        assert request["operation"] == "request"
        return {"submitted": True}

    monkeypatch.setattr(runtime, "browser", browser)
    runtime.tool(following["job_id"], next_key, "new-session", {})
    runtime.tool(following["job_id"], next_key, "request", {"dimension": "QUERY"})
    with pytest.raises(gate.GateDeniedError):
        gate.reserve(original["execution_grants"]["QUERY"][0]["call"], "ws~new", credential, read_only=True)
    with gate.database(wire) as connection:
        assert [(row["authority"], row["used"]) for row in connection.execute("SELECT * FROM authority_limits")] == [
            (authority, 2), (authority, 2),
        ]


def test_transport_evidence_is_original_bounded_and_resource_scoped(store):
    plan(store, count=2)
    first = reserve(store, "prepare")
    key, thread, turn = bound(store, first)
    context = store.context(first["job_id"], key)
    events = [
        {"method": "transport/policy_response", "params": {"sandbox": {"type": "readOnly"}}},
        {"method": "item/reasoning/textDelta", "params": {"delta": "must-not-expose"}},
        {"method": "turn/completed", "params": {"turn": {"id": turn, "status": "completed"}}},
    ]
    trace = store.root / "jobs" / first["job_id"] / "raw-rollout.jsonl"
    trace.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    result = Runtime(store).sol_tool(context, key, "transport_evidence", {"job_id": first["job_id"]})
    assert result["sha256"] == digest(trace)
    assert result["events"] == [{"line": 1, "event": events[0]}, {"line": 3, "event": events[2]}]
    store.finish_job(first["job_id"], thread, turn, "completed")
    second = reserve(store, "prepare")
    with pytest.raises(WorkflowError, match="this resource"):
        Runtime(store).sol_tool(context, key, "transport_evidence", {"job_id": second["job_id"]})


def test_path_escape_and_capabilities(store):
    plan(store)
    job = reserve(store, "prepare")
    with pytest.raises(WorkflowError):
        contained(store.root, "../secret")
    with pytest.raises(WorkflowError):
        store.context(job["job_id"], "bad-token")
    assert "token" not in json.dumps(store.status())


def test_backup_retains_state(store, tmp_path):
    plan(store)
    prepare(store)
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    with pytest.raises(WorkflowError):
        store.backup(backup)


def test_full_catalog_and_report_counts(tmp_path):
    catalog = WORKFLOW_DATA / "catalog.json"
    validate_plan(read_json(catalog))
    matrix = tmp_path / "matrix.html"
    render_matrix(catalog, matrix)
    assert matrix.read_text(encoding="utf-8").count("<tr class=") >= 87 * 4
    results = tmp_path / "results.html"
    render_results(None, results)
    text = results.read_text(encoding="utf-8")
    rows = [row for group in read_json(WORKFLOW_DATA / "historical-results.json")["groups"] for row in group["rows"]]
    assert text.count("<tr class=") == len(rows)
    for status in ("pass", "fail", "review"):
        assert text.count("badge " + status) == sum(list(row["status"].values()).count(status) for row in rows)
    assert "<table>" in text and "rowspan=" in text


def test_plan_generator_no_overwrite(tmp_path):
    target = tmp_path / "plan.json"
    make_plan(WORKFLOW_DATA / "catalog.json", "v2", ["R0001"], target)
    assert len(read_json(target)["resources"]) == 1
    assert read_json(target)["resources"][0]["execution_policy_version"] == 2
    with pytest.raises(WorkflowError):
        make_plan(WORKFLOW_DATA / "catalog.json", "v2", ["R0001"], target)


def test_stock_controller_not_used_for_new(store, monkeypatch):
    plan(store)
    service = Service(store)
    job = reserve(store, "prepare")
    with pytest.raises(WorkflowError, match="configure"):
        service.launch(job["job_id"])
    assert not (store.root / "jobs" / job["job_id"] / "launch.json").exists()


def test_models_cannot_repair_ledger(store):
    plan(store)
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    with pytest.raises(WorkflowError):
        Runtime(store).tool(job["job_id"], key, "repair_file", {"path": "state.sqlite3"})


def test_old_dispatch_interlock_is_program_owned(tmp_path, monkeypatch):
    from scripts.web.e2e import qwen_batch_guard as guard

    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    guard._verify_workflow_owner()
    json_write(tmp_path / "evidence/resource-link-workflow-owner.json", {"state": "owned"})
    with pytest.raises(guard.GuardError, match="workflow owns"):
        guard._verify_workflow_owner()


def test_creation_approval_default_off(store):
    value = plan(store)
    with store.transaction() as connection:
        spec = value["resources"][0]
        spec["dimensions"]["CREATE"] = {
            "support": "supported",
            "contracts": [
                {
                    "id": "R0001-CREATE-1",
                    "source_type": spec["key"],
                    "historical": {"iframe": "untested", "open": "untested"},
                }
            ],
        }
        connection.execute("UPDATE tasks SET spec=?", (json.dumps(spec),))
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    value = packet(store, context)
    value["requests"]["CREATE"] = "请创建批准的免费测试资源。"
    with pytest.raises(WorkflowError, match="explicit OPS"):
        store.approve_packet(job["job_id"], key, value)


def test_existing_account_creation_authorization_is_exact_and_independent(store):
    from scripts.web.e2e.resource_link_workflow.creation import authorization_path, validate_authorization

    spec = plan(store)["resources"][0]
    spec["dimensions"]["CREATE"] = copy.deepcopy(spec["dimensions"]["QUERY"])
    spec["dimensions"]["CREATE"]["contracts"][0]["id"] = "R0001-CREATE-1"
    with store.transaction() as connection:
        connection.execute("UPDATE tasks SET spec=?", (json.dumps(spec),))
    job = reserve(store, "prepare")
    key, _, _ = bound(store, job)
    value = packet(store, store.context(job["job_id"], key))
    value["requests"]["CREATE"] = "请先确认当前云账号，再创建已批准的免费测试资源。"
    value["cleanup_requests"] = {"CREATE": "只清理本会话本维度创建的测试资源。"}
    value["cleanup_recipe"] = "删除实际创建 ID 后精确查询不存在（offline fixture）"
    value["execution_grants"]["CREATE"] = [
        *copy.deepcopy(value["execution_grants"]["QUERY"]),
        {
            "id": "create",
            "kind": "create",
            "quota": 1,
            "call": {"product": "test", "action": "CreateExample", "region_id": "test", "params": {"Name": "rl-R0001"}},
        },
        {
            "id": "delete",
            "kind": "delete",
            "quota": 1,
            "parent": "create",
            "response_path": ["Id"],
            "target_param": "Id",
            "call": {
                "product": "test",
                "action": "DeleteExample",
                "region_id": "test",
                "params": {"Id": "$created_id"},
            },
        },
    ]
    json_write(store.root / "inputs/R0001/proposal.json", value)
    json_write(store.root / "inputs/R0001/review.json", {"fixture": "user permission and OPS exact-operation review"})
    runtime = Runtime(store)
    authorization = runtime.authorize_creation(Path("inputs/R0001/proposal.json"), "inputs/R0001/review.json", 0)
    assert authorization["identity_mode"] == "existing_account"
    assert not authorization["cloud_write_performed"]
    assert "cloud_policy_verified" not in read_json(authorization_path(store.root, value))
    validate_authorization(store.root, "R0001", value)
    changed = copy.deepcopy(value)
    changed["execution_grants"]["CREATE"][2]["call"]["params"]["Name"] = "unreviewed"
    with pytest.raises(WorkflowError, match="explicit OPS"):
        validate_authorization(store.root, "R0001", changed)
    with pytest.raises(WorkflowError, match="explicit OPS"):
        validate_authorization(store.root, "R0002", value)
    store.approve_packet(job["job_id"], key, value)


def test_unknown_wire_creation_cannot_be_omitted_from_receipt(store):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    plan(store)
    worker, _ = execute_worker(store)
    review = reserve(store, "review")
    key, _, _ = bound(store, review)
    path = store.root / "execution-gate.sqlite3"
    gate.initialize(path)
    with gate.database(path) as connection:
        scope = f"{worker['job_id']}:CREATE"
        connection.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used,parent,response_path,target_param) "
            "VALUES(?,?,?,?,?,0,?,?,?)",
            (
                scope,
                "create",
                "create",
                json.dumps({"product": "test", "action": "CreateExample", "region_id": "test", "params": {}}),
                1,
                None,
                "null",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used,parent,response_path,target_param) "
            "VALUES(?,?,?,?,?,0,?,?,?)",
            (
                scope,
                "delete",
                "delete",
                "{}",
                1,
                "create",
                '["Id"]',
                "Id",
            ),
        )
        connection.execute(
            "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created) VALUES(?,?,?,?,?,?,?,?)",
            (
                "wire-intent",
                scope,
                "create",
                "ws~full",
                "unknown",
                None,
                "fake-fingerprint",
                1,
            ),
        )
    context = Runtime(store).tool(review["job_id"], key, "context", {})
    assert len(context["creation_attempts"]) == 1
    with pytest.raises(WorkflowError, match="wire creation outcome unknown"):
        store.accept(review["job_id"], key, {"task_id": "offline:R0001", "attempt": 1, "decisions": [], "cleanup": []})


@pytest.mark.parametrize(
    "state,response",
    [
        (
            "unknown",
            {
                "status": 404,
                "headers": {"x-acs-request-id": "offline-request"},
                "body": {
                    "Code": "EntityNotExist.Policy",
                    "HostId": "ram.aliyuncs.com",
                    "RequestId": "offline-request",
                    "Message": "The policy does not exist: offline-policy",
                },
            },
        ),
        ("succeeded", {"RequestId": "offline-request", "TotalCount": 0}),
        ("unknown", None),
    ],
)
@pytest.mark.parametrize("identity_binding", ["same", "credential", "session", "authority", "late", "action"])
def test_cleanup_wire_export_preserves_raw_response_and_never_changes_journal(
    tmp_path,
    state,
    response,
    identity_binding,
):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize
    from scripts.web.e2e.resource_link_workflow.wire_evidence import cleanup_reads

    path = tmp_path / "wire.sqlite3"
    initialize(path)
    raw = json.dumps(response, indent=2) if response is not None else None
    call = {
        "product": "ram",
        "action": "GetPolicy",
        "version": "2015-05-01",
        "region_id": "cn-hangzhou",
        "params": {"PolicyName": "$created_id", "PolicyType": "Custom"},
    }
    with sqlite3.connect(path) as db:
        for scope, phase in [("worker:CREATE", "cleanup"), ("other:CREATE", "cleanup"), ("worker:QUERY", "execute")]:
            for kind in ("identity", "read"):
                policy = None if kind == "identity" else json.dumps({"phase": phase, "required_cleanup": True})
                reviewed = (
                    {"product": "sts", "action": "GetCallerIdentity", "params": {}} if kind == "identity" else call
                )
                authority = scope + "-authority"
                credential, session, created = "offline-private-credential-fingerprint", "ws~offline-full", 2
                if kind == "identity":
                    created = 3 if identity_binding == "late" else 1
                    if identity_binding == "credential":
                        credential = "other-private-credential-fingerprint"
                    if identity_binding == "session":
                        session = "ws~other-full"
                    if identity_binding == "authority":
                        authority = "other-authority"
                    if identity_binding == "action":
                        reviewed["action"] = "OtherAction"
                db.execute(
                    "INSERT INTO grants(scope,id,kind,call,quota,used,authority,read_policy) VALUES(?,?,?,?,?,?,?,?)",
                    (scope, kind, kind, json.dumps(reviewed), 3, 1, authority, policy),
                )
                result = (
                    json.dumps({"AccountId": "offline-account", "RequestId": "offline-identity"})
                    if (kind == "identity")
                    else raw
                )
                db.execute(
                    "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created,tool_use_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        scope + kind,
                        scope,
                        kind,
                        session,
                        "succeeded" if kind == "identity" else state,
                        result,
                        credential,
                        created,
                        kind + "-tool",
                    ),
                )
        before = list(db.iterdump())
    records = cleanup_reads(path, ["worker", "worker"])
    assert len(records) == 1
    record = records[0]
    assert record["job_id"] == "worker" and record["scope"] == "worker:CREATE"
    assert record["response_text"] == raw and record["response"] == response and record["state"] == state
    assert record["call"] == call  # Preserve $created_id; do not invent actual request parameters.
    assert record["used"] == 1 and record["quota"] == 3
    if identity_binding == "same":
        assert record["signed_identity"]["response"]["AccountId"] == "offline-account"
    else:
        assert record["signed_identity"] is None
    assert record["absence_verified"] is False and record["eligible_for_automatic_verdict"] is False
    assert "credential" not in record and "offline-private-credential-fingerprint" not in json.dumps(record)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_cleanup_wire_export_does_not_initialize_absent_or_legacy_journal(tmp_path):
    from scripts.web.e2e.resource_link_workflow.wire_evidence import cleanup_reads

    absent = tmp_path / "missing.sqlite3"
    assert cleanup_reads(absent, ["worker"]) == [] and not absent.exists()
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE grants(id TEXT)")
    assert cleanup_reads(legacy, ["worker"]) == []


@pytest.mark.parametrize("delete_state", ["succeeded", "unknown", "failed"])
@pytest.mark.parametrize("identity_binding", ["same", "session", "credential", "authority", "late"])
def test_execution_cleanup_export_preserves_delete_and_positive_pending_read(
    tmp_path,
    delete_state,
    identity_binding,
):
    from scripts.web.e2e.resource_link_workflow.execution_gate import initialize
    from scripts.web.e2e.resource_link_workflow.wire_evidence import cleanup_execution, cleanup_reads

    path = tmp_path / "wire.sqlite3"
    initialize(path)
    pending = {
        "RequestId": "offline-read",
        "TotalCount": 1,
        "ResourceGroups": {"ResourceGroup": [{"Id": "offline-group", "Status": "PendingDelete"}]},
    }
    deleted = {"RequestId": "offline-delete", "StackId": "offline-stack"}
    with sqlite3.connect(path) as db:
        for scope, session in [
            ("worker:CREATE", "ws~original"),
            ("worker:ROS", "ws~original"),
            ("other:CREATE", "ws~original"),
            ("worker:CREATE", "ws~other"),
        ]:
            for kind in ("identity", "delete", "read"):
                authority, credential, created = scope + "-authority", "private-fingerprint", 2
                bound_session = session
                if kind == "identity":
                    created = 3 if identity_binding == "late" else 1
                    if identity_binding == "session":
                        bound_session = "ws~unrelated"
                    elif identity_binding == "credential":
                        credential = "other-private-fingerprint"
                    elif identity_binding == "authority":
                        authority = "other-authority"
                grant = kind + session
                call = {
                    "product": "sts" if kind == "identity" else "resourcemanager",
                    "action": {
                        "identity": "GetCallerIdentity",
                        "delete": "DeleteResourceGroup",
                        "read": "ListResourceGroups",
                    }[kind],
                    "params": {} if kind == "identity" else {"ResourceGroupId": "$created_id"},
                }
                policy = json.dumps({"phase": "cleanup", "required_cleanup": True}) if kind == "read" else None
                db.execute(
                    "INSERT INTO grants(scope,id,kind,call,quota,used,authority,read_policy) VALUES(?,?,?,?,?,?,?,?)",
                    (scope, grant, kind, json.dumps(call), 1, 1, authority, policy),
                )
                response = (
                    {"AccountId": "offline-account"}
                    if kind == "identity"
                    else (deleted if kind == "delete" else pending)
                )
                db.execute(
                    "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created,"
                    "tool_use_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        scope + grant,
                        scope,
                        grant,
                        bound_session,
                        delete_state if kind == "delete" else "succeeded",
                        json.dumps(response, indent=2),
                        credential,
                        created,
                        kind + "-original-tool",
                    ),
                )
        before = list(db.iterdump())
    records = cleanup_execution(path, "worker", "CREATE", "ws~original")
    assert len(records) == 2
    by_kind = {r["kind"]: r for r in records}
    deletion = by_kind["original_cleanup_delete_response"]
    reading = by_kind["original_cleanup_wire_response"]
    assert deletion["state"] == delete_state and deletion["response"] == deleted
    assert deletion["response_text"] == json.dumps(deleted, indent=2)
    assert reading["response"] == pending
    for record in records:
        assert record["job_id"] == "worker" and record["scope"] == "worker:CREATE"
        assert record["session_id"] == "ws~original" and record["used"] == record["quota"] == 1
        assert record["absence_verified"] is False and record["eligible_for_automatic_verdict"] is False
        assert (record["signed_identity"] is not None) == (identity_binding == "same")
        assert "private-fingerprint" not in json.dumps(record) and "credential" not in record
        assert record["call"]["params"]["ResourceGroupId"] == "$created_id"
    assert all(r["kind"] == "original_cleanup_wire_response" for r in cleanup_reads(path, ["worker"]))
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_execution_cleanup_export_does_not_create_journal_or_accept_unknown_dimension(tmp_path):
    from scripts.web.e2e.resource_link_workflow.wire_evidence import cleanup_execution

    path = tmp_path / "absent.sqlite3"
    assert cleanup_execution(path, "worker", "ROS", "ws~original") == []
    assert not path.exists()
    for job, dimension, session in [
        ("", "ROS", "ws~original"),
        ("worker", "unapproved", "ws~original"),
        ("worker", "ROS", ""),
    ]:
        with pytest.raises(ValueError, match="exact job"):
            cleanup_execution(path, job, dimension, session)


@pytest.mark.parametrize("recovery", [False, True])
def test_cleanup_source_returns_original_scope_facts_without_changing_source_or_verdict(store, monkeypatch, recovery):
    from scripts.web.e2e.resource_link_workflow import runtime as module

    if recovery:
        worker, before, _ = cleanup_queue_fixture(store)
        json_write(store.root / "inputs/review.json", {"offline": True})
        store.queue_cleanup(
            before["task_id"],
            {
                "original_job_id": worker["job_id"],
                "dimension": "QUERY",
                "evidence": "inputs/review.json",
                "evidence_sha256": digest(store.root / "inputs/review.json"),
            },
        )
        job = reserve(store, "cleanup")
    else:
        plan(store)
        prepare(store)
        job = worker = reserve(store, "dispatch")
    key, _, _ = bound(store, job)
    context = store.context(job["job_id"], key)
    context["slot"]["target"] = "original-target"
    output = Path(context["output_dir"])
    original_output = store.root / "jobs" / worker["job_id"] / "output"
    json_write(
        original_output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "original-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~original",
        },
    )
    json_write(output / "QUERY-cleanup-intent.json", {"offline": True})
    monkeypatch.setattr(store, "context", lambda *_: context)
    monkeypatch.setattr(module, "assert_stack_source_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "assert_cleanup_reads_complete", lambda *_: None)
    source = output / "QUERY-cleanup-source.json"
    original = {"session_id": "ws~original", "messages": {"last": "later duplicate delete denied"}}

    def browser(_self, request):
        assert request["session_id"] == "ws~original" and request["operation"] == "source"
        json_write(source, original)
        return {"source_evidence": str(source)}

    def export(path, job_id, dimension, session):
        assert path == store.root / "execution-gate.sqlite3"
        assert (job_id, dimension, session) == (worker["job_id"], "QUERY", "ws~original")
        return [
            {
                "kind": "original_cleanup_delete_response",
                "state": "succeeded",
                "absence_verified": False,
                "eligible_for_automatic_verdict": False,
            }
        ]

    monkeypatch.setattr(Runtime, "browser", browser)
    monkeypatch.setattr(module.wire_evidence, "cleanup_execution", export)
    result = Runtime(store).tool(job["job_id"], key, "cleanup-source", {"dimension": "QUERY"})
    assert result["original_cleanup_responses"][0]["state"] == "succeeded"
    assert result["absence_verified"] is False and result["eligible_for_automatic_verdict"] is False
    assert result["sha256"] == digest(source) and read_json(source) == original
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0


def test_review_context_indexes_immutable_cleanup_envelopes_on_first_read(store, monkeypatch):
    import os

    from scripts.web.e2e.resource_link_workflow import wire_evidence

    plan(store)
    worker, _ = execute_worker(store)
    review = reserve(store, "review")
    key, _, _ = bound(store, review)
    record = {
        "job_id": worker["job_id"],
        "dimension": "CREATE",
        "attempt_id": "offline-negative",
        "tool_use_id": "offline-tool",
        "state": "unknown",
        "response_text": "original-404",
        "absence_verified": False,
    }
    monkeypatch.setattr(wire_evidence, "cleanup_reads", lambda *_: [record])
    runtime = Runtime(store)
    context = runtime.tool(review["job_id"], key, "context", {})
    entry = context["cleanup_wire_reads"][0]
    assert entry["absence_verified"] is False
    assert any(item["path"] == entry["path"] for item in context["artifact_index"])
    path = store.root / entry["path"]
    assert read_json(path) == record and digest(path) == entry["sha256"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert runtime.tool(review["job_id"], key, "context", {})["cleanup_wire_reads"] == [entry]
    assert not store.status()["can_finalize"]
    json_write(path, {**record, "response_text": "changed"})
    with pytest.raises(WorkflowError, match="wire export changed"):
        runtime.tool(review["job_id"], key, "context", {})


def test_control_attestation_includes_loaded_cleanup_wire_evidence(store, monkeypatch):
    from scripts.web.e2e.resource_link_workflow import wire_evidence

    runtime = Runtime(store)
    assert runtime.attest_control()["cleanup_wire_evidence"]["loaded_sha256"] == digest(Path(wire_evidence.__file__))
    monkeypatch.setattr(wire_evidence, "LOADED_SHA", "stale")
    with pytest.raises(WorkflowError, match="cleanup wire evidence code changed"):
        runtime.attest_control()


@pytest.mark.parametrize(
    "modes,expected",
    [
        ([], "Terraform（历史入口）"),
        (["iframe"], "ROS 托管 Terraform / 历史入口"),
        (["iframe", "open"], "ROS 托管 Terraform"),
    ],
)
def test_result_report_distinguishes_historical_and_actual_managed_tf(store, tmp_path, modes, expected):
    import re

    directory = WORKFLOW_DATA
    baseline_sha = digest(directory / "historical-results.json")
    spec = next(item for item in read_json(directory / "catalog.json")["resources"] if item["id"] == "R0021")
    store.import_plan({"schema_version": 1, "plan_id": "offline", "resources": [spec]})
    with store.transaction() as db:
        for mode in modes:
            db.execute(
                "INSERT INTO verdicts(task_id,scene,status,receipt,created) VALUES(?,?,?,?,?)",
                (
                    "offline:R0021",
                    "R0021-TF-1-" + mode,
                    "pass",
                    json.dumps(
                        {
                            "reason": "offline report fixture; not real acceptance",
                            "screenshot": "offline.png",
                            "source_evidence": "offline.json",
                        }
                    ),
                    1,
                ),
            )
    target = tmp_path / "report.html"
    render_results(store, target)
    block = target.read_text(encoding="utf-8").split(">SSHKeyPair</th>", 1)[1].split("</tr>", 1)[0]
    assert re.search(r"<td>([^<]+)<span", block).group(1) == expected
    assert digest(directory / "historical-results.json") == baseline_sha


def test_result_report_includes_unexecuted_frozen_contract_without_inventing_verdict(store, monkeypatch, tmp_path):
    from scripts.web.e2e.resource_link_workflow import reports

    directory = WORKFLOW_DATA
    baseline_sha = digest(directory / "historical-results.json")
    spec = next(item for item in read_json(directory / "catalog.json")["resources"] if item["id"] == "R0076")
    store.import_plan({"schema_version": 1, "plan_id": "offline", "resources": [spec]})
    before = store.status()
    rendered = []
    monkeypatch.setattr(reports, "document", lambda title, groups: rendered.append(copy.deepcopy(groups)) or title)
    render_results(store, tmp_path / "report.html")
    render_results(store, tmp_path / "report.html")
    assert rendered[0] == rendered[1]
    group = next(group for group in rendered[0] if group["key"] == spec["key"])
    for dimension, source in spec["dimensions"].items():
        for contract in source["contracts"]:
            matches = [
                row
                for row in group["rows"]
                if row["dimension"] == dimension and row["source_type"] == contract["source_type"]
            ]
            assert len(matches) == 1
            if dimension == "CREATE":
                assert matches[0]["status"] == {"iframe": "untested", "open": "untested"}
                assert "尚无" in matches[0]["reason"]
    assert store.status() == before
    with store.transaction() as db:
        assert not db.execute("SELECT 1 FROM verdicts").fetchone()
        assert not db.execute("SELECT 1 FROM observations").fetchone()
    assert digest(directory / "historical-results.json") == baseline_sha


@pytest.mark.parametrize(
    "owner_state,expected_status,expected_label",
    [
        (None, "untested", "不适用 · 只读"),
        ("present", "execution_gap", "仍有资源未清理"),
        ("unknown", "execution_gap", "仍有资源未清理"),
        ("deleted", "pass", "✓ 已核验清理"),
        ("missing_proof", "review", "清理证据待核"),
        ("mixed", "execution_gap", "仍有资源未清理"),
    ],
)
def test_query_sample_cleanup_report_preserves_owned_evidence(
    store,
    monkeypatch,
    tmp_path,
    owner_state,
    expected_status,
    expected_label,
):
    from scripts.web.e2e.resource_link_workflow import reports

    directory = WORKFLOW_DATA
    spec = next(item for item in read_json(directory / "catalog.json")["resources"] if item["id"] == "R0036")
    store.import_plan({"schema_version": 1, "plan_id": "offline", "resources": [spec]})
    rendered = []
    monkeypatch.setattr(reports, "document", lambda title, groups: rendered.append(copy.deepcopy(groups)) or title)
    render_results(store, tmp_path / "before.html")
    original = next(group for group in rendered[-1] if group["key"] == spec["key"])
    original_status = next(row["status"] for row in original["rows"] if row["dimension"] == "QUERY")
    baseline_sha = digest(directory / "historical-results.json")

    if owner_state is not None:
        resource = {"id": "offline-cpfs", "region": "cn-hangzhou"}
        source = store.root / "inputs/offline-absence.json"
        proof = store.root / "inputs/offline-cleanup.json"
        json_write(source, {"offline_fixture": True, "FileSystems": []})
        json_write(
            proof,
            {
                "ownership_id": "offline-owner",
                "resource": resource,
                "absent": True,
                "failed": False,
                "source_evidence": source.relative_to(store.root).as_posix(),
                "source_sha256": digest(source),
            },
        )
        binding = {"path": proof.relative_to(store.root).as_posix(), "sha256": digest(proof)}
        state = "deleted" if owner_state in {"deleted", "missing_proof", "mixed"} else owner_state
        with store.transaction() as db:
            db.execute(
                "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                (
                    "offline-owner",
                    "offline:R0036",
                    "QUERY",
                    json.dumps(resource),
                    state,
                    json.dumps({} if owner_state == "missing_proof" else binding),
                ),
            )
            if owner_state == "mixed":
                db.execute(
                    "INSERT INTO owned VALUES(?,?,?,?,?,?)",
                    (
                        "offline-dependency",
                        "offline:R0036",
                        "QUERY",
                        "{}",
                        "present",
                        "{}",
                    ),
                )

    before = store.status()
    render_results(store, tmp_path / "after.html")
    group = next(group for group in rendered[-1] if group["key"] == spec["key"])
    row = next(row for row in group["rows"] if row["dimension"] == "QUERY")
    assert row["cleanup"]["status"] == expected_status
    assert row["cleanup"]["label"] == expected_label
    assert row["creation"]["status"] == "untested"
    assert row["status"] == original_status
    if owner_state is not None:
        assert row["creation"]["label"] == "不适用 · 查询导航"
        assert "临时样本" in row["cleanup"]["note"]
    if owner_state == "deleted":
        assert len(row["cleanup"]["links"]) == 1
    assert store.status() == before
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert digest(directory / "historical-results.json") == baseline_sha


def test_missing_cleanup_never_releases_slot(store):
    plan(store)
    job, key, _, _, receipt, _ = review_fixture(store)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            ("owned-fixture", receipt["task_id"], "CREATE", "{}", "present", "{}"),
        )
    with pytest.raises(WorkflowError, match="every owned"):
        store.accept(job["job_id"], key, receipt)
    assert any(slot["task_id"] for slot in store.status()["slots"])


def test_unknown_submission_cannot_launch_again(store):
    plan(store)
    job = reserve(store, "prepare")
    key = token(store, job)
    store.mark_submission(job["job_id"], key)
    store.park(job["job_id"], "unknown thread creation")
    with pytest.raises(WorkflowError, match="not repeatable"):
        Service(store).launch(job["job_id"])


def test_no_sample_classification_needs_bounded_proof(store):
    plan(store)
    job = reserve(store, "prepare")
    key, thread, turn = bound(store, job)
    with pytest.raises(WorkflowError):
        store.classify_preparation(job["job_id"], key)
    json_write(
        store.root / "inputs/coverage.json",
        {"resources_found": 0, "coverage": [{"region": "test", "api": "ListExample"}], "failure_blind_spots": []},
    )
    store.classify_preparation(job["job_id"], key, "inputs/coverage.json")
    store.finish_job(job["job_id"], thread, turn, "completed")
    assert store.status()["can_finalize"]


def _serve_http_restart_fixture(root):
    import os
    from unittest.mock import patch

    from scripts.web.e2e.resource_link_workflow.server import serve

    native = Runtime.process_identity

    def identity(pid):
        return "offline test controller" if pid == os.getpid() else native(pid)

    with patch.object(Runtime, "process_identity", staticmethod(identity)):
        serve(Path(root), 0)


def test_http_restart_does_not_reset_actions(store):
    import multiprocessing
    import time

    from scripts.web.e2e.resource_link_workflow.__main__ import call

    plan(store)

    def start():
        # The Windows venv executable is a redirector. Multiprocessing launches
        # the actual interpreter so Process.pid identifies the serving process.
        process = multiprocessing.get_context("spawn").Process(
            target=_serve_http_restart_fixture, args=(str(store.root),),
        )
        process.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                descriptor = read_json(store.root / "server.json")
                if descriptor["pid"] == process.pid:
                    call(store.root, "status")
                    return process
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        if process.is_alive():
            process.terminate()
        process.join(timeout=3)
        pytest.fail(f"test Server did not start (exit code {process.exitcode})")

    first = start()
    try:
        action = call(store.root, "next")["actions"][0]
    finally:
        first.terminate()
        first.join(timeout=3)
        assert not first.is_alive()
        first.close()
    second = start()
    try:
        assert call(store.root, "next")["actions"][0] == action
        assert call(store.root, "status")["completed"] == 0
    finally:
        second.terminate()
        second.join(timeout=3)
        assert not second.is_alive()
        second.close()


def test_append_ids_and_import_exact_old_pass(tmp_path):
    from scripts.web.e2e.resource_link_workflow.reports import add_resource

    catalog = tmp_path / "catalog.json"
    json_write(catalog, {"schema_version": 1, "plan_id": "catalog", "resources": [resource(1)]})
    item = resource(99)
    json_write(
        tmp_path / "historical-results.json",
        {
            "groups": [
                {
                    "key": item["key"],
                    "rows": [
                        {
                            "dimension": "QUERY",
                            "source_type": "ALIYUN::TEST::Example",
                            "status": {"iframe": "pass", "open": "untested"},
                        }
                    ],
                }
            ]
        },
    )
    assert add_resource(catalog, item) == "R0002"
    registered = read_json(catalog)["resources"][1]
    assert registered["dimensions"]["QUERY"]["contracts"][0]["historical"]["iframe"] == "pass"
    assert read_json(catalog)["resources"][0]["id"] == "R0001"


def test_new_plan_skips_exact_accepted_positive_checkpoints(store, tmp_path):
    value = plan(store)
    job, key, thread, turn, receipt, _ = review_fixture(store)
    store.accept(job["job_id"], key, receipt)
    store.finish_job(job["job_id"], thread, turn, "completed")
    catalog = tmp_path / "catalog.json"
    json_write(catalog, value)
    output = tmp_path / "next-plan.json"
    make_plan(catalog, "new-run", ["R0001"], output, store)
    from scripts.web.e2e.resource_link_workflow.plans import expected_scenes

    assert expected_scenes(read_json(output)["resources"][0]) == []


def test_ops_recovery_rejects_live_model_turn(store):
    plan(store)
    job = reserve(store, "prepare")
    key, thread, turn = bound(store, job)
    store.block(job["job_id"], key, "fixture blocker")
    task_id = store.status()["tasks"][0]["id"]
    with pytest.raises(WorkflowError, match="all model turns"):
        store.recover_blocked(task_id, {"task_id": task_id, "cleanup": []}, "fixed")
    store.finish_job(job["job_id"], thread, turn, "completed")
    store.recover_blocked(task_id, {"task_id": task_id, "cleanup": []}, "fixed")
    assert store.status()["tasks"][0]["state"] == "repair"


def test_rereview_preserves_slot_and_owned_without_replaying_worker(store):
    plan(store)
    worker, _ = execute_worker(store)
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    context = store.context(review["job_id"], key)
    store.block(review["job_id"], key, "evidence access gap")
    with pytest.raises(WorkflowError, match="terminal worker"):
        store.reopen_review(context["task_id"], "artifact access fixed")
    store.finish_job(review["job_id"], thread, turn, "completed")
    store.reopen_review(context["task_id"], "artifact access fixed")
    assert store.status()["tasks"][0]["state"] == "reported"
    assert store.next_action()["actions"][0]["kind"] == "review"
    with store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE role='glm'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0


@pytest.mark.parametrize("damage", [None, "source", "owner", "task", "present", "duplicate"])
def test_rereview_can_bind_only_exact_previously_deleted_cleanup(store, damage):
    plan(store)
    worker, _ = execute_worker(store)
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    task_id = store.context(review["job_id"], key)["task_id"]
    store.block(review["job_id"], key, "legacy deleted proof binding missing")
    store.finish_job(review["job_id"], thread, turn, "completed")
    raw = "inputs/original-absence.json"
    proof = "inputs/original-cleanup.json"
    resource_value = {"id": "offline-created", "account": "offline"}
    json_write(store.root / raw, {"offline": "original successful empty response"})
    value = {
        "ownership_id": "legacy-owner",
        "resource": resource_value,
        "absent": True,
        "failed": False,
        "source_evidence": raw,
        "source_sha256": digest(store.root / raw),
    }
    if damage == "owner":
        value["resource"] = {"id": "other"}
    json_write(store.root / proof, value)
    original_binding = {"path": "inputs/cloud.json", "sha256": digest(store.root / "inputs/cloud.json")}
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            (
                "legacy-owner",
                task_id,
                "TF",
                json.dumps(resource_value),
                "present" if damage == "present" else "deleted",
                json.dumps(original_binding),
            ),
        )
    entries = [{"ownership_id": "legacy-owner", "evidence": proof}]
    recovery = {"task_id": "other-task" if damage == "task" else task_id, "cleanup": entries}
    if damage == "source":
        json_write(store.root / raw, {"changed": True})
    elif damage == "duplicate":
        entries.append(entries[0].copy())
    if damage:
        with pytest.raises(WorkflowError, match="cleanup"):
            store.reopen_review(task_id, "restore exact binding", recovery)
    else:
        store.reopen_review(task_id, "restore exact binding", recovery)
    with store.transaction() as connection:
        binding = json.loads(connection.execute("SELECT evidence FROM owned").fetchone()[0])
        assert binding == (original_binding if damage else {"path": proof, "sha256": digest(store.root / proof)})
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE role='glm'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM tasks").fetchone()[0] == ("blocked" if damage else "reported")


def test_ops_recovery_persists_original_cleanup_proof_without_acceptance(store):
    plan(store)
    worker, _ = execute_worker(store)
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    task_id = store.context(review["job_id"], key)["task_id"]
    store.block(review["job_id"], key, "original cleanup independently confirmed")
    store.finish_job(review["job_id"], thread, turn, "completed")
    raw, proof = "inputs/recovery-raw.json", "inputs/recovery-proof.json"
    resource_value = {"id": "offline-created"}
    json_write(store.root / raw, {"offline": "actual empty response fixture"})
    json_write(
        store.root / proof,
        {
            "ownership_id": "owner",
            "resource": resource_value,
            "absent": True,
            "failed": False,
            "source_evidence": raw,
            "source_sha256": digest(store.root / raw),
        },
    )
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO owned VALUES(?,?,?,?,?,?)",
            (
                "owner",
                task_id,
                "TF",
                json.dumps(resource_value),
                "present",
                "{}",
            ),
        )
    store.recover_blocked(
        task_id,
        {
            "task_id": task_id,
            "cleanup": [
                {"ownership_id": "owner", "evidence": proof},
            ],
        },
        "exact original cleanup",
    )
    with store.transaction() as connection:
        owner = connection.execute("SELECT state,evidence FROM owned").fetchone()
        assert owner["state"] == "deleted"
        assert json.loads(owner["evidence"]) == {"path": proof, "sha256": digest(store.root / proof)}
        assert connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0


def test_review_reads_exact_original_json_pointer_and_retains_full_sha(store):
    plan(store)
    worker, _ = execute_worker(store)
    review = reserve(store, "review")
    key, _, _ = bound(store, review)
    source = store.root / "jobs" / worker["job_id"] / "output" / "cleanup-source.json"
    json_write(source, {"session_id": "ws~full", "messages": {"tools": {"real": {"isError": False}}}})
    result = Runtime(store).tool(
        review["job_id"],
        key,
        "read_artifact",
        {"path": source.relative_to(store.root).as_posix(), "pointer": "/messages/tools/real"},
    )
    assert json.loads(result["text"]) == {"isError": False}
    assert result["sha256"] == digest(source)


def test_product_repair_barrier_drains_other_workers(store):
    plan(store, 2)
    prepare(store)
    first = reserve(store, "dispatch")
    first_key, first_thread, first_turn = bound(store, first)
    prepare(store)
    second = reserve(store, "dispatch")
    bound(store, second)
    store.submit(first["job_id"], first_key, "fixture execution gap")
    store.finish_job(first["job_id"], first_thread, first_turn, "completed")
    review = reserve(store, "review")
    key, thread, turn = bound(store, review)
    context = store.context(review["job_id"], key)
    store.accept(
        review["job_id"],
        key,
        {
            "task_id": context["task_id"],
            "attempt": 1,
            "decisions": [
                {"scene": scene["id"], "status": "product_gap", "reason": "fixture product defect"}
                for scene in context["expected_scenes"]
            ],
            "cleanup": [],
        },
    )
    store.finish_job(review["job_id"], thread, turn, "completed")
    assert store.next_action()["type"] == "wait"  # Do not mutate shared code while sibling GLM runs.


@pytest.mark.parametrize("mode", ["iframe", "open"])
@pytest.mark.parametrize("source_case", ["matching", "wrong_region", "missing_region", "unrelated_tool", "wrong_scope"])
def test_policy_query_click_requires_same_signed_account_source_region(store, monkeypatch, mode, source_case):
    from scripts.web.e2e.resource_link_workflow import execution_gate as gate

    plan(store)
    prepare(store)
    worker = reserve(store, "dispatch")
    key, _, _ = bound(store, worker)
    context = store.context(worker["job_id"], key)
    scene = context["expected_scenes"][0]
    scene.update(dimension="QUERY", source_type="ALIYUN::RAM::ManagedPolicy", mode=mode)
    context["slot"]["target"] = "offline-target"
    output = Path(context["output_dir"])
    json_write(output / "new-session-intent.json", {"previous_session": "previous"})
    json_write(
        output / "self-created-session.json",
        {
            "job_id": worker["job_id"],
            "target": "offline-target",
            "origin": context["slot"]["origin"],
            "session_id": "ws~offline",
        },
    )
    selector = 'a.resource-link[data-resource-link][aria-label="offline-policy"]'
    packet = read_json(store.root / context["packet_path"])
    packet["scenes"][scene["id"]].update(
        account="123",
        region="cn-hangzhou",
        expected_type="ALIYUN::RAM::ManagedPolicy",
        selector=selector,
        identifier="offline-policy",
        reference_types=["ALIYUN::RAM::ManagedPolicy"],
        name_prefix="offline-policy",
        render_text=["offline-policy", "offline-description"],
    )
    json_write(store.root / context["packet_path"], packet)
    source = {"toolUseId": "offline-read", "evidence": "api_response", "requestRegionId": "cn-hangzhou"}
    if source_case == "wrong_region":
        source["requestRegionId"] = "cn-beijing"
    elif source_case == "missing_region":
        source.pop("requestRegionId")
    elif source_case == "unrelated_tool":
        source["toolUseId"] = "unrelated-read"
    reference = {
        "resourceType": "ALIYUN::RAM::ManagedPolicy",
        "identifier": "offline-policy",
        "scopeKind": "account",
        "accountScope": "credential:wrong" if source_case == "wrong_scope" else "credential:offline",
        "attributes": {"PolicyName": "offline-policy", "Description": "offline-description"},
        "sources": [source],
    }
    if source_case == "unrelated_tool":
        reference["sources"].append(
            {
                "toolUseId": "offline-read",
                "evidence": "api_response",
                "requestRegionId": "cn-beijing",
            }
        )
    original = {"session_id": "ws~offline", "references": {"items": [reference]}}
    json_write(output / "QUERY-product-source.json", original)
    monkeypatch.setattr(
        gate,
        "reference_observations",
        lambda *_: [
            {
                "account": "123",
                "reference_scope": "credential:offline",
                "region": "cn-hangzhou",
                "tool_use_id": "offline-read",
            }
        ],
    )
    runtime = Runtime(store)
    monkeypatch.setattr(store, "context", lambda *_: context)
    clicked = []

    def browser(request):
        assert request["selector"] == selector
        assert request["render_identifier"] == "offline-policy"
        assert request["render_text"] == ["offline-policy", "offline-description"]
        assert request["mode"] == mode and request["session_id"] == "ws~offline"
        clicked.append(request)
        screenshot = output / f"{scene['id']}.png"
        screenshot.write_bytes(PNG)
        return {"screenshot": str(screenshot), "actual_ui_click": True, "session_id": "ws~offline"}

    monkeypatch.setattr(runtime, "browser", browser)
    if source_case == "matching":
        result = runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
        assert result["navigation_identifier"] == "offline-policy"
        assert len(clicked) == 1
    else:
        with pytest.raises(WorkflowError, match="exact resource in fresh signed product source"):
            runtime.tool(worker["job_id"], key, "click", {"scene": scene["id"]})
        assert clicked == []
    assert read_json(output / "QUERY-product-source.json") == original
    assert "regionId" not in reference
    assert not store.status()["completed"]
