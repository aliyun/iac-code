from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeAlias

import httpx
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Role, Task, TaskState, TaskStatus, TaskStatusUpdateEvent
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict

from iac_code.a2a.backup import backup_session_async, run_sync_fenced
from iac_code.a2a.events import (
    iac_code_session_metadata,
    make_text_part,
    publish_interactive_permission_boundary,
    publish_mcp_warnings,
    publish_permission_input_received,
    publish_stream_event,
    with_iac_code_session_metadata,
)
from iac_code.a2a.exposure import normalize_a2a_exposure_types
from iac_code.a2a.input_required import (
    PermissionInputRegistry,
    PermissionResponse,
    backup_permission_wait_checkpoint,
    parse_permission_response,
    permission_ack_message,
)
from iac_code.a2a.metadata_redaction import A2AMetadataEchoRedactor
from iac_code.a2a.metrics import A2AMetrics, NoOpA2AMetrics
from iac_code.a2a.parts import (
    allowed_cwd_roots,
    is_relative_to,
    parts_to_prompt,
    parts_to_user_input,
    resolve_workspace_path,
    trust_request_cwd,
)
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_executor import (
    RICH_CANDIDATE_PRESENTATION,
    IacCodeA2APipelineExecutor,
    recoverable_task_id_from_sidecar,
)
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import existing_a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import (
    A2APipelineSnapshotStore,
    reduce_pipeline_events,
    snapshot_needs_backup_commit_repair,
)
from iac_code.a2a.pipeline_stream import BACKUP_COMMITTED_EVENT_TYPE, PipelineA2AEventPublisher
from iac_code.a2a.projection import a2a_safe_mode_enabled
from iac_code.a2a.request_mode import resolve_request_run_mode
from iac_code.a2a.runtime_overrides import (
    a2a_request_context,
    configure_runtime_model,
    credentials_with_metadata_api_key,
    refresh_runtime_cloud_tools,
)
from iac_code.a2a.task_store import A2ATaskStore, _close_runtime
from iac_code.a2a.thinking_metadata import A2AThinkingMetadata
from iac_code.a2a.types import (
    TASK_STATE_CANCELED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_WORKING,
)
from iac_code.agent.message import ContentBlock
from iac_code.agent.message import Message as AgentMessage
from iac_code.commands.registry import PromptCommand
from iac_code.config import get_active_provider_key, get_provider_config, load_credentials
from iac_code.i18n import SUPPORTED_LANGUAGES, _
from iac_code.mcp.errors import MCPConnectionError
from iac_code.mcp.prompt_dispatch import is_mcp_prompt_file_path
from iac_code.pipeline.config import RunMode
from iac_code.pipeline.constants import (
    PIPELINE_EVENT_CLEANUP_COMPLETED,
    PIPELINE_EVENT_CLEANUP_FAILED,
    PIPELINE_EVENT_CLEANUP_PROGRESS,
    PIPELINE_EVENT_CLEANUP_STARTED,
)
from iac_code.pipeline.engine.cleanup import (
    CLEANUP_PROMPT_METADATA_TYPE,
    CleanupLedger,
    CleanupObserver,
    cleanup_prompt_ledger_path,
    create_cleanup_prompt_message,
    is_active_cleanup_prompt_message,
    mark_cleanup_prompt_message_completed,
)
from iac_code.pipeline.engine.user_input import PipelineUserInput, normalize_pipeline_user_input
from iac_code.providers.request_policy import ProviderRequestPolicy
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.services.capabilities.multimodal import is_model_multimodal
from iac_code.services.permission_wait import (
    PermissionWaitCheckpointStore,
    RecoveredPermissionAuditBoundary,
    canonical_digest,
    permission_execution_identity,
    recover_permission_audit_boundary,
)
from iac_code.services.permissions.audit import emit_permission_boundary_audit
from iac_code.services.providers.aliyun import DEFAULT_REGION, AliyunCredential
from iac_code.services.session_backup import (
    BackupReason,
    SessionBackupBlocked,
    SessionBackupService,
    SessionReconcileResult,
)
from iac_code.services.session_backup_state import (
    NORMAL_HANDOFF_PROOF_KEY,
    BackupPublicationProof,
    SessionBackupState,
)
from iac_code.services.session_storage import SessionStorage
from iac_code.services.telemetry.attributes import normalize_telemetry_channel
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    PermissionWaitSuspended,
    TextDeltaEvent,
)
from iac_code.utils.file_security import atomic_write_text, ensure_private_dir, ensure_private_file
from iac_code.utils.public_errors import sanitize_strict_text
from iac_code.utils.public_paths import build_public_path_roots

logger = logging.getLogger(__name__)
_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS = 1
_CANCEL_ACTIVE_TASK_DRAIN_TIMEOUT_SECONDS = 30
_ERROR_TEXT_MAX_CHARS = 1000
_DEFERRED_CLEANUP_PROMPTS_FILENAME = "cleanup-deferred-prompts.json"
_CLEANUP_ONLY_METADATA_KEY = "cleanupOnly"


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    raw = type(exc).__name__ if not message else f"{type(exc).__name__}: {message}"
    return raw[:_ERROR_TEXT_MAX_CHARS]


def _a2a_safe_mode_enabled() -> bool:
    return a2a_safe_mode_enabled()


A2APermissionResolver: TypeAlias = Callable[[Any], "bool | Awaitable[bool]"]


def _allowed_cwd_roots() -> list[Path]:
    return allowed_cwd_roots()


def _is_relative_to(path: Path, root: Path) -> bool:
    return is_relative_to(path, root)


def _cleanup_prompt_from_handoff(handoff: dict[str, Any]) -> str | None:
    data = handoff.get("data")
    if not isinstance(data, dict):
        return None
    cleanup = data.get("cleanup")
    if not isinstance(cleanup, dict):
        return None
    prompt = cleanup.get("prompt")
    return prompt if isinstance(prompt, str) and prompt else None


def _cleanup_ledger_path_from_handoff(handoff: dict[str, Any]) -> str | None:
    data = handoff.get("data")
    if not isinstance(data, dict):
        return None
    cleanup = data.get("cleanup")
    if not isinstance(cleanup, dict):
        return None
    path = cleanup.get("ledgerPath") or cleanup.get("ledger_path")
    return path if isinstance(path, str) and path else None


def _cleanup_payload_from_private_ledger_or_unavailable(
    *,
    ledger_path: Path,
) -> dict[str, Any]:
    ledger = CleanupLedger(ledger_path)
    try:
        ledger_exists = ledger_path.exists()
    except OSError:
        ledger_exists = False
    if not ledger_exists or ledger.load_failed():
        return {
            "status": "unavailable",
            "statusMessage": _("Cleanup state unavailable. Inspect the session file and cloud resources manually."),
        }
    prompt = ledger.build_pending_prompt()
    if prompt is None:
        return {"status": "completed", "resourceCount": 0}
    return {
        "status": "pending",
        "resourceCount": len(prompt.resources),
        "statusMessage": prompt.status_message,
        "prompt": prompt.prompt,
        "ledgerPath": str(ledger_path),
    }


