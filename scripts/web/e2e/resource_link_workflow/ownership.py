"""Creation-generation ownership; a deleted same-name object is not a new object."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .files import WorkflowError, contained, digest, read_json

LOADED_SHA = digest(Path(__file__))


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def evidence_generation(
    connection: sqlite3.Connection, root: Path, task_id: str, evidence: dict[str, Any],
) -> str | None:
    """Resolve only SHA-bound original GLM provenance, including retained cleanup proofs."""
    visited: set[str] = set()
    for _ in range(5):
        relative = evidence.get("path")
        if not isinstance(relative, str) or relative in visited:
            return None
        visited.add(relative)
        try:
            path = contained(root, relative)
            if not path.is_file() or digest(path) != evidence.get("sha256"):
                return None
            value = read_json(path)
        except (WorkflowError, OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        candidates = []
        parts = Path(relative).parts
        if len(parts) >= 4 and parts[0] == "jobs" and parts[2] == "output":
            candidates.append(parts[1])
        scope = value.get("scope")
        if isinstance(scope, str) and re.fullmatch(r"[a-f0-9]{32}:(ROS|TF|CREATE|QUERY)", scope):
            candidates.append(scope.split(":")[0])
        for candidate in candidates:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            fields = "task_id,role,purpose" if "purpose" in columns else "task_id,role"
            job = connection.execute(f"SELECT {fields} FROM jobs WHERE id=?", (candidate,)).fetchone()
            if job and tuple(job)[:2] == (task_id, "glm"):
                if "purpose" in columns and job["purpose"] == "continuation":
                    from .native_continuation import load

                    recovery = load(root, connection, task_id)
                    original = connection.execute("SELECT task_id,role FROM jobs WHERE id=?",
                                                  (recovery["original_job_id"],)).fetchone()
                    if not original or tuple(original) != (task_id, "glm"):
                        raise WorkflowError("native ownership must retain original creator")
                    return recovery["original_job_id"]
                return candidate
        evidence = {"path": value.get("source_evidence"), "sha256": value.get("source_sha256")}
    return None


def owner_key(
    connection: sqlite3.Connection, root: Path, task_id: str, dimension: str,
    resource: dict[str, Any], generation: str,
) -> str:
    """Keep proven legacy IDs; never transfer their deleted state to another generation."""
    job = connection.execute("SELECT task_id,role FROM jobs WHERE id=?", (generation,)).fetchone()
    if not job or tuple(job) != (task_id, "glm") or dimension not in {"ROS", "TF", "CREATE", "QUERY"}:
        raise WorkflowError("ownership generation must be an original GLM of this task")
    legacy = hashlib.sha256(_serialized([task_id, dimension, resource]).encode()).hexdigest()
    old = connection.execute("SELECT evidence FROM owned WHERE id=?", (legacy,)).fetchone()
    if old:
        binding = json.loads(old[0])
        proven = evidence_generation(connection, root, task_id, binding)
        if proven == generation or wire_evidence_generation(
            connection, root, task_id, dimension, resource, binding,
        ) == generation:
            return legacy
    return hashlib.sha256(_serialized([task_id, dimension, resource, generation]).encode()).hexdigest()


def wire_evidence_generation(
    connection: sqlite3.Connection, root: Path, task_id: str, dimension: str,
    resource: dict[str, Any], evidence: dict[str, Any],
) -> str | None:
    """Legacy cleanup exports bind the original creator, not a cleanup-only GLM.

    Require the unchanged raw export's complete session and structured tool key
    to match exactly one successful wire creation of this exact resource. No
    name-only inference, new calls, journal mutation or historical reacceptance.
    """
    from scripts.web.e2e.resource_link_workflow.execution_gate import creations

    if not (root / "execution-gate.sqlite3").is_file():
        return None
    workers = [row[0] for row in connection.execute(
        "SELECT id FROM jobs WHERE task_id=? AND role='glm'", (task_id,),
    )]
    records = [record for record in creations(root / "execution-gate.sqlite3", workers)
               if record["dimension"] == dimension and record["resource"] == resource
               and record["state"] == "succeeded" and record.get("tool_use_id")]
    visited: set[str] = set()
    for _ in range(5):
        relative = evidence.get("path")
        if not isinstance(relative, str) or relative in visited:
            return None
        visited.add(relative)
        try:
            path = contained(root, relative)
            if not path.is_file() or digest(path) != evidence.get("sha256"):
                return None
            value = read_json(path)
        except (WorkflowError, OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        tools = value.get("messages", {}).get("tools", {}) if isinstance(value.get("messages"), dict) else {}
        entries = tools.values() if isinstance(tools, dict) else tools if isinstance(tools, list) else []
        keys = {entry.get("toolUseId") for entry in entries if isinstance(entry, dict)
                and isinstance(entry.get("toolUseId"), str)}
        matches = {record["job_id"] for record in records
                   if record["session_id"] == value.get("session_id") and record["tool_use_id"] in keys}
        if len(matches) == 1:
            return matches.pop()
        if matches:
            return None
        evidence = {"path": value.get("source_evidence"), "sha256": value.get("source_sha256")}
    return None


def register_generation(
    connection: sqlite3.Connection, root: Path, task_id: str, dimension: str,
    resource: dict[str, Any], generation: str, evidence: dict[str, Any],
) -> str:
    """Append a present owner for this creation without editing historical cleanup/PASS."""
    identity = owner_key(connection, root, task_id, dimension, resource, generation)
    if evidence_generation(connection, root, task_id, evidence) != generation:
        raise WorkflowError("ownership evidence must bind its exact original GLM generation")
    connection.execute(
        "INSERT OR IGNORE INTO owned VALUES(?,?,?,?,?,?)",
        (identity, task_id, dimension, _serialized(resource), "present", _serialized(evidence)),
    )
    return identity
