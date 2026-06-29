from __future__ import annotations

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

from iac_code.a2a.artifacts import (
    sanitize_public_artifact_text,
    sanitize_public_tool_output_data,
)
from iac_code.a2a.exposure import A2AExposureType, normalize_a2a_exposure_types
from iac_code.a2a.metadata_redaction import A2AMetadataEchoRedactor
from iac_code.i18n import _
from iac_code.types.stream_events import (
    ErrorEvent,
    MCPProgressEvent,
    MessageEndEvent,
    PermissionRequestEvent,
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
logger = logging.getLogger(__name__)
A2APermissionResolver: TypeAlias = Callable[[PermissionRequestEvent], "bool | Awaitable[bool]"]
IAC_CODE_SESSION_ID_METADATA_KEY = "iacCodeSessionId"
_METADATA_REDACTOR = A2AMetadataEchoRedactor()


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
        return sanitize_public_artifact_text(value)[:_METADATA_MAX_CHARS]
    if isinstance(value, dict):
        return {str(k): _truncate(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, _depth=_depth + 1) for v in value]
    return value


def _sanitize_trace_input(value: Any) -> Any:
    return _METADATA_REDACTOR.redact(_truncate(value))


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
    for warning in warnings[pushed_count:]:
        message = str(getattr(warning, "message", None) or warning)
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
                    "mcpWarning": {
                        "serverName": str(getattr(warning, "server_name", "")),
                        "code": str(getattr(warning, "code", "")),
                        "message": message,
                    }
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
        return metadata.to_dict()
    raw_bytes = raw.get("raw")
    if isinstance(raw_bytes, bytes):
        metadata = artifact_store.save_bytes(filename=filename, content=raw_bytes, media_type=str(media_type))
        return metadata.to_dict()
    return None


def _tool_result_metadata(result: Any, *, is_error: bool = False) -> Any:
    sanitized = sanitize_public_tool_output_data(result)
    if is_error and isinstance(sanitized, str):
        return sanitize_public_artifact_text(sanitized)
    return _truncate(sanitized)


def _artifact_update_event(*, task_id: str, context_id: str, metadata: dict[str, Any]) -> TaskArtifactUpdateEvent:
    artifact_metadata = {
        "uri": metadata["uri"],
        "mediaType": metadata["mediaType"],
        "byteSize": metadata["byteSize"],
        "sha256": metadata["sha256"],
    }
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
    auto_approve_permissions: bool = False,
    exposure_types: Any = None,
    iac_code_session_id: str | None = None,
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
                        "partialJson": _truncate(event.partial_json),
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
                        "input": _sanitize_trace_input(event.input),
                    }
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, ToolResultEvent):
        artifact_metadata = _extract_artifact_metadata(event.result, artifact_store)
        if A2AExposureType.TOOL_TRACE not in enabled_exposure_types:
            if artifact_metadata is not None:
                await event_queue.enqueue_event(
                    _artifact_update_event(task_id=task_id, context_id=context_id, metadata=artifact_metadata)
                )
            return None
        tool_metadata = {
            "status": "failed" if event.is_error else "completed",
            "toolUseId": event.tool_use_id,
            "name": event.tool_name,
            "result": _tool_result_metadata(event.result, is_error=event.is_error),
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
        progress_metadata = {
            "status": "progress",
            "toolUseId": event.tool_use_id or "",
            "name": "mcp__{}__{}".format(event.server_name, event.tool_name),
            "mcp": {
                "serverName": event.server_name,
                "toolName": event.tool_name,
                "progress": event.progress,
                "total": event.total,
                "message": _truncate(event.message) if event.message else None,
            },
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

    if isinstance(event, PermissionRequestEvent):
        approved = auto_approve_permissions
        if permission_resolver is not None:
            decision = permission_resolver(event)
            approved = bool(await decision) if inspect.isawaitable(decision) else bool(decision)
        if event.response_future is not None and not event.response_future.done():
            event.response_future.set_result(approved)
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
                        "toolName": event.tool_name,
                        "toolUseId": event.tool_use_id,
                        "toolInput": _sanitize_trace_input(event.tool_input),
                    }
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, MessageEndEvent):
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={
                "iac_code": {
                    "usage": {
                        "inputTokens": event.usage.input_tokens,
                        "outputTokens": event.usage.output_tokens,
                        "totalTokens": event.usage.total_tokens,
                    }
                }
            },
            iac_code_session_id=iac_code_session_id,
        )
        return None

    if isinstance(event, ErrorEvent):
        error_metadata: dict[str, Any] = {"retryable": event.is_retryable}
        if event.error_id:
            error_metadata["errorId"] = event.error_id
        if event.is_retryable:
            text = "A temporary error occurred. Please retry."
            state = TaskState.TASK_STATE_INPUT_REQUIRED
        else:
            raw = event.error or "Unknown error"
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
