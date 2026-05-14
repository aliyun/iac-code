"""Output format writers for non-interactive (headless) mode.

Each writer consumes StreamEvents and writes formatted output to a file-like stream.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from enum import Enum
from typing import IO, Any

from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    StreamEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


class OutputFormat(str, Enum):
    """Supported output formats for non-interactive mode."""

    TEXT = "text"
    JSON = "json"
    STREAM_JSON = "stream-json"


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
            sys.stderr.write(f"Error: {event.error}\n")
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

    def handle(self, event: StreamEvent) -> None:
        if isinstance(event, TextDeltaEvent):
            self._text_chunks.append(event.text)
        elif isinstance(event, ToolUseStartEvent):
            self._tool_uses.setdefault(event.tool_use_id, {})["name"] = event.name
        elif isinstance(event, ToolUseEndEvent):
            self._tool_uses.setdefault(event.tool_use_id, {})["input"] = event.input
        elif isinstance(event, ToolResultEvent):
            entry = self._tool_uses.setdefault(event.tool_use_id, {})
            entry["result"] = event.result
            entry["is_error"] = event.is_error
        elif isinstance(event, MessageEndEvent):
            self._usage = {
                "input_tokens": event.usage.input_tokens,
                "output_tokens": event.usage.output_tokens,
            }
        elif isinstance(event, ErrorEvent):
            self._error = event.error

    def finalize(self) -> None:
        result: dict[str, Any] = {
            "text": "".join(self._text_chunks),
            "tool_uses": list(self._tool_uses.values()),
            "usage": self._usage,
        }
        if self._error is not None:
            result["error"] = self._error
        self._stream.write(json.dumps(result, ensure_ascii=False))
        self._stream.write("\n")
        self._stream.flush()


class StreamJsonWriter:
    """Writes each event as a newline-delimited JSON (NDJSON) line immediately on handle."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout

    def handle(self, event: StreamEvent) -> None:
        data = dataclasses.asdict(event)
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
