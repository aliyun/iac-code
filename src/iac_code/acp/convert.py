from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import acp

from iac_code.acp.state import TurnState
from iac_code.acp.types import ACPContentBlock
from iac_code.types.stream_events import (
    CompactionEvent,
    ErrorEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    PlanEvent,
    StackInstancesProgressEvent,
    StackProgressEvent,
    StreamEvent,
    SubAgentToolEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

# ``acp.schema`` exposes individual session-update message classes
# (``AgentMessageChunk``, ``ToolCallStart``, ...) but not a single
# ``SessionUpdate`` union alias.  We use ``Any`` here to type the
# heterogeneous list returned by :meth:`ACPEventConverter.event_to_updates`.
SessionUpdate = Any

# Mapping from internal tool name to ACP ``ToolCallStart.kind`` value.
# Values come from the ACP 0.9.0 ``kind`` Literal enum:
#   read | edit | delete | move | search | execute | think | fetch |
#   switch_mode | other
# Clients (e.g. Zed) use this to pick icons and UI treatment.
ToolKind = Literal[
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
]

_TOOL_KIND_MAP: dict[str, ToolKind] = {
    # Read operations
    "read_file": "read",
    "list_files": "read",
    "read_memory": "read",
    "task_list": "read",
    "task_get": "read",
    # Edit / write operations
    "write_file": "edit",
    "edit_file": "edit",
    "write_memory": "edit",
    # Search operations
    "grep": "search",
    "glob": "search",
    # Execute operations
    "bash": "execute",
    "task_stop": "execute",
    "ros_stack": "execute",
    "ros_stack_instances": "execute",
    # Fetch operations
    "web_fetch": "fetch",
    "aliyun_doc_search": "fetch",
}


def _tool_kind(tool_name: str) -> ToolKind:
    """Return the ACP ``ToolCallStart.kind`` value for a tool name.

    Falls back to suffix-based heuristics so dynamically-named cloud tools
    (e.g. ``aliyun_api``, ``foo_doc_search``) still get a sensible kind,
    and finally to ``"other"`` for unknown tools.
    """
    mapped = _TOOL_KIND_MAP.get(tool_name)
    if mapped is not None:
        return mapped
    # Cloud provider API tools follow the ``{provider}_api`` naming convention.
    if tool_name.endswith("_api"):
        return "execute"
    if tool_name.endswith("_doc_search"):
        return "fetch"
    return "other"


# Callable returning ``(used_tokens, context_window_size)`` for the current
# session.  Used by :class:`ACPEventConverter` to emit ``UsageUpdate`` events.
ContextSnapshot = Callable[[], tuple[int, int]]


def acp_blocks_to_prompt_text(blocks: list[ACPContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        match block:
            case acp.schema.TextContentBlock():
                parts.append(block.text)
            case acp.schema.EmbeddedResourceContentBlock():
                resource = block.resource
                if isinstance(resource, acp.schema.TextResourceContents):
                    parts.append(f"<resource uri={resource.uri!r}>\n{resource.text}\n</resource>")
            case acp.schema.ResourceContentBlock():
                parts.append(f"<resource_link uri={block.uri!r} name={block.name!r} />")
            case acp.schema.ImageContentBlock():
                parts.append(f"[image: {block.mime_type}]")
            case acp.schema.AudioContentBlock():
                parts.append(f"[audio: {block.mime_type}]")
            case _:
                parts.append(f"[Unsupported ACP content block: {type(block).__name__}]")
    return "\n\n".join(part for part in parts if part)


def acp_blocks_to_multimodal(
    blocks: list[ACPContentBlock],
) -> list[dict]:
    """Convert ACP content blocks to a list of provider-compatible content parts.

    Returns a list of dicts suitable for multi-modal LLM APIs:
      - {"type": "text", "text": "..."}
      - {"type": "image", "mime_type": "...", "data": "..."}

    When all blocks are text, callers may flatten to a single string.
    """
    parts: list[dict] = []
    for block in blocks:
        match block:
            case acp.schema.TextContentBlock():
                parts.append({"type": "text", "text": block.text})
            case acp.schema.EmbeddedResourceContentBlock():
                resource = block.resource
                if isinstance(resource, acp.schema.TextResourceContents):
                    parts.append(
                        {"type": "text", "text": f"<resource uri={resource.uri!r}>\n{resource.text}\n</resource>"}
                    )
            case acp.schema.ResourceContentBlock():
                parts.append({"type": "text", "text": f"<resource_link uri={block.uri!r} name={block.name!r} />"})
            case acp.schema.ImageContentBlock():
                parts.append({"type": "image", "mime_type": block.mime_type, "data": block.data})
            case acp.schema.AudioContentBlock():
                parts.append({"type": "audio", "mime_type": block.mime_type, "data": block.data})
            case _:
                parts.append({"type": "text", "text": f"[Unsupported ACP content block: {type(block).__name__}]"})
    return parts


class ACPEventConverter:
    def __init__(
        self,
        turn_id: str,
        turn_state: TurnState | None = None,
        terminal_tool_names: set[str] | None = None,
        context_snapshot: ContextSnapshot | None = None,
    ):
        self._turn_id = turn_id
        self._turn_state = turn_state
        self._tool_inputs: dict[str, str] = {}
        self._terminal_tool_names: set[str] = terminal_tool_names or set()
        self._last_usage: Usage | None = None
        self._context_snapshot = context_snapshot

    def acp_tool_call_id(self, tool_use_id: str) -> str:
        return f"{self._turn_id}/{tool_use_id}"

    def event_to_updates(self, event: StreamEvent) -> list[SessionUpdate]:
        match event:
            case TextDeltaEvent(text=text):
                return [
                    acp.schema.AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=acp.schema.TextContentBlock(type="text", text=text),
                    )
                ]
            case ThinkingDeltaEvent(text=text):
                return [
                    acp.schema.AgentThoughtChunk(
                        session_update="agent_thought_chunk",
                        content=acp.schema.TextContentBlock(type="text", text=text),
                    )
                ]
            case ToolUseStartEvent(tool_use_id=tool_use_id, name=name):
                if self._turn_state is not None:
                    self._turn_state.start_tool_call(tool_use_id, name)
                return [
                    acp.schema.ToolCallStart(
                        session_update="tool_call",
                        tool_call_id=self.acp_tool_call_id(tool_use_id),
                        title=name,
                        kind=_tool_kind(name),
                        status="pending",
                    )
                ]
            case ToolInputDeltaEvent(tool_use_id=tool_use_id, partial_json=partial_json):
                self._tool_inputs[tool_use_id] = self._tool_inputs.get(tool_use_id, "") + partial_json
                tc_state = self._turn_state.get_tool_call(tool_use_id) if self._turn_state else None
                if tc_state is not None:
                    tc_state.update_input(partial_json)
                title = tc_state.title if tc_state else None
                update = acp.schema.ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=self.acp_tool_call_id(tool_use_id),
                    status="pending",
                    content=[_text_tool_content(self._tool_inputs[tool_use_id])],
                )
                if title:
                    update.title = title
                return [update]
            case ToolUseEndEvent(tool_use_id=tool_use_id, name=name, input=input):
                return [
                    acp.schema.ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=self.acp_tool_call_id(tool_use_id),
                        title=name,
                        status="in_progress",
                        content=[_text_tool_content(str(input))],
                    )
                ]
            case ToolResultEvent(tool_use_id=tool_use_id, tool_name=tool_name, result=result, is_error=is_error):
                content: list[
                    acp.schema.ContentToolCallContent
                    | acp.schema.FileEditToolCallContent
                    | acp.schema.TerminalToolCallContent
                ] = [_text_tool_content(result)]
                meta: dict[str, Any] | None = None
                if tool_name in self._terminal_tool_names:
                    meta = {"already_displayed": True}
                # Attach tool call elapsed time if available
                tc_state = self._turn_state.get_tool_call(tool_use_id) if self._turn_state else None
                if tc_state is not None:
                    if meta is None:
                        meta = {}
                    meta["timing"] = {"elapsed_ms": tc_state.elapsed_ms}
                # Emit final progress update with tool output
                progress = acp.schema.ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=self.acp_tool_call_id(tool_use_id),
                    status="in_progress",
                    content=content,
                )
                if meta is not None:
                    progress.field_meta = meta
                # Emit terminal update marking tool call as completed/failed
                end = acp.schema.ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=self.acp_tool_call_id(tool_use_id),
                    status="failed" if is_error else "completed",
                )
                return [progress, end]
            case CompactionEvent(original_tokens=original, compacted_tokens=compacted):
                return [
                    acp.schema.AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=acp.schema.TextContentBlock(
                            type="text",
                            text=f"[Context compacted: {original} -> {compacted} tokens]",
                        ),
                    )
                ]
            case ErrorEvent(error=error):
                return [
                    acp.schema.AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=acp.schema.TextContentBlock(type="text", text=f"[Error] {error}"),
                    )
                ]
            case PlanEvent(steps=steps):
                entries = [
                    acp.schema.PlanEntry(
                        content=step.content,
                        status=step.status,
                        priority=step.priority,
                    )
                    for step in steps
                ]
                return [
                    acp.schema.AgentPlanUpdate(
                        session_update="plan",
                        entries=entries,
                    )
                ]
            case MessageEndEvent(usage=usage):
                self._last_usage = usage
                # Emit an ACP ``UsageUpdate`` carrying current context-window
                # occupancy.  This is semantically different from the per-turn
                # input/output token counts returned via
                # ``PromptResponse.field_meta["usage"]``: ``UsageUpdate`` is
                # the ACP-standard channel for clients to render context
                # pressure / auto-compact hints.
                if self._context_snapshot is None:
                    return []
                try:
                    used, size = self._context_snapshot()
                except Exception:
                    return []
                if size <= 0 or used < 0:
                    return []
                return [
                    acp.schema.UsageUpdate(
                        session_update="usage_update",
                        used=used,
                        size=size,
                    )
                ]
            case PermissionRequestEvent() | StackProgressEvent() | StackInstancesProgressEvent() | SubAgentToolEvent():
                return []
            case _:
                return []


