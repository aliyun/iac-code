from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeAlias

from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import ParseDict

from iac_code.a2a.artifacts import sanitize_public_artifact_text
from iac_code.a2a.exposure import A2AExposureType, normalize_a2a_exposure_types
from iac_code.a2a.runtime_overrides import get_a2a_preferred_language
from iac_code.i18n import _, translate_message
from iac_code.mcp.progress import mcp_progress_metadata, mcp_progress_public_name
from iac_code.services.permissions.audit import (
    build_input_summary,
    build_redacted_tool_input,
    emit_auto_permission_audit,
    emit_permission_boundary_audit,
    is_aliyun_api_non_read_only_permission_event,
)
from iac_code.types.stream_events import (
    ErrorEvent,
    MCPProgressEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

_METADATA_MAX_CHARS = 4000
_ERROR_TEXT_MAX_CHARS = 1000
_METADATA_MAX_DEPTH = 32
_ARTIFACT_CONTAINER_KEYS = {"artifact", "artifacts"}
_ARTIFACT_PAYLOAD_KEYS = {"content", "bytes", "base64", "raw", "path"}
logger = logging.getLogger(__name__)
A2APermissionResolver: TypeAlias = Callable[[PermissionRequestEvent], "bool | Awaitable[bool]"]
IAC_CODE_SESSION_ID_METADATA_KEY = "iacCodeSessionId"


def iac_code_session_metadata(session_id: str) -> dict[str, Any]:
    return {"iac_code": {IAC_CODE_SESSION_ID_METADATA_KEY: session_id}}


def with_iac_code_session_metadata(metadata: dict[str, Any] | None, session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return metadata
    merged = dict(metadata or {})
    iac_code = dict(merged.get("iac_code") or {})
    iac_code[IAC_CODE_SESSION_ID_METADATA_KEY] = session_id
    merged["iac_code"] = iac_code
    return merged


def _truncate(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= _METADATA_MAX_DEPTH:
        return "[truncated-depth]"
    if isinstance(value, str):
        return value[:_METADATA_MAX_CHARS]
    if isinstance(value, dict):
        return {str(k): _truncate(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, _depth=_depth + 1) for v in value]
    return value


def _public_tool_input_metadata(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "inputSummary": build_input_summary(tool_name, tool_input),
        # This is protocol data consumed as AG-UI TOOL_CALL_ARGS, not an audit
        # record. Keep the canonical arguments here; the A2A wire boundary
        # applies the existing path-only safe-mode projection to a copy.
        "toolInput": copy.deepcopy(tool_input),
    }


def _public_permission_input_metadata(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "aliyun_api":
        return {"inputSummary": build_input_summary(tool_name, tool_input)}
    return {"toolInput": build_redacted_tool_input(tool_input)}


def _permission_request_event(event: Any) -> PermissionRequestEvent | None:
    while isinstance(event, SubPipelineStreamEvent):
        event = event.inner
    return event if isinstance(event, PermissionRequestEvent) else None


def make_text_part(text: str) -> Part:
    return Part(text=text)


async def publish_mcp_warnings(
    event_queue: Any,
    *,
    task_id: str,
    context_id: str,
    runtime: Any,
    state: int = TaskState.TASK_STATE_WORKING,
    iac_code_session_id: str | None = None,
) -> None:
    warnings = list(getattr(runtime, "mcp_config_warnings", None) or [])
    pushed_count = getattr(runtime, "_a2a_mcp_warnings_pushed_count", 0)
    if pushed_count >= len(warnings):
        return
    from iac_code.mcp.manager import mcp_warning_metadata

    for warning in warnings[pushed_count:]:
        warning_metadata = mcp_warning_metadata(warning)
        message = warning_metadata["message"]
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=state,
            message=_agent_text_message(
                task_id=task_id,
                context_id=context_id,
                text=_("MCP warning: {message}").format(message=message),
            ),
            metadata={
                "iac_code": {
                    "mcpWarning": warning_metadata,
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
    setattr(runtime, "_a2a_mcp_warnings_pushed_count", len(warnings))


def _extract_artifact_metadata(result: Any, artifact_store: Any | None) -> dict[str, Any] | None:
    if artifact_store is None or not isinstance(result, dict):
        return None
    raw = result.get("artifact")
    if not isinstance(raw, dict):
        return None
    filename = raw.get("filename")
    media_type = raw.get("mediaType") or raw.get("media_type") or "application/octet-stream"
    if not isinstance(filename, str):
        return None
    content = raw.get("content")
    if isinstance(content, str):
        metadata = artifact_store.save_text(filename=filename, content=content, media_type=str(media_type))
        return metadata.to_dict()
    encoded = raw.get("bytes") or raw.get("base64")
    if isinstance(encoded, str):
        metadata = artifact_store.save_base64(filename=filename, content=encoded, media_type=str(media_type))
        return metadata.to_dict()
    source_path = raw.get("path")
    if isinstance(source_path, str):
        path = Path(source_path)
        if not path.is_file():
            return None
        metadata = artifact_store.save_bytes(filename=filename, content=path.read_bytes(), media_type=str(media_type))
        return {**metadata.to_dict(), "sourcePath": str(path.resolve())}
    raw_bytes = raw.get("raw")
    if isinstance(raw_bytes, bytes):
        metadata = artifact_store.save_bytes(filename=filename, content=raw_bytes, media_type=str(media_type))
        return metadata.to_dict()
    return None


def _tool_result_metadata(
    result: Any,
    *,
    is_error: bool = False,
    public_path_roots: list[dict[str, str]] | None = None,
) -> Any:
    del is_error, public_path_roots
    return _tool_result_metadata_value(copy.deepcopy(result))


def _tool_result_metadata_value(value: Any, *, _depth: int = 0, _artifact_scope: bool = False) -> Any:
    """Build protobuf-safe trace metadata without applying redaction.

    Artifact payload bodies remain outside the trace metadata, matching the
    existing artifact externalization contract. All other already-exposed
    values are preserved, subject only to the existing depth/string bounds.
    """

    if _depth >= _METADATA_MAX_DEPTH:
        return "[truncated-depth]"
    if isinstance(value, str):
        return value[:_METADATA_MAX_CHARS]
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if _artifact_scope and key_lower in _ARTIFACT_PAYLOAD_KEYS:
                continue
            output[key_text] = _tool_result_metadata_value(
                item,
                _depth=_depth + 1,
                _artifact_scope=_artifact_scope or key_lower in _ARTIFACT_CONTAINER_KEYS,
            )
        return output
    if isinstance(value, list | tuple):
        return [_tool_result_metadata_value(item, _depth=_depth + 1, _artifact_scope=_artifact_scope) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:_METADATA_MAX_CHARS]


def _artifact_update_event(*, task_id: str, context_id: str, metadata: dict[str, Any]) -> TaskArtifactUpdateEvent:
    artifact_metadata = {
        "uri": metadata["uri"],
        "mediaType": metadata["mediaType"],
        "byteSize": metadata["byteSize"],
        "sha256": metadata["sha256"],
    }
    source_path = metadata.get("sourcePath")
    if isinstance(source_path, str) and source_path:
        artifact_metadata["sourcePath"] = source_path
    artifact = Artifact(
        artifact_id=str(metadata["artifactId"]),
        name=str(metadata["filename"]),
        parts=[
            Part(
                url=str(metadata["uri"]),
                filename=str(metadata["filename"]),
                media_type=str(metadata["mediaType"]),
            )
        ],
    )
    ParseDict(artifact_metadata, artifact.metadata)
    ParseDict(artifact_metadata, artifact.parts[0].metadata)
    return TaskArtifactUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        artifact=artifact,
        append=False,
        last_chunk=True,
    )


def _agent_text_message(*, task_id: str, context_id: str, text: str) -> Message:
    return Message(
        message_id=f"{task_id}-message",
        task_id=task_id,
        context_id=context_id,
        role=Role.ROLE_AGENT,
        parts=[make_text_part(text)],
    )


async def _enqueue_status(
    event_queue: Any,
    *,
    task_id: str,
    context_id: str,
    state: int,
    message: Message | None = None,
    metadata: dict[str, Any] | None = None,
    iac_code_session_id: str | None = None,
) -> None:
    update = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.Name(state), message=message),
    )
    metadata = with_iac_code_session_metadata(metadata, iac_code_session_id)
    if metadata is not None:
        ParseDict(metadata, update.metadata)
    await event_queue.enqueue_event(update)


async def publish_stream_event(
    event_queue: Any,
    *,
    task_id: str,
    context_id: str,
    event: Any,
    artifact_store: Any | None = None,
    permission_resolver: A2APermissionResolver | None = None,
    permission_input_registry: Any | None = None,
    auto_approve_permissions: bool = False,
    exposure_types: Any = None,
    iac_code_session_id: str | None = None,
    permission_wait_cwd: str | None = None,
    permission_wait_backup_service: Any | None = None,
    permission_wait_metrics: Any | None = None,
) -> str | None:
    enabled_exposure_types = normalize_a2a_exposure_types(exposure_types)

    if isinstance(event, TextDeltaEvent):
        if not event.text:
            return None
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            message=_agent_text_message(task_id=task_id, context_id=context_id, text=event.text),
            iac_code_session_id=iac_code_session_id,
        )
        return event.text

    if isinstance(event, ThinkingDeltaEvent):
        if event.is_metadata_only:
            return None
        if A2AExposureType.RAW_THINKING not in enabled_exposure_types:
            return None
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={"iac_code": {"thinking": {"type": "raw_thinking", "text": _truncate(event.text)}}},
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, ToolUseStartEvent):
        if A2AExposureType.TOOL_TRACE not in enabled_exposure_types:
            return None
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={"iac_code": {"tool": {"status": "started", "toolUseId": event.tool_use_id, "name": event.name}}},
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, ToolInputDeltaEvent):
        if A2AExposureType.TOOL_TRACE not in enabled_exposure_types:
            return None
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={
                "iac_code": {
                    "tool": {
                        "status": "input_delta",
                        "toolUseId": event.tool_use_id,
                        "partialJsonLength": len(event.partial_json),
                    }
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, ToolUseEndEvent):
        if A2AExposureType.TOOL_TRACE not in enabled_exposure_types:
            return None
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={
                "iac_code": {
                    "tool": {
                        "status": "input_complete",
                        "toolUseId": event.tool_use_id,
                        "name": event.name,
                        **_public_tool_input_metadata(event.name, event.input),
                    }
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, ToolResultEvent):
        artifact_metadata = _extract_artifact_metadata(event.result, artifact_store)
        if artifact_metadata is None:
            artifact_metadata = _extract_artifact_metadata(event.metadata, artifact_store)
        if A2AExposureType.TOOL_TRACE not in enabled_exposure_types:
            if artifact_metadata is not None:
                await event_queue.enqueue_event(
                    _artifact_update_event(task_id=task_id, context_id=context_id, metadata=artifact_metadata)
                )
            return None
        result_metadata = _tool_result_metadata(event.result, is_error=event.is_error)
        tool_metadata = {
            "status": "failed" if event.is_error else "completed",
            "toolUseId": event.tool_use_id,
            "name": event.tool_name,
            "result": result_metadata,
        }
        if artifact_metadata is not None:
            tool_metadata["artifact"] = artifact_metadata
            await event_queue.enqueue_event(
                _artifact_update_event(task_id=task_id, context_id=context_id, metadata=artifact_metadata)
            )
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={"iac_code": {"tool": tool_metadata}},
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, MCPProgressEvent):
        if A2AExposureType.TOOL_TRACE not in enabled_exposure_types:
            return None
        canonical_progress = mcp_progress_metadata(event)
        progress_metadata = {
            "status": "progress",
            "toolUseId": canonical_progress["toolUseId"],
            "name": mcp_progress_public_name(event),
            "mcp": {
                "serverName": canonical_progress["originalServerName"],
                "toolName": canonical_progress["originalToolName"],
                "progress": canonical_progress.get("progress"),
                "total": canonical_progress.get("total"),
                "message": canonical_progress.get("message"),
            },
            "mcpProgress": canonical_progress,
        }
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={"iac_code": {"tool": progress_metadata}},
            iac_code_session_id=iac_code_session_id,
        )
        return None

    permission_event = _permission_request_event(event)
    if permission_event is not None:
        if permission_input_registry is not None and permission_resolver is None and not auto_approve_permissions:
            await publish_interactive_permission_boundary(
                event_queue,
                permission_event=permission_event,
                permission_input_registry=permission_input_registry,
                task_id=task_id,
                context_id=context_id,
                iac_code_session_id=iac_code_session_id,
                permission_wait_cwd=permission_wait_cwd,
                permission_wait_backup_service=permission_wait_backup_service,
                permission_wait_metrics=permission_wait_metrics,
                wait_for_response=True,
            )
            return None
        approved = auto_approve_permissions
        if permission_resolver is not None:
            decision = permission_resolver(permission_event)
            approved = bool(await decision) if inspect.isawaitable(decision) else bool(decision)
        elif is_aliyun_api_non_read_only_permission_event(permission_event):
            approved = False
        if permission_event.response_future is not None and not permission_event.response_future.done():
            audit_ok = True
            if permission_resolver is not None:
                audit_ok = _emit_resolver_permission_audit(permission_event, approved)
            else:
                audit_ok = _emit_auto_permission_audit(permission_event, approved)
            if approved and not audit_ok:
                approved = False
            permission_event.response_future.set_result(approved)
        if A2AExposureType.TOOL_TRACE not in enabled_exposure_types:
            return None
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={
                "iac_code": {
                    "permission": {
                        "autoApproved": approved,
                        "toolName": permission_event.tool_name,
                        "toolUseId": permission_event.tool_use_id,
                        **_public_permission_input_metadata(permission_event.tool_name, permission_event.tool_input),
                    }
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, MessageEndEvent):
        usage = {
            "inputTokens": event.usage.input_tokens,
            "outputTokens": event.usage.output_tokens,
            "totalTokens": event.usage.total_tokens,
        }
        provider = getattr(event.usage, "provider", None)
        model = getattr(event.usage, "model", None)
        cached_input_tokens = getattr(event.usage, "cache_read_input_tokens", None)
        if isinstance(provider, str) and provider:
            usage["provider"] = provider
        if isinstance(model, str) and model:
            usage["model"] = model
        if isinstance(cached_input_tokens, int) and not isinstance(cached_input_tokens, bool):
            usage["cachedInputTokens"] = cached_input_tokens
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={
                "iac_code": {
                    "usage": usage
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, ErrorEvent):
        error_metadata: dict[str, Any] = {"retryable": event.is_retryable}
        if event.error_id:
            error_metadata["errorId"] = event.error_id
        language = get_a2a_preferred_language() or "en"
        if event.is_retryable:
            text = translate_message("A temporary error occurred. Please retry.", language=language)
            state = TaskState.TASK_STATE_INPUT_REQUIRED
        else:
            if event.i18n_message_id:
                raw = translate_message(event.i18n_message_id, language=language).format(
                    **(event.i18n_message_args or {})
                )
            else:
                raw = event.error or translate_message("Unknown error", language=language)
            # Provider ErrorEvent is an explicitly retained compatibility
            # exception; ordinary A2A payloads are projected at the wire edge.
            text = sanitize_public_artifact_text(raw)[:_ERROR_TEXT_MAX_CHARS]
            state = TaskState.TASK_STATE_FAILED
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=state,
            message=_agent_text_message(task_id=task_id, context_id=context_id, text=text),
            metadata={"iac_code": {"error": error_metadata}},
            iac_code_session_id=iac_code_session_id,
        )
        return None

    logger.debug("Skipping unmapped A2A stream event: %s", type(event).__name__)
    return None


async def publish_interactive_permission_boundary(
    event_queue: Any,
    *,
    permission_event: PermissionRequestEvent,
    permission_input_registry: Any,
    task_id: str,
    context_id: str,
    iac_code_session_id: str | None,
    permission_wait_cwd: str | None,
    permission_wait_backup_service: Any | None,
    permission_wait_metrics: Any | None = None,
    before_permission_backup: Callable[[Any], Awaitable[None]] | None = None,
    before_permission_claim_backup: Callable[[Any, dict[str, Any]], Awaitable[None]] | None = None,
    wait_for_response: bool,
) -> Any:
    """Publish one real external permission wait, optionally detaching Normal SSE."""

    pending = await permission_input_registry.register(
        permission_event,
        task_id=task_id,
        context_id=context_id,
        scope="normal",
    )
    try:
        if (
            iac_code_session_id is not None
            and permission_wait_cwd is not None
            and permission_wait_backup_service is not None
        ):
            await permission_input_registry.open_durable_boundary(
                pending,
                cwd=permission_wait_cwd,
                session_id=iac_code_session_id,
                permission_class="normal",
                backup_service=permission_wait_backup_service,
                metrics=permission_wait_metrics,
                before_backup=before_permission_backup,
                before_claim_backup=before_permission_claim_backup,
            )
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            metadata={
                "iac_code": {
                    "input": pending.envelope(),
                    "permission": {
                        "autoApproved": False,
                        "pending": True,
                        "toolName": permission_event.tool_name,
                        "toolUseId": permission_event.tool_use_id,
                    },
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
        if not wait_for_response:
            return pending
        future = permission_event.response_future
        if future is None:
            await permission_input_registry.fail(pending)
            return pending
        outcome = await asyncio.shield(future)
        from iac_code.types.stream_events import PermissionWaitOutcome

        if outcome is PermissionWaitOutcome.SUSPEND:
            return pending
        await publish_permission_input_received(
            event_queue,
            pending=pending,
            iac_code_session_id=iac_code_session_id,
        )
        return pending
    except BaseException:
        await permission_input_registry.fail(pending)
        raise
    finally:
        if wait_for_response:
            await permission_input_registry.complete(pending)


async def publish_permission_input_received(
    event_queue: Any,
    *,
    pending: Any,
    iac_code_session_id: str | None,
) -> None:
    await _enqueue_status(
        event_queue,
        task_id=pending.task_id,
        context_id=pending.context_id,
        state=TaskState.TASK_STATE_WORKING,
        metadata={
            "iac_code": {
                "inputReceived": {
                    "kind": "permission",
                    "inputId": pending.input_id,
                    "toolUseId": pending.request.tool_use_id,
                }
            }
        },
        iac_code_session_id=iac_code_session_id,
    )


def _emit_auto_permission_audit(
    request: PermissionRequestEvent,
    approved: bool,
    *,
    persistence_failure: bool = False,
) -> bool:
    if persistence_failure:
        return emit_permission_boundary_audit(
            request,
            decision="deny",
            scope="auto_deny",
            source="a2a_auto_persistence_failure",
            reason_type="persistence_failure",
            reason_detail="permission metadata persistence failed",
        )
    source = "a2a_auto_approve" if approved else "a2a_auto_deny"
    return emit_auto_permission_audit(
        request,
        decision="allow" if approved else "deny",
        scope="auto_approve" if approved else "auto_deny",
        source=source,
    )


def _emit_resolver_permission_audit(
    request: PermissionRequestEvent,
    approved: bool,
    *,
    persistence_failure: bool = False,
) -> bool:
    if request.permission_decision_audited and not persistence_failure:
        return True
    source = "a2a_resolver"
    reason_type = "a2a_resolver"
    reason_detail = "allow" if approved else "deny"
    if persistence_failure:
        source = "a2a_resolver_persistence_failure"
        reason_type = "persistence_failure"
        reason_detail = "permission metadata persistence failed"
    return emit_permission_boundary_audit(
        request,
        decision="allow" if approved else "deny",
        scope="a2a_resolver",
        source=source,
        reason_type=reason_type,
        reason_detail=reason_detail,
    )
