"""PipelineSession — sidecar directory persistence for pipeline crash recovery."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

import yaml

from iac_code.pipeline.engine.types import StepStatus

PipelineStatus = Literal["running", "waiting_input", "completed", "user_aborted", "failed", "discarded"]
RESUMABLE_STATUSES: set[PipelineStatus] = {"running", "waiting_input"}
SKIP_RESTORE_STATUSES: set[PipelineStatus] = {"completed", "user_aborted", "failed", "discarded"}

_ALL_STATUSES = RESUMABLE_STATUSES | SKIP_RESTORE_STATUSES
_EMPTY_IDENTITY = {
    "pipeline_name": "",
    "step_ids": [],
    "sub_pipeline_step_ids": {},
    "pipeline_fingerprint": "",
}
_EMPTY_LEGACY_RESTORE = {
    "state_machine_snapshot": {
        "current_index": 0,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {},
    },
    "context_snapshot": {},
    "current_step": None,
}
logger = logging.getLogger(__name__)
_PRESERVE_METADATA = object()
_MetadataValue = dict[str, Any] | None | object


@dataclass(frozen=True)
class PipelineIdentity:
    pipeline_name: str = ""
    step_ids: list[str] | None = None
    sub_pipeline_step_ids: dict[str, list[str]] | None = None
    pipeline_fingerprint: str = ""


@dataclass(frozen=True)
class RestoreResult:
    ok: bool
    reason: str | None = None
    status: str | None = None
    state_machine_snapshot: dict[str, Any] | None = None
    context_snapshot: dict[str, Any] | None = None
    current_step: str | None = None
    execution: dict[str, Any] | None = None
    attempts: dict[str, Any] | None = None
    normal_handoff: dict[str, Any] | None = None


class PipelineSession:
    """Manages pipeline state snapshots in a sidecar directory.

    Layout (nested under the session directory; see PipelineRunner)::

        <session_id>/pipeline/
        ├── meta.yaml      — state machine snapshot + current step
        ├── context.yaml   — PipelineContext snapshot (conclusions, stale, history)
        └── events.jsonl   — local event log (rollbacks, transitions)
    """

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.meta_path = session_dir / "meta.yaml"
        self.context_path = session_dir / "context.yaml"
        self.events_path = session_dir / "events.jsonl"

    def save_running_sync(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self._save_snapshot_sync(
            "running",
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    async def save_running(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.save_running_sync(
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    def save_waiting_input_sync(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self._save_snapshot_sync(
            "waiting_input",
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    async def save_waiting_input(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.save_waiting_input_sync(
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    def save_completed_sync(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self._save_snapshot_sync(
            "completed",
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    async def save_completed(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.save_completed_sync(
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    def save_failed_sync(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self._save_snapshot_sync(
            "failed",
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    async def save_failed(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.save_failed_sync(
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    def save_user_aborted_sync(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self._save_snapshot_sync(
            "user_aborted",
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    async def save_user_aborted(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.save_user_aborted_sync(
            step_id,
            state_machine_snapshot,
            context_snapshot,
            identity,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    async def save_step_completion(
        self,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.save_running_sync(
            step_id,
            state_machine_snapshot,
            context_snapshot,
            _EMPTY_IDENTITY,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    async def save_rollback(
        self,
        from_step: str,
        to_step: str,
        reason: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.save_rollback_sync(
            from_step,
            to_step,
            reason,
            state_machine_snapshot,
            context_snapshot,
            identity or _EMPTY_IDENTITY,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    def save_rollback_sync(
        self,
        from_step: str,
        to_step: str,
        reason: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._append_event(
            {"type": "rollback", "from": from_step, "to": to_step, "reason": reason, "timestamp": time.time()}
        )
        self.save_running_sync(
            to_step,
            state_machine_snapshot,
            context_snapshot,
            identity or _EMPTY_IDENTITY,
            reason=reason,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    def mark_discarded(self, reason: str | None = None) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        existing = self._load_existing_meta()
        existing.update(
            {
                "status": "discarded",
                "resume_policy": "none",
                "terminal": True,
                "current_step": existing.get("current_step"),
                "state_machine": existing.get("state_machine", {}),
                "updated_at": time.time(),
                "discarded_at": time.time(),
                "reason": reason,
            }
        )
        for key, value in _EMPTY_IDENTITY.items():
            existing.setdefault(key, value)
        self._atomic_write_yaml(self.meta_path, existing)

    @overload
    def restore_sync(self) -> dict: ...

    @overload
    def restore_sync(self, identity: PipelineIdentity | dict) -> RestoreResult: ...

    def restore_sync(self, identity: PipelineIdentity | dict | None = None) -> RestoreResult | dict:
        """Sync variant of restore() for use during PipelineRunner.__init__ where
        no event loop is available. Functionally identical to restore() but
        callable synchronously."""
        result = self._restore_result(identity)
        if identity is not None:
            return result
        if not result.ok:
            return dict(_EMPTY_LEGACY_RESTORE)
        return {
            "state_machine_snapshot": result.state_machine_snapshot,
            "context_snapshot": result.context_snapshot,
            "current_step": result.current_step,
        }

    @overload
    async def restore(self) -> dict: ...

    @overload
    async def restore(self, identity: PipelineIdentity | dict) -> RestoreResult: ...

    async def restore(self, identity: PipelineIdentity | dict | None = None) -> RestoreResult | dict:
        """Async wrapper (preserves existing API; internally same as restore_sync)."""
        if identity is None:
            return self.restore_sync()
        return self.restore_sync(identity)

    def exists(self) -> bool:
        return self.meta_path.exists()

    def is_resumable(self, identity: PipelineIdentity | dict) -> bool:
        result = self.restore_sync(identity)
        return result.ok

    def has_resumable_status(self) -> bool:
        """Return True when sidecar metadata advertises an active pipeline state.

        This deliberately checks only the lightweight status marker. Full
        identity/context validation still happens during restore.
        """
        if not self.meta_path.exists():
            return False
        try:
            meta = yaml.safe_load(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return False
        if not isinstance(meta, dict):
            return False
        status = meta.get("status", "running")
        return status in RESUMABLE_STATUSES

    def delete(self) -> bool:
        """Compatibility no-delete API: mark an existing sidecar as discarded."""
        if not self.session_dir.exists():
            return True
        try:
            self.mark_discarded(reason="delete requested; sidecar preserved")
        except OSError:
            logger.exception("Failed to mark pipeline sidecar discarded: path=%s", self.session_dir)
            return False
        return True

    def load_attempts_metadata(self) -> dict[str, Any] | None:
        attempts = self._load_existing_meta().get("attempts")
        return attempts if isinstance(attempts, dict) else None

    def _append_event(self, event: dict) -> None:
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _load_existing_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            return {}
        try:
            loaded = yaml.safe_load(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save_snapshot_sync(
        self,
        status: PipelineStatus,
        step_id: str,
        state_machine_snapshot: dict,
        context_snapshot: dict,
        identity: PipelineIdentity | dict,
        reason: str | None = None,
        *,
        execution: _MetadataValue = _PRESERVE_METADATA,
        attempts: _MetadataValue = _PRESERVE_METADATA,
        normal_handoff: _MetadataValue = _PRESERVE_METADATA,
    ) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        existing_meta = self._load_existing_meta()
        identity_data = self._identity_from(identity)
        terminal = status in SKIP_RESTORE_STATUSES
        meta = {
            **identity_data,
            "status": status,
            "resume_policy": "none" if terminal else "active",
            "terminal": terminal,
            "current_step": step_id,
            "state_machine": state_machine_snapshot,
            "updated_at": time.time(),
            "reason": reason,
        }
        self._preserve_metadata_field(meta, existing_meta, "execution", execution)
        self._preserve_metadata_field(meta, existing_meta, "attempts", attempts)
        self._preserve_metadata_field(meta, existing_meta, "normal_handoff", normal_handoff)
        self._atomic_write_yaml(self.context_path, context_snapshot)
        self._atomic_write_yaml(self.meta_path, meta)

    def _preserve_metadata_field(
        self,
        meta: dict[str, Any],
        existing_meta: dict[str, Any],
        key: str,
        value: _MetadataValue,
    ) -> None:
        if value is _PRESERVE_METADATA:
            existing_value = existing_meta.get(key)
            if isinstance(existing_value, dict):
                meta[key] = existing_value
            return
        if isinstance(value, dict):
            meta[key] = value

    def _restore_result(self, identity: PipelineIdentity | dict | None) -> RestoreResult:
        if not self.meta_path.exists():
            return RestoreResult(ok=False, reason="missing_meta")

        try:
            meta = yaml.safe_load(self.meta_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._restore_failure("missing_meta")
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return self._restore_failure("corrupt_meta")
        if not isinstance(meta, dict):
            return self._restore_failure("invalid_meta")

        status = meta.get("status", "running")
        if not isinstance(status, str):
            return self._restore_failure("invalid_meta")
        if status not in _ALL_STATUSES:
            return self._restore_failure("unknown_status", status=status)
        if not self._valid_metadata_fields(meta):
            return self._restore_failure("invalid_meta", status=status)
        execution, attempts, normal_handoff = self._restore_metadata_fields(meta)
        if status in SKIP_RESTORE_STATUSES:
            return RestoreResult(
                ok=False,
                reason=meta.get("reason"),
                status=status,
                execution=execution,
                attempts=attempts,
                normal_handoff=normal_handoff,
            )

        identity_data = self._identity_from(identity) if identity is not None else None
        if identity_data is not None and not self._identity_matches_data(meta, identity_data):
            return RestoreResult(ok=False, reason="pipeline_identity_mismatch", status=status)

        if "current_step" not in meta or "state_machine" not in meta:
            return self._restore_failure("invalid_meta", status=status)
        if not isinstance(meta["current_step"], str):
            return self._restore_failure("invalid_meta", status=status)
        state_machine = meta["state_machine"]
        if not isinstance(state_machine, dict):
            return self._restore_failure("invalid_meta", status=status)
        if identity_data is not None and not self._valid_state_machine_snapshot(
            state_machine, meta["current_step"], identity_data.get("step_ids") or []
        ):
            return self._restore_failure("invalid_meta", status=status)
        if not self.context_path.exists():
            return self._restore_failure("missing_context", status=status)
        try:
            context_data = yaml.safe_load(self.context_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._restore_failure("missing_context", status=status)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return self._restore_failure("corrupt_context", status=status)
        if context_data is None:
            context_data = {}
        if not isinstance(context_data, dict):
            return self._restore_failure("invalid_context", status=status)
        if identity_data is not None and not self._valid_context_snapshot(context_data):
            return self._restore_failure("invalid_context", status=status)
        return RestoreResult(
            ok=True,
            status=status,
            state_machine_snapshot=state_machine,
            context_snapshot=context_data,
            current_step=meta["current_step"],
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

    def _restore_metadata_fields(
        self, meta: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        execution = meta.get("execution")
        attempts = meta.get("attempts")
        normal_handoff = meta.get("normal_handoff")
        return (
            execution if isinstance(execution, dict) else None,
            attempts if isinstance(attempts, dict) else None,
            normal_handoff if isinstance(normal_handoff, dict) else None,
        )

    def _restore_failure(self, reason: str | None, status: str | None = None) -> RestoreResult:
        logger.warning(
            "Failed to restore pipeline sidecar: reason=%s status=%s path=%s",
            reason,
            status,
            self.session_dir,
        )
        return RestoreResult(ok=False, reason=reason, status=status)

    def _identity_from(self, identity: PipelineIdentity | dict) -> dict[str, Any]:
        if isinstance(identity, PipelineIdentity):
            return {
                "pipeline_name": identity.pipeline_name,
                "step_ids": list(identity.step_ids or []),
                "sub_pipeline_step_ids": dict(identity.sub_pipeline_step_ids or {}),
                "pipeline_fingerprint": identity.pipeline_fingerprint,
            }
        return {
            "pipeline_name": identity.get("pipeline_name", ""),
            "step_ids": list(identity.get("step_ids") or []),
            "sub_pipeline_step_ids": dict(identity.get("sub_pipeline_step_ids") or {}),
            "pipeline_fingerprint": identity.get("pipeline_fingerprint", ""),
        }

    def _identity_matches(self, meta: dict, identity: PipelineIdentity | dict) -> bool:
        expected = self._identity_from(identity)
        return self._identity_matches_data(meta, expected)

    def _identity_matches_data(self, meta: dict, expected: dict[str, Any]) -> bool:
        return all(meta.get(key) == value for key, value in expected.items())

    def _valid_state_machine_snapshot(self, snapshot: dict[str, Any], current_step: str, step_ids: list[str]) -> bool:
        required = ("current_index", "rollback_count", "step_statuses")
        if any(key not in snapshot for key in required):
            return False

        current_index = snapshot["current_index"]
        rollback_count = snapshot["rollback_count"]
        interrupt_rollback_count = snapshot.get("interrupt_rollback_count", 0)
        if not self._valid_non_negative_int(current_index):
            return False
        if not self._valid_non_negative_int(rollback_count):
            return False
        if not self._valid_non_negative_int(interrupt_rollback_count):
            return False
        if step_ids and (current_index >= len(step_ids) or step_ids[current_index] != current_step):
            return False

        step_statuses = snapshot["step_statuses"]
        if not isinstance(step_statuses, dict):
            return False
        valid_statuses = {status.value for status in StepStatus}
        for step_id, step_status in step_statuses.items():
            if not isinstance(step_id, str):
                return False
            if step_ids and step_id not in step_ids:
                return False
            if not isinstance(step_status, str) or step_status not in valid_statuses:
                return False
        return True

    def _valid_context_snapshot(self, snapshot: dict[str, Any]) -> bool:
        for field_name, field_data in snapshot.items():
            if not isinstance(field_name, str):
                return False
            if not isinstance(field_data, dict):
                return False
            if "value" not in field_data or "version" not in field_data or "stale" not in field_data:
                return False
            value = field_data["value"]
            if value is not None and not isinstance(value, (dict, list)):
                return False
            if not self._valid_non_negative_int(field_data["version"]):
                return False
            if not isinstance(field_data["stale"], bool):
                return False
            updated_at = field_data.get("updated_at")
            if updated_at is not None and (isinstance(updated_at, bool) or not isinstance(updated_at, int | float)):
                return False
            history = field_data.get("history", [])
            if not isinstance(history, list):
                return False
        return True

    def _valid_metadata_fields(self, meta: dict[str, Any]) -> bool:
        return (
            self._valid_execution_metadata(meta.get("execution"))
            and self._valid_attempts_metadata(meta.get("attempts"))
            and self._valid_optional_dict_metadata(meta.get("normal_handoff"))
        )

    def _valid_optional_dict_metadata(self, value: Any) -> bool:
        return value is None or isinstance(value, dict)

    def _valid_optional_string(self, value: Any) -> bool:
        return value is None or isinstance(value, str)

    def _valid_execution_metadata(self, value: Any) -> bool:
        if value is None:
            return True
        if not isinstance(value, dict):
            return False
        for key in ("kind", "step_id", "active_attempt_id", "transcript_id"):
            if not self._valid_optional_string(value.get(key)):
                return False
        candidates = value.get("candidates")
        if candidates is None:
            return True
        if not isinstance(candidates, dict):
            return False
        for candidate_key, candidate_data in candidates.items():
            if not isinstance(candidate_key, str) or not isinstance(candidate_data, dict):
                return False
            for key in ("active_attempt_id", "transcript_id", "sub_pipeline_id", "sub_step_id"):
                if not self._valid_optional_string(candidate_data.get(key)):
                    return False
            candidate_index = candidate_data.get("candidate_index")
            if candidate_index is not None and not self._valid_non_negative_int(candidate_index):
                return False
        return True

    def _valid_attempts_metadata(self, value: Any) -> bool:
        if value is None:
            return True
        if not isinstance(value, dict):
            return False
        next_attempt_number = value.get("next_attempt_number", 1)
        if not self._valid_positive_int(next_attempt_number):
            return False
        items = value.get("items", {})
        if not isinstance(items, dict):
            return False
        for attempt_key, attempt_data in items.items():
            if not isinstance(attempt_key, str) or not isinstance(attempt_data, dict):
                return False
            for key in (
                "attempt_id",
                "scope",
                "step_id",
                "status",
                "transcript_id",
                "parent_step_id",
                "sub_pipeline_id",
                "sub_step_id",
            ):
                if not self._valid_optional_string(attempt_data.get(key)):
                    return False
            candidate_index = attempt_data.get("candidate_index")
            if candidate_index is not None and not self._valid_non_negative_int(candidate_index):
                return False
        return True

    def _valid_non_negative_int(self, value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def _valid_positive_int(self, value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1

    def _atomic_write_yaml(self, path: Path, data: dict) -> None:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.session_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                encoding="utf-8",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                yaml.dump(data, tmp_file, allow_unicode=True)
            os.replace(tmp_path, path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