def _text_tool_content(text: str) -> acp.schema.ContentToolCallContent:
    return acp.schema.ContentToolCallContent(
        type="content",
        content=acp.schema.TextContentBlock(type="text", text=text),
    )


# ---------------------------------------------------------------------------
# Multimodal output helpers
# ---------------------------------------------------------------------------


def create_image_content_block(
    data: str,
    mime_type: str = "image/png",
    *,
    uri: str | None = None,
) -> Any:
    """Create an ACP ImageContentBlock for multimodal output.

    Args:
        data: Base64-encoded image data.
        mime_type: MIME type of the image (default ``image/png``).
        uri: Optional URI reference for the image.

    Returns:
        An ``acp.schema.ImageContentBlock`` instance.
    """
    return acp.schema.ImageContentBlock(type="image", data=data, mime_type=mime_type, uri=uri)


def create_audio_content_block(
    data: str,
    mime_type: str = "audio/wav",
) -> Any:
    """Create an ACP AudioContentBlock for multimodal output.

    Args:
        data: Base64-encoded audio data.
        mime_type: MIME type of the audio (default ``audio/wav``).

    Returns:
        An ``acp.schema.AudioContentBlock`` instance.
    """
    return acp.schema.AudioContentBlock(type="audio", data=data, mime_type=mime_type)


