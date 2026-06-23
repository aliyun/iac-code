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
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.events import make_text_part, publish_stream_event
from iac_code.a2a.exposure import normalize_a2a_exposure_types
from iac_code.a2a.metrics import A2AMetrics, NoOpA2AMetrics
from iac_code.a2a.parts import (
    allowed_cwd_roots,
    is_relative_to,
    parts_to_pipeline_input,
    parts_to_prompt,
    resolve_workspace_path,
)
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, recoverable_task_id_from_sidecar
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import existing_a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.a2a.types import (
    TASK_STATE_CANCELED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_WORKING,
)
from iac_code.agent.message import Message as AgentMessage
from iac_code.config import get_active_provider_key, get_provider_config, load_credentials
from iac_code.i18n import _
from iac_code.pipeline.config import RunMode, get_run_mode
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
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.services.capabilities.multimodal import is_model_multimodal
from iac_code.services.providers.aliyun import DEFAULT_REGION, AliyunCredential, use_aliyun_credential
from iac_code.services.session_storage import SessionStorage
from iac_code.services.telemetry import use_session_id, use_user_id
from iac_code.types.stream_events import TextDeltaEvent
from iac_code.utils.file_security import atomic_write_text, ensure_private_dir, ensure_private_file
from iac_code.utils.public_errors import public_exception_summary, sanitize_public_text

logger = logging.getLogger(__name__)
_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS = 1
_ERROR_TEXT_MAX_CHARS = 1000
_DEFERRED_CLEANUP_PROMPTS_FILENAME = "cleanup-deferred-prompts.json"


def _format_exception(exc: BaseException) -> str:
    return public_exception_summary(exc, max_chars=_ERROR_TEXT_MAX_CHARS)


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
    public_snapshot: dict[str, Any] | None = None,
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
) -> tuple[A2APipelineSnapshotStore, A2APipelineJournal, dict[str, Any], list[dict[str, Any]] | None] | None:
    try:
        pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
        snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
        journal = A2APipelineJournal(pipeline_dir)
        snapshot = snapshot_store.load()
    except Exception:
        logger.warning("Failed to load A2A pipeline snapshot", exc_info=True)
        return None
    journal_events: list[dict[str, Any]] | None = None
    if not isinstance(snapshot, dict):
        try:
            journal_events = journal.read_all_repairing_tail()
        except Exception:
            logger.warning("Failed to rebuild A2A pipeline snapshot from journal", exc_info=True)
            return None
        if not journal_events:
            return None
        snapshot = reduce_pipeline_events(journal_events)
    return snapshot_store, journal, snapshot, journal_events


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
        return "cleanup_started"
    if status == "in_progress":
        return "cleanup_progress"
    if status == "completed":
        return "cleanup_completed"
    if status == "failed":
        return "cleanup_failed"
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
    text = sanitize_public_text(str(value))
    return text[:_ERROR_TEXT_MAX_CHARS] + "..." if len(text) > _ERROR_TEXT_MAX_CHARS else text