def _session_has_user_message(
    messages: list[AgentMessage],
    *,
    content: str,
    metadata_type: str | None = None,
) -> bool:
    for message in messages:
        if getattr(message, "role", None) != "user" or getattr(message, "content", None) != content:
            continue
        if metadata_type is None:
            return True
        metadata = getattr(message, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("type") == metadata_type:
            return True
    return False


def _messages_have_cleanup_prompt(messages: list[Any]) -> bool:
    return any(_message_is_cleanup_prompt(message) for message in messages)


def _messages_have_active_cleanup_prompt(messages: list[Any]) -> bool:
    return any(is_active_cleanup_prompt_message(message) for message in messages)


def _session_has_active_cleanup_prompt_content(messages: list[AgentMessage], *, content: str) -> bool:
    for message in messages:
        if getattr(message, "role", None) != "user" or getattr(message, "content", None) != content:
            continue
        if is_active_cleanup_prompt_message(message):
            return True
    return False


def _message_is_cleanup_prompt(message: Any) -> bool:
    metadata = getattr(message, "metadata", None)
    return isinstance(metadata, dict) and metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE


def _cleanup_ledger_for_a2a_normal_chat(*, cwd: str, session_id: str) -> CleanupLedger | None:
    try:
        messages = SessionStorage().load(cwd, session_id)
    except Exception:
        logger.warning("Failed to inspect A2A session cleanup prompt", exc_info=True)
        messages = []
    has_active_cleanup_prompt = False
    for message in messages:
        if not is_active_cleanup_prompt_message(message):
            continue
        has_active_cleanup_prompt = True
        ledger_path = cleanup_prompt_ledger_path(message)
        if ledger_path:
            return CleanupLedger(ledger_path)
    try:
        path = SessionStorage().session_dir(cwd, session_id) / "pipeline" / "cleanup.yaml"
    except Exception:
        logger.warning("Failed to locate A2A pipeline cleanup ledger", exc_info=True)
        return None
    if not path.exists():
        return None
    ledger = CleanupLedger(path)
    if has_active_cleanup_prompt:
        return ledger
    if ledger.load_failed():
        return None
    return ledger if ledger.pending_resources() else None


def _cleanup_only_summary(ledger: CleanupLedger | None) -> dict[str, Any]:
    """Return a bounded, non-secret result for an internal cleanup-only turn."""
    if ledger is None:
        return {"requested": True, "status": "unavailable", "resourceCount": 0, "resources": []}
    if ledger.load_failed():
        return {"requested": True, "status": "unavailable", "resourceCount": 0, "resources": []}
    try:
        resources = ledger.cleanup_resources()
        pending = ledger.pending_resources()
    except Exception:
        logger.warning("Failed to summarize A2A cleanup-only result", exc_info=True)
        return {"requested": True, "status": "unavailable", "resourceCount": 0, "resources": []}

    pending_statuses = {str(getattr(resource, "cleanup_status", "") or "pending") for resource in pending}
    if not pending:
        status = "completed"
    elif pending_statuses and pending_statuses <= {"failed"}:
        status = "failed"
    else:
        status = "pending"
    public_resources = []
    for resource in resources[:24]:
        item = {
            "provider": getattr(resource, "provider", None),
            "resourceType": getattr(resource, "resource_type", None),
            "resourceId": getattr(resource, "resource_id", None),
            "resourceName": getattr(resource, "resource_name", None),
            "regionId": getattr(resource, "region_id", None),
            "sourceStepId": getattr(resource, "source_step_id", None),
            "cleanupStatus": getattr(resource, "cleanup_status", None),
            "progressStatus": getattr(resource, "progress_status", None),
        }
        public_resources.append({key: value for key, value in item.items() if value is not None})
    return {
        "requested": True,
        "status": status,
        "resourceCount": len(pending),
        "resources": public_resources,
    }


def _default_cleanup_ledger_path(*, cwd: str, session_id: str) -> Path:
    return SessionStorage().session_dir(cwd, session_id) / "pipeline" / "cleanup.yaml"


def _ensure_cleanup_prompt_in_session(*, cwd: str, session_id: str, ledger: CleanupLedger, runtime: Any) -> None:
    cleanup_prompt = ledger.build_pending_prompt()
    if cleanup_prompt is None:
        return
    message = create_cleanup_prompt_message(
        cleanup_prompt.prompt,
        cleanup_ledger_path=ledger.path,
        cleanup_status="pending",
    )
    session_storage = SessionStorage()
    messages = session_storage.load(cwd, session_id)
    if _session_has_active_cleanup_prompt_content(
        messages,
        content=cleanup_prompt.prompt,
    ):
        _ensure_cleanup_prompt_in_runtime(runtime=runtime, message=message)
        return
    session_storage.append(cwd, session_id, message)
    ledger.record_prompt_queued(cleanup_prompt, ui_surface="a2a")
    _ensure_cleanup_prompt_in_runtime(runtime=runtime, message=message)


def _ensure_cleanup_prompt_in_runtime(*, runtime: Any, message: AgentMessage) -> None:
    context_manager = getattr(getattr(runtime, "agent_loop", None), "context_manager", None)
    remover = getattr(context_manager, "remove_cleanup_prompt_messages", None)
    add_raw_message = getattr(context_manager, "add_raw_message", None)
    if not callable(add_raw_message):
        return
    if callable(remover):
        try:
            remover()
        except Exception:
            logger.warning("Failed to replace A2A cleanup prompt in runtime context", exc_info=True)
    try:
        add_raw_message(message.to_dict())
    except Exception:
        logger.warning("Failed to inject A2A cleanup prompt into runtime context", exc_info=True)


def _runtime_has_cleanup_prompt(runtime: Any) -> bool:
    context_manager = getattr(getattr(runtime, "agent_loop", None), "context_manager", None)
    get_messages = getattr(context_manager, "get_messages", None)
    if not callable(get_messages):
        return False
    try:
        messages = get_messages()
    except Exception:
        return False
    return isinstance(messages, list) and _messages_have_active_cleanup_prompt(messages)


def _session_has_cleanup_prompt(*, cwd: str, session_id: str) -> bool:
    try:
        messages = SessionStorage().load(cwd, session_id)
    except Exception:
        logger.warning("Failed to inspect A2A session cleanup prompt", exc_info=True)
        return False
    return _messages_have_active_cleanup_prompt(messages)


def _a2a_cleanup_prompt_exists(*, runtime: Any, cwd: str, session_id: str) -> bool:
    return _runtime_has_cleanup_prompt(runtime) or _session_has_cleanup_prompt(cwd=cwd, session_id=session_id)


def _a2a_cleanup_ledger_unavailable(
    ledger: CleanupLedger | None,
    *,
    runtime: Any,
    cwd: str,
    session_id: str,
) -> bool:
    if not _a2a_cleanup_prompt_exists(runtime=runtime, cwd=cwd, session_id=session_id):
        return False
    if ledger is None:
        return True
    try:
        if not ledger.path.exists():
            return True
    except Exception:
        return True
    return ledger.load_failed()


def _a2a_deferred_cleanup_prompts_path(*, cwd: str, session_id: str) -> Path:
    return SessionStorage().session_dir(cwd, session_id) / "a2a" / _DEFERRED_CLEANUP_PROMPTS_FILENAME


def _read_a2a_deferred_cleanup_prompts(*, cwd: str, session_id: str) -> tuple[list[str], bool]:
    path = _a2a_deferred_cleanup_prompts_path(cwd=cwd, session_id=session_id)
    if not path.exists():
        return [], False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to load deferred A2A cleanup prompts", exc_info=True)
        return [], True
    raw_prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(raw_prompts, list):
        raw_prompt = data.get("prompt") if isinstance(data, dict) else None
        raw_prompts = [raw_prompt] if isinstance(raw_prompt, str) else []
    return [prompt for prompt in raw_prompts if isinstance(prompt, str) and prompt.strip()], False


def _load_a2a_deferred_cleanup_prompts(*, cwd: str, session_id: str) -> list[str]:
    prompts, _load_failed = _read_a2a_deferred_cleanup_prompts(cwd=cwd, session_id=session_id)
    return prompts


def _save_a2a_deferred_cleanup_prompts(*, cwd: str, session_id: str, prompts: list[str]) -> None:
    path = _a2a_deferred_cleanup_prompts_path(cwd=cwd, session_id=session_id)
    if not prompts:
        _clear_a2a_deferred_cleanup_prompts(cwd=cwd, session_id=session_id)
        return
    try:
        ensure_private_dir(path.parent)
        atomic_write_text(
            path,
            json.dumps({"prompts": prompts}, ensure_ascii=False, sort_keys=True),
        )
        ensure_private_file(path)
    except OSError:
        logger.warning("Failed to persist deferred A2A cleanup prompt", exc_info=True)


def _append_a2a_deferred_cleanup_prompt(*, cwd: str, session_id: str, prompt: str) -> bool:
    prompt = prompt.strip()
    if not prompt:
        return True
    prompts, load_failed = _read_a2a_deferred_cleanup_prompts(cwd=cwd, session_id=session_id)
    if load_failed:
        return False
    if prompts and _is_cleanup_continue_prompt(prompt):
        prompts = [prompts[-1]]
    else:
        prompts = [prompt]
    _save_a2a_deferred_cleanup_prompts(cwd=cwd, session_id=session_id, prompts=prompts)
    return True


def _clear_a2a_deferred_cleanup_prompts(*, cwd: str, session_id: str) -> None:
    path = _a2a_deferred_cleanup_prompts_path(cwd=cwd, session_id=session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Failed to clear deferred A2A cleanup prompts", exc_info=True)


def _a2a_prompts_after_cleanup(*, cwd: str, session_id: str, prompt: str) -> tuple[list[str], bool] | None:
    deferred_prompts, load_failed = _read_a2a_deferred_cleanup_prompts(cwd=cwd, session_id=session_id)
    if load_failed:
        return None
    if not deferred_prompts:
        return [prompt], False
    if prompt.strip():
        if not _append_a2a_deferred_cleanup_prompt(cwd=cwd, session_id=session_id, prompt=prompt):
            return None
        deferred_prompts, load_failed = _read_a2a_deferred_cleanup_prompts(cwd=cwd, session_id=session_id)
        if load_failed:
            return None
    return deferred_prompts, True


def _is_cleanup_continue_prompt(prompt: str) -> bool:
    normalized = prompt.strip().lower()
    return normalized in {"continue", "继续"}


def _a2a_pipeline_state_for_session(
    *,
    cwd: str,
    session_id: str,
) -> tuple[A2APipelineSnapshotStore, A2APipelineJournal, dict[str, Any], list[dict[str, Any]]] | None:
    try:
        pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
        snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
        journal = A2APipelineJournal(pipeline_dir)
        snapshot = snapshot_store.load()
    except Exception as exc:
        logger.warning(
            "Failed to load A2A pipeline snapshot error_type=%s",
            type(exc).__name__,
        )
        return None
    try:
        journal_events = journal.read_all_repairing_tail()
    except Exception as exc:
        # Route decisions must be conservative: a stale snapshot can expose an obsolete normalHandoff.
        logger.warning(
            "Failed to read A2A pipeline journal error_type=%s",
            type(exc).__name__,
        )
        return None

    snapshot_sequence = _a2a_pipeline_sequence_number(snapshot.get("lastSequence")) if isinstance(snapshot, dict) else 0
    journal_sequence = max(
        (_a2a_pipeline_sequence_number(event.get("sequence")) for event in journal_events if isinstance(event, dict)),
        default=0,
    )
    needs_backup_commit_repair = isinstance(snapshot, dict) and snapshot_needs_backup_commit_repair(
        snapshot, journal_events
    )
    if journal_events and (
        not isinstance(snapshot, dict) or journal_sequence != snapshot_sequence or needs_backup_commit_repair
    ):
        snapshot = reduce_pipeline_events(journal_events)
        if not isinstance(snapshot, dict):
            return None
        try:
            snapshot_store.save(snapshot)
        except Exception as exc:
            logger.debug("Failed to save repaired A2A pipeline snapshot error_type=%s", type(exc).__name__)
    elif not isinstance(snapshot, dict):
        return None
    return snapshot_store, journal, snapshot, journal_events


def _a2a_pipeline_sequence_number(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _prune_completed_cleanup_prompt_from_runtime(runtime: Any, ledger: CleanupLedger | None) -> None:
    if ledger is None and _runtime_has_cleanup_prompt(runtime):
        logger.warning("Keeping A2A cleanup prompt because cleanup ledger is unavailable")
        return
    if ledger is not None and ledger.load_failed():
        logger.warning("Keeping A2A cleanup prompt because cleanup ledger is unreadable")
        return
    if ledger is not None and not ledger.path.exists() and _runtime_has_cleanup_prompt(runtime):
        logger.warning("Keeping A2A cleanup prompt because cleanup ledger is unavailable")
        return
    if ledger is not None and ledger.pending_resources():
        return
    context_manager = getattr(getattr(runtime, "agent_loop", None), "context_manager", None)
    remover = getattr(context_manager, "remove_cleanup_prompt_messages", None)
    if not callable(remover):
        return
    try:
        remover()
    except Exception:
        logger.warning("Failed to remove completed A2A cleanup prompt from context", exc_info=True)


def _mark_completed_cleanup_prompts(
    *,
    runtime: Any,
    cwd: str,
    session_id: str,
    ledger: CleanupLedger,
) -> None:
    ledger_path = getattr(ledger, "path", None)
    context_manager = getattr(getattr(runtime, "agent_loop", None), "context_manager", None)
    get_messages = getattr(context_manager, "get_messages", None)
    if callable(get_messages):
        try:
            messages = get_messages()
        except Exception:
            messages = []
        if isinstance(messages, list):
            for message in messages:
                mark_cleanup_prompt_message_completed(message, cleanup_ledger_path=ledger_path)

    session_storage = SessionStorage()
    try:
        messages = session_storage.load(cwd, session_id)
    except Exception:
        logger.warning("Failed to load A2A session while marking cleanup prompt completed", exc_info=True)
        return
    changed = False
    for message in messages:
        changed = mark_cleanup_prompt_message_completed(message, cleanup_ledger_path=ledger_path) or changed
    if not changed:
        return
    try:
        session_storage.save(cwd, session_id, messages)
    except Exception:
        logger.warning("Failed to mark A2A cleanup prompt completed in session", exc_info=True)


def _cleanup_publisher_for_a2a_normal_chat(
    *,
    event_queue: EventQueue,
    cwd: str,
    session_id: str,
    task_id: str,
    context_id: str,
    artifact_store: Any | None,
    exposure_types: Any,
) -> PipelineA2AEventPublisher | None:
    state = _a2a_pipeline_state_for_session(cwd=cwd, session_id=session_id)
    if state is None:
        return None
    snapshot_store, journal, snapshot, journal_events = state

    translator = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id=_string_value(snapshot.get("pipelineRunId")) or context_id,
            task_id=_string_value(snapshot.get("taskId")) or task_id,
            context_id=_string_value(snapshot.get("contextId")) or context_id,
            pipeline_name=_string_value(snapshot.get("pipelineName")) or "pipeline",
        )
    )
    try:
        if journal_events is None:
            journal_events = journal.read_all_repairing_tail()
        translator.hydrate_from_events(journal_events)
    except Exception:
        logger.warning("Failed to hydrate A2A cleanup event translator", exc_info=True)
    return PipelineA2AEventPublisher(
        event_queue,
        translator,
        journal,
        snapshot_store,
        artifact_store=artifact_store,
        exposure_types=exposure_types,
        delivery_task_id=task_id,
        delivery_context_id=context_id,
    )


async def _observe_cleanup_stream(
    events: AsyncIterator[Any],
    ledger: CleanupLedger,
    *,
    publisher: PipelineA2AEventPublisher | None = None,
) -> AsyncIterator[Any]:
    if ledger.load_failed():
        async for event in events:
            yield event
        return
    observer = CleanupObserver(ledger)
    previous = (
        _published_cleanup_resource_states(publisher, ledger)
        if publisher is not None
        else _cleanup_resource_states(ledger)
    )
    if publisher is not None:
        previous = await _publish_cleanup_resource_changes(publisher, ledger, previous)
    async for event in events:
        observer.observe(event)
        if publisher is not None:
            previous = await _publish_cleanup_resource_changes(publisher, ledger, previous)
        yield event


def _cleanup_resource_state(resource: Any) -> tuple[Any, ...]:
    return (
        getattr(resource, "cleanup_status", None),
        getattr(resource, "progress_status", None),
        getattr(resource, "progress_percentage", None),
        getattr(resource, "cleanup_tool_use_id", None),
        getattr(resource, "last_error", None),
    )


def _cleanup_resource_states(ledger: CleanupLedger) -> dict[str, tuple[Any, ...]]:
    return {resource.key: _cleanup_resource_state(resource) for resource in ledger.cleanup_resources()}


def _published_cleanup_resource_states(
    publisher: PipelineA2AEventPublisher,
    ledger: CleanupLedger,
) -> dict[str, tuple[Any, ...]]:
    snapshot_store = getattr(publisher, "snapshot_store", None)
    load = getattr(snapshot_store, "load", None)
    if not callable(load):
        return {}
    try:
        snapshot = load()
    except Exception:
        logger.warning("Failed to load A2A cleanup snapshot state for catch-up", exc_info=True)
        return {}
    if not isinstance(snapshot, dict):
        return {}
    cleanup = snapshot.get("cleanup")
    if not isinstance(cleanup, dict):
        return {}
    snapshot_resources = [item for item in cleanup.get("resources", []) if isinstance(item, dict)]
    states: dict[str, tuple[Any, ...]] = {}
    for resource in ledger.cleanup_resources():
        match = _matching_snapshot_cleanup_resource(resource, snapshot_resources)
        if match is not None:
            states[resource.key] = _snapshot_cleanup_resource_state(match)
    return states


def _matching_snapshot_cleanup_resource(resource: Any, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for candidate in candidates:
        if candidate.get("resourceId") != getattr(resource, "resource_id", None):
            continue
        if not _optional_cleanup_field_matches(candidate.get("regionId"), getattr(resource, "region_id", None)):
            continue
        if not _optional_cleanup_field_matches(candidate.get("provider"), getattr(resource, "provider", None)):
            continue
        resource_type = candidate.get("resourceType") or candidate.get("resource_type")
        if not _optional_cleanup_field_matches(resource_type, getattr(resource, "resource_type", None)):
            continue
        return candidate
    return None


def _optional_cleanup_field_matches(snapshot_value: Any, ledger_value: Any) -> bool:
    snapshot_text = snapshot_value if isinstance(snapshot_value, str) and snapshot_value else None
    ledger_text = ledger_value if isinstance(ledger_value, str) and ledger_value else None
    return snapshot_text is None or ledger_text is None or snapshot_text == ledger_text


def _snapshot_cleanup_resource_state(resource: dict[str, Any]) -> tuple[Any, ...]:
    return (
        resource.get("cleanupStatus") or resource.get("cleanup_status") or resource.get("status"),
        resource.get("progressStatus") or resource.get("stackStatus"),
        resource.get("progressPercentage"),
        resource.get("cleanupToolUseId") or resource.get("cleanup_tool_use_id"),
        resource.get("lastError"),
    )


async def _publish_cleanup_resource_changes(
    publisher: PipelineA2AEventPublisher,
    ledger: CleanupLedger,
    previous: dict[str, tuple[Any, ...]],
) -> dict[str, tuple[Any, ...]]:
    resources = ledger.cleanup_resources()
    current = {resource.key: _cleanup_resource_state(resource) for resource in resources}
    next_previous = dict(previous)
    for resource in resources:
        state = current.get(resource.key)
        if state is None or previous.get(resource.key) == state:
            continue
        event_type = _cleanup_event_type_for_status(resource.cleanup_status)
        if event_type is None:
            continue
        try:
            published = await publisher.publish_manual(
                event_type,
                "cleanup",
                status="working",
                data=_cleanup_resource_event_data(resource, resource_count=len(resources)),
                require_durable_metadata=True,
            )
        except Exception:
            logger.warning("Failed to publish A2A cleanup progress event", exc_info=True)
            continue
        if published is not None:
            next_previous[resource.key] = state
    return next_previous


def _cleanup_event_type_for_status(status: str) -> str | None:
    if status == "started":
        return PIPELINE_EVENT_CLEANUP_STARTED
    if status == "in_progress":
        return PIPELINE_EVENT_CLEANUP_PROGRESS
    if status == "completed":
        return PIPELINE_EVENT_CLEANUP_COMPLETED
    if status == "failed":
        return PIPELINE_EVENT_CLEANUP_FAILED
    return None


def _cleanup_resource_event_data(resource: Any, *, resource_count: int) -> dict[str, Any]:
    data = {
        "status": getattr(resource, "cleanup_status", None),
        "resourceCount": resource_count,
        "provider": getattr(resource, "provider", None),
        "resourceType": getattr(resource, "resource_type", None),
        "resourceId": getattr(resource, "resource_id", None),
        "resourceName": getattr(resource, "resource_name", None),
        "regionId": getattr(resource, "region_id", None),
        "sourceStepId": getattr(resource, "source_step_id", None),
        "cleanupStatus": getattr(resource, "cleanup_status", None),
        "cleanupToolUseId": getattr(resource, "cleanup_tool_use_id", None),
        "progressStatus": getattr(resource, "progress_status", None),
        "progressPercentage": getattr(resource, "progress_percentage", None),
        "stackStatus": getattr(resource, "progress_status", None),
        "lastError": _public_cleanup_error(getattr(resource, "last_error", None)),
    }
    return {key: value for key, value in data.items() if value is not None}


def _public_cleanup_error(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:_ERROR_TEXT_MAX_CHARS] + "..." if len(text) > _ERROR_TEXT_MAX_CHARS else text


async def _stream_a2a_normal_events(
    *,
    runtime: Any,
    prompt: str | list[ContentBlock],
    prompt_text: str,
    cleanup_ledger: CleanupLedger | None,
    cleanup_publisher: PipelineA2AEventPublisher | None,
    cleanup_only: bool,
    cwd: str,
    session_id: str,
) -> AsyncIterator[Any]:
    if _a2a_cleanup_ledger_unavailable(cleanup_ledger, runtime=runtime, cwd=cwd, session_id=session_id):
        if not cleanup_only and not _append_a2a_deferred_cleanup_prompt(
            cwd=cwd,
            session_id=session_id,
            prompt=prompt_text,
        ):
            yield TextDeltaEvent(
                text=_("Rollback cleanup deferred prompt state is unavailable. Please repair it before continuing.")
            )
            return
        yield TextDeltaEvent(
            text=_("Rollback cleanup state is unavailable. Please repair the cleanup ledger before continuing.")
        )
        return

    if cleanup_ledger is not None and cleanup_ledger.load_failed():
        if _runtime_has_cleanup_prompt(runtime) or _session_has_cleanup_prompt(cwd=cwd, session_id=session_id):
            if not cleanup_only and not _append_a2a_deferred_cleanup_prompt(
                cwd=cwd,
                session_id=session_id,
                prompt=prompt_text,
            ):
                yield TextDeltaEvent(
                    text=_("Rollback cleanup deferred prompt state is unavailable. Please repair it before continuing.")
                )
                return
            yield TextDeltaEvent(
                text=_("Rollback cleanup state is unavailable. Please repair the cleanup ledger before continuing.")
            )
            return

    run_cleanup_continuation = (
        cleanup_ledger is not None
        and not cleanup_ledger.load_failed()
        and bool(cleanup_ledger.pending_resources())
        and callable(getattr(runtime.agent_loop, "continue_streaming", None))
    )
    if run_cleanup_continuation and cleanup_ledger is not None:
        _ensure_cleanup_prompt_in_session(cwd=cwd, session_id=session_id, ledger=cleanup_ledger, runtime=runtime)
        cleanup_stream = _observe_cleanup_stream(
            runtime.agent_loop.continue_streaming(),
            cleanup_ledger,
            publisher=cleanup_publisher,
        )
        async for event in cleanup_stream:
            yield event
        if cleanup_ledger.pending_resources():
            if cleanup_only:
                yield TextDeltaEvent(
                    text=_("Rollback cleanup is still in progress. Please continue after cleanup completes.")
                )
                return
            if not _append_a2a_deferred_cleanup_prompt(cwd=cwd, session_id=session_id, prompt=prompt_text):
                yield TextDeltaEvent(
                    text=_("Rollback cleanup deferred prompt state is unavailable. Please repair it before continuing.")
                )
                return
            yield TextDeltaEvent(
                text=_("Rollback cleanup is still in progress. Please continue after cleanup completes.")
            )
            return
        _mark_completed_cleanup_prompts(runtime=runtime, cwd=cwd, session_id=session_id, ledger=cleanup_ledger)
        _prune_completed_cleanup_prompt_from_runtime(runtime, cleanup_ledger)

    if cleanup_only:
        # This A2A turn exists only to drain rollback cleanup after a proven
        # Pipeline-to-normal handoff. Never consume a synthetic prompt or
        # generate an unrelated normal-chat answer.
        return

    prompts_after_cleanup = _a2a_prompts_after_cleanup(cwd=cwd, session_id=session_id, prompt=prompt_text)
    if prompts_after_cleanup is None:
        yield TextDeltaEvent(
            text=_("Rollback cleanup deferred prompt state is unavailable. Please repair it before continuing.")
        )
        return
    deferred_prompts, has_deferred_prompts = prompts_after_cleanup
    prompts_to_run: list[str | list[ContentBlock]] = []
    if has_deferred_prompts:
        prompts_to_run.extend(deferred_prompts)
    else:
        prompts_to_run.append(prompt)
    for prompt_to_run in prompts_to_run:
        prompt_stream = await _a2a_mcp_prompt_command_stream(
            runtime=runtime,
            prompt=prompt_to_run,
            session_id=session_id,
        )
        if prompt_stream is None:
            prompt_stream = runtime.agent_loop.run_streaming(prompt_to_run)
        if cleanup_ledger is not None:
            prompt_stream = _observe_cleanup_stream(prompt_stream, cleanup_ledger, publisher=cleanup_publisher)
        async for event in prompt_stream:
            yield event
    if cleanup_ledger is not None and not cleanup_ledger.load_failed() and not cleanup_ledger.pending_resources():
        _mark_completed_cleanup_prompts(runtime=runtime, cwd=cwd, session_id=session_id, ledger=cleanup_ledger)
        _prune_completed_cleanup_prompt_from_runtime(runtime, cleanup_ledger)
    if has_deferred_prompts:
        _clear_a2a_deferred_cleanup_prompts(cwd=cwd, session_id=session_id)


def _lookup_a2a_mcp_prompt_command(runtime: Any, prompt: str | list[ContentBlock]) -> tuple[PromptCommand, str] | None:
    if not isinstance(prompt, str):
        return None
    stripped = prompt.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(None, 1)
    if not parts or not parts[0]:
        return None
    command_registry = getattr(runtime, "command_registry", None)
    if command_registry is None:
        return None
    name = parts[0]
    command = command_registry.get(name) or command_registry.get(name.lower())
    if not isinstance(command, PromptCommand):
        return None
    skill = command.skill
    file_path = str(getattr(skill, "file_path", "") if skill is not None else "")
    if not is_mcp_prompt_file_path(file_path):
        return None
    args = parts[1] if len(parts) > 1 else ""
    return command, args


async def _a2a_mcp_prompt_command_stream(
    *,
    runtime: Any,
    prompt: str | list[ContentBlock],
    session_id: str,
) -> AsyncIterator[Any] | None:
    match = _lookup_a2a_mcp_prompt_command(runtime, prompt)
    if match is None:
        return None
    command, args = match
    from iac_code.skills.processor import process_prompt_command

    result = await process_prompt_command(command, args, session_id=session_id)
    if result.is_fork:
        return runtime.agent_loop.run_streaming(result.prompt_content)

    injected = False
    context_manager = getattr(runtime.agent_loop, "context_manager", None)
    add_raw_message = getattr(context_manager, "add_raw_message", None)
    if callable(add_raw_message):
        for message in result.new_messages:
            injected_message = add_raw_message(message)
            _persist_a2a_injected_prompt_message(runtime.agent_loop, injected_message)
            injected = True

    if result.context_modifier:
        apply_context_modifier = getattr(runtime.agent_loop, "_apply_context_modifier", None)
        if callable(apply_context_modifier):
            apply_context_modifier(result.context_modifier)

    continue_streaming = getattr(runtime.agent_loop, "continue_streaming", None)
    if injected and callable(continue_streaming):
        return continue_streaming()
    return runtime.agent_loop.run_streaming(result.prompt_content)


def _persist_a2a_injected_prompt_message(agent_loop: Any, message: Any) -> None:
    session_storage = getattr(agent_loop, "_session_storage", None)
    if session_storage is None or message is None:
        return
    append = getattr(session_storage, "append", None)
    if not callable(append):
        return
    cwd = getattr(agent_loop, "_cwd", None)
    session_id = getattr(agent_loop, "_session_id", None)
    if not cwd or not session_id:
        return
    append(
        cwd,
        session_id,
        message,
        git_branch=getattr(agent_loop, "_current_git_branch", None),
    )


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) and value else ""


class IacCodeA2AExecutor(AgentExecutor):
    def __init__(
        self,
        *,
        task_store: A2ATaskStore,
        model: str,
        metrics: A2AMetrics | None = None,
        artifact_store: Any | None = None,
        push_notifier: Any | None = None,
        permission_resolver: A2APermissionResolver | None = None,
        permission_input_registry: PermissionInputRegistry | None = None,
        auto_approve_permissions: bool = False,
        permission_wait_policy: Any | None = None,
        thinking_exposure_types: Any = None,
        backup_service: Any | None = None,
    ) -> None:
        self._task_store = task_store
        self._model = model
        self._metrics = metrics or NoOpA2AMetrics()
        self._artifact_store = artifact_store
        self._push_notifier = push_notifier
        self._permission_resolver = permission_resolver
        self._permission_input_registry = permission_input_registry or PermissionInputRegistry()
        self._auto_approve_permissions = auto_approve_permissions
        from iac_code.services.permission_wait import PermissionWaitCoordinator, PermissionWaitPolicy

        self._permission_wait_policy = permission_wait_policy or PermissionWaitPolicy()
        self._permission_wait_coordinator = PermissionWaitCoordinator(self._permission_wait_policy)
        self._permission_input_registry.set_permission_wait_coordinator(self._permission_wait_coordinator)
        self._task_store.set_permission_wait_active_probe(self._permission_wait_coordinator.has_live_owners)
        self._thinking_exposure_types = normalize_a2a_exposure_types(thinking_exposure_types)
        self._metadata_echo_redactor = A2AMetadataEchoRedactor()
        self._backup_service = backup_service or SessionBackupService()

    async def resolve_sideband_permission(self, response: PermissionResponse) -> Message | None:
        if not await self._permission_input_registry.is_sideband_response(response):
            return None
        approved = await self._permission_input_registry.answer(response)
        return permission_ack_message(response, approved=approved)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        metadata = getattr(context, "metadata", None) or getattr(getattr(context, "message", None), "metadata", None)
        permission_response = parse_permission_response(getattr(context, "message", None))
        context_id = (
            context.context_id
            or (permission_response.context_id if permission_response is not None else None)
            or "ctx-" + uuid.uuid4().hex[:12]
        )
        telemetry_channel = await self._task_store.resolve_context_telemetry_channel(
            context_id,
            self._resolve_telemetry_channel(metadata),
        )
        with a2a_request_context(telemetry_channel=telemetry_channel):
            await self._execute(context, event_queue, context_id=context_id)

    async def _execute(self, context: RequestContext, event_queue: EventQueue, *, context_id: str) -> None:
        requested_task_id = context.task_id or None
        task_id = requested_task_id or "task-" + uuid.uuid4().hex[:12]
        permission_response = parse_permission_response(getattr(context, "message", None))
        if permission_response is not None:
            try:
                pending = await self._permission_input_registry.pending_for_response(permission_response)
                approved = await self._permission_input_registry.answer(permission_response)
            except InvalidParamsError:
                if await self._resume_persisted_permission(
                    context,
                    event_queue,
                    response=permission_response,
                ):
                    return
                raise
            if pending.state == "suspended_decision_claimed":
                if pending.boundary_id is not None:
                    owner_released = await self._permission_wait_coordinator.wait_for_suspended_owner(
                        pending.boundary_id
                    )
                    if not owner_released:
                        await self._publish_status(
                            event_queue,
                            task_id=permission_response.task_id,
                            context_id=permission_response.context_id,
                            state=TaskState.TASK_STATE_WORKING,
                            metadata={
                                "iac_code": {
                                    "permissionAck": {
                                        "schemaVersion": 1,
                                        "kind": "permission_ack",
                                        "inputId": permission_response.input_id,
                                        "toolUseId": permission_response.tool_use_id,
                                        "decision": "allow_once" if approved else "deny",
                                        "accepted": True,
                                        "recoveryPending": True,
                                    },
                                    "permissionWait": {"status": "suspending", "resumable": True},
                                }
                            },
                        )
                        # Keep this single correlated response alive while the
                        # old owner finishes cleanup.  Each wait is bounded and
                        # holds no resolution/file lock; once owner completion
                        # arrives this same request performs the one recovery,
                        # so the user never has to repeat an accepted decision.
                        while not await self._permission_wait_coordinator.wait_for_suspended_owner(pending.boundary_id):
                            pass
                await self._permission_input_registry.complete(pending)
                if await self._resume_persisted_permission(
                    context,
                    event_queue,
                    response=permission_response,
                ):
                    return
                raise InvalidParamsError("permission_resume_invalid: suspended permission is unavailable.")
            continuation = await self._permission_input_registry.claim_continuation(pending)
            if continuation is not None:
                await publish_permission_input_received(
                    event_queue,
                    pending=pending,
                    iac_code_session_id=None,
                )
                await continuation(event_queue, pending)
                return
            await self._publish_status(
                event_queue,
                task_id=permission_response.task_id,
                context_id=permission_response.context_id,
                state=TaskState.TASK_STATE_WORKING,
                metadata={
                    "iac_code": {
                        "inputReceived": {
                            "kind": "permission",
                            "inputId": permission_response.input_id,
                            "toolUseId": permission_response.tool_use_id,
                            "decision": "allow_once" if approved else "deny",
                        },
                        "permissionAck": {
                            "schemaVersion": 1,
                            "kind": "permission_ack",
                            "inputId": permission_response.input_id,
                            "toolUseId": permission_response.tool_use_id,
                            "decision": "allow_once" if approved else "deny",
                            "accepted": True,
                        },
                    }
                },
            )
            # A live top-level Pipeline reply is delivered on a separate
            # StartChat/A2A reentry stream while progress remains owned by the
            # parent stream.  Return the same compact acknowledgement used by
            # sideband permissions so that the reentry caller can prove its
            # correlated decision was accepted without taking over progress.
            return
        task = None
        initial_task_published = False
        public_path_roots: list[dict[str, str]] | None = None
        context_execution_token: str | None = None
        active_pipeline_owner: asyncio.Task[Any] | None = None

        async def publish_initial_task_if_missing() -> None:
            nonlocal initial_task_published
            if initial_task_published or isinstance(getattr(context, "current_task", None), Task):
                return
            await self._publish_initial_task(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                context=context,
                public_path_roots=public_path_roots,
            )
            initial_task_published = True

        async def release_context_execution() -> None:
            nonlocal context_execution_token
            if context_execution_token is None:
                return
            token = context_execution_token
            context_execution_token = None
            await self._task_store.end_context_execution(context_id, token)

        try:
            metadata = getattr(context, "metadata", None) or getattr(
                getattr(context, "message", None), "metadata", None
            )
            cwd = self._resolve_cwd(metadata)
            public_path_roots = build_public_path_roots(cwd=cwd)
            pipeline_mode = resolve_request_run_mode(metadata) == RunMode.PIPELINE
            if pipeline_mode and requested_task_id:
                reservation = await self._task_store.begin_context_execution_if_task_active(
                    context_id,
                    requested_task_id,
                )
                if reservation is not None:
                    context_execution_token, active_pipeline_owner = reservation
            if context_execution_token is None:
                try:
                    (
                        context_execution_token,
                        _reconcile_result,
                    ) = await self._task_store.begin_context_execution_after_reconciliation(
                        context_id,
                        lambda: self._reconcile_session_before_route_locked(context_id=context_id, cwd=cwd),
                        wait_timeout=_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS,
                    )
                except TimeoutError as exc:
                    raise ValueError(_("Task is already working.")) from exc
            user_id = self._resolve_user_id(metadata)
            preferred_language = self._resolve_preferred_language(metadata)
            candidate_presentation = self._resolve_candidate_presentation(metadata)
            cleanup_only = self._resolve_cleanup_only(metadata)
            metadata_model = self._resolve_model(metadata)
            metadata_api_key = self._resolve_api_key(metadata)
            request_policy_override = self._resolve_request_policy(metadata)
            model = metadata_model or self._model
            aliyun_credential = self._resolve_aliyun_credential(metadata)
            route_pipeline_handoff_to_normal = False
            if pipeline_mode:
                route_pipeline_handoff_to_normal = await self._should_route_pipeline_handoff_to_normal(
                    context_id=context_id,
                    cwd=cwd,
                )
            if cleanup_only and pipeline_mode and not route_pipeline_handoff_to_normal:
                raise InvalidParamsError(_("Cleanup-only continuation requires a completed Pipeline handoff."))
            pipeline_input: PipelineUserInput | None = None
            normal_input: PipelineUserInput | None = None
            if pipeline_mode and not route_pipeline_handoff_to_normal:
                try:
                    pipeline_input = self._pipeline_input_from_context(context, cwd=cwd)
                except ValueError as exc:
                    raise InvalidParamsError(str(exc)) from exc
                self._validate_pipeline_request_input(pipeline_input, model=model)
            else:
                try:
                    normal_input = self._normal_input_from_context(context, cwd=cwd)
                except ValueError as exc:
                    raise InvalidParamsError(str(exc)) from exc
                if normal_input.has_images:
                    self._validate_pipeline_request_input(normal_input, model=model)
            if pipeline_mode and requested_task_id is None:
                recovered_task_id = await self._recoverable_pipeline_task_id_for_context(context_id=context_id, cwd=cwd)
                if recovered_task_id is not None:
                    task_id = recovered_task_id
            owner = self._task_store.owner_for_context(getattr(context, "call_context", None))
            task = await self._task_store.get_or_create_task(
                task_id=task_id,
                context_id=context_id,
                owner=owner,
                restore_interrupted=not pipeline_mode,
            )
            await publish_initial_task_if_missing()
            await self._task_store.ensure_task_not_expired(task.task_id)
        except InvalidParamsError:
            await release_context_execution()
            raise
        except Exception as exc:
            await release_context_execution()
            await publish_initial_task_if_missing()
            if _is_retryable_executor_error(exc):
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                    text=_("A temporary error occurred. Please retry."),
                )
                if task is not None:
                    task.state = TASK_STATE_INPUT_REQUIRED
                    self._task_store.mirror_task(task)
                    await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_executor_error()
                return
            self._log_executor_exception("setup", task_id=task_id, context_id=context_id)
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=str(exc),
            )
            if task is not None:
                task.state = TASK_STATE_FAILED
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        if (
            not (pipeline_mode and not route_pipeline_handoff_to_normal)
            and normal_input is not None
            and normal_input.is_empty
        ):
            try:
                task.state = TASK_STATE_FAILED
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_FAILED,
                    text="A2A server currently accepts text input only.",
                )
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_task_failed()
            finally:
                await release_context_execution()
            return

        if pipeline_mode and not route_pipeline_handoff_to_normal:
            assert pipeline_input is not None
            pipeline_executor = IacCodeA2APipelineExecutor(
                task_store=self._task_store,
                model=model,
                metrics=self._metrics,
                artifact_store=self._artifact_store,
                push_notifier=self._push_notifier,
                permission_resolver=self._permission_resolver,
                permission_input_registry=self._permission_input_registry,
                auto_approve_permissions=self._auto_approve_permissions,
                thinking_exposure_types=self._thinking_exposure_types,
                user_id=user_id,
                aliyun_credential=aliyun_credential,
                preferred_language=preferred_language,
                candidate_presentation=candidate_presentation,
                model_from_metadata=metadata_model is not None,
                metadata_api_key=metadata_api_key,
                request_policy_override=request_policy_override,
                backup_service=self._backup_service,
            )
            try:
                pipeline_result = await pipeline_executor.execute(
                    context=context,
                    event_queue=event_queue,
                    task=task,
                    task_id=task_id,
                    context_id=context_id,
                    cwd=cwd,
                    pipeline_input=pipeline_input,
                    active_followup_only=active_pipeline_owner is not None,
                )
                if active_pipeline_owner is not None and pipeline_result is False:
                    owner_finished = active_pipeline_owner.done()
                    if owner_finished:
                        if task.active_task is active_pipeline_owner:
                            task.active_task = None
                        task.state = TASK_STATE_INPUT_REQUIRED
                        self._task_store.mirror_task(task)
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=TaskState.TASK_STATE_INPUT_REQUIRED,
                        text=_("A temporary error occurred. Please retry."),
                    )
                    if owner_finished:
                        await self._notify_terminal_task(
                            task_id=task.task_id,
                            context_id=task.context_id,
                            state=task.state,
                        )
            finally:
                await release_context_execution()
            return
        if route_pipeline_handoff_to_normal:
            try:
                await self._ensure_pipeline_handoff_context_in_session(context_id=context_id, cwd=cwd)
            except BaseException:
                await release_context_execution()
                raise

        task.active_task = asyncio.current_task()
        task.state = TASK_STATE_WORKING
        self._task_store.mirror_task(task)
        await release_context_execution()

        def runtime_factory(session_id: str) -> Any:
            session_storage = SessionStorage()
            ensure_v2_session = getattr(session_storage, "ensure_v2_session_dir_for_new_session", None)
            if callable(ensure_v2_session):
                ensure_v2_session(cwd, session_id)
            resume_messages = None
            if session_storage.exists(cwd, session_id):
                loaded = session_storage.load(cwd, session_id)
                has_permission_checkpoint = False
                try:
                    has_permission_checkpoint = bool(
                        PermissionWaitCheckpointStore(cwd, session_id, storage=session_storage).list_active()
                    )
                except ValueError:
                    has_permission_checkpoint = False
                resume_messages = (
                    loaded
                    if loaded and has_permission_checkpoint
                    else SessionStorage.repair_interrupted(loaded)
                    if loaded
                    else None
                )
            return create_agent_runtime(
                AgentFactoryOptions(
                    model=model,
                    session_id=session_id,
                    cwd=cwd,
                    resume_messages=resume_messages,
                    a2a_safe_mode=_a2a_safe_mode_enabled(),
                    source="a2a",
                )
            )

        try:
            with a2a_request_context(
                user_id=user_id,
                aliyun_credential=aliyun_credential,
                preferred_language=preferred_language,
            ):
                ctx = await self._task_store.get_or_create_context(
                    context_id=context_id,
                    cwd=cwd,
                    runtime_factory=runtime_factory,
                )
                if not hasattr(ctx.runtime, "agent_loop"):
                    old_runtime = ctx.runtime
                    ctx.runtime = runtime_factory(ctx.session_id)
                    self._task_store.mirror_context(ctx)
                    await _close_runtime(old_runtime)
        except asyncio.CancelledError:
            task.active_task = None
            task.state = TASK_STATE_CANCELED
            self._task_store.mirror_task(task)
            with contextlib.suppress(Exception):
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_CANCELED,
                    text=_("Task canceled."),
                )
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_canceled()
            raise
        except Exception as exc:
            self._log_executor_exception("runtime setup", task_id=task_id, context_id=context_id)
            task.active_task = None
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=self._sanitize_error(exc),
            )
            task.state = TASK_STATE_FAILED
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_executor_error()
            self._metrics.record_task_failed()
            return

        if ctx.lock is None:
            ctx.lock = asyncio.Lock()
        if ctx.active_task_id is not None:
            task.active_task = None
            task.state = TASK_STATE_FAILED
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=_("Task is already working."),
                session_id=ctx.session_id,
            )
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        lock = ctx.lock
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            task.active_task = None
            task.state = TASK_STATE_CANCELED
            self._task_store.mirror_task(task)
            with contextlib.suppress(Exception):
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_CANCELED,
                    text=_("Task canceled."),
                    session_id=ctx.session_id,
                )
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_canceled()
            raise
        except TimeoutError:
            task.active_task = None
            task.state = TASK_STATE_FAILED
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=_("Task is already working."),
                session_id=ctx.session_id,
            )
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        try:
            ctx.active_task_id = task.task_id
            task.state = TASK_STATE_WORKING
            task.active_task = asyncio.current_task()
            self._task_store.mirror_task(task)
            self._task_store.mirror_context(ctx)
            try:
                runtime = ctx.runtime
                if runtime is None:
                    raise RuntimeError("A2A context runtime missing")
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_SUBMITTED,
                    session_id=ctx.session_id,
                )
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_WORKING,
                    session_id=ctx.session_id,
                )
                await publish_mcp_warnings(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    runtime=runtime,
                    iac_code_session_id=ctx.session_id,
                )
                await self._publish_mcp_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    runtime=runtime,
                    session_id=ctx.session_id,
                )
                with a2a_request_context(
                    session_id=ctx.session_id,
                    user_id=user_id,
                    aliyun_credential=aliyun_credential,
                    preferred_language=preferred_language,
                ):
                    configure_runtime_model(
                        runtime,
                        model,
                        from_metadata=metadata_model is not None,
                        metadata_api_key=metadata_api_key,
                        request_policy_override=request_policy_override,
                    )
                    refresh_runtime_cloud_tools(runtime)
                    cleanup_ledger = _cleanup_ledger_for_a2a_normal_chat(cwd=cwd, session_id=ctx.session_id)
                    _prune_completed_cleanup_prompt_from_runtime(runtime, cleanup_ledger)
                    cleanup_publisher = None
                    if cleanup_ledger is not None:
                        cleanup_publisher = _cleanup_publisher_for_a2a_normal_chat(
                            event_queue=event_queue,
                            cwd=cwd,
                            session_id=ctx.session_id,
                            task_id=task_id,
                            context_id=context_id,
                            artifact_store=self._artifact_store,
                            exposure_types=self._thinking_exposure_types,
                        )
                    assert normal_input is not None
                    stream = _stream_a2a_normal_events(
                        runtime=runtime,
                        prompt=normal_input.content,
                        prompt_text=normal_input.display_text,
                        cleanup_ledger=cleanup_ledger,
                        cleanup_publisher=cleanup_publisher,
                        cleanup_only=cleanup_only,
                        cwd=cwd,
                        session_id=ctx.session_id,
                    )
                    current_assistant_text: list[str] = []
                    final_assistant_text = ""
                    detached_permission = None

                    async def finalize_normal_turn(target_queue: EventQueue) -> None:
                        nonlocal final_assistant_text
                        if current_assistant_text:
                            final_assistant_text = "".join(current_assistant_text)
                        await publish_mcp_warnings(
                            target_queue,
                            task_id=task_id,
                            context_id=context_id,
                            runtime=runtime,
                            iac_code_session_id=ctx.session_id,
                        )
                        await self._publish_mcp_status(
                            target_queue,
                            task_id=task_id,
                            context_id=context_id,
                            runtime=runtime,
                            session_id=ctx.session_id,
                        )
                        final_metadata: dict[str, Any] = {"assistantFinal": {"complete": True}}
                        if cleanup_only:
                            final_metadata[_CLEANUP_ONLY_METADATA_KEY] = _cleanup_only_summary(cleanup_ledger)
                        await self._publish_status(
                            target_queue,
                            task_id=task_id,
                            context_id=context_id,
                            state=TaskState.TASK_STATE_WORKING,
                            text=final_assistant_text or None,
                            metadata={"iac_code": final_metadata},
                            session_id=ctx.session_id,
                        )
                        task.state = TASK_STATE_INPUT_REQUIRED
                        ctx.active_task_id = None
                        task.touch()
                        ctx.touch()
                        self._task_store.mirror_task(task)
                        self._task_store.mirror_context(ctx)
                        await backup_session_async(
                            self._backup_service,
                            cwd,
                            ctx.session_id,
                            reason=BackupReason.NORMAL_TURN_END,
                            critical=False,
                            metrics=self._metrics,
                        )
                        terminal_metadata = None
                        if cleanup_only:
                            terminal_metadata = {
                                "iac_code": {_CLEANUP_ONLY_METADATA_KEY: _cleanup_only_summary(cleanup_ledger)}
                            }
                        await self._publish_status(
                            target_queue,
                            task_id=task_id,
                            context_id=context_id,
                            state=TaskState.TASK_STATE_INPUT_REQUIRED,
                            metadata=terminal_metadata,
                            session_id=ctx.session_id,
                        )
                        await self._notify_terminal_task(
                            task_id=task.task_id,
                            context_id=task.context_id,
                            state=task.state,
                        )
                        self._metrics.record_turn_completed()

                    async def mark_detached_input_required() -> None:
                        task.state = TASK_STATE_INPUT_REQUIRED
                        ctx.active_task_id = None
                        task.touch()
                        ctx.touch()
                        self._task_store.mirror_task(task)
                        self._task_store.mirror_context(ctx)
                        await self._notify_terminal_task(
                            task_id=task.task_id,
                            context_id=task.context_id,
                            state=task.state,
                        )

                    async def resolve_consumed_boundary(pending: Any) -> None:
                        checkpoint_store = pending.checkpoint_store
                        if checkpoint_store is not None and pending.boundary_id is not None:
                            persisted = SessionStorage().load(cwd, ctx.session_id)
                            digest = canonical_digest(persisted[-1].to_dict()) if persisted else ""
                            decision_record = checkpoint_store.load(pending.boundary_id)
                            decision = decision_record.get("decision") if isinstance(decision_record, dict) else {}
                            checkpoint_store.resolve(
                                pending.boundary_id,
                                result_digest=digest,
                                ack={
                                    "decision": decision.get("value"),
                                    "accepted": True,
                                },
                            )
                        await self._permission_input_registry.complete(pending)

                    async def resume_detached_normal(target_queue: EventQueue, pending: Any) -> None:
                        if ctx.lock is None:
                            ctx.lock = asyncio.Lock()
                        await ctx.lock.acquire()
                        try:
                            ctx.active_task_id = task.task_id
                            task.active_task = asyncio.current_task()
                            task.state = TASK_STATE_WORKING
                            self._task_store.mirror_task(task)
                            self._task_store.mirror_context(ctx)
                            with a2a_request_context(
                                session_id=ctx.session_id,
                                user_id=user_id,
                                aliyun_credential=aliyun_credential,
                                preferred_language=preferred_language,
                            ):
                                completed = await consume_normal_stream(target_queue)
                            await resolve_consumed_boundary(pending)
                            if completed:
                                await finalize_normal_turn(target_queue)
                            else:
                                await mark_detached_input_required()
                        finally:
                            task.active_task = None
                            ctx.active_task_id = None
                            ctx.touch()
                            task.touch()
                            self._task_store.mirror_task(task)
                            self._task_store.mirror_context(ctx)
                            ctx.lock.release()

                    async def consume_normal_stream(target_queue: EventQueue) -> bool:
                        nonlocal current_assistant_text, final_assistant_text, detached_permission
                        async for event in stream:
                            if isinstance(event, MessageStartEvent):
                                current_assistant_text = []
                            elif isinstance(event, TextDeltaEvent):
                                current_assistant_text.append(event.text)
                            elif isinstance(event, MessageEndEvent):
                                if event.stop_reason not in {"tool_use", "tool_calls"}:
                                    final_assistant_text = "".join(current_assistant_text)
                                current_assistant_text = []
                            await publish_mcp_warnings(
                                target_queue,
                                task_id=task_id,
                                context_id=context_id,
                                runtime=runtime,
                                iac_code_session_id=ctx.session_id,
                            )
                            await self._publish_mcp_status(
                                target_queue,
                                task_id=task_id,
                                context_id=context_id,
                                runtime=runtime,
                                session_id=ctx.session_id,
                            )
                            interactive_permission = (
                                isinstance(event, PermissionRequestEvent)
                                and self._permission_resolver is None
                                and not self._auto_approve_permissions
                            )
                            if interactive_permission:
                                pending = await publish_interactive_permission_boundary(
                                    target_queue,
                                    permission_event=event,
                                    permission_input_registry=self._permission_input_registry,
                                    task_id=task_id,
                                    context_id=context_id,
                                    iac_code_session_id=ctx.session_id,
                                    permission_wait_cwd=cwd,
                                    permission_wait_backup_service=self._backup_service,
                                    permission_wait_metrics=self._metrics,
                                    wait_for_response=False,
                                )
                                detached_permission = pending
                                pending.continuation = resume_detached_normal

                                async def suspend_detached(pending_permission: Any = pending) -> None:
                                    pending_permission.continuation = None
                                    close_stream = getattr(stream, "aclose", None)
                                    if callable(close_stream):
                                        with contextlib.suppress(RuntimeError):
                                            await close_stream()
                                    try:
                                        await self._task_store.discard_context_runtime(context_id)
                                    finally:
                                        await self._permission_input_registry.complete(pending_permission)

                                pending.suspend_callback = suspend_detached
                                return False
                            text_chunk = await publish_stream_event(
                                target_queue,
                                task_id=task_id,
                                context_id=context_id,
                                event=event,
                                artifact_store=self._artifact_store,
                                permission_resolver=self._permission_resolver,
                                permission_input_registry=self._permission_input_registry,
                                auto_approve_permissions=self._auto_approve_permissions,
                                exposure_types=self._thinking_exposure_types,
                                iac_code_session_id=ctx.session_id,
                                permission_wait_cwd=cwd,
                                permission_wait_backup_service=self._backup_service,
                                permission_wait_metrics=self._metrics,
                            )
                            if text_chunk:
                                task.output_text.append(text_chunk)
                        return True

                    completed = await consume_normal_stream(event_queue)
                if completed:
                    await finalize_normal_turn(event_queue)
                else:
                    await mark_detached_input_required()
            except asyncio.CancelledError:
                task.state = TASK_STATE_CANCELED
                ctx.active_task_id = None
                task.touch()
                ctx.touch()
                self._task_store.mirror_task(task)
                self._task_store.mirror_context(ctx)
                await self._task_store.discard_context_runtime(context_id)
                if await self._publish_backup_blocked_after_terminal_backup_failure(
                    event_queue,
                    task=task,
                    ctx=ctx,
                    cwd=cwd,
                    task_id=task_id,
                    context_id=context_id,
                    reason=BackupReason.TERMINAL,
                    blocked_terminal_state=TASK_STATE_CANCELED,
                ):
                    self._metrics.record_task_canceled()
                    return
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_CANCELED,
                    text=_("Task canceled."),
                    session_id=ctx.session_id,
                )
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_task_canceled()
            except Exception as exc:
                if _is_retryable_executor_error(exc):
                    task.state = TASK_STATE_INPUT_REQUIRED
                    ctx.active_task_id = None
                    task.touch()
                    ctx.touch()
                    self._task_store.mirror_task(task)
                    self._task_store.mirror_context(ctx)
                    await backup_session_async(
                        self._backup_service,
                        cwd,
                        ctx.session_id,
                        reason=BackupReason.INPUT_REQUIRED,
                        critical=False,
                        metrics=self._metrics,
                    )
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=TaskState.TASK_STATE_INPUT_REQUIRED,
                        text=_("A temporary error occurred. Please retry."),
                        session_id=ctx.session_id,
                    )
                    await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                    self._metrics.record_executor_error()
                else:
                    self._log_executor_exception("streaming", task_id=task_id, context_id=context_id)
                    task.state = TASK_STATE_FAILED
                    ctx.active_task_id = None
                    task.touch()
                    ctx.touch()
                    self._task_store.mirror_task(task)
                    self._task_store.mirror_context(ctx)
                    if await self._publish_backup_blocked_after_terminal_backup_failure(
                        event_queue,
                        task=task,
                        ctx=ctx,
                        cwd=cwd,
                        task_id=task_id,
                        context_id=context_id,
                        reason=BackupReason.TERMINAL,
                        blocked_terminal_state=TASK_STATE_FAILED,
                    ):
                        self._metrics.record_executor_error()
                        self._metrics.record_task_failed()
                        return
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=TaskState.TASK_STATE_FAILED,
                        text=self._sanitize_error(exc),
                        session_id=ctx.session_id,
                    )
                    await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                    self._metrics.record_executor_error()
                    self._metrics.record_task_failed()
            finally:
                task.active_task = None
                ctx.active_task_id = None
                ctx.touch()
                task.touch()
                self._task_store.mirror_context(ctx)
                # Force-flush telemetry between tasks. The a2a server may run in
                # an ephemeral sandbox that's destroyed immediately after the
                # response is delivered, before the natural batch interval or
                # process-exit graceful_shutdown can run. Synchronous flush is
                # offloaded to a worker thread so the event loop is not blocked.
                from iac_code.services.telemetry import flush_telemetry

                try:
                    await asyncio.to_thread(flush_telemetry)
                except Exception:
                    logger.debug("flush_telemetry after task failed", exc_info=True)
        finally:
            lock.release()

    async def _resume_persisted_permission(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        *,
        response: PermissionResponse,
    ) -> bool:
        """Claim and resume a permission whose process-local registry was lost."""

        try:
            task_record = await self._task_store.get_task_record(response.task_id)
            context_record = await self._task_store.get_context_record(response.context_id)
        except ValueError:
            return False
        if task_record.context_id != response.context_id:
            raise InvalidParamsError("input_response_mismatch: permission task context changed.")
        store = PermissionWaitCheckpointStore(context_record.cwd, context_record.session_id)
        record = store.find(
            task_id=response.task_id,
            context_id=response.context_id,
            input_id=response.input_id,
            tool_use_id=response.tool_use_id,
        )
        if record is None:
            return False
        expected_value = "allow_once" if response.decision == "allow_once" else "deny"
        boundary_id = str(record["boundaryId"])
        if record.get("phase") == "RESOLVED":
            decision = record.get("decision")
            if not isinstance(decision, dict) or decision.get("value") != expected_value:
                raise InvalidParamsError("permission_resume_invalid: permission decision conflicts with receipt.")
            await self._publish_permission_recovery_ack(
                event_queue,
                response=response,
                decision=expected_value,
                duplicate=True,
            )
            return True

        if record.get("phase") == "RESTORING":
            decision = record.get("decision")
            if not isinstance(decision, dict) or decision.get("value") != expected_value:
                raise InvalidParamsError(
                    "permission_resume_invalid: permission decision conflicts with active recovery."
                )
            await self._publish_permission_recovery_ack(
                event_queue,
                response=response,
                decision=str(decision["value"]),
                duplicate=True,
                session_id=context_record.session_id,
            )
            return True

        metadata = getattr(context, "metadata", None) or getattr(getattr(context, "message", None), "metadata", None)
        model = self._resolve_model(metadata) or self._model
        metadata_api_key = self._resolve_api_key(metadata)
        request_policy_override = self._resolve_request_policy(metadata)
        user_id = self._resolve_user_id(metadata)
        preferred_language = self._resolve_preferred_language(metadata)
        aliyun_credential = self._resolve_aliyun_credential(metadata)

        def make_pipeline_executor() -> IacCodeA2APipelineExecutor:
            return IacCodeA2APipelineExecutor(
                task_store=self._task_store,
                model=model,
                metrics=self._metrics,
                artifact_store=self._artifact_store,
                push_notifier=self._push_notifier,
                permission_resolver=self._permission_resolver,
                permission_input_registry=self._permission_input_registry,
                auto_approve_permissions=self._auto_approve_permissions,
                thinking_exposure_types=self._thinking_exposure_types,
                user_id=user_id,
                aliyun_credential=aliyun_credential,
                preferred_language=preferred_language,
                candidate_presentation=self._resolve_candidate_presentation(metadata),
                model_from_metadata=self._resolve_model(metadata) is not None,
                metadata_api_key=metadata_api_key,
                request_policy_override=request_policy_override,
                backup_service=self._backup_service,
            )

        persisted_decision = record.get("decision")
        audit_already_final = False
        if isinstance(persisted_decision, Mapping) and persisted_decision.get("status") in {"claimed", "applied"}:
            if persisted_decision.get("value") != expected_value:
                raise InvalidParamsError("permission_resume_invalid: permission response conflicts with checkpoint.")
            audit_already_final = persisted_decision.get("auditStatus") in {"recorded", "failed"}
        pipeline_executor: IacCodeA2APipelineExecutor | None = None
        audit_event: PermissionRequestEvent | None = None
        if not audit_already_final:
            recovered = recover_permission_audit_boundary(
                record,
                cwd=context_record.cwd,
                session_id=context_record.session_id,
            )
            if recovered is None:
                raise InvalidParamsError("permission_resume_invalid: canonical permission request changed.")
            try:
                if record.get("permissionClass") == "pipeline":
                    pipeline_executor = make_pipeline_executor()
                    audit_event = await pipeline_executor.rebuild_permission_audit_event(
                        cwd=context_record.cwd,
                        session_id=context_record.session_id,
                        checkpoint=record,
                        recovered=recovered,
                    )
                else:
                    audit_event = await self._rebuild_normal_permission_audit_event(
                        recovered=recovered,
                        cwd=context_record.cwd,
                        session_id=context_record.session_id,
                        model=model,
                        model_from_metadata=self._resolve_model(metadata) is not None,
                        metadata_api_key=metadata_api_key,
                        request_policy_override=request_policy_override,
                        user_id=user_id,
                        aliyun_credential=aliyun_credential,
                        preferred_language=preferred_language,
                    )
            except InvalidParamsError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise InvalidParamsError(f"permission_resume_invalid: {exc}") from exc

            permission_audit = getattr(audit_event.permission_result, "audit", None)
            with a2a_request_context(aliyun_credential=aliyun_credential):
                principal_ref, region = permission_execution_identity(
                    tool_name=audit_event.tool_name,
                    tool_input=audit_event.tool_input,
                    permission_audit=permission_audit,
                )
            if principal_ref != record.get("principalRef") or region != record.get("region"):
                raise InvalidParamsError("permission_resume_invalid: cloud execution identity changed.")

        record = store.reconcile_deadline(
            boundary_id,
            grace_seconds=self._permission_wait_policy.timeout_grace_seconds,
            live_owner=False,
        )
        try:
            record, _created = store.claim_decision(
                boundary_id,
                value=expected_value,
                source="user",
            )
        except ValueError as exc:
            raise InvalidParamsError(f"permission_resume_invalid: {exc}") from exc
        decision = record.get("decision")
        if isinstance(decision, dict):
            claim_id = str(decision.get("claimId") or "")

            def audit_claim(value: str) -> bool:
                if audit_event is None:
                    return audit_already_final
                return emit_permission_boundary_audit(
                    audit_event,
                    session_id=context_record.session_id,
                    decision="allow" if value == "allow_once" else "deny",
                    scope="a2a_input_required",
                    source="a2a_user_permission",
                    reason_type="user_decision",
                    reason_detail=value,
                )

            record, _audit_created = store.run_claim_audit_once(
                boundary_id,
                claim_id=claim_id,
                audit=audit_claim,
            )
            expected_value = str(record["decision"]["value"])
        decision = record.get("decision")
        if isinstance(decision, dict) and decision.get("backupStatus") != "committed":
            claim_id = str(decision.get("claimId") or "")
            await backup_permission_wait_checkpoint(
                store=store,
                boundary_id=boundary_id,
                cwd=context_record.cwd,
                session_id=context_record.session_id,
                backup_service=self._backup_service,
                metrics=self._metrics,
            )
            record = store.mark_claim_backed_up(boundary_id, claim_id=claim_id)
        if not await self._permission_wait_coordinator.acquire_restore(boundary_id):
            await self._publish_permission_recovery_ack(
                event_queue,
                response=response,
                decision=expected_value,
                duplicate=True,
                session_id=context_record.session_id,
            )
            return True
        try:
            record = store.begin_restore(boundary_id)
        except ValueError as exc:
            await self._permission_wait_coordinator.release_restore(boundary_id)
            raise InvalidParamsError(f"permission_resume_invalid: {exc}") from exc

        await self._publish_status(
            event_queue,
            task_id=response.task_id,
            context_id=response.context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={
                "iac_code": {
                    "permissionRecovered": {
                        "inputId": response.input_id,
                        "toolUseId": response.tool_use_id,
                    }
                }
            },
            session_id=context_record.session_id,
        )
        normal_final_assistant_text: str | None = None
        try:
            if record.get("permissionClass") == "pipeline":
                task = await self._task_store.get_or_create_task(
                    task_id=response.task_id,
                    context_id=response.context_id,
                    restore_interrupted=False,
                )
                if pipeline_executor is None:
                    pipeline_executor = make_pipeline_executor()
                await pipeline_executor.execute(
                    context=context,
                    event_queue=event_queue,
                    task=task,
                    task_id=response.task_id,
                    context_id=response.context_id,
                    cwd=context_record.cwd,
                    pipeline_input="",
                    permission_checkpoint=record,
                )
            else:
                storage = SessionStorage()
                messages = storage.load(context_record.cwd, context_record.session_id)
                if not messages:
                    raise InvalidParamsError("permission_resume_invalid: session transcript is unavailable.")
                runtime = create_agent_runtime(
                    AgentFactoryOptions(
                        model=model,
                        session_id=context_record.session_id,
                        cwd=context_record.cwd,
                        resume_messages=messages,
                        a2a_safe_mode=_a2a_safe_mode_enabled(),
                        source="a2a",
                    )
                )
                configure_runtime_model(
                    runtime,
                    model,
                    from_metadata=self._resolve_model(metadata) is not None,
                    metadata_api_key=metadata_api_key,
                    request_policy_override=request_policy_override,
                )
                refresh_runtime_cloud_tools(runtime)
                task = await self._task_store.get_or_create_task(
                    task_id=response.task_id,
                    context_id=response.context_id,
                )
                current_assistant_text: list[str] = []
                normal_final_assistant_text = ""
                try:
                    with a2a_request_context(
                        session_id=context_record.session_id,
                        user_id=user_id,
                        aliyun_credential=aliyun_credential,
                        preferred_language=preferred_language,
                    ):
                        async for event in runtime.agent_loop.resume_permission_boundary(record):
                            if isinstance(event, MessageStartEvent):
                                current_assistant_text = []
                            elif isinstance(event, TextDeltaEvent):
                                current_assistant_text.append(event.text)
                            elif isinstance(event, MessageEndEvent):
                                if event.stop_reason not in {"tool_use", "tool_calls"}:
                                    normal_final_assistant_text = "".join(current_assistant_text)
                                current_assistant_text = []
                            text_chunk = await publish_stream_event(
                                event_queue,
                                task_id=response.task_id,
                                context_id=response.context_id,
                                event=event,
                                artifact_store=self._artifact_store,
                                permission_resolver=self._permission_resolver,
                                permission_input_registry=self._permission_input_registry,
                                auto_approve_permissions=self._auto_approve_permissions,
                                exposure_types=self._thinking_exposure_types,
                                iac_code_session_id=context_record.session_id,
                                permission_wait_cwd=context_record.cwd,
                                permission_wait_backup_service=self._backup_service,
                                permission_wait_metrics=self._metrics,
                            )
                            if text_chunk:
                                task.output_text.append(text_chunk)
                        if current_assistant_text:
                            normal_final_assistant_text = "".join(current_assistant_text)
                finally:
                    await _close_runtime(runtime)
        except PermissionWaitSuspended:
            store.mark_suspended(boundary_id)
            await self._publish_status(
                event_queue,
                task_id=response.task_id,
                context_id=response.context_id,
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                metadata={
                    "iac_code": {
                        "permissionWait": {"status": "suspended", "resumable": True},
                    }
                },
                session_id=context_record.session_id,
            )
            return True
        except BaseException:
            with contextlib.suppress(ValueError):
                store.reconcile_deadline(
                    boundary_id,
                    grace_seconds=self._permission_wait_policy.timeout_grace_seconds,
                    live_owner=False,
                )
            raise
        finally:
            await self._permission_wait_coordinator.release_restore(boundary_id)

        storage = SessionStorage()
        persisted_messages = storage.load(context_record.cwd, context_record.session_id)
        result_digest = canonical_digest(persisted_messages[-1].to_dict()) if persisted_messages else ""
        store.resolve(
            boundary_id,
            result_digest=result_digest,
            ack={"decision": expected_value, "accepted": True},
        )
        task = await self._task_store.get_or_create_task(
            task_id=response.task_id,
            context_id=response.context_id,
        )
        task.state = TASK_STATE_INPUT_REQUIRED
        self._task_store.mirror_task(task)
        await self._publish_permission_recovery_ack(
            event_queue,
            response=response,
            decision=expected_value,
            duplicate=False,
            session_id=context_record.session_id,
        )
        if normal_final_assistant_text is not None:
            await self._publish_status(
                event_queue,
                task_id=response.task_id,
                context_id=response.context_id,
                state=TaskState.TASK_STATE_WORKING,
                text=normal_final_assistant_text or None,
                metadata={"iac_code": {"assistantFinal": {"complete": True}}},
                session_id=context_record.session_id,
            )
            await backup_session_async(
                self._backup_service,
                context_record.cwd,
                context_record.session_id,
                reason=BackupReason.NORMAL_TURN_END,
                critical=False,
                metrics=self._metrics,
            )
            await self._publish_status(
                event_queue,
                task_id=response.task_id,
                context_id=response.context_id,
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                session_id=context_record.session_id,
            )
            await self._notify_terminal_task(
                task_id=task.task_id,
                context_id=task.context_id,
                state=task.state,
            )
            self._metrics.record_turn_completed()
        return True

    async def _rebuild_normal_permission_audit_event(
        self,
        *,
        recovered: RecoveredPermissionAuditBoundary,
        cwd: str,
        session_id: str,
        model: str,
        model_from_metadata: bool,
        metadata_api_key: str | None,
        request_policy_override: ProviderRequestPolicy | None,
        user_id: str | None,
        aliyun_credential: AliyunCredential | None,
        preferred_language: str | None,
    ) -> PermissionRequestEvent:
        """Use a current Normal runtime to rebuild restart audit metadata/settings."""

        storage = SessionStorage()
        messages = storage.load(cwd, session_id)
        if not messages:
            raise ValueError("permission_resume_invalid: session transcript is unavailable")
        runtime = create_agent_runtime(
            AgentFactoryOptions(
                model=model,
                session_id=session_id,
                cwd=cwd,
                resume_messages=messages,
                a2a_safe_mode=_a2a_safe_mode_enabled(),
                source="a2a",
            )
        )
        try:
            configure_runtime_model(
                runtime,
                model,
                from_metadata=model_from_metadata,
                metadata_api_key=metadata_api_key,
                request_policy_override=request_policy_override,
            )
            refresh_runtime_cloud_tools(runtime)
            with a2a_request_context(
                session_id=session_id,
                user_id=user_id,
                aliyun_credential=aliyun_credential,
                preferred_language=preferred_language,
            ):
                return await runtime.agent_loop.rebuild_permission_audit_event(
                    tool_name=recovered.tool_name,
                    tool_input=recovered.tool_input,
                    tool_use_id=recovered.tool_use_id,
                    audit_context=recovered.audit_context,
                )
        finally:
            await _close_runtime(runtime)

    async def _publish_permission_recovery_ack(
        self,
        event_queue: EventQueue,
        *,
        response: PermissionResponse,
        decision: str,
        duplicate: bool,
        session_id: str | None = None,
    ) -> None:
        await self._publish_status(
            event_queue,
            task_id=response.task_id,
            context_id=response.context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={
                "iac_code": {
                    "inputReceived": {
                        "kind": "permission",
                        "inputId": response.input_id,
                        "toolUseId": response.tool_use_id,
                        "decision": decision,
                        "recovered": True,
                        "duplicate": duplicate,
                    }
                }
            },
            session_id=session_id,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id or "unknown"
        if task_id:
            await self._permission_input_registry.cancel_task(task_id)
        if task_id and await self._task_store.cancel_task_and_wait(
            task_id,
            timeout=_CANCEL_ACTIVE_TASK_DRAIN_TIMEOUT_SECONDS,
        ):
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_CANCELED,
                text="Task cancellation requested.",
            )
            self._metrics.record_task_canceled()
            return
        if task_id:
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text="Task not running.",
            )

    def _resolve_cwd(self, metadata: Any | None) -> str:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        cwd: str | None = None
        if metadata:
            raw_iac_meta = metadata.get("iac_code") if isinstance(metadata, Mapping) else None
            if isinstance(raw_iac_meta, Mapping):
                raw_cwd = raw_iac_meta.get("cwd")
                if isinstance(raw_cwd, str):
                    cwd = raw_cwd
        if cwd is None:
            cwd = os.getcwd()
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("Invalid A2A workspace metadata.")
        logical_cwd = os.path.normpath(cwd)
        resolved_cwd = resolve_workspace_path(Path(logical_cwd))
        if not trust_request_cwd() and not any(_is_relative_to(resolved_cwd, root) for root in _allowed_cwd_roots()):
            raise ValueError("Invalid A2A workspace metadata.")
        if resolved_cwd.exists():
            if not resolved_cwd.is_dir():
                raise ValueError("Invalid A2A workspace metadata.")
        else:
            resolved_cwd.mkdir(parents=True, exist_ok=True)
        return logical_cwd

    def _resolve_user_id(self, metadata: Any | None) -> str | None:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return None
        raw_iac_meta = metadata.get("iac_code")
        if not isinstance(raw_iac_meta, Mapping):
            return None
        raw_user_id = raw_iac_meta.get("user_id")
        if isinstance(raw_user_id, str) and raw_user_id.strip():
            return raw_user_id.strip()
        return None

    def _resolve_telemetry_channel(self, metadata: Any | None) -> str | None:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return None
        raw_iac_meta = metadata.get("iac_code")
        if not isinstance(raw_iac_meta, Mapping):
            return None
        return normalize_telemetry_channel(raw_iac_meta.get("channel"))

    def _resolve_preferred_language(self, metadata: Any | None) -> str | None:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return None
        raw_iac_meta = metadata.get("iac_code")
        if not isinstance(raw_iac_meta, Mapping):
            return None
        raw_language = raw_iac_meta.get("preferredLanguage") or raw_iac_meta.get("preferred_language")
        if not isinstance(raw_language, str):
            return None
        language = raw_language.strip().lower().split("-", 1)[0].split("_", 1)[0]
        return language if language in SUPPORTED_LANGUAGES else None

    def _resolve_candidate_presentation(self, metadata: Any | None) -> str | None:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return None
        raw_iac_meta = metadata.get("iac_code")
        if not isinstance(raw_iac_meta, Mapping):
            return None
        value = raw_iac_meta.get("candidatePresentation") or raw_iac_meta.get("candidate_presentation")
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        return RICH_CANDIDATE_PRESENTATION if normalized == RICH_CANDIDATE_PRESENTATION else None

    def _resolve_cleanup_only(self, metadata: Any | None) -> bool:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return False
        raw_iac_meta = metadata.get("iac_code")
        return isinstance(raw_iac_meta, Mapping) and raw_iac_meta.get(_CLEANUP_ONLY_METADATA_KEY) is True

    def _resolve_model(self, metadata: Any | None) -> str | None:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return None
        raw_iac_meta = metadata.get("iac_code")
        if not isinstance(raw_iac_meta, Mapping):
            return None
        raw_model = raw_iac_meta.get("iac_code_model")
        if isinstance(raw_model, str) and raw_model.strip():
            return raw_model.strip()
        return None

    def _resolve_api_key(self, metadata: Any | None) -> str | None:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return None
        raw_iac_meta = metadata.get("iac_code")
        if not isinstance(raw_iac_meta, Mapping):
            return None
        raw_api_key = raw_iac_meta.get("iac_code_api_key")
        if isinstance(raw_api_key, str) and raw_api_key.strip():
            return raw_api_key.strip()
        return None

    def _resolve_request_policy(self, metadata: Any | None) -> ProviderRequestPolicy | None:
        return A2AThinkingMetadata.request_policy_from_metadata(metadata)

    def _resolve_aliyun_credential(self, metadata: Any | None) -> AliyunCredential | None:
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if not isinstance(metadata, Mapping):
            return None
        raw_iac_meta = metadata.get("iac_code")
        if not isinstance(raw_iac_meta, Mapping):
            return None

        def _read(name: str) -> str | None:
            raw_value = raw_iac_meta.get(name)
            if isinstance(raw_value, str) and raw_value.strip():
                return raw_value.strip()
            return None

        access_key_id = _read("alibaba_cloud_access_key_id")
        access_key_secret = _read("alibaba_cloud_access_key_secret")
        if not access_key_id or not access_key_secret:
            return None
        sts_token = _read("alibaba_cloud_security_token") or ""
        return AliyunCredential(
            mode="StsToken" if sts_token else "AK",
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            region_id=_read("alibaba_cloud_region_id") or DEFAULT_REGION,
            sts_token=sts_token,
        )

    def _prompt_from_context(self, context: RequestContext, *, cwd: str) -> str:
        message = getattr(context, "message", None)
        if not isinstance(message, Message):
            return context.get_user_input()
        return parts_to_prompt(message.parts, cwd=cwd)

    def _normal_input_from_context(self, context: RequestContext, *, cwd: str) -> PipelineUserInput:
        message = getattr(context, "message", None)
        if not isinstance(message, Message):
            return normalize_pipeline_user_input(context.get_user_input())
        user_input = parts_to_user_input(message.parts, cwd=cwd)
        if user_input.has_images:
            return user_input
        return normalize_pipeline_user_input(user_input.display_text)

    def _pipeline_input_from_context(self, context: RequestContext, *, cwd: str) -> PipelineUserInput:
        message = getattr(context, "message", None)
        if not isinstance(message, Message):
            return normalize_pipeline_user_input(context.get_user_input())
        return parts_to_user_input(message.parts, cwd=cwd)

    def validate_pipeline_message_request(self, message: Message) -> None:
        metadata = getattr(message, "metadata", None)
        try:
            cwd = self._resolve_cwd(metadata)
            pipeline_input = parts_to_user_input(message.parts, cwd=cwd)
        except ValueError as exc:
            raise InvalidParamsError(str(exc)) from exc
        model = self._resolve_model(metadata) or self._model
        self._validate_pipeline_request_input(pipeline_input, model=model)

    def _validate_pipeline_request_input(self, pipeline_input: PipelineUserInput, *, model: str | None = None) -> None:
        if pipeline_input.is_empty:
            raise InvalidParamsError("A2A server received empty input.")
        model = model or self._model
        if pipeline_input.has_images and not self._model_supports_image_input(model=model):
            raise InvalidParamsError(_("Current model {model} does not support image input.").format(model=model))

    def _model_supports_image_input(self, *, model: str | None = None) -> bool:
        model = model or self._model
        provider_key = get_active_provider_key()
        provider_config = get_provider_config(provider_key) if provider_key else {}
        api_base = provider_config.get("apiBase") if isinstance(provider_config.get("apiBase"), str) else None
        credentials = load_credentials(model=model)
        api_key = credentials.get(provider_key, "") if provider_key else None
        return is_model_multimodal(
            model,
            provider_key=provider_key,
            base_url=api_base,
            api_key=api_key,
        )

    def _sanitize_error(self, exc: Exception) -> str:
        if isinstance(exc, ValueError):
            msg = str(exc).lower()
            if "provider" in msg or "configure" in msg or "/auth" in msg:
                return _("Authentication required. Configure credentials and retry.")
        if type(exc).__name__ == "AuthenticationError":
            return _("Authentication required. Configure credentials and retry.")
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status == 401:
            return _("Authentication required. Configure credentials and retry.")
        return _format_exception(exc)

    async def _publish_backup_blocked_after_terminal_backup_failure(
        self,
        event_queue: EventQueue,
        *,
        task: Any,
        ctx: Any,
        cwd: str,
        task_id: str,
        context_id: str,
        reason: BackupReason,
        blocked_terminal_state: str,
    ) -> bool:
        try:
            await backup_session_async(
                self._backup_service,
                cwd,
                ctx.session_id,
                reason=reason,
                critical=True,
                metrics=self._metrics,
            )
            return False
        except SessionBackupBlocked as exc:
            logger.warning(
                "A2A terminal session backup blocked task publication reason=%s retry_count=%s error=%s",
                reason.value,
                getattr(exc, "retry_count", 0),
                sanitize_strict_text(str(exc))[:_ERROR_TEXT_MAX_CHARS],
            )
            record_backup_blocked = getattr(self._metrics, "record_backup_blocked", None)
            if callable(record_backup_blocked):
                try:
                    record_backup_blocked(reason=reason.value, recoverable=True)
                except Exception as metric_exc:
                    logger.debug("Failed to record A2A backup_blocked metric: %s", type(metric_exc).__name__)
            task.state = TASK_STATE_INPUT_REQUIRED
            ctx.active_task_id = None
            task.touch()
            ctx.touch()
            self._task_store.mirror_task(task)
            self._task_store.mirror_context(ctx)
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                text=_("Session backup failed. Retry after the backup path is available."),
                metadata={
                    "iac_code": {
                        "backupBlocked": {
                            "reason": reason.value,
                            "blockedTerminalState": blocked_terminal_state,
                            "error": _format_exception(exc),
                            "recoverable": True,
                        }
                    }
                },
                session_id=ctx.session_id,
            )
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            return True

    async def _reconcile_session_before_route(
        self,
        *,
        context_id: str,
        cwd: str,
    ) -> SessionReconcileResult | None:
        reconcile = getattr(self._backup_service, "reconcile_session", None)
        if not callable(reconcile):
            return None
        async with self._task_store.reconciliation_lock(context_id):
            await self._task_store.ensure_context_reconciliation_safe(context_id)
            return await self._reconcile_session_before_route_locked(context_id=context_id, cwd=cwd)

    async def _reconcile_session_before_route_locked(
        self,
        *,
        context_id: str,
        cwd: str,
    ) -> SessionReconcileResult | None:
        reconcile = getattr(self._backup_service, "reconcile_session", None)
        if not callable(reconcile):
            return None
        try:
            context = await self._task_store.get_context_record(context_id)
        except Exception:
            return None
        if context.cwd != cwd:
            return None
        result = await run_sync_fenced(
            reconcile,
            cwd,
            context.session_id,
            attempted_proof_validator=lambda key, proof: self._validate_attempted_publication_proof(
                cwd=cwd,
                session_id=context.session_id,
                key=key,
                proof=proof,
            ),
        )
        if not isinstance(result, SessionReconcileResult):
            raise TypeError("session backup reconciliation returned an invalid result")
        proven_handoff = self._normal_handoff_has_state_proof(
            cwd=cwd,
            session_id=context.session_id,
            state=result.state,
        )
        if result.payload_changed or (proven_handoff and context.active_task_id is not None):
            await self._task_store.refresh_context_from_session(
                context_id=context_id,
                cwd=cwd,
                session_id=context.session_id,
                clear_active_task_for_proven_handoff=proven_handoff,
            )
        return result

    def _validate_attempted_publication_proof(
        self,
        *,
        cwd: str,
        session_id: str,
        key: str,
        proof: BackupPublicationProof,
    ) -> bool:
        if key != NORMAL_HANDOFF_PROOF_KEY:
            return False
        state = _a2a_pipeline_state_for_session(cwd=cwd, session_id=session_id)
        if state is None:
            return False
        _snapshot_store, _journal, _snapshot, journal_events = state
        return _handoff_from_publication_proof(proof, journal_events=journal_events) is not None

    def _normal_handoff_has_state_proof(
        self,
        *,
        cwd: str,
        session_id: str,
        state: SessionBackupState | None = None,
        handoff: dict[str, Any] | None = None,
        journal_events: list[dict[str, Any]] | None = None,
    ) -> bool:
        if state is None:
            read_local_state = getattr(self._backup_service, "read_local_state", None)
            if not callable(read_local_state):
                return False
            try:
                state = read_local_state(cwd, session_id)
            except Exception:
                return False
        if state is None or state.status != "succeeded":
            return False
        proof = state.publication_proofs.get(NORMAL_HANDOFF_PROOF_KEY)
        if proof is None:
            return False
        if journal_events is None:
            pipeline_state = _a2a_pipeline_state_for_session(cwd=cwd, session_id=session_id)
            if pipeline_state is None:
                return False
            _snapshot_store, _journal, _snapshot, journal_events = pipeline_state
        if (
            handoff is not None
            and _publication_proof_matches_handoff(
                proof,
                handoff=handoff,
                journal_events=journal_events,
            )
            and _is_normal_handoff(handoff)
        ):
            return True
        return _handoff_from_publication_proof(proof, journal_events=journal_events) is not None

    def _normal_handoff_from_state_proof(
        self,
        *,
        cwd: str,
        session_id: str,
        journal_events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        read_local_state = getattr(self._backup_service, "read_local_state", None)
        if not callable(read_local_state):
            return None
        try:
            state = read_local_state(cwd, session_id)
        except Exception:
            return None
        if state is None or state.status != "succeeded":
            return None
        proof = state.publication_proofs.get(NORMAL_HANDOFF_PROOF_KEY)
        if proof is None:
            return None
        return _handoff_from_publication_proof(proof, journal_events=journal_events)

    async def _should_route_pipeline_handoff_to_normal(self, *, context_id: str, cwd: str) -> bool:
        try:
            ctx = await self._task_store.get_context_record(context_id)
        except Exception:
            return False
        if ctx.cwd != cwd:
            return False
        state = _a2a_pipeline_state_for_session(cwd=cwd, session_id=ctx.session_id)
        if state is None:
            return False
        _snapshot_store, _journal, snapshot, journal_events = state
        handoff = snapshot.get("normalHandoff")
        if not isinstance(handoff, dict) or not _normal_handoff_has_backup_ack(handoff, journal_events):
            handoff = self._normal_handoff_from_state_proof(
                cwd=cwd,
                session_id=ctx.session_id,
                journal_events=journal_events,
            )
            if handoff is None:
                return False
        return handoff.get("action") == "switch_to_normal" and handoff.get("targetMode") == "normal"

    async def _ensure_pipeline_handoff_context_in_session(self, *, context_id: str, cwd: str) -> None:
        try:
            ctx = await self._task_store.get_context_record(context_id)
        except Exception:
            return
        if ctx.cwd != cwd:
            return
        state = _a2a_pipeline_state_for_session(cwd=cwd, session_id=ctx.session_id)
        if state is None:
            return
        _snapshot_store, _journal, snapshot, journal_events = state
        handoff = snapshot.get("normalHandoff")
        if not isinstance(handoff, dict) or not _normal_handoff_has_backup_ack(handoff, journal_events):
            handoff = self._normal_handoff_from_state_proof(
                cwd=cwd,
                session_id=ctx.session_id,
                journal_events=journal_events,
            )
            if handoff is None:
                return
        summary = handoff.get("summary")
        cleanup_payload = None
        data = handoff.get("data")
        if isinstance(data, dict) and isinstance(data.get("cleanup"), dict):
            cleanup_payload = _cleanup_payload_from_private_ledger_or_unavailable(
                ledger_path=_default_cleanup_ledger_path(cwd=cwd, session_id=ctx.session_id),
            )
        cleanup_prompt = cleanup_payload.get("prompt") if isinstance(cleanup_payload, dict) else None
        cleanup_ledger_path = cleanup_payload.get("ledgerPath") if isinstance(cleanup_payload, dict) else None
        if not isinstance(cleanup_prompt, str) or not cleanup_prompt:
            cleanup_prompt = None
        if not isinstance(cleanup_ledger_path, str) or not cleanup_ledger_path:
            cleanup_ledger_path = None
        if (not isinstance(summary, str) or not summary) and cleanup_prompt is None:
            return

        session_storage = SessionStorage()
        messages = session_storage.load(cwd, ctx.session_id)
        if isinstance(summary, str) and summary and not _session_has_user_message(messages, content=summary):
            session_storage.append(cwd, ctx.session_id, AgentMessage(role="user", content=summary))
            messages.append(AgentMessage(role="user", content=summary))
        if cleanup_prompt is not None and not _session_has_active_cleanup_prompt_content(
            messages,
            content=cleanup_prompt,
        ):
            session_storage.append(
                cwd,
                ctx.session_id,
                create_cleanup_prompt_message(
                    cleanup_prompt,
                    cleanup_ledger_path=cleanup_ledger_path,
                    cleanup_status="pending" if cleanup_ledger_path else None,
                ),
            )

    async def _recoverable_pipeline_task_id_for_context(self, *, context_id: str, cwd: str) -> str | None:
        try:
            ctx = await self._task_store.get_context_record(context_id)
        except Exception:
            return None
        if ctx.cwd != cwd:
            return None
        try:
            return recoverable_task_id_from_sidecar(cwd=cwd, session_id=ctx.session_id, context_id=context_id)
        except Exception:
            logger.debug("Failed to recover A2A pipeline task id", exc_info=True)
            return None

    def _log_executor_exception(self, stage: str, *, task_id: str, context_id: str) -> None:
        logger.error("A2A executor %s failed (task_id=%s, context_id=%s)", stage, task_id, context_id)

    async def _publish_mcp_status(
        self,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
        runtime: Any,
        session_id: str | None,
    ) -> None:
        from iac_code.mcp.manager import mcp_status_metadata

        status_metadata = mcp_status_metadata(
            getattr(runtime, "mcp_manager", None),
            warnings=list(getattr(runtime, "mcp_config_warnings", None) or []),
            pending_configs=getattr(runtime, "mcp_pending_configs", None),
        )
        if status_metadata is None:
            return
        status_signature = (task_id, repr(status_metadata))
        if getattr(runtime, "_a2a_mcp_status_pushed_signature", None) == status_signature:
            return
        await self._publish_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={"iac_code": {"mcpStatus": status_metadata}},
            session_id=session_id,
        )
        setattr(runtime, "_a2a_mcp_status_pushed_signature", status_signature)

    async def _publish_status(
        self,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
        state: int,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        message = None
        if text:
            message = Message(
                message_id=f"{task_id}-{state}",
                task_id=task_id,
                context_id=context_id,
                role=Role.ROLE_AGENT,
                parts=[make_text_part(text)],
            )
        status = TaskStatus(state=TaskState.Name(state), message=message)
        status.timestamp.GetCurrentTime()
        update = TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status)
        metadata = with_iac_code_session_metadata(metadata, session_id)
        if metadata is not None:
            ParseDict(metadata, update.metadata)
        await event_queue.enqueue_event(update)

    async def _publish_initial_task(
        self,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
        context: RequestContext,
        session_id: str | None = None,
        public_path_roots: list[dict[str, str]] | None = None,
    ) -> None:
        task = Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.Name(TaskState.TASK_STATE_SUBMITTED)),
        )
        if session_id:
            ParseDict(iac_code_session_metadata(session_id), task.metadata)
        message = getattr(context, "message", None)
        if isinstance(message, Message):
            task.history.append(
                self._metadata_echo_redactor.redact_message_echo(
                    message,
                    public_path_roots=public_path_roots,
                )
            )
        await event_queue.enqueue_event(task)

    def _refresh_runtime_cloud_tools(self, runtime: Any) -> None:
        refresh_runtime_cloud_tools(runtime)

    def _configure_runtime_model(
        self,
        runtime: Any,
        model: str,
        *,
        from_metadata: bool,
        metadata_api_key: str | None = None,
        request_policy_override: ProviderRequestPolicy | None = None,
    ) -> None:
        configure_runtime_model(
            runtime,
            model,
            from_metadata=from_metadata,
            metadata_api_key=metadata_api_key,
            request_policy_override=request_policy_override,
        )

    def _credentials_with_metadata_api_key(
        self,
        *,
        model: str,
        credentials: dict[str, str],
        provider_key_override: str | None,
        metadata_api_key: str,
    ) -> dict[str, str]:
        return credentials_with_metadata_api_key(
            model=model,
            credentials=credentials,
            provider_key_override=provider_key_override,
            metadata_api_key=metadata_api_key,
        )

    async def _notify_terminal_task(self, *, task_id: str, context_id: str, state: str) -> None:
        if self._push_notifier is None:
            return
        try:
            await self._push_notifier.notify_task_state(task_id=task_id, context_id=context_id, state=state)
        except Exception as exc:
            logger.warning("A2A push notification failed: %s", sanitize_strict_text(str(exc)))