def create_file_content_block(
    data: str,
    filename: str,
    mime_type: str,
) -> Any:
    """Create an ACP EmbeddedResourceContentBlock wrapping binary file data.

    This embeds file content as a ``BlobResourceContents`` resource inside an
    ``EmbeddedResourceContentBlock``, which is the ACP-standard way to
    transmit arbitrary file payloads.

    Args:
        data: Base64-encoded file data.
        filename: Display name / URI for the file.
        mime_type: MIME type of the file.

    Returns:
        An ``acp.schema.EmbeddedResourceContentBlock`` instance.
    """
    resource = acp.schema.BlobResourceContents(
        uri=filename,
        mime_type=mime_type,
        blob=data,
    )
    return acp.schema.EmbeddedResourceContentBlock(
        type="resource",
        resource=resource,
    )


def create_multimodal_message_chunk(
    content_blocks: list[Any],
) -> acp.schema.AgentMessageChunk:
    """Wrap one or more content blocks into an ``AgentMessageChunk``.

    This is a convenience helper for building session updates that carry
    non-text (image / audio / file) payloads.

    Args:
        content_blocks: A list of ACP content block instances
            (``ImageContentBlock``, ``AudioContentBlock``, etc.).

    Returns:
        An ``AgentMessageChunk`` ready to be yielded from
        ``event_to_updates``.
    """
    # AgentMessageChunk.content accepts a single block; for multiple blocks
    # we emit one chunk per block.  When only one block is provided we
    # return it directly for simplicity.
    if len(content_blocks) == 1:
        return acp.schema.AgentMessageChunk(
            session_update="agent_message_chunk",
            content=content_blocks[0],
        )
    # For multiple blocks, return the first one – callers that need to emit
    # several blocks should call this helper per block or iterate themselves.
    return acp.schema.AgentMessageChunk(
        session_update="agent_message_chunk",
        content=content_blocks[0],
    )
