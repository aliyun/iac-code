"""Output format writers for non-interactive (headless) mode.

Each writer consumes StreamEvents and writes formatted output to a file-like stream.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from enum import Enum
from typing import IO, Any

from iac_code.a2a.artifacts import sanitize_public_tool_output_data
from iac_code.mcp.progress import mcp_progress_metadata
from iac_code.mcp.redaction import sanitize_mcp_public_data
from iac_code.services.permissions.audit import build_input_summary
from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY
from iac_code.types.stream_events import (
    TOOL_RENDER_METADATA_KEY,
    ErrorEvent,
    MCPProgressEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    StreamEvent,
    SubAgentToolEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from iac_code.utils.public_errors import sanitize_public_text


class OutputFormat(str, Enum):
    """Supported output formats for non-interactive mode."""

    TEXT = "text"
    JSON = "json"
    STREAM_JSON = "stream-json"


def _public_tool_result(event: ToolResultEvent) -> Any:
    result = sanitize_public_tool_output_data(event.result, public_path_roots=event.public_path_roots)
    return sanitize_mcp_public_data(result, fallback_summary="") if event.tool_name.startswith("mcp__") else result


def _public_tool_metadata(event: ToolResultEvent, metadata: Any) -> Any:
    public_metadata = _strip_internal_tool_metadata(metadata)
    if event.is_error:
        public_metadata = _sanitize_public_value(public_metadata, public_path_roots=event.public_path_roots)
    result = sanitize_public_tool_output_data(
        public_metadata,
        public_path_roots=event.public_path_roots,
    )
    return sanitize_mcp_public_data(result, fallback_summary="") if event.tool_name.startswith("mcp__") else result


def _strip_internal_tool_metadata(metadata: Any) -> Any:
    if isinstance(metadata, dict):
        return {
            str(key): _strip_internal_tool_metadata(value)
            for key, value in metadata.items()
            if key not in {EXTERNALIZED_RESULT_PATH_METADATA_KEY, TOOL_RENDER_METADATA_KEY}
        }
    if isinstance(metadata, list):
        return [_strip_internal_tool_metadata(item) for item in metadata]
    if isinstance(metadata, tuple):
        return [_strip_internal_tool_metadata(item) for item in metadata]
    return metadata


def _sanitize_public_value(value: Any, *, public_path_roots: list[dict[str, str]] | None = None) -> Any:
    if isinstance(value, str):
        return sanitize_public_text(value, public_path_roots=public_path_roots)
    if isinstance(value, list):
        return [_sanitize_public_value(item, public_path_roots=public_path_roots) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_public_value(item, public_path_roots=public_path_roots) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _sanitize_public_value(item, public_path_roots=public_path_roots) for key, item in value.items()
        }
    return value


def stream_json_event_data(event: StreamEvent) -> dict[str, Any]:
    """Return the public stream-json representation for a stream event."""
    if isinstance(event, ThinkingDeltaEvent):
        # Provider signatures and encrypted thinking payloads are internal
        # round-trip state, not part of the public stream-json contract.
        return {"text": event.text, "type": event.type}
    if isinstance(event, ToolUseStartEvent):
        return {
            "tool_use_id": event.tool_use_id,
            "name": event.name,
            "type": event.type,
        }
    if isinstance(event, PermissionRequestEvent):
        return {
            "input_summary": build_input_summary(event.tool_name, event.tool_input),
            "tool_name": event.tool_name,
            "tool_use_id": event.tool_use_id,
            "type": event.type,
        }
    if isinstance(event, ToolInputDeltaEvent):
        return {
            "partial_json_length": len(event.partial_json),
            "tool_use_id": event.tool_use_id,
            "type": event.type,
        }
    if isinstance(event, ToolUseEndEvent):
        return {
            "input_summary": build_input_summary(event.name, event.input),
            "name": event.name,
            "tool_use_id": event.tool_use_id,
            "type": event.type,
        }
    if isinstance(event, SubAgentToolEvent):
        return {
            "child_input_summary": build_input_summary(event.child_tool_name, event.child_tool_input),
            "child_tool_name": event.child_tool_name,
            "is_done": event.is_done,
            "is_error": event.is_error,
            "parent_tool_use_id": event.parent_tool_use_id,
            "type": event.type,
        }
    if isinstance(event, SubPipelineStreamEvent):
        return {
            "candidate_index": event.candidate_index,
            "inner": stream_json_event_data(event.inner),
            "sub_pipeline_id": event.sub_pipeline_id,
            "type": event.type,
        }
    if isinstance(event, MCPProgressEvent):
        data = dataclasses.asdict(event)
        canonical_progress = mcp_progress_metadata(event)
        data["server_name"] = canonical_progress["originalServerName"]
        data["tool_name"] = canonical_progress["originalToolName"]
        data["public_name"] = canonical_progress["publicName"]
        data["message"] = canonical_progress.get("message")
        data["mcpProgress"] = canonical_progress
        return data

    data = dataclasses.asdict(event)
    if isinstance(event, ErrorEvent):
        data["error"] = sanitize_public_text(event.error)
    elif isinstance(event, ToolResultEvent):
        data.pop("public_path_roots", None)
        if data.get("metadata") is None:
            data.pop("metadata", None)
        elif "metadata" in data:
            data["metadata"] = _public_tool_metadata(event, data["metadata"])
            if data["metadata"] == {}:
                data.pop("metadata", None)
        data["result"] = _public_tool_result(event)
    return data


def _stream_json_event_data(event: StreamEvent) -> dict[str, Any]:
    return stream_json_event_data(event)


class TextWriter:
    """Writes only assistant text content to the output stream.

    Tool calls and other events are silently consumed.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout
        self._has_output = False

    def handle(self, event: StreamEvent) -> None:
        if isinstance(event, TextDeltaEvent):
            self._stream.write(event.text)
            self._stream.flush()
            self._has_output = True
        elif isinstance(event, ErrorEvent):
            sys.stderr.write(f"Error: {sanitize_public_text(event.error)}\n")
            sys.stderr.flush()

    def finalize(self) -> None:
        if self._has_output:
            self._stream.write("\n")
            self._stream.flush()


