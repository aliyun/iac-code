"""Cleanup ledger and observer for pipeline rollback leftovers."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from iac_code.agent.message import Message
from iac_code.i18n import _
from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
from iac_code.types.stream_events import StackProgressEvent, ToolResultEvent, ToolUseEndEvent
from iac_code.utils.public_errors import sanitize_public_text
from iac_code.utils.state_io import atomic_write_text

logger = logging.getLogger(__name__)

CleanupStatus = Literal["pending", "started", "in_progress", "completed", "failed", "skipped"]
_LOAD_FAILED_KEY = "_load_failed"
_LOAD_ERROR_KEY = "_load_error"
_RETRYABLE_CLEANUP_STATUSES = {"pending", "failed"}
_ACTIVE_CLEANUP_STATUSES = {"started", "in_progress"}
_FOLLOWUP_CLEANUP_STATUSES = _RETRYABLE_CLEANUP_STATUSES | _ACTIVE_CLEANUP_STATUSES
_TERMINAL_CLEANUP_STATUSES = {"completed", "skipped"}
_DELETE_COMPLETE_STATUSES = {"DELETE_COMPLETE"}
_DELETE_FAILED_STATUSES = {"DELETE_FAILED"}
_LEDGER_LOCKS: dict[Path, threading.RLock] = {}
_LEDGER_LOCKS_LOCK = threading.Lock()


@dataclass(frozen=True)
class ObservedResource:
    provider: str
    resource_type: str
    resource_id: str
    resource_name: str = ""
    region_id: str = ""
    source_step_id: str = ""
    source_attempt_id: str | None = None
    observed_action: str = ""
    observed_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return _resource_key(self.provider, self.resource_type, self.resource_id, self.region_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ObservedResource":
        return cls(
            provider=str(data.get("provider") or ""),
            resource_type=str(data.get("resource_type") or ""),
            resource_id=str(data.get("resource_id") or ""),
            resource_name=str(data.get("resource_name") or ""),
            region_id=str(data.get("region_id") or ""),
            source_step_id=str(data.get("source_step_id") or ""),
            source_attempt_id=_optional_str(data.get("source_attempt_id")),
            observed_action=str(data.get("observed_action") or ""),
            observed_at=_float_value(data.get("observed_at")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class CleanupResource:
    provider: str
    resource_type: str
    resource_id: str
    resource_name: str = ""
    region_id: str = ""
    source_step_id: str = ""
    source_attempt_id: str | None = None
    cleanup_reason: str = ""
    cleanup_required: bool = True
    cleanup_status: CleanupStatus = "pending"
    cleanup_tool_use_id: str | None = None
    cleanup_action: str | None = None
    progress_status: str | None = None
    progress_percentage: float | None = None
    last_error: str | None = None
    observed_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return _resource_key(self.provider, self.resource_type, self.resource_id, self.region_id)

    @classmethod
    def from_observed(cls, resource: ObservedResource, *, reason: str) -> "CleanupResource":
        now = time.time()
        return cls(
            provider=resource.provider,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            resource_name=resource.resource_name,
            region_id=resource.region_id,
            source_step_id=resource.source_step_id,
            source_attempt_id=resource.source_attempt_id,
            cleanup_reason=reason,
            observed_at=resource.observed_at,
            updated_at=now,
            metadata=dict(resource.metadata),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CleanupResource":
        status = str(data.get("cleanup_status") or "pending")
        if status not in {"pending", "started", "in_progress", "completed", "failed", "skipped"}:
            status = "pending"
        return cls(
            provider=str(data.get("provider") or ""),
            resource_type=str(data.get("resource_type") or ""),
            resource_id=str(data.get("resource_id") or ""),
            resource_name=str(data.get("resource_name") or ""),
            region_id=str(data.get("region_id") or ""),
            source_step_id=str(data.get("source_step_id") or ""),
            source_attempt_id=_optional_str(data.get("source_attempt_id")),
            cleanup_reason=str(data.get("cleanup_reason") or ""),
            cleanup_required=bool(data.get("cleanup_required", True)),
            cleanup_status=cast(CleanupStatus, status),
            cleanup_tool_use_id=_optional_str(data.get("cleanup_tool_use_id")),
            cleanup_action=_optional_str(data.get("cleanup_action")),
            progress_status=_optional_str(data.get("progress_status")),
            progress_percentage=_optional_float(data.get("progress_percentage")),
            last_error=_optional_str(data.get("last_error")),
            observed_at=_float_value(data.get("observed_at")),
            updated_at=_float_value(data.get("updated_at")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class CleanupPrompt:
    resources: list[CleanupResource]
    prompt: str
    status_message: str


class CleanupLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def observed_resources(self) -> list[ObservedResource]:
        data = self._load()
        return [ObservedResource.from_dict(item) for item in _dict_list(data.get("observed_resources"))]

    def cleanup_resources(self) -> list[CleanupResource]:
        data = self._load()
        return [CleanupResource.from_dict(item) for item in _dict_list(data.get("cleanup_resources"))]

    def history_entries(self) -> list[dict[str, Any]]:
        data = self._load()
        return [dict(item) for item in _dict_list(data.get("history"))]

    def pending_resources(self, *, include_failed: bool = True, include_active: bool = True) -> list[CleanupResource]:
        if include_failed and include_active:
            statuses = set(_FOLLOWUP_CLEANUP_STATUSES)
        else:
            statuses = set(_RETRYABLE_CLEANUP_STATUSES if include_failed else {"pending"})
            if include_active:
                statuses.update(_ACTIVE_CLEANUP_STATUSES)
        return [
            resource
            for resource in self.cleanup_resources()
            if resource.cleanup_required and resource.cleanup_status in statuses
        ]

    def load_failed(self) -> bool:
        return bool(self._load().get(_LOAD_FAILED_KEY))

    def load_error(self) -> str | None:
        error = self._load().get(_LOAD_ERROR_KEY)
        return error if isinstance(error, str) and error else None

    def active_resources(self) -> list[CleanupResource]:
        return [
            resource
            for resource in self.cleanup_resources()
            if resource.cleanup_required and resource.cleanup_status in _ACTIVE_CLEANUP_STATUSES
        ]

    def record_observed(self, resource: ObservedResource) -> None:
        if not resource.resource_id:
            return
        with self._write_lock():
            data = self._load_for_write()
            if data is None:
                return
            observed = {
                ObservedResource.from_dict(item).key: ObservedResource.from_dict(item)
                for item in _dict_list(data.get("observed_resources"))
            }
            observed[resource.key] = resource
            data["observed_resources"] = [asdict(item) for item in observed.values()]
            self._save(data)

    def mark_cleanup_required(
        self,
        resources: list[CleanupResource],
        *,
        source_step_id: str,
        reason: str,
    ) -> None:
        if not resources:
            return
        with self._write_lock():
            data = self._load_for_write()
            if data is None:
                return
            cleanup = {
                CleanupResource.from_dict(item).key: CleanupResource.from_dict(item)
                for item in _dict_list(data.get("cleanup_resources"))
            }
            now = time.time()
            changed_count = 0
            for resource in resources:
                if not resource.resource_id:
                    continue
                existing = cleanup.get(resource.key)
                merged = _merge_cleanup_required(
                    existing,
                    resource,
                    source_step_id=source_step_id,
                    reason=reason,
                    now=now,
                )
                if existing == merged:
                    continue
                cleanup[resource.key] = merged
                changed_count += 1
            if changed_count == 0:
                return
            data["cleanup_resources"] = [asdict(item) for item in cleanup.values()]
            self._append_history(
                data,
                {
                    "type": "cleanup_required",
                    "source_step_id": source_step_id,
                    "reason": reason,
                    "resource_count": changed_count,
                    "timestamp": now,
                },
            )
            self._save(data)

    def update_resource(
        self,
        *,
        provider: str,
        resource_type: str,
        resource_id: str,
        region_id: str | None = None,
        cleanup_status: CleanupStatus | None = None,
        cleanup_tool_use_id: str | None = None,
        cleanup_action: str | None = None,
        progress_status: str | None = None,
        progress_percentage: float | None = None,
        last_error: str | None = None,
        clear_last_error: bool = False,
    ) -> bool:
        with self._write_lock():
            data = self._load_for_write()
            if data is None:
                return False
            changed = False
            history_entries: list[dict[str, Any]] = []
            updated_items: list[CleanupResource] = []
            for item in _dict_list(data.get("cleanup_resources")):
                resource = CleanupResource.from_dict(item)
                if not _matches_resource(resource, provider, resource_type, resource_id, region_id):
                    updated_items.append(resource)
                    continue
                if resource.cleanup_status in _TERMINAL_CLEANUP_STATUSES and cleanup_status != resource.cleanup_status:
                    updated_items.append(resource)
                    continue
                updates: dict[str, Any] = {"updated_at": time.time()}
                if cleanup_status is not None:
                    updates["cleanup_status"] = cleanup_status
                if cleanup_tool_use_id is not None:
                    updates["cleanup_tool_use_id"] = cleanup_tool_use_id
                if cleanup_action is not None:
                    updates["cleanup_action"] = cleanup_action
                if progress_status is not None:
                    updates["progress_status"] = progress_status
                if progress_percentage is not None:
                    updates["progress_percentage"] = progress_percentage
                if last_error is not None:
                    updates["last_error"] = _safe_history_error(last_error)
                elif clear_last_error:
                    updates["last_error"] = None
                updated = replace(resource, **updates)
                updated_items.append(updated)
                changed = True
                if _cleanup_lifecycle_state(updated) != _cleanup_lifecycle_state(resource):
                    history_entries.append(_cleanup_lifecycle_history_entry(updated))
            if changed:
                data["cleanup_resources"] = [asdict(item) for item in updated_items]
                for entry in history_entries:
                    self._append_history(data, entry)
                self._save(data)
            return changed

    def record_prompt_queued(self, prompt: CleanupPrompt, *, ui_surface: str) -> None:
        with self._write_lock():
            data = self._load_for_write()
            if data is None:
                return
            resources = list(prompt.resources or [])
            self._append_history(
                data,
                {
                    "type": "cleanup_prompt_queued",
                    "ui_surface": ui_surface,
                    "resource_count": len(resources),
                    "resources": [_cleanup_resource_history_data(resource) for resource in resources],
                    "timestamp": time.time(),
                },
            )
            self._save(data)

    def record_tool_use_mapping(
        self,
        *,
        tool_use_id: str,
        provider: str,
        resource_type: str,
        resource_id: str,
        region_id: str,
        action: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        if not tool_use_id or not resource_id:
            return
        with self._write_lock():
            data = self._load_for_write()
            if data is None:
                return
            mappings = {
                str(item.get("tool_use_id")): dict(item)
                for item in _dict_list(data.get("tool_uses"))
                if item.get("tool_use_id")
            }
            mappings[tool_use_id] = {
                "tool_use_id": tool_use_id,
                "provider": provider,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "region_id": region_id,
                "action": action,
                "tool_name": tool_name,
                "input_summary": _safe_history_error(
                    json.dumps(
                        _cleanup_tool_input_summary(tool_input, resource_id=resource_id, region_id=region_id),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ),
            }
            data["tool_uses"] = list(mappings.values())
            self._save(data)

    def tool_use_mapping(self, tool_use_id: str) -> dict[str, Any] | None:
        if not tool_use_id:
            return None
        data = self._load()
        for item in _dict_list(data.get("tool_uses")):
            if item.get("tool_use_id") == tool_use_id:
                return dict(item)
        return None

    def record_tool_result_unmatched(self, *, tool_use_id: str, tool_name: str) -> None:
        with self._write_lock():
            data = self._load_for_write()
            if data is None:
                return
            self._append_history(
                data,
                {
                    "type": "cleanup_tool_result_unmatched",
                    "tool_use_id": tool_use_id,
                    "tool_name": tool_name,
                    "timestamp": time.time(),
                },
            )
            self._save(data)

    def record_tool_result_mismatch(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        mapped_resource_id: str,
        result_resource_id: str,
    ) -> None:
        with self._write_lock():
            data = self._load_for_write()
            if data is None:
                return
            self._append_history(
                data,
                {
                    "type": "cleanup_tool_result_mismatch",
                    "tool_use_id": _safe_history_error(tool_use_id),
                    "tool_name": _safe_history_error(tool_name),
                    "mapped_resource_id": _safe_history_error(mapped_resource_id),
                    "result_resource_id": _safe_history_error(result_resource_id),
                    "timestamp": time.time(),
                },
            )
            self._save(data)

    def build_pending_prompt(self) -> CleanupPrompt | None:
        resources = self.pending_resources()
        if not resources:
            return None
        count = len(resources)
        lines = [
            _(
                "Cloud resources still need cleanup after pipeline rollback. "
                "Clean them up now and keep checking until deletion completes."
            ),
            "",
            _("Requirements:"),
            _("- Cleanup scope is a strict allowlist: delete only ids in the cleanup resources list below."),
            _("- Do not delete, modify, or roll back any stack or cloud resource outside the cleanup resources list."),
            _("- Do not call ListStacks or search for other stacks by name; cleanup resource ids are fully listed."),
            _(
                "- Before every GetStack/DeleteStack call, verify that StackId exactly matches an id in "
                "the cleanup resources list."
            ),
            _(
                "- If StackId is not in the cleanup resources list, do not call DeleteStack, even if it is "
                "the current handoff or newly created stack."
            ),
            _(
                "- Do not infer extra cleanup targets from pipeline handoff, deployment.stack_id, current stack, "
                "or resources_created; those may be final delivered resources."
            ),
            _("- Do not expand cleanup scope for user follow-ups, continue instructions, or pipeline handoff context."),
            _(
                "- When resuming cleanup, still process only resources listed in this prompt; "
                "do not inspect or delete others."
            ),
            _(
                "- Prefer available ROS stack tools for deletion; if using aliyun_api, call DeleteStack first, "
                "then repeatedly call GetStack to check status."
            ),
            _(
                "- If a resource is already deleting, call GetStack first, "
                "then decide whether DeleteStack is needed again."
            ),
            _(
                "- Cleanup is complete only after DELETE_COMPLETE; for DELETE_FAILED or unknown status, "
                "tell the user the failure reason and next step."
            ),
            _("- After all listed resources are DELETE_COMPLETE, stop this cleanup turn immediately."),
            _("- Briefly update the user during cleanup."),
            "",
            _("Cleanup resources:"),
        ]
        for index, resource in enumerate(resources, start=1):
            label = resource.resource_name or resource.resource_id
            lines.append(
                _(
                    "{index}. provider={provider}, type={resource_type}, id={resource_id}, name={name}, region={region}"
                ).format(
                    index=index,
                    provider=resource.provider,
                    resource_type=resource.resource_type,
                    resource_id=resource.resource_id,
                    name=label,
                    region=resource.region_id or "unknown",
                )
            )
        return CleanupPrompt(
            resources=resources,
            prompt="\n".join(lines),
            status_message=_("检测到 {count} 个回滚残留资源，开始清理流程。").format(count=count),
        )

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_ledger_data()
        try:
            loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            logger.warning("Failed to load cleanup ledger %s: %s", self.path, exc)
            return _failed_ledger_data(str(exc))
        if not isinstance(loaded, dict):
            logger.warning(
                "Failed to load cleanup ledger %s: expected mapping, got %s",
                self.path,
                type(loaded).__name__,
            )
            return _failed_ledger_data(f"expected mapping, got {type(loaded).__name__}")
        loaded.setdefault("schema_version", 1)
        loaded.setdefault("observed_resources", [])
        loaded.setdefault("cleanup_resources", [])
        loaded.setdefault("tool_uses", [])
        loaded.setdefault("history", [])
        return loaded

    def _load_for_write(self) -> dict[str, Any] | None:
        data = self._load()
        if not data.get(_LOAD_FAILED_KEY):
            return data
        return None

    def _save(self, data: dict[str, Any]) -> None:
        content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        atomic_write_text(self.path, content, durable=True)

    @staticmethod
    def _append_history(data: dict[str, Any], entry: dict[str, Any]) -> None:
        history = data.setdefault("history", [])
        if isinstance(history, list):
            history.append(entry)

    def _write_lock(self) -> threading.RLock:
        return _ledger_path_lock(self.path)


class CleanupObserver:
    def __init__(self, ledger: CleanupLedger) -> None:
        self._ledger = ledger
        self._tool_inputs: dict[str, dict[str, Any]] = {}

    def observe(self, event: Any) -> None:
        if isinstance(event, ToolUseEndEvent):
            self._observe_tool_use(event)
        elif isinstance(event, ToolResultEvent):
            self._observe_tool_result(event)
        elif isinstance(event, StackProgressEvent):
            self._observe_stack_progress(event)

    def _observe_tool_use(self, event: ToolUseEndEvent) -> None:
        self._tool_inputs[event.tool_use_id] = {"tool_name": event.name, "input": dict(event.input)}
        operation = _stack_operation_from_tool_input(event.name, event.input)
        if operation is None or operation["action"] not in {"DeleteStack", "GetStack"}:
            return
        stack_id = _stack_id_from_sources(operation["params"])
        if stack_id is None:
            return
        self._ledger.record_tool_use_mapping(
            tool_use_id=event.tool_use_id,
            provider=operation["provider"],
            resource_type="stack",
            resource_id=stack_id,
            region_id=operation["region_id"],
            action=operation["action"],
            tool_name=event.name,
            tool_input=event.input,
        )
        if operation["action"] != "DeleteStack":
            return
        self._ledger.update_resource(
            provider=operation["provider"],
            resource_type="stack",
            resource_id=stack_id,
            region_id=operation["region_id"],
            cleanup_status="started",
            cleanup_tool_use_id=event.tool_use_id,
            cleanup_action="DeleteStack",
            progress_status="DELETE_STARTED",
            clear_last_error=True,
        )

    def _observe_tool_result(self, event: ToolResultEvent) -> None:
        record = self._tool_inputs.get(event.tool_use_id)
        result = _json_object(event.result) or {}
        operation: dict[str, Any] | None = None
        stack_id: str | None = None
        result_stack_id = _stack_id_from_sources(result)
        if isinstance(record, dict):
            tool_name = str(record.get("tool_name") or event.tool_name)
            tool_input = record.get("input")
            if not isinstance(tool_input, dict):
                return
            operation = _stack_operation_from_tool_input(tool_name, tool_input)
            if operation is None or operation["action"] not in {"DeleteStack", "GetStack"}:
                return
            stack_id = _stack_id_from_sources(operation["params"])
            if stack_id is None:
                return
            if result_stack_id is not None and result_stack_id != stack_id:
                self._record_tool_result_mismatch(
                    tool_use_id=event.tool_use_id,
                    tool_name=tool_name,
                    mapped_resource_id=stack_id,
                    result_resource_id=result_stack_id,
                )
                return
        else:
            mapping = self._ledger.tool_use_mapping(event.tool_use_id)
            if mapping is None:
                if _is_cleanup_stack_tool_name(event.tool_name):
                    logger.warning(
                        "Unmatched cleanup tool result: tool_use_id=%s tool_name=%s",
                        event.tool_use_id,
                        event.tool_name,
                    )
                    self._ledger.record_tool_result_unmatched(
                        tool_use_id=event.tool_use_id,
                        tool_name=event.tool_name,
                    )
                return
            operation = _stack_operation_from_tool_mapping(mapping)
            if operation is None:
                return
            stack_id = operation["resource_id"]
            if result_stack_id is not None and result_stack_id != stack_id:
                self._record_tool_result_mismatch(
                    tool_use_id=event.tool_use_id,
                    tool_name=event.tool_name,
                    mapped_resource_id=stack_id,
                    result_resource_id=result_stack_id,
                )
                return
        if stack_id is None:
            return
        status = _status_from_result(result)
        if operation["action"] == "DeleteStack" and status is None and not event.is_error:
            self._ledger.update_resource(
                provider=operation["provider"],
                resource_type="stack",
                resource_id=stack_id,
                region_id=operation["region_id"],
                cleanup_status="in_progress",
                cleanup_tool_use_id=event.tool_use_id,
                cleanup_action="DeleteStack",
                progress_status="DELETE_REQUESTED",
                clear_last_error=True,
            )
            return
        cleanup_status = _cleanup_status_from_stack_status(status, event.is_error)
        self._ledger.update_resource(
            provider=operation["provider"],
            resource_type="stack",
            resource_id=stack_id,
            region_id=operation["region_id"],
            cleanup_status=cleanup_status,
            cleanup_tool_use_id=event.tool_use_id,
            cleanup_action=operation["action"],
            progress_status=status,
            last_error=event.result if cleanup_status == "failed" else None,
            clear_last_error=cleanup_status != "failed",
        )

    def _observe_stack_progress(self, event: StackProgressEvent) -> None:
        status = event.status
        self._ledger.update_resource(
            provider="ros",
            resource_type="stack",
            resource_id=event.stack_id,
            cleanup_status=_cleanup_status_from_stack_status(status, False),
            progress_status=status,
            progress_percentage=event.progress_percentage,
            last_error=status if status in _DELETE_FAILED_STATUSES else None,
            clear_last_error=status not in _DELETE_FAILED_STATUSES,
        )

    def _record_tool_result_mismatch(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        mapped_resource_id: str,
        result_resource_id: str,
    ) -> None:
        safe_tool_use_id = _safe_history_error(tool_use_id) or ""
        safe_tool_name = _safe_history_error(tool_name) or ""
        safe_mapped_resource_id = _safe_history_error(mapped_resource_id) or ""
        safe_result_resource_id = _safe_history_error(result_resource_id) or ""
        logger.warning(
            "Mismatched cleanup tool result: tool_use_id=%s tool_name=%s mapped_resource_id=%s result_resource_id=%s",
            safe_tool_use_id,
            safe_tool_name,
            safe_mapped_resource_id,
            safe_result_resource_id,
        )
        self._ledger.record_tool_result_mismatch(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            mapped_resource_id=mapped_resource_id,
            result_resource_id=result_resource_id,
        )


def create_cleanup_prompt_message(
    prompt: str,
    *,
    cleanup_ledger_path: str | Path | None = None,
    cleanup_status: str | None = None,
) -> Message:
    metadata = {"type": CLEANUP_PROMPT_METADATA_TYPE, "source": "pipeline_cleanup"}
    if cleanup_ledger_path is not None:
        metadata["cleanupLedgerPath"] = str(cleanup_ledger_path)
    if cleanup_status is not None:
        metadata["cleanupStatus"] = cleanup_status
    return Message(role="user", content=prompt, metadata=metadata)


def is_cleanup_prompt_message(message: Message) -> bool:
    return message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE


def cleanup_prompt_ledger_path(message: Message) -> str | None:
    if not is_cleanup_prompt_message(message):
        return None
    value = message.metadata.get("cleanupLedgerPath") or message.metadata.get("cleanup_ledger_path")
    return value if isinstance(value, str) and value else None


def is_active_cleanup_prompt_message(message: Message) -> bool:
    if not is_cleanup_prompt_message(message):
        return False
    status = message.metadata.get("cleanupStatus") or message.metadata.get("cleanup_status")
    return status not in {"completed", "skipped"}


def mark_cleanup_prompt_message_completed(message: Message, *, cleanup_ledger_path: str | Path | None = None) -> bool:
    if not is_cleanup_prompt_message(message):
        return False
    if cleanup_ledger_path is not None:
        existing_path = cleanup_prompt_ledger_path(message)
        if existing_path is not None and existing_path != str(cleanup_ledger_path):
            return False
    if message.metadata.get("cleanupStatus") == "completed":
        return False
    message.metadata = {**message.metadata, "cleanupStatus": "completed"}
    return True


def _ledger_path_lock(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LEDGER_LOCKS_LOCK:
        lock = _LEDGER_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _LEDGER_LOCKS[resolved] = lock
        return lock


def _merge_cleanup_required(
    existing: CleanupResource | None,
    incoming: CleanupResource,
    *,
    source_step_id: str,
    reason: str,
    now: float,
) -> CleanupResource:
    if existing is None:
        return replace(
            incoming,
            cleanup_required=True,
            cleanup_reason=incoming.cleanup_reason or reason,
            source_step_id=incoming.source_step_id or source_step_id,
            updated_at=now,
        )
    if existing.cleanup_status in _TERMINAL_CLEANUP_STATUSES:
        return existing
    cleanup_status = incoming.cleanup_status
    if existing.cleanup_status in _ACTIVE_CLEANUP_STATUSES or existing.cleanup_status == "failed":
        cleanup_status = existing.cleanup_status
    return replace(
        incoming,
        cleanup_required=True,
        cleanup_reason=incoming.cleanup_reason or existing.cleanup_reason or reason,
        source_step_id=incoming.source_step_id or existing.source_step_id or source_step_id,
        source_attempt_id=incoming.source_attempt_id or existing.source_attempt_id,
        cleanup_status=cleanup_status,
        cleanup_tool_use_id=existing.cleanup_tool_use_id,
        cleanup_action=existing.cleanup_action,
        progress_status=existing.progress_status,
        progress_percentage=existing.progress_percentage,
        last_error=existing.last_error,
        observed_at=existing.observed_at or incoming.observed_at,
        updated_at=now,
    )


def _resource_key(provider: str, resource_type: str, resource_id: str, region_id: str) -> str:
    return "|".join([provider.strip().lower(), resource_type.strip().lower(), region_id.strip(), resource_id.strip()])


def _matches_resource(
    resource: CleanupResource,
    provider: str,
    resource_type: str,
    resource_id: str,
    region_id: str | None,
) -> bool:
    if resource.provider.lower() != provider.lower():
        return False
    if resource.resource_type.lower() != resource_type.lower():
        return False
    if resource.resource_id != resource_id:
        return False
    return not region_id or not resource.region_id or resource.region_id == region_id


def _stack_operation_from_tool_input(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
    params = _dict_value(tool_input.get("params") or tool_input.get("parameters"))
    normalized_tool_name = tool_name.lower()
    if normalized_tool_name == "ros_stack":
        action = _first_string(tool_input, ("action", "Action"))
    elif normalized_tool_name == "aliyun_api":
        product = _first_string(tool_input, ("product", "Product", "service", "Service"))
        if product is None or product.lower() != "ros":
            return None
        action = _first_string(tool_input, ("action", "Action"))
    else:
        return None
    if action not in {"CreateStack", "UpdateStack", "ContinueCreateStack", "DeleteStack", "GetStack"}:
        return None
    return {
        "provider": "ros",
        "action": action,
        "params": params,
        "region_id": _first_string(tool_input, ("region_id", "regionId", "RegionId"))
        or _first_string(params, ("region_id", "regionId", "RegionId"))
        or "",
    }


def _stack_operation_from_tool_mapping(mapping: dict[str, Any]) -> dict[str, Any] | None:
    action = mapping.get("action")
    if action not in {"DeleteStack", "GetStack"}:
        return None
    resource_id = _optional_str(mapping.get("resource_id"))
    if resource_id is None:
        return None
    return {
        "provider": str(mapping.get("provider") or "ros"),
        "resource_type": str(mapping.get("resource_type") or "stack"),
        "resource_id": resource_id,
        "action": action,
        "region_id": str(mapping.get("region_id") or ""),
    }


def _cleanup_tool_input_summary(
    tool_input: dict[str, Any],
    *,
    resource_id: str,
    region_id: str,
) -> dict[str, Any]:
    params = _dict_value(tool_input.get("params") or tool_input.get("parameters"))
    return {
        "action": _first_string(tool_input, ("action", "Action")),
        "product": _first_string(tool_input, ("product", "Product", "service", "Service")),
        "region_id": region_id,
        "stack_id": resource_id,
        "param_keys": sorted(str(key) for key in params),
    }


def _is_cleanup_stack_tool_name(tool_name: str) -> bool:
    return tool_name.lower() in {"ros_stack", "aliyun_api"}


def _cleanup_status_from_stack_status(status: str | None, is_error: bool) -> CleanupStatus:
    if status in _DELETE_COMPLETE_STATUSES:
        return "completed"
    if status in _DELETE_FAILED_STATUSES or is_error:
        return "failed"
    return "in_progress"


def _cleanup_lifecycle_state(resource: CleanupResource) -> tuple[Any, ...]:
    return (
        resource.cleanup_status,
        resource.cleanup_tool_use_id,
        resource.cleanup_action,
        resource.progress_status,
        resource.progress_percentage,
        resource.last_error,
    )


def _cleanup_lifecycle_history_entry(resource: CleanupResource) -> dict[str, Any]:
    event_type = {
        "started": "cleanup_started",
        "in_progress": "cleanup_progress",
        "completed": "cleanup_completed",
        "failed": "cleanup_failed",
        "skipped": "cleanup_skipped",
        "pending": "cleanup_pending",
    }.get(resource.cleanup_status, "cleanup_progress")
    entry = {
        "type": event_type,
        "resource": _cleanup_resource_history_data(resource),
        "cleanup_status": resource.cleanup_status,
        "cleanup_tool_use_id": resource.cleanup_tool_use_id,
        "cleanup_action": resource.cleanup_action,
        "progress_status": resource.progress_status,
        "progress_percentage": resource.progress_percentage,
        "last_error": _safe_history_error(resource.last_error),
        "timestamp": resource.updated_at or time.time(),
    }
    return {key: value for key, value in entry.items() if value is not None}


def _cleanup_resource_history_data(resource: CleanupResource) -> dict[str, Any]:
    return {
        "provider": resource.provider,
        "resource_type": resource.resource_type,
        "resource_id": resource.resource_id,
        "resource_name": resource.resource_name,
        "region_id": resource.region_id,
        "source_step_id": resource.source_step_id,
        "source_attempt_id": resource.source_attempt_id,
        "cleanup_status": resource.cleanup_status,
        "progress_status": resource.progress_status,
    }


def _safe_history_error(value: str | None) -> str | None:
    if not value:
        return None
    text = sanitize_public_text(value)
    return text[:1000] + "..." if len(text) > 1000 else text


def _status_from_result(result: dict[str, Any]) -> str | None:
    nested = _dict_value(result.get("Stack") or result.get("stack"))
    return _first_string(
        result,
        ("StackStatus", "stackStatus", "stack_status", "Status", "status"),
    ) or _first_string(nested, ("StackStatus", "stackStatus", "stack_status", "Status", "status"))


def _stack_id_from_sources(*sources: dict[str, Any]) -> str | None:
    for source in sources:
        stack_id = _first_string(source, ("StackId", "stackId", "stack_id"))
        if stack_id:
            return stack_id
        nested = _dict_value(source.get("Stack") or source.get("stack"))
        stack_id = _first_string(nested, ("StackId", "stackId", "stack_id"))
        if stack_id:
            return stack_id
    return None


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _empty_ledger_data() -> dict[str, Any]:
    return {"schema_version": 1, "observed_resources": [], "cleanup_resources": [], "tool_uses": [], "history": []}


def _failed_ledger_data(reason: str) -> dict[str, Any]:
    data = _empty_ledger_data()
    data[_LOAD_FAILED_KEY] = True
    data[_LOAD_ERROR_KEY] = reason
    return data


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _float_value(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