async def _stream_a2a_normal_events(
    *,
    runtime: Any,
    prompt: str,
    cleanup_ledger: CleanupLedger | None,
    cleanup_publisher: PipelineA2AEventPublisher | None,
    cwd: str,
    session_id: str,
) -> AsyncIterator[Any]:
    if _a2a_cleanup_ledger_unavailable(cleanup_ledger, runtime=runtime, cwd=cwd, session_id=session_id):
        if not _append_a2a_deferred_cleanup_prompt(cwd=cwd, session_id=session_id, prompt=prompt):
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
            if not _append_a2a_deferred_cleanup_prompt(cwd=cwd, session_id=session_id, prompt=prompt):
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
            if not _append_a2a_deferred_cleanup_prompt(cwd=cwd, session_id=session_id, prompt=prompt):
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

    prompts_after_cleanup = _a2a_prompts_after_cleanup(cwd=cwd, session_id=session_id, prompt=prompt)
    if prompts_after_cleanup is None:
        yield TextDeltaEvent(
            text=_("Rollback cleanup deferred prompt state is unavailable. Please repair it before continuing.")
        )
        return
    prompts_to_run, has_deferred_prompts = prompts_after_cleanup
    for prompt_to_run in prompts_to_run:
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
        auto_approve_permissions: bool = False,
        thinking_exposure_types: Any = None,
    ) -> None:
        self._task_store = task_store
        self._model = model
        self._metrics = metrics or NoOpA2AMetrics()
        self._artifact_store = artifact_store
        self._push_notifier = push_notifier
        self._permission_resolver = permission_resolver
        self._auto_approve_permissions = auto_approve_permissions
        self._thinking_exposure_types = normalize_a2a_exposure_types(thinking_exposure_types)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        requested_task_id = context.task_id or None
        task_id = requested_task_id or "task-" + uuid.uuid4().hex[:12]
        context_id = context.context_id or "ctx-" + uuid.uuid4().hex[:12]
        task = None
        initial_task_published = False

        async def publish_initial_task_if_missing() -> None:
            nonlocal initial_task_published
            if initial_task_published or isinstance(getattr(context, "current_task", None), Task):
                return
            await self._publish_initial_task(event_queue, task_id=task_id, context_id=context_id, context=context)
            initial_task_published = True

        try:
            metadata = getattr(context, "metadata", None) or getattr(
                getattr(context, "message", None), "metadata", None
            )
            cwd = self._resolve_cwd(metadata)
            user_id = self._resolve_user_id(metadata)
            metadata_model = self._resolve_model(metadata)
            model = metadata_model or self._model
            aliyun_credential = self._resolve_aliyun_credential(metadata)
            pipeline_mode = get_run_mode() == RunMode.PIPELINE
            route_pipeline_handoff_to_normal = False
            if pipeline_mode:
                route_pipeline_handoff_to_normal = await self._should_route_pipeline_handoff_to_normal(
                    context_id=context_id,
                    cwd=cwd,
                )
            pipeline_input: PipelineUserInput | None = None
            if pipeline_mode and not route_pipeline_handoff_to_normal:
                try:
                    pipeline_input = self._pipeline_input_from_context(context, cwd=cwd)
                except ValueError as exc:
                    raise InvalidParamsError(sanitize_public_text(str(exc))) from exc
                prompt = pipeline_input.display_text
                self._validate_pipeline_request_input(pipeline_input, model=model)
            else:
                prompt = self._prompt_from_context(context, cwd=cwd)
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
            raise
        except Exception as exc:
            await publish_initial_task_if_missing()
            if _is_retryable_executor_error(exc):
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                    text="A temporary error occurred. Please retry.",
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
                text=sanitize_public_text(str(exc)),
            )
            if task is not None:
                task.state = TASK_STATE_FAILED
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        if not (pipeline_mode and not route_pipeline_handoff_to_normal) and not prompt.strip():
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
                auto_approve_permissions=self._auto_approve_permissions,
                thinking_exposure_types=self._thinking_exposure_types,
            )
            await pipeline_executor.execute(
                context=context,
                event_queue=event_queue,
                task=task,
                task_id=task_id,
                context_id=context_id,
                cwd=cwd,
                pipeline_input=pipeline_input,
            )
            return
        if route_pipeline_handoff_to_normal:
            await self._ensure_pipeline_handoff_context_in_session(context_id=context_id, cwd=cwd)

        def runtime_factory(session_id: str) -> Any:
            session_storage = SessionStorage()
            resume_messages = None
            if session_storage.exists(cwd, session_id):
                loaded = session_storage.load(cwd, session_id)
                resume_messages = SessionStorage.repair_interrupted(loaded) if loaded else None
            return create_agent_runtime(
                AgentFactoryOptions(
                    model=model,
                    session_id=session_id,
                    cwd=cwd,
                    resume_messages=resume_messages,
                )
            )

        try:
            aliyun_credential_ctx = (
                use_aliyun_credential(aliyun_credential) if aliyun_credential else contextlib.nullcontext()
            )
            with aliyun_credential_ctx:
                ctx = await self._task_store.get_or_create_context(
                    context_id=context_id,
                    cwd=cwd,
                    runtime_factory=runtime_factory,
                )
                if not hasattr(ctx.runtime, "agent_loop"):
                    ctx.runtime = runtime_factory(ctx.session_id)
                    self._task_store.mirror_context(ctx)
        except Exception as exc:
            self._log_executor_exception("runtime setup", task_id=task_id, context_id=context_id)
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
            task.state = TASK_STATE_FAILED
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=_("Task is already working."),
            )
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        lock = ctx.lock
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS)
        except TimeoutError:
            task.state = TASK_STATE_FAILED
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=_("Task is already working."),
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
                )
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_WORKING,
                )
                user_id_ctx = use_user_id(user_id) if user_id else contextlib.nullcontext()
                aliyun_credential_ctx = (
                    use_aliyun_credential(aliyun_credential) if aliyun_credential else contextlib.nullcontext()
                )
                with use_session_id(ctx.session_id), user_id_ctx, aliyun_credential_ctx:
                    self._configure_runtime_model(runtime, model, from_metadata=metadata_model is not None)
                    self._refresh_runtime_cloud_tools(runtime)
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
                    stream = _stream_a2a_normal_events(
                        runtime=runtime,
                        prompt=prompt,
                        cleanup_ledger=cleanup_ledger,
                        cleanup_publisher=cleanup_publisher,
                        cwd=cwd,
                        session_id=ctx.session_id,
                    )
                    async for event in stream:
                        text_chunk = await publish_stream_event(
                            event_queue,
                            task_id=task_id,
                            context_id=context_id,
                            event=event,
                            artifact_store=self._artifact_store,
                            permission_resolver=self._permission_resolver,
                            auto_approve_permissions=self._auto_approve_permissions,
                            exposure_types=self._thinking_exposure_types,
                        )
                        if text_chunk:
                            task.output_text.append(text_chunk)
                task.state = TASK_STATE_INPUT_REQUIRED
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                )
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_turn_completed()
            except asyncio.CancelledError:
                task.state = TASK_STATE_CANCELED
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_CANCELED,
                    text=_("Task canceled."),
                )
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_task_canceled()
            except Exception as exc:
                if _is_retryable_executor_error(exc):
                    task.state = TASK_STATE_INPUT_REQUIRED
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=TaskState.TASK_STATE_INPUT_REQUIRED,
                        text="A temporary error occurred. Please retry.",
                    )
                    self._task_store.mirror_task(task)
                    await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                    self._metrics.record_executor_error()
                else:
                    task.state = TASK_STATE_FAILED
                    self._log_executor_exception("streaming", task_id=task_id, context_id=context_id)
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=TaskState.TASK_STATE_FAILED,
                        text=self._sanitize_error(exc),
                    )
                    self._task_store.mirror_task(task)
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

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id or "unknown"
        if task_id and await self._task_store.cancel_task(task_id):
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
        if not any(_is_relative_to(resolved_cwd, root) for root in _allowed_cwd_roots()):
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

    def _pipeline_input_from_context(self, context: RequestContext, *, cwd: str) -> PipelineUserInput:
        message = getattr(context, "message", None)
        if not isinstance(message, Message):
            return normalize_pipeline_user_input(context.get_user_input())
        return parts_to_pipeline_input(message.parts, cwd=cwd)

    def validate_pipeline_message_request(self, message: Message) -> None:
        metadata = getattr(message, "metadata", None)
        try:
            cwd = self._resolve_cwd(metadata)
            pipeline_input = parts_to_pipeline_input(message.parts, cwd=cwd)
        except ValueError as exc:
            raise InvalidParamsError(sanitize_public_text(str(exc))) from exc
        model = self._resolve_model(metadata) or self._model
        self._validate_pipeline_request_input(pipeline_input, model=model)

    def _validate_pipeline_request_input(self, pipeline_input: PipelineUserInput, *, model: str | None = None) -> None:
        if pipeline_input.is_empty:
            raise InvalidParamsError("A2A server received empty input.")
        model = model or self._model
        if pipeline_input.has_images and not self._model_supports_image_input(model=model):
            raise InvalidParamsError(f"Current model {model} does not support image input.")

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
                return "Authentication required. Please configure your API credentials."
        if type(exc).__name__ == "AuthenticationError":
            return "Authentication required. Please configure your API credentials."
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status == 401:
            return "Authentication required. Please configure your API credentials."
        return _format_exception(exc)

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
        _snapshot_store, _journal, snapshot, _journal_events = state
        handoff = snapshot.get("normalHandoff")
        if not isinstance(handoff, dict):
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
        _snapshot_store, _journal, snapshot, _journal_events = state
        handoff = snapshot.get("normalHandoff")
        if not isinstance(handoff, dict):
            return
        summary = handoff.get("summary")
        cleanup_payload = None
        data = handoff.get("data")
        if isinstance(data, dict) and isinstance(data.get("cleanup"), dict):
            cleanup_payload = _cleanup_payload_from_private_ledger_or_unavailable(
                ledger_path=_default_cleanup_ledger_path(cwd=cwd, session_id=ctx.session_id),
                public_snapshot=snapshot,
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
        logger.exception("A2A executor %s failed (task_id=%s, context_id=%s)", stage, task_id, context_id)

    async def _publish_status(
        self,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
        state: int,
        text: str | None = None,
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
        await event_queue.enqueue_event(TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status))

    async def _publish_initial_task(
        self,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
        context: RequestContext,
    ) -> None:
        task = Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.Name(TaskState.TASK_STATE_SUBMITTED)),
        )
        message = getattr(context, "message", None)
        if isinstance(message, Message):
            task.history.append(message)
        await event_queue.enqueue_event(task)

    def _refresh_runtime_cloud_tools(self, runtime: Any) -> None:
        refresh_cloud_tools = getattr(runtime, "refresh_cloud_tools", None)
        if callable(refresh_cloud_tools):
            refresh_cloud_tools()
            return
        tool_registry = getattr(runtime, "tool_registry", None)
        if tool_registry is None:
            return
        from iac_code.services.cloud_credentials import CloudCredentials
        from iac_code.tools.cloud.registry import register_cloud_tools

        register_cloud_tools(tool_registry, CloudCredentials())

    def _configure_runtime_model(self, runtime: Any, model: str, *, from_metadata: bool) -> None:
        provider_manager = getattr(runtime, "provider_manager", None)
        reconfigure = getattr(provider_manager, "reconfigure", None)
        if not callable(reconfigure):
            return
        was_metadata_model = bool(getattr(runtime, "_iac_code_a2a_metadata_model_applied", False))
        if not from_metadata and not was_metadata_model:
            return

        from iac_code.config import load_credentials

        provider_key_override = getattr(provider_manager, "_provider_key_override", None)
        base_url_override = getattr(provider_manager, "_base_url_override", None)
        credentials = getattr(provider_manager, "_credentials", None)
        if not isinstance(credentials, dict) or provider_key_override is None:
            credentials = load_credentials(model=model)
        reconfigure(model, credentials, provider_key_override, base_url_override)
        setattr(runtime, "_iac_code_a2a_metadata_model_applied", from_metadata)

    async def _notify_terminal_task(self, *, task_id: str, context_id: str, state: str) -> None:
        if self._push_notifier is None:
            return
        try:
            await self._push_notifier.notify_task_state(task_id=task_id, context_id=context_id, state=state)
        except Exception:
            logger.warning("A2A push notification failed", exc_info=True)


def _is_retryable_executor_error(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.TransportError, ConnectionError))
