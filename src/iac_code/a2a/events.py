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

from iac_code.types.stream_events import (
    ErrorEvent,
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
_METADATA_MAX_DEPTH = 32
logger = logging.getLogger(__name__)
A2APermissionResolver: TypeAlias = Callable[[PermissionRequestEvent], "bool | Awaitable[bool]"]


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


def make_text_part(text: str) -> Part:
    return Part(text=text)


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


def _tool_result_metadata(result: Any) -> Any:
    if not isinstance(result, dict):
        return _truncate(result)
    data = dict(result)
    raw_artifact = data.get("artifact")
    if isinstance(raw_artifact, dict) and any(
        key in raw_artifact for key in ("content", "bytes", "base64", "raw", "path")
    ):
        artifact = dict(raw_artifact)
        artifact.pop("content", None)
        artifact.pop("bytes", None)
        artifact.pop("base64", None)
        artifact.pop("raw", None)
        artifact.pop("path", None)
        data["artifact"] = artifact
    return _truncate(data)


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
) -> None:
    update = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.Name(state), message=message),
    )
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
) -> str | None:
    if isinstance(event, TextDeltaEvent):
        if not event.text:
            return None
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            message=_agent_text_message(task_id=task_id, context_id=context_id, text=event.text),
        )
        return event.text

    if isinstance(event, ThinkingDeltaEvent):
        return None

    if isinstance(event, ToolUseStartEvent):
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            metadata={"iac_code": {"tool": {"status": "started", "toolUseId": event.tool_use_id, "name": event.name}}},
        )
        return None

    if isinstance(event, ToolInputDeltaEvent):
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
        )
        return None

    if isinstance(event, ToolUseEndEvent):
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
                        "input": _truncate(event.input),
                    }
                }
            },
        )
        return None

    if isinstance(event, ToolResultEvent):
        artifact_metadata = _extract_artifact_metadata(event.result, artifact_store)
        tool_metadata = {
            "status": "failed" if event.is_error else "completed",
            "toolUseId": event.tool_use_id,
            "name": event.tool_name,
            "result": _tool_result_metadata(event.result),
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
        )
        return None

    if isinstance(event, PermissionRequestEvent):
        approved = auto_approve_permissions
        if permission_resolver is not None:
            decision = permission_resolver(event)
            approved = bool(await decision) if inspect.isawaitable(decision) else bool(decision)
        if event.response_future is not None and not event.response_future.done():
            event.response_future.set_result(approved)
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
                        "toolInput": _truncate(event.tool_input),
                    }
                }
            },
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
        )
        return None

    if isinstance(event, ErrorEvent):
        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_INPUT_REQUIRED if event.is_retryable else TaskState.TASK_STATE_FAILED,
            message=_agent_text_message(
                task_id=task_id,
                context_id=context_id,
                text="A temporary error occurred. Please retry."
                if event.is_retryable
                else "An internal error occurred.",
            ),
        )
        return None

    logger.debug("Skipping unmapped A2A stream event: %s", type(event).__name__)
    return None
