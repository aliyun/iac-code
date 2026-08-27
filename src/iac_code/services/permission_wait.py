"""Bounded, session-owned persistence for externally answerable permissions."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast

from iac_code.services.session_layout import SessionPaths, ensure_session_owned_dir
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import PermissionWaitOutcome
from iac_code.utils.file_security import ensure_private_file
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock

PermissionClass = Literal["normal", "pipeline"]
PermissionPrincipalKind = Literal["a2a_user", "credential"]
PermissionPhase = Literal[
    "WAITING",
    "TIMEOUT_GRACE",
    "SUSPENDING",
    "SUSPENDED",
    "RESTORING",
    "RESOLVED",
    "CANCELED",
]

_BOUNDARY_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROOT_MESSAGE_REF = re.compile(r"^session\.jsonl:(0|[1-9][0-9]*)$")
_PIPELINE_MESSAGE_REF = re.compile(r"^pipeline/transcripts/([A-Za-z0-9_.-]+)/session\.jsonl:(0|[1-9][0-9]*)$")
_ACTIVE_PHASES = {"WAITING", "TIMEOUT_GRACE", "SUSPENDING", "SUSPENDED", "RESTORING"}
logger = logging.getLogger(__name__)


def _parse_timeout(value: object, *, name: str, allow_zero: bool) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be null or a finite number.")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be null or a finite {qualifier} number.")
    return result


@dataclass(frozen=True)
class PermissionWaitPolicy:
    resident_timeout_seconds: float | None = None
    sub_pipeline_timeout_seconds: float | None = None
    timeout_grace_seconds: float = 30.0

    @classmethod
    def from_config(cls, raw: object | None) -> PermissionWaitPolicy:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("permission_wait must be an object.")
        config = dict(raw)
        allowed = {
            "resident_timeout_seconds",
            "sub_pipeline_timeout_seconds",
            "timeout_grace_seconds",
        }
        unknown = sorted(str(key) for key in config if key not in allowed)
        if unknown:
            raise ValueError("Unknown permission_wait fields: {}.".format(", ".join(unknown)))
        resident = _parse_timeout(
            config.get("resident_timeout_seconds"),
            name="permission_wait.resident_timeout_seconds",
            allow_zero=False,
        )
        sub_pipeline = _parse_timeout(
            config.get("sub_pipeline_timeout_seconds"),
            name="permission_wait.sub_pipeline_timeout_seconds",
            allow_zero=False,
        )
        grace_value = config.get("timeout_grace_seconds", 30)
        grace = _parse_timeout(
            grace_value,
            name="permission_wait.timeout_grace_seconds",
            allow_zero=True,
        )
        assert grace is not None
        return cls(
            resident_timeout_seconds=resident,
            sub_pipeline_timeout_seconds=sub_pipeline,
            timeout_grace_seconds=grace,
        )

    def to_config(self) -> dict[str, float | None]:
        return {
            "resident_timeout_seconds": self.resident_timeout_seconds,
            "sub_pipeline_timeout_seconds": self.sub_pipeline_timeout_seconds,
            "timeout_grace_seconds": self.timeout_grace_seconds,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecoveredPermissionAuditBoundary:
    """Canonical tool/audit data reconstructed from a persisted transcript."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    audit_context: dict[str, Any]


_permission_execution_principal: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "iac_code_permission_execution_principal",
    default=None,
)


@dataclass(frozen=True)
class PermissionExecutionIdentityScope:
    """Install one stable request principal for cloud permission correlation."""

    principal_id: str

    def __post_init__(self) -> None:
        if not self.principal_id:
            raise ValueError("permission execution principal must be non-empty")

    @contextmanager
    def install(self) -> Iterator[None]:
        token = _permission_execution_principal.set(self.principal_id)
        try:
            yield
        finally:
            _permission_execution_principal.reset(token)

    @staticmethod
    def current_principal_id() -> str | None:
        return _permission_execution_principal.get()


@dataclass(frozen=True)
class PermissionExecutionIdentity:
    """Stable principal and effective Region bound to one cloud permission."""

    principal_ref: str | None
    region: str | None
    principal_kind: PermissionPrincipalKind | None = None

    @classmethod
    def resolve(
        cls,
        *,
        tool_name: str,
        tool_input: Mapping[str, Any],
        permission_audit: object | None = None,
        principal_kind: PermissionPrincipalKind | None = None,
    ) -> PermissionExecutionIdentity:
        operation = getattr(permission_audit, "operation", None)
        operation = operation if isinstance(operation, Mapping) else {}
        cloud_operation = (
            principal_kind is not None
            or bool(operation.get("product"))
            or tool_name == "aliyun_api"
            or tool_name.startswith("ros_")
        )
        if not cloud_operation:
            return cls(None, None)

        from iac_code.services.providers.aliyun import AliyunCredentials

        credential = AliyunCredentials.load()
        region = tool_input.get("region_id")
        params = tool_input.get("params")
        if not isinstance(region, str) or not region:
            if isinstance(params, Mapping):
                region = params.get("RegionId")
        if not isinstance(region, str) or not region:
            region = operation.get("region")
        if (not isinstance(region, str) or not region) and credential is not None:
            region = credential.region_id
        effective_region = region if isinstance(region, str) and region else None

        a2a_user_id = PermissionExecutionIdentityScope.current_principal_id()
        effective_principal_kind = principal_kind or ("a2a_user" if a2a_user_id is not None else "credential")
        if effective_principal_kind == "a2a_user":
            if a2a_user_id is None:
                return cls(None, effective_region, effective_principal_kind)
            principal_ref = "aliyun:" + canonical_digest(
                {
                    "principalType": "a2a_user",
                    "principal": a2a_user_id,
                }
            )
            return cls(principal_ref, effective_region, effective_principal_kind)

        if credential is None:
            return cls(None, effective_region, effective_principal_kind)
        anchor = credential.ram_role_arn or credential.ram_role_name or credential.access_key_id
        if not anchor:
            return cls(None, effective_region, effective_principal_kind)
        principal_ref = "aliyun:" + canonical_digest(
            {
                "mode": credential.mode,
                "anchor": anchor,
            }
        )
        return cls(principal_ref, effective_region, effective_principal_kind)

    def as_tuple(self) -> tuple[str | None, str | None]:
        return self.principal_ref, self.region


