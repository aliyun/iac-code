"""Read-only export of original cleanup responses; never classify absence or grant a verdict."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .files import digest

LOADED_SHA = digest(Path(__file__))


def cleanup_reads(path: Path, job_ids: list[str]) -> list[dict[str, Any]]:
    return _cleanup_responses(path, job_ids)


def cleanup_execution(path: Path, job_id: str, dimension: str, session_id: str) -> list[dict[str, Any]]:
    """Return original deletion and reads, not the product's last prose summary.

    Execution sees only its sealed session and original dimension. A successful
    delete, a required read, or this export is never an absence/PASS decision.
    """
    if dimension not in {"ROS", "TF", "CREATE", "QUERY"} or not job_id or not session_id:
        raise ValueError("cleanup response export requires an exact job, dimension and session")
    return _cleanup_responses(path, [job_id], include_deletes=True, dimension=dimension, session_id=session_id)


def _cleanup_responses(
    path: Path, job_ids: list[str], *, include_deletes: bool = False,
    dimension: str | None = None, session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expose existing wire envelopes that product tool error text may omit.

    Credentials are used only inside the snapshot to bind the original signed
    identity. Neither credential fingerprints nor capabilities are exported.
    Unknown states, raw response text and consumed quotas remain unchanged.
    """
    if not path.is_file() or not job_ids:
        return []
    records = []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        if "read_policy" not in {row[1] for row in connection.execute("PRAGMA table_info(grants)")}:
            return []  # Legacy grants have no asserted cleanup phase.
        for job_id in sorted(set(job_ids)):
            for row in connection.execute(
                "SELECT a.*,g.kind,g.call,g.parent,g.response_path,g.target_param,g.target_encoding,"
                "g.authority,g.quota,g.used,g.read_policy FROM attempts a JOIN grants g "
                "ON a.scope=g.scope AND a.grant_id=g.id WHERE g.kind IN ('read','delete') "
                "AND a.scope IN (?,?,?,?) "
                "ORDER BY a.created,a.id",
                tuple(f"{job_id}:{dimension}" for dimension in ("ROS", "TF", "CREATE", "QUERY")),
            ):
                if dimension is not None and row["scope"] != f"{job_id}:{dimension}":
                    continue
                if session_id is not None and row["session"] != session_id:
                    continue
                policy = json.loads(row["read_policy"]) if row["read_policy"] else {}
                if row["kind"] == "delete":
                    if not include_deletes:
                        continue
                elif policy.get("phase") != "cleanup":
                    continue
                signed = connection.execute(
                    "SELECT a.id,a.tool_use_id,a.response,g.call FROM attempts a JOIN grants g "
                    "ON a.scope=g.scope AND a.grant_id=g.id WHERE a.scope=? AND a.session=? "
                    "AND a.credential=? AND g.authority=? AND g.kind='identity' "
                    "AND a.state='succeeded' AND a.created<? ORDER BY a.created DESC,a.id DESC",
                    (row["scope"], row["session"], row["credential"], row["authority"], row["created"]),
                ).fetchall()
                identity = next(
                    (item for item in signed if json.loads(item["call"]).get("product") == "sts"
                     and json.loads(item["call"]).get("action") == "GetCallerIdentity"),
                    None,
                )
                identity_response = json.loads(identity["response"]) if identity and identity["response"] else None
                records.append({
                    "kind": "original_cleanup_delete_response" if row["kind"] == "delete"
                    else "original_cleanup_wire_response",
                    "job_id": job_id,
                    "dimension": row["scope"].rsplit(":", 1)[-1],
                    "attempt_id": row["id"],
                    "scope": row["scope"],
                    "session_id": row["session"],
                    "reference_scope": row["reference_scope"],
                    "tool_use_id": row["tool_use_id"],
                    "created": row["created"],
                    "state": row["state"],
                    "grant_id": row["grant_id"],
                    "call": json.loads(row["call"]),
                    "read_policy": policy,
                    "parent": row["parent"],
                    "response_path": json.loads(row["response_path"]) if row["response_path"] else None,
                    "target_param": row["target_param"],
                    "target_encoding": row["target_encoding"],
                    "authority": row["authority"],
                    "quota": row["quota"],
                    "used": row["used"],
                    "response_text": row["response"],
                    "response": json.loads(row["response"]) if row["response"] else None,
                    "signed_identity": None if identity is None else {
                        "attempt_id": identity["id"], "tool_use_id": identity["tool_use_id"],
                        "response": identity_response,
                    },
                    "absence_verified": False,
                    "eligible_for_automatic_verdict": False,
                })
    return records