class JsonWriter:
    """Collects all events and writes a single JSON object on finalize.

    The output is a JSON object with keys: text, tool_uses, usage, and
    optionally error. usage is null when no MessageEndEvent was seen.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout
        self._text_chunks: list[str] = []
        self._tool_uses: dict[str, dict[str, Any]] = {}
        self._usage: dict[str, int] | None = None
        self._error: str | None = None
        self._error_id: str | None = None

    def handle(self, event: StreamEvent) -> None:
        if isinstance(event, TextDeltaEvent):
            self._text_chunks.append(event.text)
        elif isinstance(event, ToolUseStartEvent):
            self._tool_uses.setdefault(event.tool_use_id, {})["name"] = event.name
        elif isinstance(event, ToolUseEndEvent):
            entry = self._tool_uses.setdefault(event.tool_use_id, {})
            entry["name"] = event.name
            entry["input_summary"] = build_input_summary(event.name, event.input)
        elif isinstance(event, ToolResultEvent):
            entry = self._tool_uses.setdefault(event.tool_use_id, {})
            entry["result"] = _public_tool_result(event)
            entry["is_error"] = event.is_error
        elif isinstance(event, MessageEndEvent):
            usage = {
                "input_tokens": event.usage.input_tokens,
                "output_tokens": event.usage.output_tokens,
                "cache_creation_input_tokens": event.usage.cache_creation_input_tokens,
                "cache_read_input_tokens": event.usage.cache_read_input_tokens,
            }
            is_empty_synthetic_max_turns = (
                event.stop_reason == "max_turns" and self._usage is not None and not any(usage.values())
            )
            if not is_empty_synthetic_max_turns:
                self._usage = usage
        elif isinstance(event, ErrorEvent):
            self._error = sanitize_public_text(event.error)
            self._error_id = event.error_id

    def finalize(self) -> None:
        result: dict[str, Any] = {
            "text": "".join(self._text_chunks),
            "tool_uses": list(self._tool_uses.values()),
            "usage": self._usage,
        }
        if self._error is not None:
            result["error"] = self._error
        if self._error_id is not None:
            result["error_id"] = self._error_id
        self._stream.write(json.dumps(result, ensure_ascii=False))
        self._stream.write("\n")
        self._stream.flush()


class StreamJsonWriter:
    """Writes each event as a newline-delimited JSON (NDJSON) line immediately on handle."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout

    def handle(self, event: StreamEvent) -> None:
        if isinstance(event, ThinkingDeltaEvent) and event.is_metadata_only:
            return
        data = stream_json_event_data(event)
        self._stream.write(json.dumps(data, ensure_ascii=False, default=str))
        self._stream.write("\n")
        self._stream.flush()

    def finalize(self) -> None:
        pass


def create_writer(fmt: OutputFormat, stream: IO[str] | None = None) -> TextWriter | JsonWriter | StreamJsonWriter:
    """Create the appropriate writer for the given output format."""
    if fmt == OutputFormat.TEXT:
        return TextWriter(stream)
    if fmt == OutputFormat.JSON:
        return JsonWriter(stream)
    if fmt == OutputFormat.STREAM_JSON:
        return StreamJsonWriter(stream)
    raise ValueError(f"Unknown output format: {fmt}")