def _parse_permission_message_ref(value: object) -> tuple[str | None, int]:
    if not isinstance(value, str):
        raise ValueError("invalid permission continuation message reference")
    root_match = _ROOT_MESSAGE_REF.fullmatch(value)
    if root_match is not None:
        return None, int(root_match.group(1))
    pipeline_match = _PIPELINE_MESSAGE_REF.fullmatch(value)
    if pipeline_match is None:
        raise ValueError("invalid permission continuation message reference")
    transcript_id = pipeline_match.group(1)
    # Reuse the session layout's cross-platform component validation without
    # resolving or touching a caller-controlled path.
    SessionPaths.from_session_dir(Path(".")).transcript_dir(transcript_id)
    return transcript_id, int(pipeline_match.group(2))


def canonicalize_permission_continuation_frame(
    frame: Mapping[str, Any],
    *,
    audit_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind an AgentLoop-local message index to its canonical transcript."""

    result = dict(frame)
    referenced_transcript, message_index = _parse_permission_message_ref(result.get("assistantMessageRef"))
    transcript_value = audit_context.get("transcript_id") if audit_context is not None else None
    if transcript_value is None:
        if referenced_transcript is not None:
            raise ValueError("invalid permission continuation transcript context")
        return result
    if not isinstance(transcript_value, str) or not transcript_value:
        raise ValueError("invalid permission continuation transcript context")
    SessionPaths.from_session_dir(Path(".")).transcript_dir(transcript_value)
    if referenced_transcript is not None and referenced_transcript != transcript_value:
        raise ValueError("invalid permission continuation transcript context")
    result["assistantMessageRef"] = f"pipeline/transcripts/{transcript_value}/session.jsonl:{message_index}"
    return result


def recover_permission_audit_boundary(
    record: Mapping[str, Any],
    *,
    cwd: str,
    session_id: str,
    storage: SessionStorage | None = None,
) -> RecoveredPermissionAuditBoundary | None:
    """Re-read and verify the exact permission tool call from canonical storage."""

    frame = record.get("continuationFrame")
    if not isinstance(frame, Mapping) or record.get("sessionId") != session_id:
        return None
    try:
        transcript_id, message_index = _parse_permission_message_ref(frame.get("assistantMessageRef"))
        root_storage = storage or SessionStorage()
        root_session_dir = root_storage.session_dir(cwd, session_id)
        if transcript_id is None:
            messages = root_storage.load(cwd, session_id)
            audit_context: dict[str, Any] = {
                "session_id": session_id,
                "cwd": cwd,
                "audit_log_path": str(SessionPaths.from_session_dir(root_session_dir).permission_audit_path),
            }
        else:
            root_session_dir = root_storage.v2_session_dir(cwd, session_id)
            if root_session_dir is None:
                return None
            session_paths = SessionPaths.require_supported(root_session_dir)
            ensure_session_owned_dir(
                root_session_dir,
                session_paths.transcript_dir(transcript_id),
            )
            from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage

            transcript_storage = PipelineTranscriptStorage(session_paths.session_dir / "pipeline")
            messages = transcript_storage.load(cwd, transcript_id)
            audit_context = {
                "session_id": transcript_id,
                "cwd": cwd,
                "root_session_id": session_id,
                "transcript_id": transcript_id,
                "audit_log_path": str(session_paths.transcript_permission_audit_path(transcript_id)),
            }
        if not messages or message_index != len(messages) - 1:
            return None
        message = messages[message_index]
        if message.role != "assistant":
            return None
        message_content = (
            [block.model_dump(mode="json") for block in message.content]
            if isinstance(message.content, list)
            else message.content
        )
        if canonical_digest(message_content) != frame.get("assistantMessageDigest"):
            return None
        tool_uses = message.get_tool_use_blocks()
        ordered_ids = [tool_use.id for tool_use in tool_uses]
        if ordered_ids != frame.get("orderedToolUseIds"):
            return None
        current_index = frame.get("currentIndex")
        if isinstance(current_index, bool) or not isinstance(current_index, int):
            return None
        if current_index < 0 or current_index >= len(tool_uses):
            return None
        tool_use = tool_uses[current_index]
        if tool_use.id != record.get("toolUseId") or tool_use.name != record.get("toolName"):
            return None
        if canonical_digest({"name": tool_use.name, "input": tool_use.input}) != record.get("payloadDigest"):
            return None
        return RecoveredPermissionAuditBoundary(
            tool_name=tool_use.name,
            tool_input=dict(tool_use.input),
            tool_use_id=tool_use.id,
            audit_context=audit_context,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def permission_execution_identity(
    *,
    tool_name: str,
    tool_input: Mapping[str, Any],
    permission_audit: object | None = None,
    principal_kind: PermissionPrincipalKind | None = None,
) -> tuple[str | None, str | None]:
    """Return a non-secret Alibaba Cloud principal fingerprint and effective Region.

    Local permissions are deliberately not coupled to Alibaba Cloud credentials.
    A request-scoped A2A user id is the stable cloud principal when available,
    so rotating STS credentials do not invalidate the same caller's pending
    permission. Other surfaces retain the credential-anchor fallback.
    """

    return PermissionExecutionIdentity.resolve(
        tool_name=tool_name,
        tool_input=tool_input,
        permission_audit=permission_audit,
        principal_kind=principal_kind,
    ).as_tuple()


def new_boundary_id() -> str:
    return "pwb_" + uuid.uuid4().hex


class PermissionWaitCheckpointStore:
    """Atomic JSON records scoped to one existing conversation session."""

    def __init__(self, cwd: str, session_id: str, *, storage: SessionStorage | None = None) -> None:
        self.cwd = cwd
        self.session_id = session_id
        self._storage = storage or SessionStorage()
        session_dir = self._storage.v2_session_dir(cwd, session_id)
        if session_dir is None:
            session_dir = self._storage.ensure_v2_session_dir_for_new_session(cwd, session_id)
        if session_dir is None:
            raise ValueError("permission waits require a version 2 session directory")
        self.paths = SessionPaths.require_supported(session_dir)
        ensure_session_owned_dir(self.paths.session_dir, self.paths.permission_waits_dir)

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(record)
        boundary_id = self._validate_record(candidate)
        path = self._record_path(boundary_id)
        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            if path.exists():
                raise ValueError("permission boundary already exists")
            atomic_write_json(path, candidate, durable=True)
            ensure_private_file(path)
        return candidate

    def create_successor(self, record: Mapping[str, Any], *, previous_boundary_id: str) -> dict[str, Any]:
        """Atomically move an ordered tool batch from one wait boundary to the next."""

        candidate = dict(record)
        boundary_id = self._validate_record(candidate)
        new_path = self._record_path(boundary_id)
        previous_path = self._record_path(previous_boundary_id)
        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            if new_path.exists():
                raise ValueError("permission boundary already exists")
            previous = self._read(previous_path)
            if previous is None or previous.get("phase") in {"RESOLVED", "CANCELED"}:
                raise ValueError("previous permission boundary is not active")
            decision = previous.get("decision")
            if not isinstance(decision, dict) or decision.get("status") not in {"claimed", "applied"}:
                raise ValueError("previous permission decision is not available")
            receipt = {
                "schemaVersion": 1,
                "boundaryId": previous["boundaryId"],
                "inputId": previous["inputId"],
                "taskId": previous.get("taskId"),
                "contextId": previous.get("contextId"),
                "sessionId": previous["sessionId"],
                "toolUseId": previous["toolUseId"],
                "payloadDigest": previous["payloadDigest"],
                "phase": "RESOLVED",
                "generation": int(previous["generation"]) + 1,
                "decision": decision,
                "resultDigest": "",
                "nextBoundaryId": boundary_id,
                "ack": {
                    "decision": decision.get("value"),
                    "accepted": True,
                    "nextBoundaryId": boundary_id,
                },
                "resolvedAt": format_utc(utc_now()),
            }
            atomic_write_json(new_path, candidate, durable=True)
            ensure_private_file(new_path)
            atomic_write_json(previous_path, receipt, durable=True)
            ensure_private_file(previous_path)
        return candidate

    def load(self, boundary_id: str) -> dict[str, Any] | None:
        path = self._record_path(boundary_id)
        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            return self._read(path)

    def find(self, *, task_id: str, context_id: str, input_id: str, tool_use_id: str) -> dict[str, Any] | None:
        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            for path in sorted(self.paths.permission_waits_dir.glob("pwb_*.json")):
                record = self._read(path)
                if record is None:
                    continue
                if (
                    record.get("taskId") == task_id
                    and record.get("contextId") == context_id
                    and record.get("inputId") == input_id
                    and record.get("toolUseId") == tool_use_id
                ):
                    return record
        return None

    def find_by_input_id(self, input_id: str) -> dict[str, Any] | None:
        """Find one session-scoped browser correlation, including its compact receipt."""

        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            for path in sorted(self.paths.permission_waits_dir.glob("pwb_*.json")):
                record = self._read(path)
                if record is not None and record.get("inputId") == input_id:
                    return record
        return None

    def list_active(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            for path in sorted(self.paths.permission_waits_dir.glob("pwb_*.json")):
                record = self._read(path)
                if record is not None and record.get("phase") in _ACTIVE_PHASES:
                    records.append(record)
        return records

    def transaction(
        self,
        boundary_id: str,
        mutate: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        path = self._record_path(boundary_id)
        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            current = self._read(path)
            if current is None:
                raise ValueError("permission boundary not found")
            updated = mutate(dict(current))
            if updated is None:
                return current
            self._validate_record(updated)
            atomic_write_json(path, updated, durable=True)
            ensure_private_file(path)
            return updated

    def run_generation_fenced(
        self,
        boundary_id: str,
        *,
        expected_generation: int,
        operation: Callable[[], Any],
    ) -> Any:
        """Run a backup while holding permission lock before its backup lock."""

        path = self._record_path(boundary_id)
        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            current = self._read(path)
            if current is None or int(current.get("generation", 0)) != expected_generation:
                raise ValueError("permission generation changed")
            result = operation()
            verified = self._read(path)
            if verified is None or int(verified.get("generation", 0)) != expected_generation:
                raise ValueError("permission generation changed")
            return result

    def reconcile_deadline(
        self,
        boundary_id: str,
        *,
        now: datetime | None = None,
        grace_seconds: float,
        live_owner: bool,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        observed_at = now or utc_now()

        def mutate(record: dict[str, Any]) -> dict[str, Any] | None:
            if expected_generation is not None and int(record.get("generation", 0)) != expected_generation:
                raise ValueError("permission generation changed")
            phase = record.get("phase")
            decision = record.get("decision")
            decision_status = decision.get("status") if isinstance(decision, dict) else None
            if not live_owner and phase in {"WAITING", "TIMEOUT_GRACE", "SUSPENDING"}:
                record["phase"] = "SUSPENDED"
                record["generation"] = int(record["generation"]) + 1
                record["updatedAt"] = format_utc(observed_at)
                return record
            resident_deadline = parse_utc(record.get("residentDeadlineAt"))
            if phase == "WAITING" and resident_deadline is not None and observed_at >= resident_deadline:
                # A paused sandbox cannot run this callback at the resident
                # deadline.  Persist the grace window when expiry is first
                # observed so a resumed permission reply still gets the
                # configured request-versus-timeout race window.
                grace_deadline = observed_at + timedelta(seconds=grace_seconds)
                record["graceDeadlineAt"] = format_utc(grace_deadline)
                if decision_status == "none" and grace_seconds == 0:
                    record["phase"] = "SUSPENDING" if live_owner else "SUSPENDED"
                else:
                    record["phase"] = "TIMEOUT_GRACE"
                record["generation"] = int(record["generation"]) + 1
                record["updatedAt"] = format_utc(observed_at)
                return record
            grace_deadline = parse_utc(record.get("graceDeadlineAt"))
            if (
                phase == "TIMEOUT_GRACE"
                and decision_status == "none"
                and grace_deadline is not None
                and observed_at >= grace_deadline
            ):
                record["phase"] = "SUSPENDING" if live_owner else "SUSPENDED"
                record["generation"] = int(record["generation"]) + 1
                record["updatedAt"] = format_utc(observed_at)
                return record
            return None

        return self.transaction(boundary_id, mutate)

    def claim_decision(
        self,
        boundary_id: str,
        *,
        value: Literal["allow_once", "deny"],
        source: str,
        claim_id: str | None = None,
        expected_generation: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        claim = claim_id or uuid.uuid4().hex
        created = False

        def mutate(record: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal created
            if expected_generation is not None and int(record.get("generation", 0)) != expected_generation:
                raise ValueError("permission generation changed")
            if record.get("phase") in {"CANCELED"}:
                raise ValueError("permission boundary is canceled")
            decision = record.get("decision")
            if not isinstance(decision, dict):
                raise ValueError("invalid permission decision state")
            status = decision.get("status")
            if status in {"claimed", "applied"}:
                if decision.get("value") != value:
                    raise ValueError("permission response conflicts with the recorded decision")
                return None
            if status != "none":
                raise ValueError("invalid permission decision state")
            record["decision"] = {
                "status": "claimed",
                "value": value,
                "source": source,
                "claimId": claim,
                "auditStatus": "pending",
                "backupStatus": "pending",
            }
            record["generation"] = int(record["generation"]) + 1
            record["updatedAt"] = format_utc(utc_now())
            created = True
            return record

        return self.transaction(boundary_id, mutate), created

    def mark_applied(self, boundary_id: str, *, claim_id: str) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any] | None:
            decision = record.get("decision")
            if not isinstance(decision, dict) or decision.get("claimId") != claim_id:
                raise ValueError("permission claim changed")
            if decision.get("status") == "applied":
                return None
            if decision.get("status") != "claimed":
                raise ValueError("permission claim is not pending delivery")
            decision = dict(decision)
            decision["status"] = "applied"
            record["decision"] = decision
            record["generation"] = int(record["generation"]) + 1
            record["updatedAt"] = format_utc(utc_now())
            return record

        return self.transaction(boundary_id, mutate)

    def mark_claim_backed_up(self, boundary_id: str, *, claim_id: str) -> dict[str, Any]:
        """Record that the accepted decision reached every required backup target."""

        def mutate(record: dict[str, Any]) -> dict[str, Any] | None:
            decision = record.get("decision")
            if not isinstance(decision, dict) or decision.get("claimId") != claim_id:
                raise ValueError("permission claim changed")
            if decision.get("backupStatus") == "committed":
                return None
            if decision.get("status") not in {"claimed", "applied"}:
                raise ValueError("permission claim is not available for backup")
            decision = dict(decision)
            decision["backupStatus"] = "committed"
            record["decision"] = decision
            record["generation"] = int(record["generation"]) + 1
            record["updatedAt"] = format_utc(utc_now())
            return record

        return self.transaction(boundary_id, mutate)

    def run_claim_audit_once(
        self,
        boundary_id: str,
        *,
        claim_id: str,
        audit: Callable[[str], bool],
    ) -> tuple[dict[str, Any], bool]:
        """Run one authoritative decision audit under the checkpoint file lock.

        This lock only serializes the short audit-and-checkpoint commit. It is
        never held while waiting for a user, backing up a session, restoring a
        runtime, or executing a tool.
        """

        path = self._record_path(boundary_id)
        with cross_process_file_lock(self.paths.permission_waits_lock_path):
            record = self._read(path)
            if record is None:
                raise ValueError("permission boundary not found")
            decision = record.get("decision")
            if not isinstance(decision, dict) or decision.get("claimId") != claim_id:
                raise ValueError("permission claim changed")
            if decision.get("status") not in {"claimed", "applied"}:
                raise ValueError("permission claim is not available for audit")
            audit_status = decision.get("auditStatus", "pending")
            if audit_status in {"recorded", "failed"}:
                return record, False
            if audit_status != "pending":
                raise ValueError("invalid permission claim audit state")
            delivered_value = str(decision.get("value") or "")
            try:
                succeeded = bool(audit(delivered_value))
            except Exception:
                logger.exception("Permission decision audit failed boundary_id=%s", boundary_id)
                succeeded = False
            decision = dict(decision)
            if not succeeded and delivered_value == "allow_once":
                decision["value"] = "deny"
            decision["auditStatus"] = "recorded" if succeeded else "failed"
            record["decision"] = decision
            record["generation"] = int(record["generation"]) + 1
            record["updatedAt"] = format_utc(utc_now())
            self._validate_record(record)
            atomic_write_json(path, record, durable=True)
            ensure_private_file(path)
            return record, True

    def mark_suspended(self, boundary_id: str, *, expected_generation: int | None = None) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any] | None:
            if expected_generation is not None and int(record.get("generation", 0)) != expected_generation:
                raise ValueError("permission generation changed")
            if record.get("phase") == "SUSPENDED":
                return None
            if record.get("phase") not in {"SUSPENDING", "WAITING", "TIMEOUT_GRACE"}:
                raise ValueError("permission boundary cannot be suspended")
            record["phase"] = "SUSPENDED"
            record["generation"] = int(record["generation"]) + 1
            record["updatedAt"] = format_utc(utc_now())
            return record

        return self.transaction(boundary_id, mutate)

    def begin_restore(self, boundary_id: str) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            if record.get("phase") not in {"SUSPENDED", "SUSPENDING"}:
                raise ValueError("permission boundary is not recoverable")
            decision = record.get("decision")
            if not isinstance(decision, dict) or decision.get("status") not in {"claimed", "applied"}:
                raise ValueError("permission boundary has no decision to recover")
            record["phase"] = "RESTORING"
            record["generation"] = int(record["generation"]) + 1
            record["updatedAt"] = format_utc(utc_now())
            return record

        return self.transaction(boundary_id, mutate)

    def resolve(self, boundary_id: str, *, result_digest: str, ack: Mapping[str, Any]) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any] | None:
            if record.get("phase") == "RESOLVED":
                return None
            decision = record.get("decision")
            receipt = {
                "schemaVersion": 1,
                "boundaryId": record["boundaryId"],
                "inputId": record["inputId"],
                "taskId": record.get("taskId"),
                "contextId": record.get("contextId"),
                "sessionId": record["sessionId"],
                "toolUseId": record["toolUseId"],
                "payloadDigest": record["payloadDigest"],
                "phase": "RESOLVED",
                "generation": int(record["generation"]) + 1,
                "decision": decision,
                "resultDigest": result_digest,
                "ack": dict(ack),
                "resolvedAt": format_utc(utc_now()),
            }
            return receipt

        return self.transaction(boundary_id, mutate)

    def cancel(self, boundary_id: str, *, expected_generation: int | None = None) -> dict[str, Any]:
        def mutate(record: dict[str, Any]) -> dict[str, Any] | None:
            if expected_generation is not None and int(record.get("generation", 0)) != expected_generation:
                raise ValueError("permission generation changed")
            if record.get("phase") == "CANCELED":
                return None
            if record.get("phase") == "RESOLVED":
                raise ValueError("resolved permission cannot be canceled")
            decision = record.get("decision")
            if isinstance(decision, dict) and decision.get("status") != "none":
                raise ValueError("permission decision already claimed")
            record["phase"] = "CANCELED"
            record["generation"] = int(record["generation"]) + 1
            record["updatedAt"] = format_utc(utc_now())
            return record

        return self.transaction(boundary_id, mutate)

    def _record_path(self, boundary_id: str) -> Path:
        if not _BOUNDARY_ID.fullmatch(boundary_id):
            raise ValueError("invalid permission boundary id")
        return self.paths.permission_waits_dir / f"{boundary_id}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid permission checkpoint") from exc
        if not isinstance(value, dict):
            raise ValueError("invalid permission checkpoint")
        return value

    def _validate_record(self, record: Mapping[str, Any]) -> str:
        boundary_id = record.get("boundaryId")
        if not isinstance(boundary_id, str) or not _BOUNDARY_ID.fullmatch(boundary_id):
            raise ValueError("invalid permission boundary id")
        if record.get("schemaVersion") != 1 or record.get("sessionId") != self.session_id:
            raise ValueError("invalid permission checkpoint identity")
        if record.get("phase") not in {
            "WAITING",
            "TIMEOUT_GRACE",
            "SUSPENDING",
            "SUSPENDED",
            "RESTORING",
            "RESOLVED",
            "CANCELED",
        }:
            raise ValueError("invalid permission checkpoint phase")
        generation = record.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("invalid permission checkpoint generation")
        if not isinstance(record.get("payloadDigest"), str) or not _SHA256.fullmatch(record["payloadDigest"]):
            raise ValueError("invalid permission payload digest")
        if record.get("phase") in _ACTIVE_PHASES:
            if record.get("principalKind") not in {None, "a2a_user", "credential"}:
                raise ValueError("invalid permission checkpoint principal kind")
            permission_class = record.get("permissionClass")
            mode = record.get("mode")
            if permission_class not in {"normal", "pipeline"} or mode != permission_class:
                raise ValueError("invalid permission checkpoint class")
            self._validate_continuation_frame(record)
        return boundary_id

    @staticmethod
    def _validate_continuation_frame(record: Mapping[str, Any]) -> None:
        frame = record.get("continuationFrame")
        if not isinstance(frame, Mapping):
            raise ValueError("invalid permission continuation frame")
        message_ref = frame.get("assistantMessageRef")
        message_digest = frame.get("assistantMessageDigest")
        ordered_ids = frame.get("orderedToolUseIds")
        current_index = frame.get("currentIndex")
        current_payload_digest = frame.get("currentPayloadDigest")
        decisions = frame.get("decisions")
        if not isinstance(message_ref, str) or not message_ref:
            raise ValueError("invalid permission continuation message reference")
        transcript_id, _message_index = _parse_permission_message_ref(message_ref)
        permission_class = record.get("permissionClass")
        if (permission_class == "normal" and transcript_id is not None) or (
            permission_class == "pipeline" and transcript_id is None
        ):
            raise ValueError("invalid permission continuation transcript class")
        if not isinstance(message_digest, str) or not _SHA256.fullmatch(message_digest):
            raise ValueError("invalid permission continuation message digest")
        if (
            not isinstance(ordered_ids, list)
            or not ordered_ids
            or not all(isinstance(value, str) and value for value in ordered_ids)
            or len(set(ordered_ids)) != len(ordered_ids)
        ):
            raise ValueError("invalid permission continuation tool ordering")
        if (
            isinstance(current_index, bool)
            or not isinstance(current_index, int)
            or current_index < 0
            or current_index >= len(ordered_ids)
        ):
            raise ValueError("invalid permission continuation current index")
        if (
            not isinstance(current_payload_digest, str)
            or not _SHA256.fullmatch(current_payload_digest)
            or current_payload_digest != record.get("payloadDigest")
        ):
            raise ValueError("invalid permission continuation payload digest")
        if not isinstance(decisions, list) or len(decisions) != len(ordered_ids):
            raise ValueError("invalid permission continuation decisions")
        pending_indexes: list[int] = []
        for index, (tool_use_id, decision) in enumerate(zip(ordered_ids, decisions)):
            if not isinstance(decision, dict):
                raise ValueError("invalid permission continuation decision correlation")
            decision_record = cast(dict[str, Any], decision)
            if decision_record.get("toolUseId") != tool_use_id:
                raise ValueError("invalid permission continuation decision correlation")
            state = decision_record.get("state")
            if state not in {"not_evaluated", "pending", "allow", "deny"}:
                raise ValueError("invalid permission continuation decision state")
            if state == "allow" and decision_record.get("source") == "user":
                if "principalRef" not in decision_record or "region" not in decision_record:
                    raise ValueError("invalid permission continuation user identity")
                if not all(
                    value is None or isinstance(value, str)
                    for value in (decision_record["principalRef"], decision_record["region"])
                ):
                    raise ValueError("invalid permission continuation user identity")
            if state == "pending":
                pending_indexes.append(index)
        if pending_indexes != [current_index] or record.get("toolUseId") != ordered_ids[current_index]:
            raise ValueError("invalid permission continuation pending boundary")


@dataclass
class _LiveOwner:
    boundary_id: str
    generation: int
    future: asyncio.Future[bool | PermissionWaitOutcome]
    store: PermissionWaitCheckpointStore
    permission_resolution_lock: asyncio.Lock
    timer: asyncio.Task[None] | None = None
    timer_retry: asyncio.TimerHandle | None = None
    on_suspend: Callable[[], Awaitable[None] | None] | None = None
    owner_completed: asyncio.Event = field(default_factory=asyncio.Event)


def _consume_background_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve failures from a shielded delivery task if its caller went away."""

    if task.cancelled():
        return
    task.exception()


class PermissionWaitCoordinator:
    """Own process-local Futures while the checkpoint remains authoritative."""

    def __init__(self, policy: PermissionWaitPolicy | None = None) -> None:
        self.policy = policy or PermissionWaitPolicy()
        self._owners: dict[str, _LiveOwner] = {}
        self._restoring: set[str] = set()
        self._restore_lock = asyncio.Lock()

    def has_live_owners(self) -> bool:
        # A completed decision Future does not mean the continuation has
        # finished.  Keep the owner authoritative until its caller explicitly
        # unregisters it after persisting the successor ToolResult/receipt.
        return bool(self._restoring) or bool(self._owners)

    def is_restoring(self, boundary_id: str) -> bool:
        return boundary_id in self._restoring

    def has_live_boundary(self, boundary_id: str) -> bool:
        return boundary_id in self._owners

    async def acquire_restore(self, boundary_id: str) -> bool:
        async with self._restore_lock:
            if boundary_id in self._restoring:
                return False
            self._restoring.add(boundary_id)
            return True

    async def release_restore(self, boundary_id: str) -> None:
        async with self._restore_lock:
            self._restoring.discard(boundary_id)

    def register_live(
        self,
        *,
        record: Mapping[str, Any],
        store: PermissionWaitCheckpointStore,
        future: asyncio.Future[bool | PermissionWaitOutcome],
        on_suspend: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        boundary_id = str(record["boundaryId"])
        existing = self._owners.get(boundary_id)
        if existing is not None:
            if existing.future is future:
                # Pipeline publication may expose the same durable boundary
                # more than once. Keep the original owner and timer: replacing
                # it with a stale checkpoint generation can strand the Future
                # after the resident deadline.
                logger.info(
                    "Permission wait live owner registration reused boundary_id=%s generation=%s",
                    boundary_id,
                    existing.generation,
                )
                return
            raise RuntimeError("permission boundary already has a different live owner")
        owner = _LiveOwner(
            boundary_id=boundary_id,
            generation=int(record["generation"]),
            future=future,
            store=store,
            permission_resolution_lock=asyncio.Lock(),
            on_suspend=on_suspend,
        )
        self._owners[boundary_id] = owner
        logger.info(
            "Permission wait live owner registered boundary_id=%s generation=%s resident_timeout_seconds=%s",
            boundary_id,
            owner.generation,
            self.policy.resident_timeout_seconds,
        )
        if self.policy.resident_timeout_seconds is not None:
            self._start_resident_timer(owner)

    def _start_resident_timer(self, owner: _LiveOwner) -> None:
        """Start the request-independent deadline task for one live owner."""

        if self._owners.get(owner.boundary_id) is not owner or owner.future.done():
            return
        owner.timer_retry = None
        timer = asyncio.create_task(
            self._run_resident_timer(owner),
            name=f"permission-wait-{owner.boundary_id}",
        )
        owner.timer = timer
        logger.info(
            "Permission wait resident timer scheduled boundary_id=%s generation=%s loop=%s",
            owner.boundary_id,
            owner.generation,
            id(timer.get_loop()),
        )
        timer.add_done_callback(lambda completed: self._resident_timer_finished(owner, completed))

    def _resident_timer_finished(self, owner: _LiveOwner, timer: asyncio.Task[None]) -> None:
        """Re-arm only an unexpectedly canceled live deadline task.

        The timer is created while an A2A request is active, but its lifetime is
        owned by the durable permission boundary.  A transport/request cancel
        must therefore not silently strand an authoritative Future in WAITING.
        The short TimerHandle indirection also avoids creating a new Task while
        an event loop is completing its own shutdown cancellation pass.
        """

        if owner.timer is not timer:
            return
        owner.timer = None
        if timer.cancelled():
            if self._owners.get(owner.boundary_id) is not owner or owner.future.done():
                return
            logger.warning(
                "Permission wait resident timer was canceled while its owner remained live; "
                "re-arming from the persisted deadline boundary_id=%s generation=%s",
                owner.boundary_id,
                owner.generation,
            )
            loop = timer.get_loop()
            if not loop.is_closed():
                owner.timer_retry = loop.call_later(0.1, self._start_resident_timer, owner)
            return
        error = timer.exception()
        if error is not None:
            logger.error(
                "Permission wait resident timer failed boundary_id=%s generation=%s",
                owner.boundary_id,
                owner.generation,
                exc_info=(type(error), error, error.__traceback__),
            )

    def unregister_live(self, boundary_id: str) -> None:
        owner = self._owners.pop(boundary_id, None)
        if owner is None:
            return
        try:
            record = owner.store.load(boundary_id)
            if record is not None:
                logger.info(
                    "Permission wait live owner released boundary_id=%s generation=%s phase=%s future_done=%s",
                    boundary_id,
                    record.get("generation"),
                    record.get("phase"),
                    owner.future.done(),
                )
            if record is not None and record.get("phase") == "SUSPENDING":
                owner.store.mark_suspended(boundary_id, expected_generation=owner.generation)
        except ValueError:
            # A newer cross-process generation is authoritative. Its next
            # request reconciles any orphaned SUSPENDING phase.
            pass
        finally:
            owner.owner_completed.set()
            if owner.timer_retry is not None:
                owner.timer_retry.cancel()
                owner.timer_retry = None
            if owner.timer is not None and owner.timer is not asyncio.current_task():
                owner.timer.cancel()

    async def wait_for_suspended_owner(self, boundary_id: str, *, timeout_seconds: float = 5.0) -> bool:
        """Wait outside all resolution locks for a SUSPENDING owner to unwind.

        ``False`` means the same process-local owner is still alive after the
        bounded wait.  It must remain authoritative: treating it as a crashed
        owner would allow a recovery continuation to overlap its cleanup.
        """

        owner = self._owners.get(boundary_id)
        if owner is not None:
            try:
                await asyncio.wait_for(owner.owner_completed.wait(), timeout=max(0.0, timeout_seconds))
            except asyncio.TimeoutError:
                pass
        current_owner = self._owners.get(boundary_id)
        if current_owner is not None:
            return False
        return True

    async def claim_live(
        self,
        *,
        boundary_id: str,
        value: Literal["allow_once", "deny"],
        source: str = "user",
        on_new_claim: Callable[[str], bool] | None = None,
        before_delivery: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        # Keep delivery independent of the transport request that carried the
        # answer. Once the decision is durable, canceling that request must not
        # strand the resident continuation before its Future is completed.
        delivery = asyncio.create_task(
            self._claim_live_and_deliver(
                boundary_id=boundary_id,
                value=value,
                source=source,
                on_new_claim=on_new_claim,
                before_delivery=before_delivery,
            )
        )
        delivery.add_done_callback(_consume_background_exception)
        return await asyncio.shield(delivery)

    async def _claim_live_and_deliver(
        self,
        *,
        boundary_id: str,
        value: Literal["allow_once", "deny"],
        source: str,
        on_new_claim: Callable[[str], bool] | None,
        before_delivery: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
    ) -> tuple[dict[str, Any], bool]:
        owner = self._owners.get(boundary_id)
        if owner is None:
            raise LookupError("permission boundary has no live owner")
        async with owner.permission_resolution_lock:
            reconciled = owner.store.reconcile_deadline(
                boundary_id,
                grace_seconds=self.policy.timeout_grace_seconds,
                live_owner=True,
                expected_generation=owner.generation,
            )
            owner.generation = int(reconciled["generation"])
            record, created = owner.store.claim_decision(
                boundary_id,
                value=value,
                source=source,
                expected_generation=owner.generation,
            )
            owner.generation = int(record["generation"])
            decision = record["decision"]
            claim_id = str(decision["claimId"])
            delivered_value = str(decision["value"])
            if on_new_claim is not None:
                record, _audit_created = owner.store.run_claim_audit_once(
                    boundary_id,
                    claim_id=claim_id,
                    audit=on_new_claim,
                )
                owner.generation = int(record["generation"])
                delivered_value = str(record["decision"]["value"])
            decision = record["decision"]
            if decision.get("backupStatus") != "committed":
                if before_delivery is not None:
                    result = before_delivery(record)
                    if asyncio.iscoroutine(result):
                        await result
                record = owner.store.mark_claim_backed_up(boundary_id, claim_id=claim_id)
                owner.generation = int(record["generation"])
            phase = record.get("phase")
            if phase in {"SUSPENDING", "SUSPENDED", "RESTORING"}:
                return record, created
            if not owner.future.done():
                owner.future.set_result(delivered_value == "allow_once")
            record = owner.store.mark_applied(boundary_id, claim_id=claim_id)
            owner.generation = int(record["generation"])
            if owner.timer is not None:
                owner.timer.cancel()
            return record, created

    async def cancel_live(self, boundary_id: str) -> bool:
        owner = self._owners.get(boundary_id)
        if owner is None:
            return False
        async with owner.permission_resolution_lock:
            try:
                record = owner.store.cancel(boundary_id, expected_generation=owner.generation)
                owner.generation = int(record["generation"])
            except ValueError:
                return False
            if not owner.future.done():
                owner.future.cancel()
            if owner.timer is not None:
                owner.timer.cancel()
            return True

    async def suspend_now(self, boundary_id: str) -> bool:
        owner = self._owners.get(boundary_id)
        if owner is None:
            return False
        callback = None
        async with owner.permission_resolution_lock:
            try:
                record = owner.store.reconcile_deadline(
                    boundary_id,
                    grace_seconds=self.policy.timeout_grace_seconds,
                    live_owner=True,
                    expected_generation=owner.generation,
                )
            except ValueError:
                current = owner.store.load(boundary_id)
                logger.warning(
                    "Permission wait suspension lost generation fence boundary_id=%s owner_generation=%s "
                    "record_generation=%s phase=%s",
                    boundary_id,
                    owner.generation,
                    current.get("generation") if current is not None else None,
                    current.get("phase") if current is not None else None,
                )
                return False
            owner.generation = int(record["generation"])
            if record.get("phase") != "SUSPENDING":
                return False
            if not owner.future.done():
                owner.future.set_result(PermissionWaitOutcome.SUSPEND)
            callback = owner.on_suspend
        if callback is not None:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        return True

    async def _run_resident_timer(self, owner: _LiveOwner) -> None:
        try:
            record = owner.store.load(owner.boundary_id)
            if record is None:
                return
            resident_deadline = parse_utc(record.get("residentDeadlineAt"))
            if resident_deadline is None:
                return
            delay = max(0.0, (resident_deadline - utc_now()).total_seconds())
            logger.info(
                "Permission wait resident timer started boundary_id=%s generation=%s delay_seconds=%.3f",
                owner.boundary_id,
                owner.generation,
                delay,
            )
            await asyncio.sleep(delay)
            logger.info(
                "Permission wait resident timer woke boundary_id=%s generation=%s",
                owner.boundary_id,
                owner.generation,
            )
            async with owner.permission_resolution_lock:
                record = owner.store.reconcile_deadline(
                    owner.boundary_id,
                    grace_seconds=self.policy.timeout_grace_seconds,
                    live_owner=True,
                    expected_generation=owner.generation,
                )
                owner.generation = int(record["generation"])
                if record.get("phase") == "SUSPENDING":
                    grace_deadline = None
                elif record.get("phase") != "TIMEOUT_GRACE":
                    return
                else:
                    grace_deadline = parse_utc(record.get("graceDeadlineAt"))
                logger.info(
                    "Permission wait resident deadline reconciled boundary_id=%s generation=%s phase=%s",
                    owner.boundary_id,
                    owner.generation,
                    record.get("phase"),
                )
            if grace_deadline is not None:
                await asyncio.sleep(max(0.0, (grace_deadline - utc_now()).total_seconds()))
            suspended = await self.suspend_now(owner.boundary_id)
            while not suspended:
                current = owner.store.load(owner.boundary_id)
                decision = current.get("decision") if current is not None else None
                if (
                    current is None
                    or current.get("phase") != "TIMEOUT_GRACE"
                    or not isinstance(decision, Mapping)
                    or decision.get("status") != "none"
                ):
                    break
                grace_deadline = parse_utc(current.get("graceDeadlineAt"))
                if grace_deadline is None:
                    break
                await asyncio.sleep(max(0.001, (grace_deadline - utc_now()).total_seconds()))
                suspended = await self.suspend_now(owner.boundary_id)
            logger.info(
                "Permission wait grace completion handled boundary_id=%s suspended=%s",
                owner.boundary_id,
                suspended,
            )
        except ValueError:
            return


def build_permission_checkpoint(
    *,
    session_id: str,
    task_id: str | None,
    context_id: str,
    input_id: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: Mapping[str, Any],
    permission_class: PermissionClass,
    continuation_frame: Mapping[str, Any],
    policy: PermissionWaitPolicy,
    principal_ref: str | None = None,
    principal_kind: PermissionPrincipalKind | None = None,
    region: str | None = None,
    pipeline_coordinates: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    created = now or utc_now()
    boundary_id = new_boundary_id()
    frame = dict(continuation_frame)
    prepared_payload_digest = canonical_digest({"name": tool_name, "input": tool_input})
    canonical_payload_digest = frame.get("currentPayloadDigest", prepared_payload_digest)
    if not isinstance(canonical_payload_digest, str) or not _SHA256.fullmatch(canonical_payload_digest):
        raise ValueError("invalid permission continuation payload digest")
    frame["currentPayloadDigest"] = canonical_payload_digest
    resident_deadline = None
    if policy.resident_timeout_seconds is not None:
        resident_deadline = format_utc(created + timedelta(seconds=policy.resident_timeout_seconds))
    return {
        "schemaVersion": 1,
        "boundaryId": boundary_id,
        "inputId": input_id,
        "taskId": task_id,
        "contextId": context_id,
        "sessionId": session_id,
        "principalRef": principal_ref,
        "principalKind": (
            principal_kind
            or (
                "a2a_user"
                if principal_ref is not None and PermissionExecutionIdentityScope.current_principal_id() is not None
                else "credential"
                if principal_ref is not None
                else None
            )
        ),
        "region": region,
        "mode": "normal" if permission_class == "normal" else "pipeline",
        "permissionClass": permission_class,
        "toolUseId": tool_use_id,
        "toolName": tool_name,
        "payloadDigest": canonical_payload_digest,
        "pipelineCoordinates": dict(pipeline_coordinates) if pipeline_coordinates is not None else None,
        "continuationFrame": frame,
        "phase": "WAITING",
        "generation": 1,
        "createdAt": format_utc(created),
        "updatedAt": format_utc(created),
        "residentDeadlineAt": resident_deadline,
        "graceDeadlineAt": None,
        "decision": {"status": "none", "value": None, "source": None, "claimId": None},
    }


__all__ = [
    "PermissionWaitCheckpointStore",
    "PermissionWaitCoordinator",
    "PermissionWaitPolicy",
    "PermissionExecutionIdentity",
    "PermissionExecutionIdentityScope",
    "RecoveredPermissionAuditBoundary",
    "build_permission_checkpoint",
    "canonical_digest",
    "canonicalize_permission_continuation_frame",
    "format_utc",
    "parse_utc",
    "permission_execution_identity",
    "recover_permission_audit_boundary",
]