def _normal_handoff_has_backup_ack(handoff: dict[str, Any], journal_events: list[dict[str, Any]]) -> bool:
    if handoff.get("visibility") != "committed":
        return False
    handoff_sequence = _a2a_pipeline_sequence_number(handoff.get("sequence"))
    handoff_event_id = handoff.get("eventId")
    handoff_event_type = handoff.get("eventType") or "pipeline_handoff_ready"
    for event in journal_events:
        if event.get("eventType") != BACKUP_COMMITTED_EVENT_TYPE:
            continue
        if _a2a_pipeline_sequence_number(event.get("sequence")) <= handoff_sequence:
            continue
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if data.get("committedEventId") == handoff_event_id:
            return True
        if (
            _a2a_pipeline_sequence_number(data.get("committedSequence")) == handoff_sequence
            and data.get("committedEventType") == handoff_event_type
        ):
            return True
    return False


def _publication_proof_matches_handoff(
    proof: BackupPublicationProof,
    *,
    handoff: dict[str, Any],
    journal_events: list[dict[str, Any]],
) -> bool:
    if handoff.get("visibility") != "committed":
        return False
    if (
        handoff.get("eventId") != proof.event_id
        or handoff.get("eventType") != proof.event_type
        or _a2a_pipeline_sequence_number(handoff.get("sequence")) != proof.sequence
    ):
        return False
    return any(
        event.get("visibility") == "committed"
        and event.get("eventId") == proof.event_id
        and event.get("eventType") == proof.event_type
        and _a2a_pipeline_sequence_number(event.get("sequence")) == proof.sequence
        for event in journal_events
    )


def _handoff_from_publication_proof(
    proof: BackupPublicationProof,
    *,
    journal_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if proof.event_type != "pipeline_handoff_ready":
        return None
    for event in journal_events:
        if (
            event.get("visibility") != "committed"
            or event.get("eventId") != proof.event_id
            or event.get("eventType") != proof.event_type
            or _a2a_pipeline_sequence_number(event.get("sequence")) != proof.sequence
        ):
            continue
        data = event.get("data")
        data = dict(data) if isinstance(data, dict) else {}
        handoff = {
            **data,
            "data": data,
            "eventId": proof.event_id,
            "eventType": proof.event_type,
            "sequence": proof.sequence,
            "status": event.get("status"),
            "visibility": "committed",
        }
        return handoff if _is_normal_handoff(handoff) else None
    return None


def _is_normal_handoff(handoff: dict[str, Any]) -> bool:
    return handoff.get("action") == "switch_to_normal" and handoff.get("targetMode") == "normal"


def _is_retryable_executor_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            TimeoutError,
            httpx.TimeoutException,
            httpx.TransportError,
            ConnectionError,
            MCPConnectionError,
        ),
    )
