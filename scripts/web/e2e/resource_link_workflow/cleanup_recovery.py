"""Read-only OPS recovery review; never renew a capability or invent an owned ID."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from scripts.web.e2e.resource_link_workflow.execution_gate import (
    GateDeniedError,
    _matching,
    bound_target,
    canonical,
    cleanup_identity_extensions,
    database,
    mse_managed_members,
    mse_managed_params,
    mse_managed_parent,
    mse_managed_rollback_complete,
    mse_managed_scope,
    nas_managed_delete,
    nas_managed_members,
    nas_managed_params,
    nas_managed_parent,
    phase_allowed,
    resource_group_child_id,
    response_identifier,
    validate_grants,
)

from .files import WorkflowError, contained, digest, read_json
from .phases import known_original_rejected_oss_detail_read, known_original_resolved_read, readonly_continuation

LOADED_SHA = digest(Path(__file__))


def readonly_review_fields(candidate: dict[str, Any]) -> dict[str, Any]:
    """Require actual exhausted successful deletes, not just a zero counter."""
    if candidate.get("unresolved_managed_children"):
        raise WorkflowError("unresolved original MSE children require independent reconciliation, not empty cleanup")
    deletes = [call for call in candidate["remaining_calls"] if call["kind"] == "delete"]
    completed = candidate.get("successful_deletions", [])
    if (candidate.get("original_phase") != "cleanup"
            or not deletes or any(call["remaining"] != 0 for call in deletes)
            or len(completed) != len(deletes)
            or {row["grant_id"] for row in completed} != {call["id"] for call in deletes}
            or {row["parent"] for row in completed} != {
                owner["create_grant_id"] for owner in candidate["owned_from_wire"]
            }):
        raise WorkflowError("read-only cleanup requires all original deletions actually successful and exhausted")
    reads = [call for call in candidate["remaining_calls"] if call["kind"] == "read"
             and call["remaining"] > 0 and phase_allowed(call, "cleanup")]
    if not reads:
        raise WorkflowError("original cleanup read allowance exhausted; no refill")
    return {"successful_deletions": completed, "cleanup_read_calls": reads}


def exact_readonly_request(root: Path, recovery: dict[str, Any], slot: str, session: str,
                           grants: list[dict[str, Any]]) -> str:
    """Recheck the frozen read-only proposal at submission, without writes."""
    evidence = contained(root, recovery["evidence"])
    if digest(evidence) != recovery["evidence_sha256"]:
        raise WorkflowError("original cleanup review changed")
    review = read_json(evidence)
    scope = f"{recovery['original_job_id']}:{recovery['dimension']}"
    if (review.get("kind") != "ops_cleanup_readonly_review" or review.get("approved") is not True
            or any(review.get(key) != value for key, value in (
                ("original_job_id", recovery["original_job_id"]), ("dimension", recovery["dimension"]),
                ("scope", scope), ("authority", recovery["authority"]), ("session_id", session),
            ))):
        raise WorkflowError("original read-only cleanup review binding differs")
    candidate = inspect_candidate(root / "execution-gate.sqlite3", slot=slot, scope=scope, session=session,
                                  account=review["account"], grants=grants, authority=recovery["authority"])
    fields = readonly_review_fields(candidate)
    if (review.get("owned_from_wire") != candidate["owned_from_wire"]
            or any(review.get(key) != value for key, value in fields.items())):
        raise WorkflowError("original read-only cleanup calls or deletion proof changed")
    text = readonly_continuation(root / "execution-gate.sqlite3", slot, scope, session, "cleanup")
    with database(root / "execution-gate.sqlite3") as connection:
        for entry in cleanup_identity_extensions(connection, scope):
            limit = connection.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                       (entry["authority"], entry["id"])).fetchone()
            if not limit:
                raise WorkflowError("reviewed cleanup identity counter missing")
            if limit[0] == 0:
                return ("OPS 已独立批准一次清理身份续核。仅执行下列精确 STS 只读调用，等待真实账号核验结果。"
                        "本条请求禁止同时查询资源、创建或删除；身份失败则停止。\n"
                        + canonical(json.loads(entry["call"])))
    # The projection is already the final, validated machine delivery. Appending
    # another JSON array would make the browser parse the wrong final payload.
    if '"original_missing_cleanup_reads"' in text:
        return text
    return (
        text + "\n本回合仅只读续查；此前原删除已成功，禁止再次删除或查询已用尽的身份额度。原剩余调用：\n"
        + canonical(fields["cleanup_read_calls"])
    )


def exact_delete_request(root: Path, recovery: dict[str, Any], slot: str, session: str) -> str:
    """Deliver the frozen original-response-bound delete, not a new allowance.

    Only cleanup includes its already-created identifier. The strict wire gate
    still checks the actual signer, session, parent response and unused quota.
    """
    evidence = contained(root, recovery["evidence"])
    if digest(evidence) != recovery["evidence_sha256"]:
        raise WorkflowError("original cleanup review changed")
    review = read_json(evidence)
    expected_scope = f"{recovery['original_job_id']}:{recovery['dimension']}"
    if (review.get("kind") != "ops_cleanup_only_review" or review.get("approved") is not True
            or review.get("original_job_id") != recovery["original_job_id"]
            or review.get("dimension") != recovery["dimension"]
            or review.get("scope") != expected_scope or review.get("authority") != recovery["authority"]):
        raise WorkflowError("original cleanup review binding differs")
    call = review["delete_call"]
    predelete_read = None
    try:
        with sqlite3.connect((root / "execution-gate.sqlite3").as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            try:
                active, grant = _matching(connection, slot, session, call)
            except GateDeniedError:
                # A missing, still-unused empty-member read is not permission
                # to delete. Deliver that exact reviewed prerequisite first;
                # the real wire gate continues to reject deletion until its
                # actual same-signer response proves an empty group.
                active, grant, predelete_read = pending_group_predelete_read(
                    root, connection, review, slot, session, call,
                )
            if (active["scope"] != expected_scope or active["account"] != review["account"]
                    or grant["kind"] != "delete" or grant["authority"] != recovery["authority"]
                    or active["phase"] != "cleanup"):
                raise WorkflowError("original response-bound cleanup call differs")
            identity_reads = []
            for entry in cleanup_identity_extensions(connection, expected_scope):
                limit = connection.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                           (entry["authority"], entry["id"])).fetchone()
                if not limit:
                    raise WorkflowError("reviewed cleanup identity counter missing")
                if limit[0] == 0:
                    identity_reads.append(json.loads(entry["call"]))
    except (GateDeniedError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
        raise WorkflowError("original exact cleanup call no longer matches the gate") from error
    # A resumed product conversation retains its previous authentication error.
    # Deliver the already-frozen OPS explanation too; otherwise the identical
    # request invites the model to assume that no recovery happened. This is
    # context only, never a substitute for the actual gated signing identity.
    reason = review.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip() or len(reason) > 5000):
        raise WorkflowError("original cleanup recovery explanation invalid")
    recovery_notice = (
        "\nOPS 本次独立恢复审查说明（已核对不可变审查 SHA；不是新的云操作额度或当前会话身份证明）：\n"
        + reason + "\n历史失败不代表当前环境未恢复；仍须实际执行下面尚未消耗的身份核验，失败则停止。\n"
        if reason else ""
    )
    return (
        recovery_notice +
        ("\nOPS 已独立批准且尚未执行一次清理身份续核。先单独执行下列精确 STS 调用，等待实际 AccountId "
         "与原账号一致后，才执行下述原响应绑定删除。不得把身份查询与其他操作并发；身份失败则停止。\n"
         + canonical(identity_reads) if identity_reads else "")
        +
        ("\n本代删除的空成员前置核查尚未执行。先单独执行以下精确只读调用，等待原始成功响应证明"
         "成员完整为空（TotalCount=0、分页一致、Resources.Resource=[]），才执行后面的删除。"
         "不得先调用 GetStack 或重查执行阶段来源；后续 after_delete 读取必须等真实删除成功。"
         "成员非空、失败、不完整或身份不符时停止，不能删除或重复核查补额度。\n"
         + canonical(predelete_read) if predelete_read else "")
        + "\n以下是程序从原成功创建响应绑定、并重新通过原门禁只读核对的唯一待删除调用，"
        "不是新资源创建或引用来源。逐字使用其全部参数键和值；不得添加任何未列出的可选参数或默认值。"
        "例如未列出的 DisableRecovery 不得自行添加，即使值为 false。实际签名身份仍由原门禁检查，"
        "不得绕过；此文本不增加额度。删除成功后才按原动态契约执行尚未耗尽的必查读取。\n"
        + canonical(call)
    )


def pending_group_predelete_read(
    root: Path, connection: sqlite3.Connection, review: dict[str, Any], slot: str, session: str,
    call: dict[str, Any],
) -> tuple[Any, Any, dict[str, Any]]:
    """Read-only delivery of one explicitly reviewed, never-attempted prerequisite."""
    scope, authority = review["scope"], review["authority"]
    approved = review.get("predelete_read")
    if not isinstance(approved, dict):
        raise GateDeniedError()
    rows = connection.execute("SELECT * FROM grants WHERE scope=?", (scope,)).fetchall()
    grants = []
    for row in rows:
        if row["id"].startswith("ops-cleanup-identity-"):
            continue  # inspect_candidate independently validates extensions.
        entry = {"id": row["id"], "kind": row["kind"], "quota": row["quota"], "call": json.loads(row["call"])}
        for key in ("parent", "response_path", "target_param", "read_policy", "target_encoding"):
            value = dict(row).get(key)
            if value is not None:
                entry[key] = json.loads(value) if key in {"response_path", "read_policy"} else value
        grants.append(entry)
    candidate = inspect_candidate(
        root / "execution-gate.sqlite3", slot=slot, scope=scope, session=session,
        account=review["account"], grants=grants, authority=authority,
    )
    if (candidate["original_capability_expired"] or candidate["original_phase"] != "cleanup"
            or candidate["owned_from_wire"] != review.get("owned_from_wire")):
        raise GateDeniedError()
    deletes = [item for item in candidate["remaining_calls"] if item["kind"] == "delete"]
    if len(deletes) != 1 or deletes[0]["remaining"] != 1 or deletes[0]["call"] != call:
        raise GateDeniedError()
    delete = deletes[0]
    prerequisites = [item for item in candidate["remaining_calls"]
                     if item["id"] == delete.get("predelete_required_read")]
    if len(prerequisites) != 1 or prerequisites[0] != approved or approved["kind"] != "read":
        raise GateDeniedError()
    if (approved["remaining"] < 1 or not phase_allowed(approved, "cleanup")
            or approved.get("read_policy", {}).get("after_delete")
            or connection.execute("SELECT 1 FROM attempts WHERE scope=? AND grant_id=?",
                                  (scope, approved["id"])).fetchone()):
        raise GateDeniedError()
    grant = next(row for row in rows if row["id"] == delete["id"])
    binding = json.loads(grant["response_path"])
    if (not isinstance(binding, dict) or binding.get("kind") != "resource_group_empty"
            or binding.get("read_grant") != approved["id"]):
        raise GateDeniedError()
    active, read_grant = _matching(connection, slot, session, approved["call"])
    if (read_grant["id"] != approved["id"] or read_grant["authority"] != authority
            or active["account"] != review["account"]):
        raise GateDeniedError()
    return active, grant, approved["call"]


def inspect_candidate(
    path: Path, *, slot: str, scope: str, session: str, account: str,
    grants: list[dict[str, Any]], authority: str,
) -> dict[str, Any]:
    """Inspect original grants and actual responses in one read-only snapshot.

    A candidate is neither OPS approval nor a cleanup receipt. Current signer
    identity, the live UI and bounded recovery authorization remain independent
    gates; all original quotas/attempts/bindings remain untouched.
    """
    try:
        validate_grants(grants)
        if not path.is_file() or path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077):
            raise WorkflowError("private original wire journal required")
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            active = connection.execute("SELECT * FROM active WHERE slot=?", (slot,)).fetchone()
            if not active or any(active[key] != value for key, value in (
                ("scope", scope), ("session", session), ("account", account)
            )) or active["phase"] not in {"execute", "cleanup"}:
                raise WorkflowError("original slot/scope/session/account capability binding required")
            saved = {row["id"]: row for row in connection.execute("SELECT * FROM grants WHERE scope=?", (scope,))}
            extensions = cleanup_identity_extensions(connection, scope)
            for entry in extensions:
                if (entry["authority"] != authority or not connection.execute(
                    "SELECT 1 FROM cleanup_identity_reviews WHERE scope=? AND session=? AND account=?",
                    (scope, session, account),
                ).fetchone()):
                    raise WorkflowError("cleanup identity supplement original binding differs")
            grants = [*grants, *[
                {"id": entry["id"], "kind": "identity", "quota": 1, "call": json.loads(entry["call"])}
                for entry in extensions
            ]]
            if set(saved) != {entry["id"] for entry in grants}:
                raise WorkflowError("original exact grant set changed")
            remaining = {}
            for entry in grants:
                row = saved[entry["id"]]
                original = {
                    "id": row["id"], "kind": row["kind"], "quota": row["quota"], "call": json.loads(row["call"])
                }
                for key in ("parent", "response_path", "target_param", "read_policy", "target_encoding"):
                    raw = dict(row).get(key)
                    value = json.loads(raw) if key in {"response_path", "read_policy"} and raw is not None else raw
                    if key in entry or value is not None:
                        original[key] = value
                if canonical(original) != canonical(entry) or row["authority"] != authority:
                    raise WorkflowError("original exact calls or cumulative authority changed")
                limit = connection.execute(
                    "SELECT used FROM authority_limits WHERE authority=? AND id=?", (authority, row["id"])
                ).fetchone()
                if not limit or not 0 <= row["used"] <= limit[0] <= row["quota"]:
                    raise WorkflowError("original cumulative counters missing or inconsistent")
                remaining[row["id"]] = row["quota"] - limit[0]
            attempts = connection.execute("SELECT * FROM attempts WHERE scope=?", (scope,)).fetchall()
            rejected_detail_reads = []
            for row in attempts:
                grant = saved[row["grant_id"]]
                bound_row = {**dict(row), "kind": grant["kind"], "call": grant["call"]}
                resolved = known_original_resolved_read(
                    connection, bound_row,
                )
                rejected = known_original_rejected_oss_detail_read(connection, bound_row)
                if row["session"] != session or (row["state"] != "succeeded" and not resolved and not rejected):
                    raise WorkflowError(
                        "unknown outcome or inconsistent original session requires independent investigation"
                    )
                if (resolved or rejected) and not connection.execute(
                    "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                    (scope, session, row["credential"], account),
                ).fetchone():
                    raise WorkflowError("failed read lacks original actual signed account identity")
                if rejected:
                    rejected_detail_reads.append({
                        "attempt_id": row["id"], "grant_id": row["grant_id"],
                        "response_sha256": hashlib.sha256(canonical(json.loads(row["response"])).encode()).hexdigest(),
                        "http_status": 403, "code": "SignatureDoesNotMatch", "absence_verified": False,
                        "original_state_preserved": True,
                    })
            creations = [
                row for row in attempts if row["state"] == "succeeded" and saved[row["grant_id"]]["kind"] == "create"
            ]
            if not creations:
                raise WorkflowError("no actual successful creation response to recover; do not activate cleanup")
            parents = {}
            owned = []
            for row in creations:
                if row["grant_id"] in parents:
                    raise WorkflowError("ambiguous creation response; do not guess an owned ID")
                identity = connection.execute(
                    "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                    (scope, session, row["credential"], account),
                ).fetchone()
                if not identity:
                    raise WorkflowError("creation lacks original actual signed account identity")
                body = json.loads(row["response"])
                children = [g for g in grants if g["kind"] == "delete" and g.get("parent") == row["grant_id"]]
                if len(children) != 1:
                    raise WorkflowError("one original response-bound delete required")
                parent_call = json.loads(saved[row["grant_id"]]["call"])
                identifier = project(body, children[0]["response_path"], parent_call)
                parents[row["grant_id"]] = (body, parent_call, row)
                owned.append({
                    "creation_attempt_id": row["id"], "create_grant_id": row["grant_id"],
                    "identifier": identifier, "account": account,
                    "region": json.loads(saved[row["grant_id"]]["call"])["region_id"],
                    "response_sha256": hashlib.sha256(canonical(body).encode()).hexdigest(),
                })
            calls = []
            unresolved_managed_children = []
            for entry in grants:
                if entry["kind"] == "create":
                    continue
                call = json.loads(canonical(entry["call"]))
                if entry.get("parent"):
                    if entry["parent"] not in parents:
                        continue  # No response for this parent means no scoped delete/read permission.
                    body, parent_call, parent = parents[entry["parent"]]
                    binding = entry["response_path"]
                    if isinstance(binding, dict) and binding.get("kind") == "resource_group_child":
                        identifier = resource_group_child_id(connection, active, parent, binding)
                        if not identifier:
                            raise WorkflowError("original own-stack child response proof is missing or inconsistent")
                    elif isinstance(binding, dict) and binding.get("kind") == "nas_managed_child":
                        signed_parent = nas_managed_parent(connection, active, entry["parent"])
                        proof = (nas_managed_members(connection, active, signed_parent, binding)
                                 if signed_parent else None)
                        if not proof:
                            raise WorkflowError("original own-stack NAS child proof is missing or inconsistent")
                        params = {"FileSystemId": proof["filesystem_id"]}
                        if binding["child"] == "mount_target":
                            params["MountTargetDomain"] = proof["mount_target_domain"]
                        # Execute calls remain informational after deletion;
                        # they are not eligible for read-only cleanup delivery.
                        # Cleanup calls additionally require the actual signed
                        # successful delete of this same original root.
                        if phase_allowed(entry, "cleanup") and nas_managed_params(
                            connection, active, signed_parent, saved[entry["id"]],
                        ) != params:
                            raise WorkflowError("original NAS cleanup lacks its own successful deletion proof")
                        call["params"].update(params)
                        identifier = None
                    elif isinstance(binding, dict) and binding.get("kind") == "mse_managed_child":
                        signed_parent = mse_managed_parent(connection, active, entry["parent"])
                        proof = (mse_managed_members(connection, active, signed_parent, binding)
                                 if signed_parent else None)
                        if not proof:
                            if not signed_parent or not mse_managed_rollback_complete(
                                connection, active, signed_parent, binding,
                            ):
                                raise WorkflowError("original own-stack MSE child proof is missing or inconsistent")
                            # A proved rollback permits only the original root's deletion.
                            # Missing/partial child IDs never become guessed calls or absence.
                            unresolved_managed_children.append({
                                "grant_id": entry["id"], "parent": entry["parent"], "child": binding["child"],
                                "remaining": remaining[entry["id"]], "call_deliverable": False,
                                "cleanup_confirmed": False, "independent_reconciliation_required": True,
                            })
                            continue
                        if call["product"] == "ros":
                            params = {"StackId": proof["stack_id"],
                                      "LogicalResourceId": binding["logical_ids"][binding["child"]]}
                        else:
                            params = {entry["target_param"]:
                                      proof["members"][binding["child"]]["PhysicalResourceId"]}
                        # Before deletion this remains an informational candidate;
                        # the live gate enforces after_delete. Once deletion exists,
                        # verify its original signer/response before offering reads.
                        if (phase_allowed(entry, "cleanup") and nas_managed_delete(connection, signed_parent)
                                and mse_managed_params(
                                    connection, active, signed_parent, saved[entry["id"]],
                                ) != params):
                            raise WorkflowError("original MSE cleanup lacks its own successful deletion proof")
                        call["params"].update(params)
                        identifier = None
                    else:
                        identifier = project(body, binding, parent_call)
                    if identifier is not None:
                        call["params"][entry["target_param"]] = bound_target(entry, identifier)
                candidate = {
                    "id": entry["id"], "kind": entry["kind"], "remaining": remaining[entry["id"]], "call": call
                }
                if "read_policy" in entry:
                    candidate["read_policy"] = entry["read_policy"]
                binding = entry.get("response_path")
                if entry["kind"] == "delete" and isinstance(binding, dict):
                    candidate["predelete_required_read"] = binding["read_grant"]
                calls.append(candidate)
            successful_deletions = []
            for row in attempts:
                grant = saved[row["grant_id"]]
                if row["state"] == "succeeded" and grant["kind"] == "delete":
                    if (grant["parent"] not in parents or row["created"] <= parents[grant["parent"]][2]["created"]
                            or not connection.execute(
                                "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                                (scope, session, row["credential"], account),
                            ).fetchone()):
                        raise WorkflowError("original successful deletion lacks creation and signed identity binding")
                    successful_deletions.append({
                        "attempt_id": row["id"], "grant_id": row["grant_id"], "parent": grant["parent"],
                        "created": row["created"], "tool_use_id": row["tool_use_id"],
                        "response_sha256": hashlib.sha256(canonical(json.loads(row["response"])).encode()).hexdigest(),
                    })
            return {
                "schema_version": 1, "kind": "cleanup_recovery_inspection", "candidate_only": True,
                "activation_allowed": False, "cloud_call_performed": False, "cleanup_confirmed": False,
                "slot": slot, "scope": scope, "session_id": session, "account": account,
                "original_phase": active["phase"],
                "authority": authority, "original_capability_expired": (
                    active["expires"] is not None and active["expires"] <= time.time()
                ),
                "original_capability_expiry": active["expires"], "owned_from_wire": owned,
                "remaining_calls": calls, "current_signer_identity_verified": False,
                "successful_deletions": sorted(successful_deletions, key=lambda row: row["grant_id"]),
                "rejected_detail_reads": sorted(rejected_detail_reads, key=lambda row: row["attempt_id"]),
                "unresolved_managed_children": unresolved_managed_children,
                "next_gate": (
                    "Independent OPS review and bounded cleanup-only activation are not implemented here. "
                    "No quota replenishment, create, new session, model dispatch or PASS."
                ),
            }
    except (OSError, KeyError, TypeError, ValueError, sqlite3.Error) as error:
        if isinstance(error, WorkflowError):
            raise
        raise WorkflowError("original cleanup recovery provenance could not be verified") from error


def authorize_identity(root: Path, candidate: dict[str, Any], evidence: str) -> dict[str, Any]:
    """Append one explicit identity read for already-created objects, never refill.

    The caller has independently checked terminal model jobs and the original
    idle UI lease. No credential is copied into this review or rebound here.
    """
    file = contained(root, evidence)
    if (not evidence.startswith("inputs/") or not file.is_file() or file.is_symlink()
            or (os.name != "nt" and file.stat().st_mode & 0o077)):
        raise WorkflowError("private immutable inputs cleanup identity review required")
    review = read_json(file)
    expected = {key: candidate[key] for key in ("scope", "authority", "account", "owned_from_wire")}
    expected["session"] = candidate["session_id"]
    originals = [item for item in candidate["remaining_calls"] if item["kind"] == "identity"]
    deletes = [item for item in candidate["remaining_calls"] if item["kind"] == "delete"]
    post_delete = review.get("post_delete_readonly")
    if post_delete is not None:
        with database(root / "execution-gate.sqlite3") as connection:
            if not mse_managed_scope(connection, candidate["scope"]):
                raise WorkflowError("post-delete identity continuation is limited to original hosted MSE cleanup")
        if post_delete != readonly_review_fields(candidate):
            raise WorkflowError("post-delete identity review must bind successful deletion and remaining reads")
    if (review.get("kind") != "ops_cleanup_identity_review" or review.get("approved") is not True
            or type(review.get("additional_identity_reads")) is not int
            or review["additional_identity_reads"] != 1
            or not isinstance(review.get("reason"), str) or not review["reason"].strip()
            or any(review.get(key) != value for key, value in expected.items())
            or len(originals) != 1 or originals[0]["remaining"] != 0
            or review.get("identity_call") != originals[0]["call"]
            or not candidate["owned_from_wire"] or len(deletes) != 1
            or deletes[0]["remaining"] != (0 if post_delete is not None else 1)):
        raise WorkflowError("exact exhausted identity and unused owned deletion review required")
    path = root / "execution-gate.sqlite3"
    # Create the optional append-only table without modifying any original row.
    with database(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cleanup_identity_reviews(scope TEXT PRIMARY KEY,grant_id TEXT,session TEXT,"
            "account TEXT,authority TEXT,evidence TEXT,evidence_sha256 TEXT)"
        )
        if connection.execute("SELECT 1 FROM cleanup_identity_reviews WHERE scope=?", (candidate["scope"],)).fetchone():
            raise WorkflowError("one cleanup identity supplement already exists; no automatic refill")
        active = connection.execute("SELECT * FROM active WHERE slot=?", (candidate["slot"],)).fetchone()
        if not active or any(active[key] != value for key, value in (
            ("scope", candidate["scope"]), ("session", candidate["session_id"]), ("account", candidate["account"]),
        )) or active["phase"] != candidate["original_phase"]:
            raise WorkflowError("original cleanup identity active binding changed")
        original = connection.execute("SELECT * FROM grants WHERE scope=? AND id=?",
                                      (candidate["scope"], originals[0]["id"])).fetchone()
        limit = connection.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                   (candidate["authority"], originals[0]["id"])).fetchone()
        if (not original or not limit or limit[0] != original["quota"]
                or original["kind"] != "identity" or original["authority"] != candidate["authority"]
                or json.loads(original["call"]) != review["identity_call"]):
            raise WorkflowError("original exhausted identity authority changed")
        for attempt in connection.execute(
            "SELECT a.*,g.kind,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
            "WHERE a.scope=?", (candidate["scope"],),
        ):
            if (attempt["session"] != candidate["session_id"] or (attempt["state"] != "succeeded"
                    and not known_original_resolved_read(connection, dict(attempt))
                    and not known_original_rejected_oss_detail_read(connection, dict(attempt)))):
                raise WorkflowError("unknown original outcome cannot receive cleanup identity supplement")
        for owner in candidate["owned_from_wire"]:
            parent = connection.execute("SELECT * FROM attempts WHERE id=?", (owner["creation_attempt_id"],)).fetchone()
            if (not parent or parent["scope"] != candidate["scope"] or parent["session"] != candidate["session_id"]
                    or parent["state"] != "succeeded"
                    or hashlib.sha256(canonical(json.loads(parent["response"])).encode()).hexdigest()
                    != owner["response_sha256"]
                    or not connection.execute(
                        "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                        (parent["scope"], parent["session"], parent["credential"], candidate["account"]),
                    ).fetchone()):
                raise WorkflowError("original actual signed ownership proof changed")
        delete = connection.execute("SELECT * FROM grants WHERE scope=? AND id=?",
                                    (candidate["scope"], deletes[0]["id"])).fetchone()
        used = connection.execute("SELECT used FROM authority_limits WHERE authority=? AND id=?",
                                  (candidate["authority"], deletes[0]["id"])).fetchone()
        expected_used = delete["quota"] if delete and post_delete is not None else 0
        if (not delete or delete["kind"] != "delete" or not used
                or used[0] != expected_used or delete["used"] != expected_used):
            raise WorkflowError("original unused deletion changed")
        ident = "ops-cleanup-identity-" + hashlib.sha256(candidate["scope"].encode()).hexdigest()[:16]
        connection.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,'identity',?,1,0,?)",
            (candidate["scope"], ident, canonical(review["identity_call"]), candidate["authority"]),
        )
        connection.execute("INSERT INTO authority_limits VALUES(?,?,0)", (candidate["authority"], ident))
        connection.execute("INSERT INTO cleanup_identity_reviews VALUES(?,?,?,?,?,?,?)",
                           (candidate["scope"], ident, candidate["session_id"], candidate["account"],
                            candidate["authority"], str(file), digest(file)))
    return {"cleanup_identity_authorized": True, "grant_id": ident, "additional_identity_reads": 1,
            "original_counters_preserved": True, "additional_create_delete": 0,
            "cloud_call_performed": False, "pass_added": False}


def project(body: Any, keys: list[str], call: dict[str, Any] | None = None) -> str:
    if call is not None:
        identifier = response_identifier(call, body, keys)
        if identifier is None:
            raise WorkflowError("original creation response lacks a precise owned identifier")
        return identifier
    for key in keys:
        body = body[key]
    if not isinstance(body, str) or not body:
        raise WorkflowError("original creation response lacks a precise owned identifier")
    return body
