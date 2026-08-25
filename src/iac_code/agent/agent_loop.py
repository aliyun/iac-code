"""Agent Loop - the core execution loop using ProviderManager and concurrent tools."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from iac_code.agent.message import (
    ContentBlock,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from iac_code.i18n import _
from iac_code.services.context_manager import ContextManager
from iac_code.services.permission_wait import canonical_digest, permission_execution_identity
from iac_code.services.permissions.audit import (
    PermissionAuditRecord,
    build_input_summary,
    emit_permission_audit,
    is_routine_read_only_allow,
    permission_audit_operation,
    redacted_tool_input_for_settings,
)
from iac_code.services.session_usage import SessionUsageStore, SessionUsageTotals
from iac_code.services.telemetry.names import IacCodeAttr
from iac_code.services.telemetry.scope import normalize_span_attributes, use_span_attributes
from iac_code.tools.base import ToolContext, ToolRegistry, ToolResult
from iac_code.tools.cloud.aliyun.contract_store import (
    PROCESS_RESOLVED_CONTRACT_STORE,
    canonical_input_sha256,
)
from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY, ResultStorage
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor
from iac_code.types.permissions import (
    MAX_PERMISSION_AUDIT_ITEMS,
    InvocationBinding,
    PermissionAuditMetadata,
    PermissionAuditSettings,
    PermissionResult,
    ToolPermissionContext,
)
from iac_code.types.stream_events import (
    TOOL_RENDER_DISPLAY_NAME_KEY,
    TOOL_RENDER_METADATA_KEY,
    TOOL_RENDER_RESULT_COMPACT_KEY,
    TOOL_RENDER_RESULT_VERBOSE_KEY,
    TOOL_RENDER_VERBOSE_RESULT_IN_TRANSCRIPT_KEY,
    CompactionEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    PermissionWaitOutcome,
    PermissionWaitSuspended,
    QueuedInputSubmittedEvent,
    StreamEvent,
    SubAgentToolEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolEmittedEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)
from iac_code.utils.public_paths import build_public_path_roots


@dataclass
class CompactResult:
    """Outcome of a manual /compact invocation.

    ``status`` distinguishes between meaningful no-ops ("empty",
    "too_short") and real failures so the UI can show an accurate message
    instead of lumping them together.
    """

    status: Literal["success", "empty", "too_short", "failed"]
    original_tokens: int = 0
    compacted_tokens: int = 0
    preserve_recent_turns: int = 0


@dataclass
class _PendingInjection:
    content: str | list[ContentBlock]
    metadata: dict[str, Any]


def _user_input_to_text(user_input: str | list[ContentBlock]) -> str:
    if isinstance(user_input, str):
        return user_input
    parts: list[str] = []
    for block in user_input:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return " ".join(part for part in parts if part)


def _normalize_memory_filename(filename: Any) -> str:
    name = str(filename).strip()
    if not name:
        return ""
    if not name.endswith(".md"):
        name = f"{name}.md"
    return name


def _extend_unique(target: list[str], values: list[str]) -> None:
    seen = set(target)
    for value in values:
        if value not in seen:
            target.append(value)
            seen.add(value)


def _with_tool_read_directories(
    permission_context: Any,
    *,
    trusted_directories: list[str],
    relative_directories: list[str],
) -> Any:
    if not trusted_directories and not relative_directories:
        return permission_context

    trusted_read_directories = list(getattr(permission_context, "trusted_read_directories", []))
    relative_read_directories = list(getattr(permission_context, "relative_read_directories", []))
    original_trusted_count = len(trusted_read_directories)
    original_relative_count = len(relative_read_directories)
    _extend_unique(trusted_read_directories, trusted_directories)
    _extend_unique(relative_read_directories, relative_directories)
    if (
        len(trusted_read_directories) == original_trusted_count
        and len(relative_read_directories) == original_relative_count
    ):
        return permission_context
    return replace(
        permission_context,
        trusted_read_directories=trusted_read_directories,
        relative_read_directories=relative_read_directories,
    )


def _emit_no_prompt_permission_audit(
    *,
    session_id: str,
    cwd: str,
    request: ToolCallRequest,
    permission: PermissionResult,
    decision: Literal["allow", "deny"],
    settings: PermissionAuditSettings | None,
    audit_log_path: str | None = None,
) -> bool:
    return _emit_permission_audit_items(
        session_id=session_id,
        cwd=cwd,
        request=request,
        audits=_permission_audits(permission, include_primary=True),
        decision=decision,
        settings=settings,
        audit_log_path=audit_log_path,
    )


def _permission_audits(
    permission: PermissionResult,
    *,
    include_primary: bool,
) -> tuple[PermissionAuditMetadata, ...]:
    audits: list[PermissionAuditMetadata] = []
    if include_primary and permission.audit is not None:
        audits.append(permission.audit)
    for audit in permission.audit_items[:MAX_PERMISSION_AUDIT_ITEMS]:
        if audit == permission.audit or audit in audits:
            continue
        audits.append(audit)
    return tuple(audits)


def _emit_permission_audit_items(
    *,
    session_id: str,
    cwd: str,
    request: ToolCallRequest,
    audits: tuple[PermissionAuditMetadata, ...],
    decision: Literal["allow", "deny"],
    settings: PermissionAuditSettings | None,
    audit_log_path: str | None = None,
) -> bool:
    input_summary = build_input_summary(request.name, request.input)
    redacted_input = redacted_tool_input_for_settings(request.input, settings)
    all_written = True
    for audit in audits:
        if is_routine_read_only_allow(decision, audit):
            continue

        result = emit_permission_audit(
            PermissionAuditRecord(
                session_id=session_id,
                cwd=cwd,
                tool_name=request.name,
                tool_use_id=request.id,
                decision=decision,
                scope=audit.scope,
                source=audit.source,
                rule_source=audit.rule_source,
                rule=audit.rule,
                reason_type=audit.reason_type,
                reason_detail=audit.reason_detail,
                operation=permission_audit_operation(audit),
                input_summary=input_summary,
                tool_input_redacted=redacted_input,
                audit_log_path=audit_log_path,
            ),
            settings=settings,
        )
        all_written = result is not False and all_written
    return all_written


def _with_prompt_permission_metadata(tool: Any, tool_input: dict, permission: PermissionResult) -> PermissionResult:
    operation = tool.permission_audit_operation(tool_input)
    if permission.audit is not None:
        if not operation:
            return permission
        merged_operation = {**permission.audit.operation, **operation}
        if merged_operation == permission.audit.operation:
            return permission
        return replace(permission, audit=replace(permission.audit, operation=merged_operation))
    if permission.behavior != "ask":
        return permission
    reason_type = permission.reason.type if permission.reason is not None else "prompt_required"
    reason_detail = permission.reason.detail if permission.reason is not None else "prompt_required"
    return replace(
        permission,
        audit=PermissionAuditMetadata(
            scope="once",
            source="permission_pipeline",
            reason_type=reason_type,
            reason_detail=reason_detail,
            is_read_only=tool.is_read_only(tool_input),
            operation=operation,
        ),
    )


def _filter_recalled_memory_content(content: str, selected_files: list[str]) -> str:
    keep = [_normalize_memory_filename(filename) for filename in selected_files]
    keep = [filename for filename in keep if filename]
    if not keep:
        return ""

    lines = content.splitlines()
    sections: dict[str, list[str]] = {}
    current_filename = ""
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_filename:
                sections[current_filename] = current_lines
            current_filename = _normalize_memory_filename(line[3:].strip())
            current_lines = [line]
            continue
        if current_filename:
            current_lines.append(line)
    if current_filename:
        sections[current_filename] = current_lines

    kept_sections = [sections[filename] for filename in keep if filename in sections]
    if len(kept_sections) != len(keep):
        return ""

    parts = ["# Recalled Memory"]
    for section in kept_sections:
        parts.append("\n".join(section).strip())
    return "\n\n".join(part for part in parts if part)


def _is_first_output_delta(event: Any) -> bool:
    return isinstance(event, (TextDeltaEvent, ThinkingDeltaEvent)) and bool(event.text)


class AgentLoop:
    """The main agent execution loop.

    Uses ProviderManager for LLM calls, ToolExecutor for concurrent tool execution,
    and yields fine-grained StreamEvents for the UI layer.
    """

    def __init__(
        self,
        provider_manager: Any,  # ProviderManager (avoid circular import)
        system_prompt: str,
        tool_registry: ToolRegistry,
        max_turns: int = 100,
        session_storage: Any = None,  # SessionStorage
        session_usage_store: SessionUsageStore | None = None,
        session_id: str | None = None,
        resume_messages: list | None = None,
        cwd: str | None = None,
        permission_context: Any = None,  # ToolPermissionContext
        permission_context_getter: Any = None,  # Callable[[], ToolPermissionContext | None]
        auto_trigger_skills: list[Any] | None = None,
        memory_recall_service: Any = None,
        system_prompt_refresher: Callable[[], str] | None = None,
        background_task_starter: Callable[[], Any] | None = None,
        pause_event: asyncio.Event | None = None,
        tool_context_trusted_read_directories: list[str] | None = None,
        tool_context_relative_read_directories: list[str] | None = None,
        pipeline_mode: bool = False,
        tool_context_env_overrides: dict[str, str] | None = None,
        root_session_id: str | None = None,
        transcript_id: str | None = None,
        result_storage_dir: str | Path | None = None,
        audit_log_path: str | Path | None = None,
        telemetry_attributes: dict[str, Any] | None = None,
    ) -> None:
        self._provider_manager = provider_manager
        self.system_prompt = system_prompt
        self.tool_registry = tool_registry
        self._max_turns = max_turns
        self._session_storage = session_storage
        self._session_id = session_id or str(uuid.uuid4())[:8]
        self._runtime_nonce = uuid.uuid4().hex
        self._owned_contract_snapshot_ids: set[str] = set()
        self._root_session_id = root_session_id or self._session_id
        self._transcript_id = transcript_id
        self._has_session_hierarchy = root_session_id is not None or transcript_id is not None
        self._audit_log_path = str(audit_log_path) if audit_log_path is not None else None
        self._cwd = cwd or os.getcwd()
        self._session_usage_store = session_usage_store or SessionUsageStore()
        self._session_usage_totals = self._session_usage_store.load(self._cwd, self._session_id)
        self._permission_context = permission_context
        self._permission_context_getter = permission_context_getter
        self._tool_context_trusted_read_directories = list(tool_context_trusted_read_directories or [])
        self._tool_context_relative_read_directories = list(tool_context_relative_read_directories or [])
        self._pipeline_mode = pipeline_mode
        scope_attributes: dict[str, Any] = {
            IacCodeAttr.MODE: "pipeline" if pipeline_mode else "normal",
        }
        scope_attributes.update(telemetry_attributes or {})
        self._telemetry_attributes = normalize_span_attributes(scope_attributes)
        self._tool_context_env_overrides = dict(tool_context_env_overrides or {})
        self._auto_trigger_skills = auto_trigger_skills or []
        self._auto_loaded_skills: set[str] = set()
        self._current_git_branch: str | None = None
        self._memory_recall_service = memory_recall_service
        self._background_task_starter = background_task_starter
        self._recorded_memory_prefetch_ids: set[int] = set()
        self._pending_memory_prefetches: list[Any] = []
        self._memory_recall_generation = 0
        self._memory_recall_active_turns = 0
        self._last_provider_request_snapshot: dict[str, Any] | None = None
        self._system_prompt_refresher = system_prompt_refresher

        model_name = ""
        if hasattr(provider_manager, "get_model_name"):
            model_name = provider_manager.get_model_name()

        self.context_manager = ContextManager(system_prompt=system_prompt, model=model_name)
        self._sync_tool_definitions()
        if resume_messages:
            self.context_manager.load_messages(resume_messages)
        self._sync_recall_suppression_from_context()
        self._tool_executor = ToolExecutor(registry=tool_registry)
        from iac_code.config import get_config_dir

        storage_dir = (
            str(result_storage_dir)
            if result_storage_dir is not None
            else os.path.join(str(get_config_dir()), "tool-results", self._session_id)
        )
        self._result_storage = ResultStorage(storage_dir=storage_dir)
        self._pending_injections: deque[str | list[ContentBlock] | _PendingInjection] = deque()
        self._current_turn_text: str = ""
        self._accepting_injected_user_messages = False
        self._pause_event = pause_event

    @property
    def current_turn_text(self) -> str:
        return self._current_turn_text

    def _cancel_owned_contract_snapshots(self) -> None:
        snapshot_ids = tuple(self._owned_contract_snapshot_ids)
        self._owned_contract_snapshot_ids.clear()
        for snapshot_id in snapshot_ids:
            if PROCESS_RESOLVED_CONTRACT_STORE.is_pending(snapshot_id):
                PROCESS_RESOLVED_CONTRACT_STORE.cancel(snapshot_id)

    def _reject_owned_contract_snapshot(self, snapshot_id: str | None) -> None:
        if snapshot_id is None:
            return
        if PROCESS_RESOLVED_CONTRACT_STORE.is_pending(snapshot_id):
            PROCESS_RESOLVED_CONTRACT_STORE.reject(snapshot_id)
        self._owned_contract_snapshot_ids.discard(snapshot_id)

    def inject_user_message(
        self,
        msg: str | list[ContentBlock],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Schedule a user message to be injected before the next LLM turn."""
        pending: str | list[ContentBlock] | _PendingInjection = msg
        if metadata is not None:
            pending = _PendingInjection(content=msg, metadata=dict(metadata))
        self._pending_injections.append(pending)

    @property
    def can_accept_injected_user_message(self) -> bool:
        """Whether a queued supplement can still be consumed by this run."""
        return self._accepting_injected_user_messages

    def try_inject_user_message(
        self,
        msg: str | list[ContentBlock],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Queue a supplement only when this loop still has a consumable turn."""
        if not self.can_accept_injected_user_message:
            return False
        self.inject_user_message(msg, metadata=metadata)
        return True

    def _drain_pending_injections(self) -> None:
        while self._pending_injections:
            injected = self._pending_injections.popleft()
            metadata: dict[str, Any] = {}
            if isinstance(injected, _PendingInjection):
                metadata = injected.metadata
                injected = injected.content
            message = self.context_manager.add_user_message(injected)
            message.metadata.update(metadata)
            if self._session_storage:
                self._session_storage.append(
                    self._cwd,
                    self._session_id,
                    message,
                    git_branch=self._current_git_branch,
                )

    def set_provider(self, provider_manager: Any, system_prompt: str | None = None) -> None:
        """Swap the provider manager in place, preserving conversation history.

        Updates the tokenizer/context-window config when the model name changes.
        Optionally refreshes the system prompt — useful when memory or skill
        listing has changed since the loop was constructed.
        """
        self._provider_manager = provider_manager
        new_model = provider_manager.get_model_name() if hasattr(provider_manager, "get_model_name") else ""
        self.context_manager.set_model(new_model)
        if system_prompt is not None:
            self.system_prompt = system_prompt
            self.context_manager.set_system_prompt(system_prompt)
        self._sync_tool_definitions(system_prompt=self.system_prompt if system_prompt is not None else None)

    def set_auto_trigger_skills(self, skill_commands: list[Any] | None) -> None:
        """Refresh skills considered for automatic trigger injection."""
        self._auto_trigger_skills = list(skill_commands or [])

    def get_memory_recall_stats(self) -> dict[str, Any]:
        if self._memory_recall_service is None:
            return {
                "total_side_queries": 0,
                "in_flight_side_queries": 0,
                "successful_side_queries": 0,
                "failed_side_queries": 0,
                "cancelled_side_queries": 0,
                "total_selected_files": 0,
                "last_duration_ms": 0,
                "last_status": "skipped",
                "last_selected_files": [],
                "last_side_query_duration_ms": 0,
                "last_side_query_status": "skipped",
                "last_side_query_selected_files": [],
                "last_prompt_preview": "",
                "last_response_preview": "",
                "last_prompt_chars": 0,
                "last_response_chars": 0,
                "total_usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": 0,
                    "recorded_events": 0,
                    "has_recorded_usage": False,
                },
                "last_usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": 0,
                    "recorded_events": 0,
                    "has_recorded_usage": False,
                },
            }
        get_snapshot = getattr(self._memory_recall_service, "get_stats_snapshot", None)
        if not callable(get_snapshot):
            return {}
        return dict(get_snapshot())

    def get_last_provider_request_snapshot(self) -> dict[str, Any]:
        if self._last_provider_request_snapshot is None:
            return {}
        return {
            "system_prompt": self._last_provider_request_snapshot.get("system_prompt", ""),
            "provider_messages": list(self._last_provider_request_snapshot.get("provider_messages") or []),
            "tools": list(self._last_provider_request_snapshot.get("tools") or []),
        }

    def _start_memory_prefetch_for_turn(self, user_input: str | list[ContentBlock]) -> Any:
        if self._memory_recall_service is None:
            return None
        query = _user_input_to_text(user_input).strip()
        if not query:
            return None
        self._sync_recall_suppression_from_context()
        start_prefetch = getattr(self._memory_recall_service, "start_prefetch", None)
        if callable(start_prefetch):
            prefetch = start_prefetch(query)
        else:
            recall = getattr(self._memory_recall_service, "recall", None)
            if not callable(recall):
                return None
            from iac_code.memory.recall import MemoryRecallPrefetch

            prefetch = MemoryRecallPrefetch(asyncio.create_task(recall(query)))
        if prefetch is not None:
            self._pending_memory_prefetches.append(prefetch)
            add_done_callback = getattr(prefetch, "add_done_callback", None)
            if callable(add_done_callback):
                session_id = self._session_id
                generation = self._memory_recall_generation
                add_done_callback(
                    lambda task, handle=prefetch, sid=session_id, gen=generation: self._handle_memory_prefetch_done(
                        handle,
                        task,
                        session_id=sid,
                        generation=gen,
                    )
                )
        return prefetch

    def _cancel_pending_memory_prefetches(self) -> None:
        for prefetch in list(self._pending_memory_prefetches):
            done = getattr(prefetch, "done", None)
            cancel = getattr(prefetch, "cancel", None)
            if callable(done) and callable(cancel) and not done():
                cancel()
        self._pending_memory_prefetches.clear()
        self._recorded_memory_prefetch_ids.clear()

    def _discard_memory_prefetch_for_turn(self, prefetch: Any | None) -> None:
        if prefetch is None:
            return
        if not any(pending is prefetch for pending in self._pending_memory_prefetches):
            return
        self._pending_memory_prefetches = [
            pending for pending in self._pending_memory_prefetches if pending is not prefetch
        ]
        done = getattr(prefetch, "done", None)
        if callable(done) and done():
            try:
                result = prefetch.result()
            except asyncio.CancelledError:
                self._forget_memory_prefetch(prefetch)
                return
            except Exception as exc:
                logger.debug("Memory recall prefetch usage unavailable: {}", exc)
                self._forget_memory_prefetch(prefetch)
                return
            self._record_memory_recall_result_usage_once(prefetch, result)
            self._forget_memory_prefetch(prefetch)
            return
        cancel = getattr(prefetch, "cancel", None)
        if callable(cancel):
            cancel()
        self._forget_memory_prefetch(prefetch)

    def _sync_recall_suppression_from_context(self) -> None:
        if self._memory_recall_service is None:
            return
        mark_files_surfaced = getattr(self._memory_recall_service, "mark_files_surfaced", None)
        if callable(mark_files_surfaced):
            mark_files_surfaced(self.context_manager.get_surfaced_memory_files())

    def _persist_context_messages(self) -> None:
        if not self._session_storage:
            return
        self._session_storage.save(
            self._cwd,
            self._session_id,
            self.context_manager.get_messages(),
            git_branch=self._current_git_branch,
            preserve_cleanup_prompts=True,
        )

    def _inject_recalled_memory_result(self, result: Any) -> bool:
        content = str(getattr(result, "content", "") or "").strip()
        selected_files = list(getattr(result, "selected_files", None) or [])
        if not content or not selected_files:
            return False
        selected_names = {_normalize_memory_filename(filename) for filename in selected_files}
        selected_names.discard("")
        if not selected_names:
            return False
        suppressed: set[str] = set()
        get_suppressed_files = getattr(self._memory_recall_service, "get_suppressed_files", None)
        if callable(get_suppressed_files):
            suppressed = {_normalize_memory_filename(filename) for filename in get_suppressed_files()}
            suppressed.discard("")
        surfaced = {
            _normalize_memory_filename(filename) for filename in self.context_manager.get_surfaced_memory_files()
        }
        surfaced.discard("")
        suppressed |= surfaced
        injectable_files = [
            filename
            for filename in selected_files
            if (normalized := _normalize_memory_filename(filename)) and normalized not in suppressed
        ]
        if not injectable_files:
            return False
        if len(injectable_files) != len(selected_files):
            content = _filter_recalled_memory_content(content, injectable_files)
            if not content:
                return False
        msg = self.context_manager.add_recalled_memory_message(content, injectable_files)
        if self._session_storage:
            self._session_storage.append(
                self._cwd,
                self._session_id,
                msg,
                git_branch=self._current_git_branch,
            )
        self._mark_recalled_files_surfaced(injectable_files)
        return True

    async def _consume_ready_memory_prefetches(self, prefetch: Any | None = None) -> None:
        await asyncio.sleep(0)
        for item in list(self._pending_memory_prefetches):
            if prefetch is not None and item is not prefetch:
                continue
            done = getattr(item, "done", None)
            if not callable(done) or not done():
                continue
            self._pending_memory_prefetches = [
                pending for pending in self._pending_memory_prefetches if pending is not item
            ]
            try:
                result = item.result()
            except asyncio.CancelledError:
                self._forget_memory_prefetch(item)
                continue
            except Exception as exc:
                logger.debug("Memory recall prefetch failed: {}", exc)
                self._forget_memory_prefetch(item)
                continue
            self._record_memory_recall_result_usage_once(item, result)
            self._inject_recalled_memory_result(result)
            self._forget_memory_prefetch(item)

    def _mark_recalled_files_surfaced(self, selected_files: list[str]) -> None:
        if self._memory_recall_service is None:
            return
        mark_files_surfaced = getattr(self._memory_recall_service, "mark_files_surfaced", None)
        if not callable(mark_files_surfaced):
            return
        if selected_files:
            mark_files_surfaced(selected_files)

    def _handle_memory_prefetch_done(
        self,
        prefetch: Any,
        task: asyncio.Task,
        *,
        session_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        if not any(item is prefetch for item in self._pending_memory_prefetches):
            return
        if session_id is not None and session_id != self._session_id:
            self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
            self._forget_memory_prefetch(prefetch)
            return
        if generation is not None and generation != self._memory_recall_generation:
            self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
            self._forget_memory_prefetch(prefetch)
            return
        try:
            result = task.result()
        except asyncio.CancelledError:
            self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
            self._forget_memory_prefetch(prefetch)
            return
        except Exception as exc:
            logger.debug("Memory recall prefetch usage unavailable: {}", exc)
            self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
            self._forget_memory_prefetch(prefetch)
            return
        self._record_memory_recall_result_usage_once(prefetch, result)
        if self._memory_recall_active_turns > 0:
            return
        self._pending_memory_prefetches = [item for item in self._pending_memory_prefetches if item is not prefetch]
        self._forget_memory_prefetch(prefetch)

    def _record_memory_recall_result_usage_once(self, prefetch: Any, result: Any) -> None:
        prefetch_id = id(prefetch)
        if prefetch_id in self._recorded_memory_prefetch_ids:
            return
        self._recorded_memory_prefetch_ids.add(prefetch_id)
        self._record_response_usage(result)

    def _forget_memory_prefetch(self, prefetch: Any) -> None:
        self._recorded_memory_prefetch_ids.discard(id(prefetch))

    def _refresh_system_prompt(self) -> None:
        if self._system_prompt_refresher is None:
            return
        try:
            system_prompt = self._system_prompt_refresher()
        except Exception as exc:
            logger.debug("Failed to refresh system prompt: {}", exc)
            return
        if not isinstance(system_prompt, str) or system_prompt == self.system_prompt:
            return
        self.system_prompt = system_prompt
        self.context_manager.set_system_prompt(system_prompt)

    async def _start_background_tasks(self) -> None:
        if self._background_task_starter is None:
            return
        try:
            result = self._background_task_starter()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.debug("Failed to start background tasks: {}", exc)

    def _sync_tool_system_prompt(self, system_prompt: str, tools: list[Any] | None = None) -> None:
        if tools is None:
            try:
                tools = list(self.tool_registry.list_tools())
            except Exception as exc:
                logger.debug("Failed to list tools while syncing system prompt: {}", exc)
                return
        for tool in tools:
            setter = getattr(tool, "set_system_prompt", None)
            if not callable(setter):
                continue
            try:
                setter(system_prompt)
            except Exception as exc:
                logger.debug("Failed to sync system prompt to tool {}: {}", getattr(tool, "name", ""), exc)

    def _system_prompt_for_current_turn(self) -> str:
        return self.system_prompt

    def _prepare_provider_system_prompt(self) -> str:
        self._refresh_system_prompt()
        system_prompt = self._system_prompt_for_current_turn()
        self.context_manager.set_system_prompt(system_prompt)
        return system_prompt

    def _get_tool_definitions(self, tools: list[Any] | None = None):
        """Convert tool registry to provider ToolDefinition format."""
        from iac_code.providers.base import ToolDefinition

        if tools is None:
            tools = list(self.tool_registry.list_tools())
        tool_definitions = []
        for tool in tools:
            tool_definitions.append(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
            )
        return tool_definitions

    def _sync_tool_definitions(self, system_prompt: str | None = None):
        """Refresh context token accounting from the current tool registry."""
        tools = list(self.tool_registry.list_tools())
        if system_prompt is not None:
            self._sync_tool_system_prompt(system_prompt, tools=tools)
        tool_definitions = self._get_tool_definitions(tools)
        self.context_manager.set_tool_definitions(tool_definitions)
        return tool_definitions

    @staticmethod
    def _provider_message_from_api(api_message: dict[str, Any]):
        from iac_code.providers.base import ContentBlock
        from iac_code.providers.base import Message as ProviderMessage

        role = api_message["role"]
        content = api_message["content"]
        if isinstance(content, str):
            return ProviderMessage(role=role, content=content)
        if isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type", "text")
                    text_value = block.get("thinking") if block_type == "thinking" else block.get("text")
                    blocks.append(
                        ContentBlock(
                            type=block_type,
                            text=text_value,
                            tool_use_id=block.get("tool_use_id") or block.get("id"),
                            name=block.get("name"),
                            input=block.get("input"),
                            content=block.get("content"),
                            is_error=block.get("is_error", False),
                            media_type=block.get("media_type"),
                            data=block.get("data"),
                            provider_metadata=(
                                dict(block["provider_metadata"])
                                if isinstance(block.get("provider_metadata"), dict)
                                else {}
                            ),
                        )
                    )
            return ProviderMessage(role=role, content=blocks)
        return None

    def _get_provider_messages(self):
        """Convert context manager messages to provider Message format."""
        provider_messages = []
        for message in self.context_manager.get_context_messages():
            provider_message = self._provider_message_from_api(message.to_api_format())
            if provider_message is not None:
                provider_messages.append(provider_message)
        return provider_messages

    def _get_provider_messages_with_telemetry(self):
        """Build provider wire messages and their non-wire telemetry sidecar together."""
        from collections import defaultdict, deque

        from iac_code.services.telemetry.content_serializer import TelemetryInputBlock, TelemetryInputMessage
        from iac_code.tools.cloud.aliyun.result_contract import (
            ALIYUN_HTTP_METADATA_KEY,
            sanitize_aliyun_http_metadata,
        )

        provider_messages = []
        unmatched: dict[str, deque[str]] = defaultdict(deque)
        telemetry_messages: list[TelemetryInputMessage] = []
        for message in self.context_manager.get_context_messages():
            provider_message = self._provider_message_from_api(message.to_api_format())
            if provider_message is not None:
                provider_messages.append(provider_message)
            if isinstance(message.content, str):
                telemetry_messages.append(TelemetryInputMessage(role=message.role, content=message.content))
                continue
            blocks: list[TelemetryInputBlock] = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    blocks.append(TelemetryInputBlock(type="text", text=block.text))
                elif isinstance(block, ToolUseBlock):
                    unmatched[block.id].append(block.name)
                    blocks.append(TelemetryInputBlock(type="tool_use", tool_use_id=block.id, name=block.name))
                elif isinstance(block, ToolResultBlock):
                    names = unmatched.get(block.tool_use_id)
                    tool_name = names.popleft() if names else None
                    aliyun_http = sanitize_aliyun_http_metadata(block.metadata.get(ALIYUN_HTTP_METADATA_KEY))
                    metadata = {ALIYUN_HTTP_METADATA_KEY: aliyun_http} if aliyun_http is not None else {}
                    blocks.append(
                        TelemetryInputBlock(
                            type="tool_result",
                            tool_use_id=block.tool_use_id,
                            name=tool_name,
                            content=block.content,
                            is_error=block.is_error,
                            metadata=metadata,
                        )
                    )
                elif isinstance(block, ThinkingBlock):
                    blocks.append(TelemetryInputBlock(type="thinking"))
                elif isinstance(block, RedactedThinkingBlock):
                    blocks.append(TelemetryInputBlock(type="redacted_thinking"))
                else:
                    blocks.append(TelemetryInputBlock(type=block.type))
            telemetry_messages.append(TelemetryInputMessage(role=message.role, content=blocks))
        return provider_messages, telemetry_messages

    async def run(self, user_input: str | list[ContentBlock]) -> str:
        """Non-streaming execution. Returns final text."""
        final_text = ""
        async for event in self.run_streaming(user_input):
            if isinstance(event, TextDeltaEvent):
                final_text += event.text
        return final_text

    async def run_streaming(
        self,
        user_input: str | list[ContentBlock],
        queued_input_provider: Callable[[], list[str]] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Streaming execution yielding fine-grained StreamEvents.

        Flow:
        1. Add user message to context
        2. Call provider.stream() -> yields StreamEvents
        3. Collect tool_use from events
        4. Execute tools concurrently via ToolExecutor
        5. Yield ToolResultEvents
        6. Loop back to step 2 if tools were called
        """
        from iac_code.services.telemetry import add_metric, get_session_id, get_user_id, log_event, start_span
        from iac_code.services.telemetry.config import should_capture_content_on_span
        from iac_code.services.telemetry.content_serializer import serialize_output_messages
        from iac_code.services.telemetry.names import (
            FRAMEWORK_IAC_CODE,
            Events,
            GenAiAttr,
            GenAiOperationName,
            GenAiSpanKind,
            Metrics,
            Spans,
        )

        entry_attrs: dict[str, Any] = {
            GenAiAttr.SPAN_KIND: GenAiSpanKind.ENTRY,
            GenAiAttr.OPERATION_NAME: GenAiOperationName.ENTER,
            GenAiAttr.SESSION_ID: get_session_id(),
            GenAiAttr.USER_ID: get_user_id(),
            GenAiAttr.FRAMEWORK: FRAMEWORK_IAC_CODE,
        }
        entry_attrs.update(self._telemetry_attributes)
        if should_capture_content_on_span():
            from iac_code.services.telemetry.content_serializer import (
                serialize_system_instructions,
                serialize_user_input,
            )

            # serialize_user_input expects str; for structured input (list[ContentBlock]),
            # extract text-only segments so telemetry stays readable without leaking image bytes.
            if isinstance(user_input, str):
                input_text_for_telemetry = user_input
            else:
                input_text_for_telemetry = " ".join(
                    getattr(b, "text", "") for b in user_input if getattr(b, "type", None) == "text"
                )
            entry_attrs[GenAiAttr.INPUT_MESSAGES] = serialize_user_input(input_text_for_telemetry)
            entry_attrs[GenAiAttr.SYSTEM_INSTRUCTIONS] = serialize_system_instructions(self.system_prompt)

        with start_span(Spans.ENTRY, entry_attrs) as entry_span:
            await self._start_background_tasks()
            interaction_started = time.monotonic()
            first_token_received = False
            final_text_chunks: list[str] = []
            final_stop_reason = "stop"
            memory_prefetch = None
            turn_cancelled = False
            self._memory_recall_active_turns += 1
            try:
                # Refresh the git branch once per turn — branch may change
                # between turns (user runs git checkout via Bash tool), but
                # is treated as stable within a single in-flight request.
                self._refresh_git_branch()
                await self._apply_auto_triggers(user_input)
                self.context_manager.add_user_message(user_input)
                if self._session_storage:
                    from iac_code.agent.message import Message

                    self._session_storage.append(
                        self._cwd,
                        self._session_id,
                        Message(role="user", content=user_input),
                        git_branch=self._current_git_branch,
                    )
                memory_prefetch = self._start_memory_prefetch_for_turn(user_input)
                try:
                    async for event in self._run_streaming_inner(
                        user_input,
                        queued_input_provider=queued_input_provider,
                        memory_prefetch=memory_prefetch,
                    ):
                        if _is_first_output_delta(event) and not first_token_received:
                            first_token_received = True
                            ttft_ns = int((time.monotonic() - interaction_started) * 1_000_000_000)
                            entry_span.set_attribute(GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN, ttft_ns)
                            entry_span.set_attribute(GenAiAttr.USER_TIME_TO_FIRST_TOKEN, ttft_ns)
                        if isinstance(event, TextDeltaEvent):
                            final_text_chunks.append(event.text)
                        if isinstance(event, MessageEndEvent):
                            final_stop_reason = event.stop_reason
                            self._record_session_usage(event.usage)
                        yield event
                except asyncio.CancelledError:
                    turn_cancelled = True
                    self._discard_memory_prefetch_for_turn(memory_prefetch)
                    log_event(Events.SESSION_CANCELLED, {"stage": "in_query"})
                    raise
            finally:
                self._cancel_owned_contract_snapshots()
                if not turn_cancelled:
                    # Recall prefetches are turn-scoped: ready results are consumed only at in-turn poll points.
                    self._discard_memory_prefetch_for_turn(memory_prefetch)
                self._memory_recall_active_turns = max(0, self._memory_recall_active_turns - 1)
                self.context_manager.set_system_prompt(self.system_prompt)
                elapsed = time.monotonic() - interaction_started
                add_metric(Metrics.ACTIVE_TIME_TOTAL, int(elapsed), {})
                if should_capture_content_on_span() and final_text_chunks:
                    entry_span.set_attribute(
                        GenAiAttr.OUTPUT_MESSAGES,
                        serialize_output_messages("".join(final_text_chunks), final_stop_reason),
                    )

    async def continue_streaming(self) -> AsyncGenerator[StreamEvent, None]:
        """Continue from already-loaded context without appending a new user message.

        Used by pipeline recovery when the original step prompt is already present
        in the restored transcript. Normal REPL turns must continue using
        run_streaming() so user messages are persisted before provider streaming.
        """
        from iac_code.services.telemetry import add_metric, get_session_id, get_user_id, log_event, start_span
        from iac_code.services.telemetry.config import should_capture_content_on_span
        from iac_code.services.telemetry.content_serializer import serialize_output_messages
        from iac_code.services.telemetry.names import (
            FRAMEWORK_IAC_CODE,
            Events,
            GenAiAttr,
            GenAiOperationName,
            GenAiSpanKind,
            Metrics,
            Spans,
        )

        entry_attrs: dict[str, Any] = {
            GenAiAttr.SPAN_KIND: GenAiSpanKind.ENTRY,
            GenAiAttr.OPERATION_NAME: GenAiOperationName.ENTER,
            GenAiAttr.SESSION_ID: get_session_id(),
            GenAiAttr.USER_ID: get_user_id(),
            GenAiAttr.FRAMEWORK: FRAMEWORK_IAC_CODE,
        }
        entry_attrs.update(self._telemetry_attributes)
        with start_span(Spans.ENTRY, entry_attrs) as entry_span:
            await self._start_background_tasks()
            interaction_started = time.monotonic()
            first_token_received = False
            final_text_chunks: list[str] = []
            final_stop_reason = "stop"
            try:
                self._refresh_git_branch()
                try:
                    async for event in self._run_streaming_inner("", memory_prefetch=None):
                        if _is_first_output_delta(event) and not first_token_received:
                            first_token_received = True
                            ttft_ns = int((time.monotonic() - interaction_started) * 1_000_000_000)
                            entry_span.set_attribute(GenAiAttr.RESPONSE_TIME_TO_FIRST_TOKEN, ttft_ns)
                            entry_span.set_attribute(GenAiAttr.USER_TIME_TO_FIRST_TOKEN, ttft_ns)
                        if isinstance(event, TextDeltaEvent):
                            final_text_chunks.append(event.text)
                        if isinstance(event, MessageEndEvent):
                            final_stop_reason = event.stop_reason
                            self._record_session_usage(event.usage)
                        yield event
                except asyncio.CancelledError:
                    log_event(Events.SESSION_CANCELLED, {"stage": "in_query"})
                    raise
            finally:
                self._cancel_owned_contract_snapshots()
                self.context_manager.set_system_prompt(self.system_prompt)
                elapsed = time.monotonic() - interaction_started
                add_metric(Metrics.ACTIVE_TIME_TOTAL, int(elapsed), {})
                if should_capture_content_on_span() and final_text_chunks:
                    entry_span.set_attribute(
                        GenAiAttr.OUTPUT_MESSAGES,
                        serialize_output_messages("".join(final_text_chunks), final_stop_reason),
                    )

    async def resume_permission_boundary(
        self,
        checkpoint: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        """Resume the exact trailing assistant tool batch from a durable wait.

        This intentionally bypasses ``SessionStorage.repair_interrupted``.  The
        trailing assistant message is not an interrupted execution: it is the
        canonical source for a permission continuation frame.
        """

        from iac_code.agent.message import Message

        frame = checkpoint.get("continuationFrame")
        decision = checkpoint.get("decision")
        if not isinstance(frame, dict) or not isinstance(decision, dict):
            raise ValueError("permission_resume_invalid: continuation frame is missing")
        if decision.get("status") not in {"claimed", "applied"} or decision.get("value") not in {
            "allow_once",
            "deny",
        }:
            raise ValueError("permission_resume_invalid: permission decision is missing")

        messages = self.context_manager.get_messages()
        if not messages or messages[-1].role != "assistant":
            raise ValueError("permission_resume_invalid: assistant tool message is missing")
        message_index = len(messages) - 1
        expected_message_ref = f"session.jsonl:{message_index}"
        if self._transcript_id is not None:
            expected_message_ref = f"pipeline/transcripts/{self._transcript_id}/session.jsonl:{message_index}"
        if frame.get("assistantMessageRef") != expected_message_ref:
            raise ValueError("permission_resume_invalid: assistant message reference changed")
        assistant_message = messages[-1]
        tool_uses = assistant_message.get_tool_use_blocks()
        ordered_ids = [tool_use.id for tool_use in tool_uses]
        if ordered_ids != frame.get("orderedToolUseIds"):
            raise ValueError("permission_resume_invalid: tool ordering changed")
        assistant_digest = canonical_digest(
            [block.model_dump(mode="json") for block in assistant_message.content]
            if isinstance(assistant_message.content, list)
            else assistant_message.content
        )
        if assistant_digest != frame.get("assistantMessageDigest"):
            raise ValueError("permission_resume_invalid: assistant message changed")
        current_index = frame.get("currentIndex")
        if isinstance(current_index, bool) or not isinstance(current_index, int):
            raise ValueError("permission_resume_invalid: current tool index is invalid")
        if current_index < 0 or current_index >= len(tool_uses):
            raise ValueError("permission_resume_invalid: current tool index is invalid")
        if tool_uses[current_index].id != checkpoint.get("toolUseId"):
            raise ValueError("permission_resume_invalid: current tool correlation changed")
        if canonical_digest(
            {"name": tool_uses[current_index].name, "input": tool_uses[current_index].input}
        ) != checkpoint.get("payloadDigest"):
            raise ValueError("permission_resume_invalid: tool payload changed")
        frame_payload_digest = frame.get("currentPayloadDigest")
        if frame_payload_digest is not None and frame_payload_digest != checkpoint.get("payloadDigest"):
            raise ValueError("permission_resume_invalid: continuation payload changed")

        requests: list[ToolCallRequest] = []
        event_queues: dict[str, asyncio.Queue[Any]] = {}
        tools_with_progress = {"agent", "ros_stack", "ros_stack_instances"}
        for tool_use in tool_uses:
            tool = self.tool_registry.get(tool_use.name)
            invocation_input = tool_use.input
            prepare_invocation_input = getattr(tool, "prepare_invocation_input", None)
            if callable(prepare_invocation_input):
                invocation_input = prepare_invocation_input(invocation_input)
            queue = None
            if tool_use.name in tools_with_progress or (tool is not None and tool.needs_event_queue()):
                queue = asyncio.Queue()
                event_queues[tool_use.id] = queue
            requests.append(
                ToolCallRequest(
                    id=tool_use.id,
                    name=tool_use.name,
                    input=invocation_input,
                    event_queue=queue,
                    invocation_binding=InvocationBinding(
                        runtime_nonce=self._runtime_nonce,
                        session_id=self._session_id,
                        tool_use_id=tool_use.id,
                        tool_name=tool_use.name,
                        canonical_input_sha256=canonical_input_sha256(invocation_input),
                    ),
                )
            )

        context = ToolContext(
            cwd=self._cwd,
            trusted_read_directories=list(self._tool_context_trusted_read_directories),
            relative_read_directories=list(self._tool_context_relative_read_directories),
            pipeline_mode=self._pipeline_mode,
            env_overrides=dict(self._tool_context_env_overrides),
            telemetry_attributes=dict(self._telemetry_attributes),
        )
        recorded_decisions = frame.get("decisions")
        if not isinstance(recorded_decisions, list) or len(recorded_decisions) != len(requests):
            raise ValueError("permission_resume_invalid: tool decisions changed")

        allowed_requests: list[ToolCallRequest] = []
        denied_by_id: dict[str, ToolResult] = {}
        continuation_decisions = [dict(item) for item in recorded_decisions if isinstance(item, dict)]
        if len(continuation_decisions) != len(requests):
            raise ValueError("permission_resume_invalid: tool decisions changed")

        for request_index, request in enumerate(requests):
            permission, audit_context = await self._permission_for_recovered_request(request, context)
            recorded = continuation_decisions[request_index]
            state = recorded.get("state")
            source = recorded.get("source")
            if request_index == current_index:
                if permission is None:
                    raise ValueError("permission_resume_invalid: current tool is unavailable")
                principal_ref, region = permission_execution_identity(
                    tool_name=request.name,
                    tool_input=request.input,
                    permission_audit=getattr(permission, "audit", None),
                )
                if principal_ref != checkpoint.get("principalRef") or region != checkpoint.get("region"):
                    raise ValueError("permission_resume_invalid: cloud execution identity changed")
                state = "allow" if decision["value"] == "allow_once" else "deny"
                source = "user"
                if permission.behavior != "deny":
                    additional_decision: Literal["allow", "deny"] = "allow" if state == "allow" else "deny"
                    additional_audit_ok = _emit_permission_audit_items(
                        session_id=self._session_id,
                        cwd=context.cwd,
                        request=request,
                        audits=_permission_audits(permission, include_primary=False),
                        decision=additional_decision,
                        settings=audit_context.get("settings"),
                        audit_log_path=audit_context.get("audit_log_path"),
                    )
                    if state == "allow" and not additional_audit_ok:
                        state = "deny"
                        source = "audit_failure"
                        recorded["deniedResult"] = _("Permission denied.")
                if state == "allow":
                    recorded.update(principalRef=principal_ref, region=region)
            elif request_index < current_index and state == "allow":
                if permission is None:
                    state = "deny"
                    source = "missing_tool"
                    recorded["deniedResult"] = _("Permission denied.")
                elif source == "user":
                    principal_ref, region = permission_execution_identity(
                        tool_name=request.name,
                        tool_input=request.input,
                        permission_audit=getattr(permission, "audit", None),
                    )
                    if (
                        "principalRef" not in recorded
                        or "region" not in recorded
                        or principal_ref != recorded.get("principalRef")
                        or region != recorded.get("region")
                    ):
                        state = "deny"
                        source = "identity_changed"
                        recorded["deniedResult"] = _("Permission denied.")
                elif source != "policy":
                    state = "deny"
                    source = "permission_changed"
                    recorded["deniedResult"] = _("Permission denied.")

                if (
                    state == "allow"
                    and permission is not None
                    and permission.behavior not in {"allow", "deny"}
                    and source == "policy"
                ):
                    state = "deny"
                    source = "permission_changed"
                    recorded["deniedResult"] = _("Permission denied.")
            elif request_index > current_index:
                if permission is None:
                    state = "allow"
                    source = "missing_tool"
                elif permission.behavior == "allow":
                    audit_ok = _emit_no_prompt_permission_audit(
                        session_id=self._session_id,
                        cwd=context.cwd,
                        request=request,
                        permission=permission,
                        decision="allow",
                        settings=audit_context.get("settings"),
                        audit_log_path=audit_context.get("audit_log_path"),
                    )
                    if audit_ok:
                        state = "allow"
                        source = "policy"
                    else:
                        state = "deny"
                        source = "audit_failure"
                        recorded["deniedResult"] = _("Permission denied.")
                elif permission.behavior == "deny":
                    _emit_no_prompt_permission_audit(
                        session_id=self._session_id,
                        cwd=context.cwd,
                        request=request,
                        permission=permission,
                        decision="deny",
                        settings=audit_context.get("settings"),
                        audit_log_path=audit_context.get("audit_log_path"),
                    )
                    state = "deny"
                    source = "policy"
                    recorded["deniedResult"] = permission.message or _("Permission denied.")
                else:
                    state = "pending"
                    source = None
                    recorded.update(state=state, source=source)
                    response_future: asyncio.Future[bool | PermissionWaitOutcome] = (
                        asyncio.get_running_loop().create_future()
                    )
                    permission_event = PermissionRequestEvent(
                        tool_name=request.name,
                        tool_input=request.input,
                        tool_use_id=request.id,
                        response_future=response_future,
                        permission_result=permission,
                        audit_context=audit_context,
                        continuation_frame={
                            **frame,
                            "currentIndex": request_index,
                            "currentPayloadDigest": canonical_digest(
                                {"name": tool_uses[request_index].name, "input": tool_uses[request_index].input}
                            ),
                            "decisions": [dict(item) for item in continuation_decisions],
                            "previousBoundaryId": checkpoint.get("boundaryId"),
                        },
                    )
                    yield permission_event
                    outcome = await asyncio.shield(response_future)
                    if outcome is PermissionWaitOutcome.SUSPEND:
                        raise PermissionWaitSuspended(permission_event.boundary_id)
                    state = "allow" if bool(outcome) else "deny"
                    source = "user"
                    additional_audit_ok = _emit_permission_audit_items(
                        session_id=self._session_id,
                        cwd=context.cwd,
                        request=request,
                        audits=_permission_audits(permission, include_primary=False),
                        decision="allow" if state == "allow" else "deny",
                        settings=audit_context.get("settings"),
                        audit_log_path=audit_context.get("audit_log_path"),
                    )
                    if state == "allow" and not additional_audit_ok:
                        state = "deny"
                        source = "audit_failure"
                        recorded["deniedResult"] = _("Permission denied.")
                    if state == "allow":
                        principal_ref, region = permission_execution_identity(
                            tool_name=request.name,
                            tool_input=request.input,
                            permission_audit=getattr(permission, "audit", None),
                        )
                        recorded.update(principalRef=principal_ref, region=region)

            # A recovered user decision never overrides a policy that has since
            # become a hard deny.  Persist that transition in the continuation
            # frame before a later permission can create a successor boundary.
            if state == "allow" and permission is not None and permission.behavior == "deny":
                _emit_no_prompt_permission_audit(
                    session_id=self._session_id,
                    cwd=context.cwd,
                    request=request,
                    permission=permission,
                    decision="deny",
                    settings=audit_context.get("settings"),
                    audit_log_path=audit_context.get("audit_log_path"),
                )
                state = "deny"
                source = "policy"
                recorded["deniedResult"] = permission.message or _("Permission denied.")
            if state == "deny":
                recorded.pop("principalRef", None)
                recorded.pop("region", None)
            recorded.update(state=state, source=source)
            if state == "allow":
                allowed_requests.append(request)
            elif state == "deny":
                denied_by_id[request.id] = ToolResult.error(
                    str(recorded.get("deniedResult") or _("Permission denied."))
                )
                self._reject_owned_contract_snapshot(request.snapshot_id)
            else:
                raise ValueError("permission_resume_invalid: prior tool decision is incomplete")

        public_path_roots = build_public_path_roots(
            cwd=context.cwd,
            additional_directories=context.additional_directories,
            trusted_read_directories=context.trusted_read_directories,
            relative_read_directories=context.relative_read_directories,
        )
        for request in requests:
            denied = denied_by_id.get(request.id)
            if denied is not None:
                yield ToolResultEvent(
                    tool_use_id=request.id,
                    tool_name=request.name,
                    result=denied.content,
                    is_error=True,
                    public_path_roots=public_path_roots,
                )

        executed_by_id: dict[str, ToolResult] = {}
        if allowed_requests:
            results = await self._tool_executor.execute_batch(allowed_requests, context)
            for request, result in zip(allowed_requests, results):
                executed_by_id[request.id] = result
                processed = self._result_storage.process(request.id, result.content)
                self._mark_read_memory_tool_result(request, result)
                result_metadata = self._tool_result_event_metadata(result.metadata, processed)
                result_metadata = self._tool_result_render_metadata(
                    result_metadata,
                    self.tool_registry.get(request.name),
                    processed.content,
                    is_error=result.is_error,
                    tool_name=request.name,
                    tool_input=request.input,
                )
                yield ToolResultEvent(
                    tool_use_id=request.id,
                    tool_name=request.name,
                    result=processed.content,
                    is_error=result.is_error,
                    public_path_roots=public_path_roots,
                    metadata=result_metadata,
                )
                result.content = processed.content
                result.metadata = result_metadata

        result_blocks: list[ToolResultBlock] = []
        for request in requests:
            denied = denied_by_id.get(request.id)
            if denied is not None:
                result_blocks.append(ToolResultBlock(tool_use_id=request.id, content=denied.content, is_error=True))
                continue
            result = executed_by_id.get(request.id)
            if result is None:
                raise ValueError("permission_resume_invalid: tool result is missing")
            result_blocks.append(
                ToolResultBlock(
                    tool_use_id=request.id,
                    content=result.content,
                    is_error=result.is_error,
                    metadata=result.metadata or {},
                )
            )
        self.context_manager.add_tool_results(result_blocks)
        if self._session_storage:
            result_content: list[ContentBlock] = list(result_blocks)
            self._session_storage.append(
                self._cwd,
                self._session_id,
                Message(role="user", content=result_content),
                git_branch=self._current_git_branch,
            )

        for request in requests:
            result = executed_by_id.get(request.id)
            if result is None:
                continue
            for raw_message in result.new_messages:
                injected = self.context_manager.add_raw_message(raw_message)
                if self._session_storage:
                    self._session_storage.append(
                        self._cwd,
                        self._session_id,
                        injected,
                        git_branch=self._current_git_branch,
                    )
            if result.context_modifier is not None:
                self._apply_context_modifier(result.context_modifier)

        async for event in self.continue_streaming():
            yield event

    async def _permission_for_recovered_request(
        self,
        request: ToolCallRequest,
        context: ToolContext,
    ) -> tuple[PermissionResult | None, dict[str, Any]]:
        tool = self.tool_registry.get(request.name)
        if tool is None:
            return None, {}
        perm_ctx = self._permission_context_getter() if self._permission_context_getter is not None else None
        if perm_ctx is None:
            perm_ctx = self._permission_context
        if perm_ctx is not None:
            from iac_code.services.permissions.pipeline import check_tool_permission

            effective_perm_ctx = _with_tool_read_directories(
                perm_ctx,
                trusted_directories=self._tool_context_trusted_read_directories,
                relative_directories=self._tool_context_relative_read_directories,
            )
            if isinstance(effective_perm_ctx, ToolPermissionContext):
                effective_perm_ctx = replace(
                    effective_perm_ctx,
                    invocation_binding=request.invocation_binding,
                    pipeline_mode=self._pipeline_mode,
                )
            _extend_unique(context.additional_directories, list(effective_perm_ctx.additional_directories))
            _extend_unique(context.trusted_read_directories, list(effective_perm_ctx.trusted_read_directories))
            _extend_unique(context.relative_read_directories, list(effective_perm_ctx.relative_read_directories))
            _extend_unique(
                context.strict_read_directories,
                list(getattr(effective_perm_ctx, "strict_read_directories", [])),
            )
            context.read_path_violation_behavior = getattr(
                effective_perm_ctx,
                "read_path_violation_behavior",
                context.read_path_violation_behavior,
            )
            context.set_permission_context(effective_perm_ctx)
            permission = await check_tool_permission(tool, request.input, effective_perm_ctx)
        else:
            permission = await tool.check_permissions(
                request.input,
                ToolPermissionContext(
                    cwd=context.cwd,
                    invocation_binding=request.invocation_binding,
                    pipeline_mode=self._pipeline_mode,
                ),
            )
        permission = _with_prompt_permission_metadata(tool, request.input, permission)
        request.snapshot_id = permission.snapshot_id
        request.security_digest = permission.security_digest
        request.execution_class = permission.execution_class
        if permission.invocation_binding is not None:
            request.invocation_binding = permission.invocation_binding
        if request.snapshot_id is not None:
            self._owned_contract_snapshot_ids.add(request.snapshot_id)
        audit_context = {
            "session_id": self._session_id,
            "cwd": context.cwd,
            "settings": perm_ctx.audit_settings if perm_ctx is not None else None,
            "metadata": permission.audit,
        }
        principal_ref, region = permission_execution_identity(
            tool_name=request.name,
            tool_input=request.input,
            permission_audit=permission.audit,
        )
        audit_context.update(principal_ref=principal_ref, region=region)
        if self._has_session_hierarchy:
            audit_context["root_session_id"] = self._root_session_id
            audit_context["transcript_id"] = self._transcript_id
        if self._audit_log_path is not None:
            audit_context["audit_log_path"] = self._audit_log_path
        return permission, audit_context

    async def rebuild_permission_audit_event(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        audit_context: Mapping[str, Any],
    ) -> PermissionRequestEvent:
        """Recheck one canonical request only to rebuild restart audit data.

        The returned event is not an execution authorization. Any process-local
        execution-contract snapshot created by the permission check is rejected;
        the real continuation must rebuild its own contract again.
        """

        tool = self.tool_registry.get(tool_name)
        if tool is None:
            raise ValueError("permission_resume_invalid: current tool is unavailable")
        invocation_input = dict(tool_input)
        prepare_invocation_input = getattr(tool, "prepare_invocation_input", None)
        if callable(prepare_invocation_input):
            invocation_input = prepare_invocation_input(invocation_input)
        request = ToolCallRequest(
            id=tool_use_id,
            name=tool_name,
            input=invocation_input,
            invocation_binding=InvocationBinding(
                runtime_nonce=self._runtime_nonce,
                session_id=self._session_id,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                canonical_input_sha256=canonical_input_sha256(invocation_input),
            ),
        )
        context = ToolContext(
            cwd=self._cwd,
            trusted_read_directories=list(self._tool_context_trusted_read_directories),
            relative_read_directories=list(self._tool_context_relative_read_directories),
            pipeline_mode=self._pipeline_mode,
            env_overrides=dict(self._tool_context_env_overrides),
            telemetry_attributes=dict(self._telemetry_attributes),
        )
        try:
            permission, rebuilt_context = await self._permission_for_recovered_request(
                request,
                context,
            )
            if permission is None:
                raise ValueError("permission_resume_invalid: current tool is unavailable")
            return PermissionRequestEvent(
                tool_name=tool_name,
                tool_input=invocation_input,
                tool_use_id=tool_use_id,
                permission_result=permission,
                audit_context={**rebuilt_context, **dict(audit_context)},
            )
        finally:
            self._reject_owned_contract_snapshot(request.snapshot_id)

    async def _stream_provider(
        self,
        *,
        messages: list[Any],
        system: str,
        tools: list[Any] | None,
        telemetry_messages: list[Any] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        with use_span_attributes(self._telemetry_attributes):
            stream_method = self._provider_manager.stream
            stream_parameters = inspect.signature(stream_method).parameters.values()
            supports_telemetry_sidecar = any(
                parameter.name == "telemetry_messages" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in stream_parameters
            )
            stream_kwargs = {"messages": messages, "system": system, "tools": tools}
            if supports_telemetry_sidecar:
                stream_kwargs["telemetry_messages"] = telemetry_messages
            provider_stream = stream_method(**stream_kwargs)
        try:
            while True:
                with use_span_attributes(self._telemetry_attributes):
                    try:
                        event = await anext(provider_stream)
                    except StopAsyncIteration:
                        return
                yield event
        finally:
            with use_span_attributes(self._telemetry_attributes):
                await provider_stream.aclose()

    async def _run_streaming_inner(
        self,
        user_input: str | list[ContentBlock],
        *,
        queued_input_provider: Callable[[], list[str]] | None = None,
        memory_prefetch: Any = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Inner streaming loop (called from run_streaming inside the ENTRY span)."""
        from iac_code.services.telemetry import start_span
        from iac_code.services.telemetry.config import should_capture_content_on_span
        from iac_code.services.telemetry.content_serializer import serialize_output_messages
        from iac_code.services.telemetry.names import GenAiAttr, GenAiOperationName, GenAiSpanKind, Spans

        for _turn in range(self._max_turns):
            # Pipeline interrupt/recovery can pause between LLM turns and
            # inject supplemental user text before the next provider call.
            if self._pause_event is not None:
                await self._pause_event.wait()
            self._drain_pending_injections()
            self._accepting_injected_user_messages = False
            self._current_turn_text = ""

            system_prompt = self._prepare_provider_system_prompt()
            tool_definitions = self._sync_tool_definitions(system_prompt=system_prompt)
            await self._consume_ready_memory_prefetches(memory_prefetch)

            # Auto-compact if needed
            if self.context_manager.needs_compaction():
                # Emit a "started" marker first so the web UI can render the
                # "正在自动压缩上下文" running indicator while the (blocking)
                # summarization LLM call is in flight, then always emit a
                # terminal event so the indicator never sticks. A no-op or
                # failure is explicit instead of looking like a successful
                # 0 -> 0 compaction.
                yield CompactionEvent(phase="started")
                compact_event = await self._auto_compact()
                yield compact_event if compact_event else CompactionEvent(phase="failed", reason="no_result")

            step_attrs = {
                GenAiAttr.SPAN_KIND: GenAiSpanKind.STEP,
                GenAiAttr.OPERATION_NAME: GenAiOperationName.REACT,
                GenAiAttr.REACT_ROUND: _turn + 1,
            }
            step_attrs.update(self._telemetry_attributes)

            with start_span(Spans.REACT_STEP, step_attrs) as step_span:
                # Collect tool uses from this turn (keyed by tool_use_id)
                pending_tool_uses_by_id: dict[str, dict[str, Any]] = {}
                text_chunks: list[str] = []
                thinking_blocks_by_index: dict[int, dict[str, Any]] = {}
                message_ended = False
                turn_stop_reason = "stop"

                provider_messages, telemetry_messages = self._get_provider_messages_with_telemetry()
                provider_tools = tool_definitions or None
                self._last_provider_request_snapshot = {
                    "system_prompt": system_prompt,
                    "provider_messages": list(provider_messages),
                    "tools": list(provider_tools or []),
                }

                # Stream from provider
                async for event in self._stream_provider(
                    messages=provider_messages,
                    system=system_prompt,
                    tools=provider_tools,
                    telemetry_messages=telemetry_messages,
                ):
                    if isinstance(event, ToolUseStartEvent):
                        event = replace(
                            event,
                            metadata=self._tool_use_start_metadata(
                                event.metadata,
                                self.tool_registry.get(event.name),
                            ),
                        )
                    if not (isinstance(event, ThinkingDeltaEvent) and event.is_metadata_only):
                        yield event

                    # Collect data from events
                    if isinstance(event, TextDeltaEvent):
                        text_chunks.append(event.text)
                        if self._pause_event is not None:
                            self._current_turn_text += event.text
                    elif isinstance(event, ThinkingDeltaEvent):
                        thinking_block = thinking_blocks_by_index.setdefault(
                            event.block_index,
                            {
                                "type": event.block_type,
                                "text": "",
                                "provider_metadata": {},
                            },
                        )
                        thinking_block["type"] = event.block_type
                        thinking_block["text"] += event.text
                        if event.provider_metadata:
                            for key, value in event.provider_metadata.items():
                                if (
                                    key == "signature"
                                    and isinstance(value, str)
                                    and isinstance(thinking_block["provider_metadata"].get(key), str)
                                ):
                                    thinking_block["provider_metadata"][key] += value
                                else:
                                    thinking_block["provider_metadata"][key] = value
                    elif isinstance(event, ToolUseStartEvent):
                        pending_tool_uses_by_id.setdefault(event.tool_use_id, {})
                        pending_tool_uses_by_id[event.tool_use_id]["id"] = event.tool_use_id
                        pending_tool_uses_by_id[event.tool_use_id]["name"] = event.name
                        if event.provider_metadata:
                            pending_tool_uses_by_id[event.tool_use_id]["provider_metadata"] = dict(
                                event.provider_metadata
                            )
                    elif isinstance(event, ToolUseEndEvent):
                        pending_tool_uses_by_id.setdefault(event.tool_use_id, {})
                        pending_tool_uses_by_id[event.tool_use_id]["id"] = event.tool_use_id
                        pending_tool_uses_by_id[event.tool_use_id]["name"] = event.name
                        pending_tool_uses_by_id[event.tool_use_id]["input"] = event.input
                        if event.provider_metadata:
                            pending_tool_uses_by_id[event.tool_use_id]["provider_metadata"] = dict(
                                event.provider_metadata
                            )
                    elif isinstance(event, TombstoneEvent):
                        pending_tool_uses_by_id.clear()
                        text_chunks.clear()
                        thinking_blocks_by_index.clear()
                        self._accepting_injected_user_messages = False
                    elif isinstance(event, MessageEndEvent):
                        message_ended = True
                        turn_stop_reason = event.stop_reason

                if not message_ended:
                    self._accepting_injected_user_messages = False
                    step_span.set_attribute(GenAiAttr.REACT_FINISH_REASON, "error")
                    yield MessageEndEvent(stop_reason="stream_error", usage=Usage())
                    break

                # Build assistant message for context
                assistant_blocks = []
                for block_index in sorted(thinking_blocks_by_index):
                    thinking_block = thinking_blocks_by_index[block_index]
                    provider_metadata = thinking_block["provider_metadata"]
                    if thinking_block["type"] == "redacted_thinking":
                        data = provider_metadata.get("data")
                        if isinstance(data, str) and data:
                            assistant_blocks.append(
                                RedactedThinkingBlock(data=data, provider_metadata=provider_metadata)
                            )
                    elif thinking_block["text"] or provider_metadata:
                        assistant_blocks.append(
                            ThinkingBlock(
                                thinking=thinking_block["text"],
                                provider_metadata=provider_metadata,
                            )
                        )
                full_text = "".join(text_chunks)
                if full_text:
                    assistant_blocks.append(TextBlock(text=full_text))
                if should_capture_content_on_span() and full_text:
                    step_span.set_attribute(
                        GenAiAttr.OUTPUT_MESSAGES,
                        serialize_output_messages(full_text, turn_stop_reason),
                    )

                # Collect completed tool uses (those with both name and input)
                completed_tools = []
                for tu in pending_tool_uses_by_id.values():
                    if "name" in tu and "input" in tu:
                        completed_tools.append(tu)
                        assistant_blocks.append(
                            ToolUseBlock(
                                id=tu["id"],
                                name=tu["name"],
                                input=tu.get("input", {}),
                                provider_metadata=tu.get("provider_metadata", {}),
                            )
                        )
                self._accepting_injected_user_messages = bool(completed_tools) and _turn < self._max_turns - 1

                if assistant_blocks:
                    self.context_manager.add_assistant_message(assistant_blocks)
                    if self._session_storage:
                        from iac_code.agent.message import Message

                        self._session_storage.append(
                            self._cwd,
                            self._session_id,
                            Message(role="assistant", content=assistant_blocks),
                            git_branch=self._current_git_branch,
                        )

                # No tool calls -> end turn
                if not completed_tools:
                    self._accepting_injected_user_messages = False
                    step_span.set_attribute(GenAiAttr.REACT_FINISH_REASON, "stop")
                    break

                step_span.set_attribute(GenAiAttr.REACT_FINISH_REASON, "tool_calls")

                # Execute tools (concurrent read-only, serial writes)
                tools_with_progress = {"agent", "ros_stack", "ros_stack_instances"}
                requests = []
                event_queues: dict[str, asyncio.Queue] = {}
                for tu in completed_tools:
                    queue = None
                    tool = self.tool_registry.get(tu["name"])
                    invocation_input = tu.get("input", {})
                    prepare_invocation_input = getattr(tool, "prepare_invocation_input", None)
                    if callable(prepare_invocation_input):
                        invocation_input = prepare_invocation_input(invocation_input)
                    if tu["name"] in tools_with_progress or (tool is not None and tool.needs_event_queue()):
                        queue = asyncio.Queue()
                        event_queues[tu["id"]] = queue
                    requests.append(
                        ToolCallRequest(
                            id=tu["id"],
                            name=tu["name"],
                            input=invocation_input,
                            event_queue=queue,
                            invocation_binding=InvocationBinding(
                                runtime_nonce=self._runtime_nonce,
                                session_id=self._session_id,
                                tool_use_id=tu["id"],
                                tool_name=tu["name"],
                                canonical_input_sha256=canonical_input_sha256(invocation_input),
                            ),
                        )
                    )
                context = ToolContext(
                    cwd=self._cwd,
                    trusted_read_directories=list(self._tool_context_trusted_read_directories),
                    relative_read_directories=list(self._tool_context_relative_read_directories),
                    pipeline_mode=self._pipeline_mode,
                    env_overrides=dict(self._tool_context_env_overrides),
                    telemetry_attributes=dict(self._telemetry_attributes),
                )

                allowed_requests: list[ToolCallRequest] = []
                denied_results: list[tuple[ToolCallRequest, ToolResult]] = []
                assistant_message_digest = canonical_digest(
                    [block.model_dump(mode="json") for block in assistant_blocks]
                )
                continuation_decisions: list[dict[str, Any]] = [
                    {
                        "toolUseId": request.id,
                        "state": "not_evaluated",
                        "source": None,
                        "deniedResult": None,
                    }
                    for request in requests
                ]
                previous_permission_boundary_id: str | None = None
                for request_index, request in enumerate(requests):
                    tool = self.tool_registry.get(request.name)
                    if tool is None:
                        allowed_requests.append(request)
                        continue

                    perm_ctx = None
                    if self._permission_context_getter is not None:
                        perm_ctx = self._permission_context_getter()
                    if perm_ctx is None:
                        perm_ctx = self._permission_context

                    if perm_ctx is not None:
                        from iac_code.services.permissions.pipeline import check_tool_permission

                        effective_perm_ctx = _with_tool_read_directories(
                            perm_ctx,
                            trusted_directories=self._tool_context_trusted_read_directories,
                            relative_directories=self._tool_context_relative_read_directories,
                        )
                        if isinstance(effective_perm_ctx, ToolPermissionContext):
                            effective_perm_ctx = replace(
                                effective_perm_ctx,
                                invocation_binding=request.invocation_binding,
                                pipeline_mode=self._pipeline_mode,
                            )
                        _extend_unique(context.additional_directories, list(effective_perm_ctx.additional_directories))
                        _extend_unique(
                            context.trusted_read_directories, list(effective_perm_ctx.trusted_read_directories)
                        )
                        _extend_unique(
                            context.relative_read_directories,
                            list(effective_perm_ctx.relative_read_directories),
                        )
                        _extend_unique(
                            context.strict_read_directories,
                            list(getattr(effective_perm_ctx, "strict_read_directories", [])),
                        )
                        context.read_path_violation_behavior = getattr(
                            effective_perm_ctx,
                            "read_path_violation_behavior",
                            context.read_path_violation_behavior,
                        )
                        context.set_permission_context(effective_perm_ctx)
                        permission = await check_tool_permission(tool, request.input, effective_perm_ctx)
                    else:
                        permission = await tool.check_permissions(
                            request.input,
                            ToolPermissionContext(
                                cwd=context.cwd,
                                invocation_binding=request.invocation_binding,
                                pipeline_mode=self._pipeline_mode,
                            ),
                        )

                    permission = _with_prompt_permission_metadata(tool, request.input, permission)
                    request.snapshot_id = permission.snapshot_id
                    request.security_digest = permission.security_digest
                    request.execution_class = permission.execution_class
                    if permission.invocation_binding is not None:
                        request.invocation_binding = permission.invocation_binding
                    if request.snapshot_id is not None:
                        self._owned_contract_snapshot_ids.add(request.snapshot_id)

                    audit_context = {
                        "session_id": self._session_id,
                        "cwd": context.cwd,
                        "settings": perm_ctx.audit_settings if perm_ctx is not None else None,
                        "metadata": permission.audit,
                    }
                    principal_ref, region = permission_execution_identity(
                        tool_name=request.name,
                        tool_input=request.input,
                        permission_audit=permission.audit,
                    )
                    audit_context.update(principal_ref=principal_ref, region=region)
                    if self._has_session_hierarchy:
                        audit_context["root_session_id"] = self._root_session_id
                        audit_context["transcript_id"] = self._transcript_id
                    if self._audit_log_path is not None:
                        audit_context["audit_log_path"] = self._audit_log_path

                    if permission.behavior == "allow":
                        audit_ok = _emit_no_prompt_permission_audit(
                            session_id=self._session_id,
                            cwd=context.cwd,
                            request=request,
                            permission=permission,
                            decision="allow",
                            settings=audit_context["settings"],
                            audit_log_path=self._audit_log_path,
                        )
                        if not audit_ok:
                            self._reject_owned_contract_snapshot(request.snapshot_id)
                            denied_results.append((request, ToolResult.error(_("Permission denied."))))
                            continuation_decisions[request_index].update(
                                state="deny",
                                source="audit_failure",
                                deniedResult=_("Permission denied."),
                            )
                            continue
                        allowed_requests.append(request)
                        continuation_decisions[request_index].update(state="allow", source="policy")
                        continue
                    if permission.behavior == "deny":
                        _emit_no_prompt_permission_audit(
                            session_id=self._session_id,
                            cwd=context.cwd,
                            request=request,
                            permission=permission,
                            decision="deny",
                            settings=audit_context["settings"],
                            audit_log_path=self._audit_log_path,
                        )
                        self._reject_owned_contract_snapshot(request.snapshot_id)
                        msg = permission.message or _("Permission denied.")
                        denied_results.append((request, ToolResult.error(msg)))
                        continuation_decisions[request_index].update(
                            state="deny",
                            source="policy",
                            deniedResult=msg,
                        )
                        continue

                    continuation_decisions[request_index].update(state="pending", source=None)
                    response_future: asyncio.Future[bool | PermissionWaitOutcome] = (
                        asyncio.get_running_loop().create_future()
                    )
                    permission_event = PermissionRequestEvent(
                        tool_name=request.name,
                        tool_input=request.input,
                        tool_use_id=request.id,
                        response_future=response_future,
                        permission_result=permission,
                        audit_context=audit_context,
                        continuation_frame={
                            "assistantMessageRef": "session.jsonl:{}".format(
                                len(self.context_manager.get_messages()) - 1
                            ),
                            "assistantMessageDigest": assistant_message_digest,
                            "orderedToolUseIds": [item.id for item in requests],
                            "currentIndex": request_index,
                            "currentPayloadDigest": canonical_digest(
                                {
                                    "name": completed_tools[request_index]["name"],
                                    "input": completed_tools[request_index].get("input", {}),
                                }
                            ),
                            "decisions": [dict(item) for item in continuation_decisions],
                            **(
                                {"previousBoundaryId": previous_permission_boundary_id}
                                if previous_permission_boundary_id is not None
                                else {}
                            ),
                        },
                    )
                    yield permission_event
                    try:
                        outcome = await asyncio.shield(response_future)
                    except asyncio.CancelledError:
                        if not permission_event.resolution_owner_managed and not response_future.done():
                            response_future.set_result(False)
                        raise
                    if outcome is PermissionWaitOutcome.SUSPEND:
                        raise PermissionWaitSuspended(permission_event.boundary_id)
                    previous_permission_boundary_id = permission_event.boundary_id
                    approved = bool(outcome)
                    additional_audit_ok = _emit_permission_audit_items(
                        session_id=self._session_id,
                        cwd=context.cwd,
                        request=request,
                        audits=_permission_audits(permission, include_primary=False),
                        decision="allow" if approved else "deny",
                        settings=audit_context["settings"],
                        audit_log_path=self._audit_log_path,
                    )
                    if approved and not additional_audit_ok:
                        approved = False
                    if approved:
                        allowed_requests.append(request)
                        continuation_decisions[request_index].update(
                            state="allow",
                            source="user",
                            principalRef=principal_ref,
                            region=region,
                        )
                    else:
                        self._reject_owned_contract_snapshot(request.snapshot_id)
                        denied_results.append((request, ToolResult.error(_("Permission denied."))))
                        continuation_decisions[request_index].update(
                            state="deny",
                            source="user",
                            deniedResult=_("Permission denied."),
                        )

                public_path_roots = build_public_path_roots(
                    cwd=context.cwd,
                    additional_directories=context.additional_directories,
                    trusted_read_directories=context.trusted_read_directories,
                    relative_read_directories=context.relative_read_directories,
                )

                for request, result in denied_results:
                    yield ToolResultEvent(
                        tool_use_id=request.id,
                        tool_name=request.name,
                        result=result.content,
                        is_error=True,
                        public_path_roots=public_path_roots,
                    )

                if not allowed_requests:
                    if denied_results:
                        denied_blocks: list[ToolResultBlock] = [
                            ToolResultBlock(
                                tool_use_id=request.id,
                                content=result.content,
                                is_error=True,
                            )
                            for request, result in denied_results
                        ]
                        self.context_manager.add_tool_results(denied_blocks)
                        if self._session_storage:
                            from iac_code.agent.message import Message

                            denied_content: list[ContentBlock] = list(denied_blocks)
                            self._session_storage.append(
                                self._cwd,
                                self._session_id,
                                Message(role="user", content=denied_content),
                                git_branch=self._current_git_branch,
                            )
                        async for event in self._submit_queued_inputs_after_tool_call(queued_input_provider):
                            yield event
                    continue

                requests = allowed_requests

                # Start tool execution
                exec_task = asyncio.create_task(self._tool_executor.execute_batch(requests, context))

                # Poll event queues while tools execute
                async def poll_event_queues():
                    while not exec_task.done():
                        for req_id, queue in event_queues.items():
                            try:
                                while True:
                                    item = queue.get_nowait()
                                    if item is None:
                                        break
                                    if isinstance(item, ToolEmittedEvent):
                                        yield item
                                    elif isinstance(item, dict):
                                        yield SubAgentToolEvent(
                                            parent_tool_use_id=req_id,
                                            child_tool_name=item["child_tool_name"],
                                            child_tool_input=item.get("child_tool_input", {}),
                                            is_done=item.get("is_done", False),
                                            is_error=item.get("is_error", False),
                                        )
                            except asyncio.QueueEmpty:
                                pass
                        await asyncio.sleep(0.05)
                    # Final drain
                    for req_id, queue in event_queues.items():
                        while not queue.empty():
                            item = queue.get_nowait()
                            if item is None:
                                continue
                            if isinstance(item, ToolEmittedEvent):
                                yield item
                            elif isinstance(item, dict):
                                yield SubAgentToolEvent(
                                    parent_tool_use_id=req_id,
                                    child_tool_name=item["child_tool_name"],
                                    child_tool_input=item.get("child_tool_input", {}),
                                    is_done=item.get("is_done", False),
                                    is_error=item.get("is_error", False),
                                )

                try:
                    async for sub_event in poll_event_queues():
                        yield sub_event

                    results = await exec_task
                    for request in requests:
                        snapshot_id = request.snapshot_id
                        if snapshot_id is not None and not PROCESS_RESOLVED_CONTRACT_STORE.is_pending(snapshot_id):
                            self._owned_contract_snapshot_ids.discard(snapshot_id)
                except asyncio.CancelledError:
                    if not exec_task.done():
                        exec_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await exec_task
                    raise

                # Process results and yield ToolResultEvents.
                terminal_step_result = False
                tool_result_blocks: list[ToolResultBlock] = [
                    ToolResultBlock(
                        tool_use_id=request.id,
                        content=result.content,
                        is_error=True,
                    )
                    for request, result in denied_results
                ]
                for req, result in zip(requests, results):
                    if (
                        result.metadata
                        and result.metadata.get("step_result") is not None
                        and result.metadata.get("complete_step_terminal", True)
                    ):
                        terminal_step_result = True
                    processed = self._result_storage.process(req.id, result.content)
                    self._mark_read_memory_tool_result(req, result)
                    result_metadata = self._tool_result_event_metadata(result.metadata, processed)
                    result_metadata = self._tool_result_render_metadata(
                        result_metadata,
                        self.tool_registry.get(req.name),
                        processed.content,
                        is_error=result.is_error,
                        tool_name=req.name,
                        tool_input=req.input,
                    )

                    yield ToolResultEvent(
                        tool_use_id=req.id,
                        tool_name=req.name,
                        result=processed.content,
                        is_error=result.is_error,
                        public_path_roots=public_path_roots,
                        metadata=result_metadata,
                    )

                    tool_result_blocks.append(
                        ToolResultBlock(
                            tool_use_id=req.id,
                            content=processed.content,
                            is_error=result.is_error,
                            metadata=self._tool_result_block_metadata(processed, result_metadata),
                        )
                    )

                self.context_manager.add_tool_results(tool_result_blocks)
                if self._session_storage:
                    from iac_code.agent.message import Message

                    result_content: list[ContentBlock] = list(tool_result_blocks)
                    self._session_storage.append(
                        self._cwd,
                        self._session_id,
                        Message(role="user", content=result_content),
                        git_branch=self._current_git_branch,
                    )

                for req, result in zip(requests, results):
                    for msg in result.new_messages:
                        injected = self.context_manager.add_raw_message(msg)
                        if self._session_storage:
                            self._session_storage.append(
                                self._cwd,
                                self._session_id,
                                injected,
                                git_branch=self._current_git_branch,
                            )
                    if result.context_modifier is not None:
                        self._apply_context_modifier(result.context_modifier)

                async for event in self._submit_queued_inputs_after_tool_call(queued_input_provider):
                    yield event
                if terminal_step_result:
                    self._accepting_injected_user_messages = False
                    break
        else:
            self._accepting_injected_user_messages = False
            yield MessageEndEvent(stop_reason="max_turns", usage=Usage())

    async def _submit_queued_inputs_after_tool_call(
        self,
        queued_input_provider: Callable[[], list[str]] | None,
    ) -> AsyncGenerator[QueuedInputSubmittedEvent, None]:
        if queued_input_provider is None:
            return

        queued_inputs = queued_input_provider()
        for raw_input in queued_inputs:
            text = raw_input.strip()
            if not text:
                continue
            await self._apply_auto_triggers(text)
            message = self.context_manager.add_user_message(text)
            message_id = "queued-{}".format(uuid.uuid4().hex)
            message.metadata["messageId"] = message_id
            if self._session_storage:
                self._session_storage.append(
                    self._cwd,
                    self._session_id,
                    message,
                    git_branch=self._current_git_branch,
                )
            yield QueuedInputSubmittedEvent(text=text, message_id=message_id)

    @staticmethod
    def _tool_result_event_metadata(metadata: dict[str, Any] | None, processed: Any) -> dict[str, Any] | None:
        from iac_code.tools.cloud.aliyun.result_contract import with_aliyun_content_state

        metadata = with_aliyun_content_state(
            metadata,
            externalized=bool(getattr(processed, "is_externalized", False)),
        )
        if not getattr(processed, "is_externalized", False) or not getattr(processed, "file_path", None):
            return metadata
        event_metadata = dict(metadata or {})
        event_metadata[EXTERNALIZED_RESULT_PATH_METADATA_KEY] = str(processed.file_path)
        return event_metadata

    @staticmethod
    def _merge_tool_render_metadata(
        metadata: dict[str, Any] | None,
        render_metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not render_metadata:
            return metadata
        merged = dict(metadata or {})
        existing = merged.get(TOOL_RENDER_METADATA_KEY)
        if isinstance(existing, dict):
            render_metadata = {**existing, **render_metadata}
        merged[TOOL_RENDER_METADATA_KEY] = render_metadata
        return merged

    @classmethod
    def _tool_use_start_metadata(cls, metadata: dict[str, Any] | None, tool: Any | None) -> dict[str, Any] | None:
        if tool is None:
            return metadata

        render_metadata: dict[str, Any] = {}
        user_facing_name = getattr(tool, "user_facing_name", None)
        if callable(user_facing_name):
            try:
                display_name = user_facing_name({})
            except Exception:
                display_name = None
            if isinstance(display_name, str) and display_name and display_name != getattr(tool, "name", None):
                render_metadata[TOOL_RENDER_DISPLAY_NAME_KEY] = display_name

        if bool(getattr(tool, "render_verbose_result_in_transcript", False)):
            render_metadata[TOOL_RENDER_VERBOSE_RESULT_IN_TRANSCRIPT_KEY] = True

        return cls._merge_tool_render_metadata(metadata, render_metadata)

    @classmethod
    def _tool_result_render_metadata(
        cls,
        metadata: dict[str, Any] | None,
        tool: Any | None,
        output: str,
        *,
        is_error: bool,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        from iac_code.tools.cloud.aliyun.result_contract import (
            ALIYUN_HTTP_METADATA_KEY,
            ALIYUN_MIGRATED_RESULT_TOOLS,
            render_aliyun_result,
            sanitize_aliyun_http_metadata,
        )

        aliyun_http = (
            sanitize_aliyun_http_metadata(metadata.get(ALIYUN_HTTP_METADATA_KEY))
            if isinstance(metadata, dict)
            else None
        )
        if tool_name in ALIYUN_MIGRATED_RESULT_TOOLS and aliyun_http is not None:
            compact = render_aliyun_result(
                tool_input or {},
                output,
                is_error=is_error,
                aliyun_http=aliyun_http,
                verbose=False,
            )
            verbose = render_aliyun_result(
                tool_input or {},
                output,
                is_error=is_error,
                aliyun_http=aliyun_http,
                verbose=True,
            )
            render_metadata: dict[str, Any] = {}
            if compact:
                render_metadata[TOOL_RENDER_RESULT_COMPACT_KEY] = compact
            if verbose:
                render_metadata[TOOL_RENDER_RESULT_VERBOSE_KEY] = verbose
                render_metadata[TOOL_RENDER_VERBOSE_RESULT_IN_TRANSCRIPT_KEY] = True
            return cls._merge_tool_render_metadata(metadata, render_metadata)

        render_result = getattr(tool, "render_tool_result_message", None)
        if not callable(render_result):
            return metadata

        render_metadata: dict[str, Any] = {}
        try:
            compact = render_result(output, is_error=is_error, verbose=False)
        except Exception:
            compact = None
        if isinstance(compact, str) and compact:
            render_metadata[TOOL_RENDER_RESULT_COMPACT_KEY] = compact

        if bool(getattr(tool, "render_verbose_result_in_transcript", False)):
            render_metadata[TOOL_RENDER_VERBOSE_RESULT_IN_TRANSCRIPT_KEY] = True
            try:
                verbose = render_result(output, is_error=is_error, verbose=True)
            except Exception:
                verbose = None
            if isinstance(verbose, str) and verbose:
                render_metadata[TOOL_RENDER_RESULT_VERBOSE_KEY] = verbose

        return cls._merge_tool_render_metadata(metadata, render_metadata)

    @staticmethod
    def _tool_result_block_metadata(processed: Any, result_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if getattr(processed, "is_externalized", False) and getattr(processed, "file_path", None):
            metadata[EXTERNALIZED_RESULT_PATH_METADATA_KEY] = str(processed.file_path)

        if isinstance(result_metadata, dict):
            from iac_code.tools.cloud.aliyun.result_contract import (
                ALIYUN_HTTP_METADATA_KEY,
                sanitize_aliyun_http_metadata,
            )
            from iac_code.tools.cloud.base_stack import persisted_stack_metadata

            aliyun_http = sanitize_aliyun_http_metadata(result_metadata.get(ALIYUN_HTTP_METADATA_KEY))
            if aliyun_http is not None:
                metadata[ALIYUN_HTTP_METADATA_KEY] = aliyun_http
            metadata.update(persisted_stack_metadata(result_metadata))
            render_metadata = result_metadata.get(TOOL_RENDER_METADATA_KEY)
            if isinstance(render_metadata, dict):
                safe_render_metadata: dict[str, Any] = {}
                for key in (
                    TOOL_RENDER_DISPLAY_NAME_KEY,
                    TOOL_RENDER_RESULT_COMPACT_KEY,
                    TOOL_RENDER_RESULT_VERBOSE_KEY,
                ):
                    value = render_metadata.get(key)
                    if isinstance(value, str):
                        safe_render_metadata[key] = value
                value = render_metadata.get(TOOL_RENDER_VERBOSE_RESULT_IN_TRANSCRIPT_KEY)
                if isinstance(value, bool):
                    safe_render_metadata[TOOL_RENDER_VERBOSE_RESULT_IN_TRANSCRIPT_KEY] = value
                if safe_render_metadata:
                    metadata[TOOL_RENDER_METADATA_KEY] = safe_render_metadata

        return metadata

    def _mark_read_memory_tool_result(self, request: ToolCallRequest, result: ToolResult) -> None:
        if request.name != "read_memory" or result.is_error or self._memory_recall_service is None:
            return
        name = request.input.get("name")
        if not isinstance(name, str) or not name.strip():
            return
        mark_files_read = getattr(self._memory_recall_service, "mark_files_read", None)
        if callable(mark_files_read):
            filename = name.strip()
            if not filename.endswith(".md"):
                filename = f"{filename}.md"
            mark_files_read([filename])

    async def _apply_auto_triggers(self, user_input: str | list[ContentBlock]) -> None:
        if not self._auto_trigger_skills:
            return
        if all(command.name in self._auto_loaded_skills for command in self._auto_trigger_skills):
            return
        prompt_text = self._auto_trigger_text(user_input)
        if not prompt_text:
            return

        from iac_code.skills.auto_trigger import process_auto_triggered_skills

        results = await process_auto_triggered_skills(
            prompt_text,
            self._auto_trigger_skills,
            loaded_skill_names=self._auto_loaded_skills,
            context_messages=self.context_manager.get_messages(),
            session_id=self._session_id,
        )
        for result in results:
            for msg in result.new_messages:
                injected = self.context_manager.add_raw_message(msg)
                if self._session_storage:
                    self._session_storage.append(
                        self._cwd,
                        self._session_id,
                        injected,
                        git_branch=self._current_git_branch,
                    )
            if result.context_modifier is not None:
                self._apply_context_modifier(result.context_modifier)

    @staticmethod
    def _auto_trigger_text(user_input: str | list[ContentBlock]) -> str:
        if isinstance(user_input, str):
            return user_input
        parts = [block.text for block in user_input if isinstance(block, TextBlock)]
        return " ".join(part for part in parts if part).strip()

    def _apply_context_modifier(self, modifier: Any) -> None:
        """Apply a context modifier from a ToolResult to the current execution context."""
        current_ctx: dict[str, Any] = {
            "allowed_tool_rules": getattr(self, "_allowed_tool_rules", []),
            "model_override": getattr(self, "_model_override", None),
            "effort_override": getattr(self, "_effort_override", None),
            "tool_context_trusted_read_directories": list(self._tool_context_trusted_read_directories),
            "tool_context_relative_read_directories": list(self._tool_context_relative_read_directories),
        }
        modified = modifier(current_ctx)
        self._allowed_tool_rules = modified.get("allowed_tool_rules", [])
        self._model_override = modified.get("model_override")
        self._effort_override = modified.get("effort_override")
        self._tool_context_trusted_read_directories = []
        self._tool_context_relative_read_directories = []
        _extend_unique(
            self._tool_context_trusted_read_directories,
            list(
                modified.get(
                    "tool_context_trusted_read_directories",
                    current_ctx["tool_context_trusted_read_directories"],
                )
                or []
            ),
        )
        _extend_unique(
            self._tool_context_relative_read_directories,
            list(
                modified.get(
                    "tool_context_relative_read_directories",
                    current_ctx["tool_context_relative_read_directories"],
                )
                or []
            ),
        )

    async def _auto_compact(self) -> CompactionEvent | None:
        """Perform automatic context compaction via provider."""
        from iac_code.services.telemetry import log_event
        from iac_code.services.telemetry.names import Events

        compaction_prompt = self.context_manager.build_compaction_prompt()
        if not compaction_prompt:
            return None
        started = time.monotonic()
        try:
            from iac_code.providers.base import Message as ProviderMessage

            response = await self._provider_manager.complete(
                messages=[ProviderMessage.user(compaction_prompt)],
                system="You are a helpful assistant that summarizes conversations concisely.",
            )
            self._record_response_usage(response)
            if response.text:
                original, new = self.context_manager.apply_compaction(response.text)
                self._sync_recall_suppression_from_context()
                self._persist_context_messages()
                duration_ms = int((time.monotonic() - started) * 1000)
                log_event(
                    Events.MEMORY_COMPACT_SUCCEEDED,
                    {
                        "rounds": 1,
                        "from_tokens": original,
                        "to_tokens": new,
                        "duration_ms": duration_ms,
                    },
                )
                return CompactionEvent(original_tokens=original, compacted_tokens=new, summary=response.text)
        except Exception as e:
            log_event(
                Events.MEMORY_COMPACT_FAILED,
                {
                    "rounds": 1,
                    "error_type": type(e).__name__,
                },
            )
            logger.error(f"Auto-compaction failed: {e}", exc_info=True)
        return None

    async def compact(self) -> CompactResult:
        """Manual compaction for /compact command."""
        if not self.context_manager.get_messages():
            return CompactResult(status="empty")
        compaction_prompt = self.context_manager.build_compaction_prompt()
        if not compaction_prompt:
            return CompactResult(
                status="too_short",
                preserve_recent_turns=self.context_manager.preserve_recent_turns,
            )
        try:
            from iac_code.providers.base import Message as ProviderMessage

            response = await self._provider_manager.complete(
                messages=[ProviderMessage.user(compaction_prompt)],
                system="You are a helpful assistant that summarizes conversations concisely.",
            )
            self._record_response_usage(response)
            if response.text:
                original, compacted = self.context_manager.apply_compaction(response.text)
                self._sync_recall_suppression_from_context()
                self._persist_context_messages()
                return CompactResult(
                    status="success",
                    original_tokens=original,
                    compacted_tokens=compacted,
                )
        except Exception as e:
            logger.error(f"Manual compaction failed: {e}", exc_info=True)
        return CompactResult(status="failed")

    def stamp_last_turn_elapsed(self, elapsed: float) -> None:
        """Record turn duration on the last assistant message and persist it."""
        msgs = self.context_manager.get_messages()
        for msg in reversed(msgs):
            if msg.role == "assistant":
                msg.elapsed_seconds = elapsed
                if self._session_storage:
                    self._session_storage.save(
                        self._cwd,
                        self._session_id,
                        msgs,
                        git_branch=self._current_git_branch,
                        preserve_cleanup_prompts=True,
                    )
                break

    def replace_session(self, session_id: str, resume_messages: list | None) -> None:
        """Swap the active session in-place, preserving provider/tools.

        Resets the conversation context to ``resume_messages`` (or empty),
        repoints the session id, and rebuilds the per-session ResultStorage
        directory. Used by the /resume command for in-process hot-swap.
        """
        from iac_code.config import get_config_dir

        self._cancel_pending_memory_prefetches()
        self._memory_recall_generation += 1
        self._last_provider_request_snapshot = None
        reset_provider_state = getattr(self._provider_manager, "reset_conversation_state", None)
        if callable(reset_provider_state):
            reset_provider_state()
        self._session_id = session_id
        self._root_session_id = session_id
        self._transcript_id = None
        self._has_session_hierarchy = False
        self._audit_log_path = None
        self._current_git_branch = None
        self._auto_loaded_skills.clear()
        self.context_manager.reset()
        if resume_messages:
            self.context_manager.load_messages(resume_messages)
        if self._session_usage_store.uses_direct_path_provider:
            self._session_usage_store = SessionUsageStore()
        self._session_usage_totals = self._session_usage_store.load(self._cwd, self._session_id)
        reset_recall_stats = getattr(self._memory_recall_service, "reset_stats", None)
        if callable(reset_recall_stats):
            reset_recall_stats()
        self._sync_recall_suppression_from_context()
        self._result_storage = ResultStorage(
            storage_dir=str(
                self._result_storage_dir_for_replaced_session(session_id)
                or Path(get_config_dir()) / "tool-results" / session_id
            ),
        )

    def _result_storage_dir_for_replaced_session(self, session_id: str) -> Path | None:
        session_dir_factory = getattr(self._session_storage, "v2_session_dir", None)
        if not callable(session_dir_factory):
            return None
        try:
            raw_session_dir = session_dir_factory(self._cwd, session_id)
        except (AttributeError, TypeError):
            return None
        if raw_session_dir is None:
            return None
        if not isinstance(raw_session_dir, (str, os.PathLike)):
            return None
        session_dir = Path(raw_session_dir)
        from iac_code.services.session_layout import SessionPaths, session_layout_version

        if session_layout_version(session_dir) is None:
            return None
        return SessionPaths.require_supported(session_dir).tool_results_dir

    def _refresh_git_branch(self) -> None:
        """Probe ``git`` once per turn and cache the result.

        Failures (no git, not a repo, timeout) silently leave the cache
        as ``None`` so the storage layer omits the field.
        """
        from iac_code.utils.project_paths import get_git_branch

        try:
            self._current_git_branch = get_git_branch(self._cwd)
        except Exception:
            self._current_git_branch = None

    def reset(self) -> None:
        self._cancel_pending_memory_prefetches()
        self._memory_recall_generation += 1
        self._last_provider_request_snapshot = None
        reset_provider_state = getattr(self._provider_manager, "reset_conversation_state", None)
        if callable(reset_provider_state):
            reset_provider_state()
        self._auto_loaded_skills.clear()
        self.context_manager.reset()
        reset_recall_stats = getattr(self._memory_recall_service, "reset_stats", None)
        if callable(reset_recall_stats):
            reset_recall_stats()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def max_turns(self) -> int:
        return self._max_turns

    def get_context_usage(self) -> dict:
        return self.context_manager.get_usage()

    def get_session_usage(self) -> SessionUsageTotals:
        return self._session_usage_totals.copy()

    def refresh_session_usage(self) -> None:
        self._session_usage_totals = self._session_usage_store.load(self._cwd, self._session_id)

    def _record_session_usage(self, usage: Usage) -> None:
        if not self._session_usage_totals.add(usage):
            return

        provider = self._get_runtime_provider_key()
        model = self._provider_manager.get_model_name() if hasattr(self._provider_manager, "get_model_name") else ""
        try:
            self._session_usage_store.append(
                self._cwd,
                self._session_id,
                usage,
                provider=provider,
                model=model,
            )
        except Exception as exc:
            logger.debug("Failed to persist session usage for {}: {}", self._session_id, exc)

    def _record_response_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if isinstance(usage, Usage):
            self._record_session_usage(usage)

    def _get_runtime_provider_key(self) -> str:
        if hasattr(self._provider_manager, "get_provider_key"):
            try:
                provider_key = self._provider_manager.get_provider_key()
            except Exception:
                pass
            else:
                if isinstance(provider_key, str):
                    return provider_key
        try:
            from iac_code.config import get_active_provider_key

            return get_active_provider_key() or ""
        except Exception:
            return ""
