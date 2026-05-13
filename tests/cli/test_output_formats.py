"""Tests for output format writers."""

from __future__ import annotations

import io
import json

from iac_code.cli.output_formats import (
    JsonWriter,
    OutputFormat,
    StreamJsonWriter,
    TextWriter,
    create_writer,
)
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

# ---------------------------------------------------------------------------
# TestTextWriter
# ---------------------------------------------------------------------------


class TestTextWriter:
    def test_text_delta_written(self) -> None:
        stream = io.StringIO()
        writer = TextWriter(stream)
        writer.handle(TextDeltaEvent(text="hello "))
        writer.handle(TextDeltaEvent(text="world"))
        writer.finalize()
        assert stream.getvalue() == "hello world\n"

    def test_non_text_events_ignored(self) -> None:
        stream = io.StringIO()
        writer = TextWriter(stream)
        writer.handle(MessageStartEvent(message_id="msg_1"))
        writer.handle(ToolUseStartEvent(tool_use_id="tu_1", name="some_tool"))
        writer.handle(ToolUseEndEvent(tool_use_id="tu_1", input={"key": "val"}))
        writer.handle(ToolResultEvent(tool_use_id="tu_1", tool_name="some_tool", result="ok"))
        writer.finalize()
        assert stream.getvalue() == ""

    def test_finalize_adds_trailing_newline(self) -> None:
        stream = io.StringIO()
        writer = TextWriter(stream)
        writer.handle(TextDeltaEvent(text="hi"))
        writer.finalize()
        assert stream.getvalue().endswith("\n")

    def test_empty_output_no_newline(self) -> None:
        stream = io.StringIO()
        writer = TextWriter(stream)
        writer.finalize()
        assert stream.getvalue() == ""


# ---------------------------------------------------------------------------
# TestJsonWriter
# ---------------------------------------------------------------------------


class TestJsonWriter:
    def test_collects_text_and_tool_results(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        writer.handle(TextDeltaEvent(text="hello "))
        writer.handle(TextDeltaEvent(text="world"))
        writer.handle(ToolUseStartEvent(tool_use_id="tu_1", name="bash"))
        writer.handle(ToolUseEndEvent(tool_use_id="tu_1", input={"cmd": "ls"}))
        writer.handle(ToolResultEvent(tool_use_id="tu_1", tool_name="bash", result="file.txt"))
        writer.handle(MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=20)))
        writer.finalize()

        result = json.loads(stream.getvalue())
        assert result["text"] == "hello world"
        assert len(result["tool_uses"]) == 1
        tool = result["tool_uses"][0]
        assert tool["name"] == "bash"
        assert tool["input"] == {"cmd": "ls"}
        assert tool["result"] == "file.txt"
        assert tool["is_error"] is False
        assert result["usage"] == {"input_tokens": 10, "output_tokens": 20}

    def test_empty_output(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        writer.finalize()

        result = json.loads(stream.getvalue())
        assert result["text"] == ""
        assert result["tool_uses"] == []
        assert result["usage"] is None

    def test_error_event_captured(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        writer.handle(ErrorEvent(error="something went wrong", is_retryable=False))
        writer.finalize()

        result = json.loads(stream.getvalue())
        assert result["error"] == "something went wrong"


# ---------------------------------------------------------------------------
# TestStreamJsonWriter
# ---------------------------------------------------------------------------


class TestStreamJsonWriter:
    def test_text_delta_emitted(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(TextDeltaEvent(text="hi"))

        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["type"] == "text_delta"
        assert data["text"] == "hi"

    def test_tool_events_emitted(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(ToolUseStartEvent(tool_use_id="tu_1", name="bash"))
        writer.handle(ToolResultEvent(tool_use_id="tu_1", tool_name="bash", result="done"))

        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["type"] == "tool_use_start"
        assert second["type"] == "tool_result"

    def test_finalize_is_noop(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.finalize()
        assert stream.getvalue() == ""


# ---------------------------------------------------------------------------
# create_writer factory
# ---------------------------------------------------------------------------


class TestCreateWriter:
    def test_creates_text_writer(self) -> None:
        writer = create_writer(OutputFormat.TEXT)
        assert isinstance(writer, TextWriter)

    def test_creates_json_writer(self) -> None:
        writer = create_writer(OutputFormat.JSON)
        assert isinstance(writer, JsonWriter)

    def test_creates_stream_json_writer(self) -> None:
        writer = create_writer(OutputFormat.STREAM_JSON)
        assert isinstance(writer, StreamJsonWriter)

    def test_passes_stream_to_writer(self) -> None:
        stream = io.StringIO()
        writer = create_writer(OutputFormat.TEXT, stream)
        assert isinstance(writer, TextWriter)
        writer.handle(TextDeltaEvent(text="test"))
        writer.finalize()
        assert stream.getvalue() == "test\n"
