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
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    StreamEvent,
    TextDeltaEvent,
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
    return sanitize_public_tool_output_data(event.result)


def _public_tool_metadata(event: ToolResultEvent, metadata: Any) -> Any:
    return sanitize_public_tool_output_data(_sanitize_public_value(metadata) if event.is_error else metadata)


def _sanitize_public_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_public_text(value)
    if isinstance(value, list):
        return [_sanitize_public_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_public_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_public_value(item) for key, item in value.items()}
    return value


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
            self._tool_uses.setdefault(event.tool_use_id, {})["input"] = event.input
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
        data = dataclasses.asdict(event)
        if isinstance(event, ErrorEvent):
            data["error"] = sanitize_public_text(event.error)
        elif isinstance(event, ToolResultEvent):
            if data.get("metadata") is None:
                data.pop("metadata", None)
            elif "metadata" in data:
                data["metadata"] = _public_tool_metadata(event, data["metadata"])
            data["result"] = _public_tool_result(event)
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
