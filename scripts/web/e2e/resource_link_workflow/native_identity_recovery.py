"""OPS-only identity recovery for the retained R0036 Web session.

This route avoids starting another Codex worker turn after the original dynamic
tool callback failed in the shared app-server. It can submit one fixed identity
request to the exact retained Web session, and nothing else.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow import execution_gate as gate

from .files import WorkflowError, contained, digest, json_write, read_json

TASK_ID = "remaining-nine-retest-20261011-v1:R0036"
ORIGINAL_REVIEW = "inputs/R0036/native-continuation-review-20261012-v1.json"
IDENTITY_PROMPT = (
    "OPS已独立批准一次原会话身份续核。只执行机器契约中的 STS GetCallerIdentity，"
    "等待真实账号结果后结束。禁止同时查询资源、创建或删除。"
)
REQUEST_OPERATION = "native-identity-recovery"


@contextmanager
def _readonly(path: Path) -> Any:
    connection = sqlite3.connect("file:" + str(path.resolve()) + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _read_continuation_turn(root: Path, job: sqlite3.Row) -> tuple[dict[str, Any], Path]:
    files = sorted((root / "jobs" / job["id"]).glob("ops-thread-read-*.json"))
    if not files:
        raise WorkflowError("fresh owning-backend continuation evidence required")
    path = files[-1]
    raw = read_json(path)
    turns = [item for item in raw.get("thread", {}).get("turns", []) if item.get("id") == job["turn_id"]]
    binding = raw.get("job_binding", {})
    if (raw.get("method") != "thread/read" or raw.get("thread", {}).get("id") != job["thread_id"]
            or len(turns) != 1 or turns[0].get("status") != "completed"
            or any(binding.get(key) != job[key] for key in
                   ("id", "task_id", "role", "state", "thread_id", "turn_id", "submission"))):
        raise WorkflowError("exact completed continuation turn required")

    calls = [item for item in turns[0].get("items", []) if item.get("type") == "dynamicToolCall"]
    operations = [(item.get("arguments", {}).get("operation"), item) for item in calls]
    cleanup = [item for operation, item in operations if operation == "cleanup"]
    if len(cleanup) != 1:
        raise WorkflowError("one exact failed original cleanup callback required")
    failure = cleanup[0]
    messages = [content.get("text") for content in failure.get("contentItems", [])
                if content.get("type") == "inputText"]
    if (failure.get("namespace") is not None or failure.get("status") != "failed"
            or failure.get("success") is not False
            or "Unsupported dynamic tool namespace: null" not in messages):
        raise WorkflowError("original cleanup callback outcome is not the reviewed namespace failure")
    for operation, item in operations:
        if item.get("success") is True and operation not in {"context", "snapshot"}:
            raise WorkflowError("continuation performed an operation beyond read-only context/snapshot")
    if any(operation in {"continue-execution", "cleanup-source", "request", "new-session"}
           and item.get("success") is True for operation, item in operations):
        raise WorkflowError("continuation already submitted a state-changing request")
    evidence = {
        "path": str(path.relative_to(root)),
        "sha256": digest(path),
        "thread_id": job["thread_id"],
        "turn_id": job["turn_id"],
        "cleanup_call_id": failure.get("id"),
        "cleanup_namespace": failure.get("namespace"),
        "cleanup_status": failure.get("status"),
        "cleanup_error": messages[0],
    }
    return evidence, raw


def inspect(runtime: Any) -> dict[str, Any]:
    """Build an immutable, read-only OPS proposal after a live idle-session check."""
    root = runtime.root
    review_path = contained(root, ORIGINAL_REVIEW)
    review = read_json(review_path)
    review_sha = digest(review_path)
    if (review.get("kind") != "ops_native_signer_continuation_review" or review.get("approved") is not True
            or review.get("task_id") != TASK_ID or review.get("original_job_id") != "fe8ad2295f19452eb379a9ba46b371dc"
            or review.get("additional_identity_reads") != 1 or review.get("new_creation_quota") != 0
            or review.get("pass_added") is not False):
        raise WorkflowError("exact already-approved R0036 identity continuation review required")

    with _readonly(root / "state.sqlite3") as db:
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('paused','revision')").fetchall())
        if meta.get("paused") != "true":
            raise WorkflowError("workflow must remain paused")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (TASK_ID,)).fetchone()
        original = db.execute("SELECT * FROM jobs WHERE id=?", (review["original_job_id"],)).fetchone()
        continuations = db.execute(
            "SELECT * FROM jobs WHERE task_id=? AND role='glm' AND purpose='continuation' ORDER BY created,rowid",
            (TASK_ID,),
        ).fetchall()
        active_jobs = db.execute("SELECT count(*) FROM jobs WHERE state!='complete'").fetchone()[0]
        issued_actions = db.execute("SELECT count(*) FROM actions WHERE state='issued'").fetchone()[0]
        slot = db.execute("SELECT * FROM slots WHERE id=?", (review["slot"],)).fetchone()
        if (not task or task["state"] != "blocked" or task["attempt"] != 3 or review["attempt"] != 2
                or not original or original["role"] != "glm" or original["purpose"] != "dispatch"
                or original["state"] != "complete" or len(continuations) != 1
                or continuations[0]["state"] != "complete" or continuations[0]["submission"] != "confirmed"
                or continuations[0]["thread_id"] != original["thread_id"]
                or active_jobs or issued_actions or not slot or slot["task_id"] != TASK_ID
                or any(slot[key] != review[key] for key in ("target", "origin"))):
            raise WorkflowError("exact blocked original generation and drained worker set required")
        if task["packet"] != review["packet"]["path"]:
            raise WorkflowError("current blocked attempt must retain the reviewed original packet")
        continuation = continuations[0]
        if (original["thread_id"] != review["thread_id"] or original["turn_id"] != review["original_turn_id"]
                or continuation["thread_id"] != review["thread_id"]):
            raise WorkflowError("original dispatch/continuation thread binding changed")
        owners = [dict(row) for row in db.execute(
            "SELECT * FROM owned WHERE task_id=? AND state!='deleted' ORDER BY id", (TASK_ID,),
        )]
        if owners != review["ownership"]:
            raise WorkflowError("original owned generation changed")

    session_path = contained(root, review["self_created_session"]["path"])
    session = read_json(session_path)
    if (digest(session_path) != review["self_created_session"]["sha256"]
            or session.get("job_id") != review["original_job_id"]
            or session.get("session_id") != review["session"]
            or session.get("target") != review["target"] or session.get("origin") != review["origin"]):
        raise WorkflowError("exact original full Web session binding changed")

    slot_info = {"origin": review["origin"], "target": review["target"]}
    snapshot = runtime.browser({"operation": "snapshot", **slot_info})
    if (snapshot.get("session_id") != review["session"] or snapshot.get("busy")
            or snapshot.get("unlocked") is not True or snapshot.get("preview_open")):
        raise WorkflowError("original full Web session must be the same, unlocked, idle, and without preview")

    with _readonly(root / "execution-gate.sqlite3") as wire:
        active = wire.execute("SELECT * FROM active WHERE slot=?", (str(review["slot"]),)).fetchone()
        grants = wire.execute("SELECT * FROM grants WHERE scope=? ORDER BY id", (review["scope"],)).fetchall()
        attempts = wire.execute("SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid",
                                (review["scope"],)).fetchall()
        delete_attempt = wire.execute(
            "SELECT 1 FROM attempts a LEFT JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
            "WHERE a.scope=? AND (g.kind='delete' OR a.grant_id LIKE 'delete-%') LIMIT 1",
            (review["scope"],),
        ).fetchone()
        extension = wire.execute("SELECT * FROM grants WHERE scope=? AND id=?",
                                 (review["scope"], review["grant_id"])).fetchone()
        extension_review = wire.execute(
            "SELECT * FROM cleanup_identity_reviews WHERE scope=? AND grant_id=?",
            (review["scope"], review["grant_id"]),
        ).fetchone()
        authority_limit = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                       (review["authority"], review["grant_id"])).fetchone()
        permit_table = wire.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_identity_recovery_permits'",
        ).fetchone()
        existing_permit = (wire.execute(
            "SELECT 1 FROM native_identity_recovery_permits WHERE scope=?", (review["scope"],),
        ).fetchone() if permit_table else None)
        if (not active or any(active[key] != review[key] for key in ("scope", "session", "account"))
                or active["phase"] != "cleanup"
                or (active["expires"] is not None and active["expires"] <= time.time())):
            raise WorkflowError("original active cleanup-phase authority/session required")
        if existing_permit:
            raise WorkflowError("identity recovery permit is already latched; never re-arm")
        if (not extension or extension["kind"] != "identity" or extension["quota"] != 1
                or extension["used"] != 0 or extension["authority"] != review["authority"]
                or gate.canonical(json.loads(extension["call"])) != gate.canonical(review["identity_call"])
                or not authority_limit or authority_limit[0] != 0):
            raise WorkflowError("approved original one-shot identity grant must remain unused")
        if (not extension_review or extension_review["session"] != review["session"]
                or extension_review["account"] != review["account"]
                or extension_review["authority"] != review["authority"]
                or extension_review["evidence_sha256"] != review_sha
                or Path(extension_review["evidence"]).resolve() != review_path.resolve()):
            raise WorkflowError("approved identity grant must bind the exact original review proof")
        if (not attempts or any(row["state"] != "succeeded" or row["session"] != review["session"]
                                for row in attempts) or delete_attempt):
            raise WorkflowError("no unknown, failed, in-flight, or successful Delete attempt may exist")
        original_identity = next((row for row in attempts if row["grant_id"] == "identity"), None)
        original_identity_grant = next((row for row in grants if row["id"] == "identity"), None)
        if (not original_identity or original_identity["id"] != review["original_identity_attempt"]
                or not original_identity["credential"] or not original_identity["reference_scope"]
                or not original_identity_grant
                or not gate.nas_native_success(json.loads(original_identity_grant["call"]),
                                               json.loads(original_identity["response"]), "identity")
                or json.loads(original_identity["response"])["body"].get("AccountId") != review["account"]):
            raise WorkflowError("approved original successful same-account identity attempt required")
        if any(row["credential"] != attempts[0]["credential"]
               or row["reference_scope"] != attempts[0]["reference_scope"] for row in attempts):
            raise WorkflowError("original signer lineage changed")
        preserved_attempts = [
            {"id": row["id"], "sha256": gate.nas_native_snapshot(row)} for row in attempts
        ]
        preserved_grants = []
        for row in grants:
            limit = wire.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                 (row["authority"], row["id"])).fetchone()
            if row["authority"] != review["authority"] or not limit or limit[0] != row["used"]:
                raise WorkflowError("original grant/authority counters diverged")
            preserved_grants.append({"id": row["id"], "used": row["used"],
                                     "definition_sha256": gate.nas_native_snapshot(
                                         {key: value for key, value in dict(row).items() if key != "used"})})
        if preserved_attempts != review["preserved_attempts"]:
            raise WorkflowError("original signed wire attempts changed")
        expected_grants = {item["id"]: item for item in review["preserved_grants"]}
        if any(item["id"] not in expected_grants
               or item != {**expected_grants[item["id"]], "used": item["used"]}
               for item in preserved_grants if item["id"] != review["grant_id"]):
            raise WorkflowError("original wire grants changed")

    cleanup_intent = contained(root, f"jobs/{continuation['id']}/output/QUERY-cleanup-intent.json")
    intent = read_json(cleanup_intent)
    if intent != {"state": "may_have_submitted"}:
        raise WorkflowError("cleanup intent changed; never replay or infer cleanup")
    output = cleanup_intent.parent
    if any(output.glob("QUERY-identity-native-recovery-*.json")):
        raise WorkflowError("identity recovery already submitted or its outcome is unknown; never replay")

    transport_evidence, _ = _read_continuation_turn(root, continuation)
    code_root = Path(runtime.config["repo"]).resolve()
    implementation = []
    for name in (
        "scripts/web/e2e/resource_link_workflow/native_identity_recovery.py",
        "scripts/web/e2e/resource_link_workflow/runtime.py",
        "scripts/web/e2e/resource_link_workflow/browser.mjs",
        "scripts/web/e2e/resource_link_workflow/__main__.py",
        "scripts/web/e2e/resource_link_workflow/OPS.zh-CN.md",
        "scripts/web/e2e/resource_link_workflow/execution_gate.py",
    ):
        path = contained(code_root, name)
        implementation.append({"path": name, "sha256": digest(path)})
    offline_tests = []
    test_path = contained(code_root, "tests/web/test_resource_link_native_identity_recovery.py")
    offline_tests.append({"path": "tests/web/test_resource_link_native_identity_recovery.py",
                          "sha256": digest(test_path)})

    return {
        "schema_version": 1,
        "kind": "ops_native_identity_recovery_review",
        "approved": False,
        "task_id": TASK_ID,
        "blocked_attempt": task["attempt"],
        "original_generation_attempt": review["attempt"],
        "original_job_id": original["id"],
        "original_turn_id": original["turn_id"],
        "continuation_job_id": continuation["id"],
        "continuation_turn_id": continuation["turn_id"],
        "thread_id": original["thread_id"],
        "slot": slot["id"],
        "target": slot["target"],
        "origin": slot["origin"],
        "session": session["session_id"],
        "scope": review["scope"],
        "authority": review["authority"],
        "account": review["account"],
        "active_phase": active["phase"],
        "original_review": {"path": ORIGINAL_REVIEW, "sha256": review_sha},
        "cleanup_intent": {"path": str(cleanup_intent.relative_to(root)), "sha256": digest(cleanup_intent),
                           "state": intent["state"]},
        "transport_failure": transport_evidence,
        "browser_snapshot": {"session_id": snapshot["session_id"], "busy": snapshot["busy"],
                             "unlocked": snapshot["unlocked"], "preview_open": snapshot["preview_open"]},
        "identity_grant": {"id": extension["id"], "quota": extension["quota"], "used": extension["used"],
                           "call": json.loads(extension["call"]),
                           "call_sha256": digest_bytes(gate.canonical(json.loads(extension["call"])).encode())},
        "preserved_attempts": preserved_attempts,
        "preserved_grants": preserved_grants,
        "fixed_request": {"operation": REQUEST_OPERATION, "text": IDENTITY_PROMPT,
                          "text_sha256": digest_bytes(IDENTITY_PROMPT.encode("utf-8"))},
        "runtime_attestation_required": True,
        "permit_contract": {"phase_unchanged": "cleanup", "identity_only": True,
                             "other_api_calls": "denied", "one_shot": True,
                             "marker_create": "exclusive-fsync-before-permit"},
        "offline_tests": offline_tests,
        "scope_limits": {"codex_glm_job_created": False, "codex_glm_turn_started": False,
                         "new_web_session": False, "cloud_delete": False, "resource_create": False,
                         "source_or_click": False, "quota_refilled": False, "pass_added": False,
                         "active_phase_changed": False},
        "implementation": implementation,
    }


def digest_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _create_one_shot_marker(marker: Path, intent: dict[str, Any]) -> None:
    """Exclusively create and durably sync the no-retry latch before arming the gate."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError as error:
        raise WorkflowError("identity recovery already attempted; outcome unknown or submitted") from error
    try:
        payload = (json.dumps(intent, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(marker, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            view = memoryview(payload)
            while view:
                written = stream.write(view)
                if not written:
                    raise OSError("short write for one-shot recovery marker")
                view = view[written:]
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            directory = os.open(marker.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        os.close(descriptor)


def write_proposal(runtime: Any, output: str) -> dict[str, Any]:
    root = runtime.root
    path = contained(root / "inputs", output)
    if path.exists() or path.is_symlink():
        raise WorkflowError("OPS identity recovery proposal is immutable")
    proposal = inspect(runtime)
    json_write(path, proposal, private=True)
    return {"proposal": str(path.relative_to(root)), "sha256": digest(path),
            "approved": False, "identity_only": True, "cloud_calls": 0}


def execute(runtime: Any, evidence: str) -> dict[str, Any]:
    root = runtime.root
    review_path = contained(root / "inputs", evidence)
    if review_path.is_symlink() or (os.name != "nt" and review_path.stat().st_mode & 0o077):
        raise WorkflowError("private OPS identity recovery approval required")
    approval = read_json(review_path)
    proposal_relative = approval.get("proposal_path")
    proposal_path = contained(root, proposal_relative)
    if not proposal_path.is_file() or proposal_path.is_symlink():
        raise WorkflowError("exact saved OPS proposal required")
    proposal = read_json(proposal_path)
    if (approval.get("proposal_sha256") != digest(proposal_path)
            or approval != {**proposal, "approved": True, "proposal_path": proposal_relative,
                            "proposal_sha256": digest(proposal_path)}):
        raise WorkflowError("OPS approval must exactly bind the immutable proposal")
    current = inspect(runtime)
    if current != proposal:
        raise WorkflowError("original session, wire state, transport, or implementation changed after review")
    attestation = runtime.attest_web(proposal["slot"])
    loaded_gate = [item for item in attestation.get("modules", [])
                   if item.get("module") == "scripts.web.e2e.resource_link_workflow.execution_gate"]
    gate_implementation = next((item for item in proposal["implementation"]
                                if item["path"] == "scripts/web/e2e/resource_link_workflow/execution_gate.py"), None)
    if (not gate_implementation or len(loaded_gate) != 1
            or loaded_gate[0].get("sha256") != gate_implementation["sha256"]):
        raise WorkflowError("same-slot Web must attest the exact reviewed execution-gate code before execution")
    output = root / "jobs" / proposal["continuation_job_id"] / "output"
    marker = output / "QUERY-identity-native-recovery-0001.json"
    if marker.exists():
        raise WorkflowError("identity recovery already attempted; outcome unknown or submitted")
    intent = {
        "state": "may_have_submitted",
        "task_id": proposal["task_id"],
        "original_job_id": proposal["original_job_id"],
        "continuation_job_id": proposal["continuation_job_id"],
        "thread_id": proposal["thread_id"],
        "turn_id": proposal["continuation_turn_id"],
        "scope": proposal["scope"],
        "session_id": proposal["session"],
        "stage": "identity",
        "review_sha256": digest(review_path),
        "proposal_sha256": digest(proposal_path),
    }
    _create_one_shot_marker(marker, intent)
    gate.arm_native_identity_recovery(
        root / "execution-gate.sqlite3", slot=str(proposal["slot"]), scope=proposal["scope"],
        session=proposal["session"], account=proposal["account"],
        grant_id=proposal["identity_grant"]["id"], authority=proposal["authority"],
        call=proposal["identity_grant"]["call"], proposal_sha256=digest(proposal_path),
        review_sha256=digest(review_path), original_review_sha256=proposal["original_review"]["sha256"],
        marker_path=str(marker.resolve()),
    )
    try:
        approved_review = read_json(contained(root, proposal["original_review"]["path"]))
        packet = read_json(contained(root, approved_review["packet"]["path"]))
        ui = packet.get("ui", {})
        reserved = {"operation", "origin", "target", "session_id", "text", "output_dir"}
        if not isinstance(ui, dict) or reserved.intersection(ui):
            raise WorkflowError("approved packet UI cannot override the fixed recovery request")
        slot = {"origin": proposal["origin"], "target": proposal["target"]}
        result = runtime.browser({
            **ui,
            "operation": REQUEST_OPERATION,
            **slot,
            "session_id": proposal["session"],
            "text": IDENTITY_PROMPT,
            "output_dir": str(output),
        })
    except BaseException:
        # The request might have reached the browser before an IPC failure. Keep
        # the durable latch in may_have_submitted; never retry this identity.
        raise
    if result.get("submitted") is not True or result.get("session_id") != proposal["session"]:
        raise WorkflowError("identity request outcome is unknown; preserve the one-shot marker")
    json_write(marker, {**intent, "state": "submitted", "target": result.get("target")}, private=True)
    return {"identity_request_submitted": True, "identity_result_verified": False,
            "codex_glm_job_created": False, "delete_replayed": False,
            "marker": str(marker.relative_to(root)),
            "next": "inspect original Web session and wire result; do not repeat"}
