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
        writer.handle(ToolUseEndEvent(tool_use_id="tu_1", name="some_tool", input={"key": "val"}))
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
        writer.handle(ToolUseEndEvent(tool_use_id="tu_1", name="bash", input={"cmd": "ls"}))
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
        assert result["usage"] == {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

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
        writer.handle(ErrorEvent(error="something went wrong", is_retryable=False, error_id="err-abc123"))
        writer.finalize()

        result = json.loads(stream.getvalue())
        assert result["error"] == "something went wrong"
        assert result["error_id"] == "err-abc123"

    def test_error_event_is_sanitized(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        writer.handle(
            ErrorEvent(
                error="RuntimeError: Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml",
                is_retryable=False,
            )
        )
        writer.finalize()

        result = json.loads(stream.getvalue())
        assert "sk-live" not in result["error"]
        assert "/Users/alice" not in result["error"]

    def test_error_event_redacts_encoded_paths_and_preserves_valid_artifact_uri(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        encoded_path = "file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Fprojects%2Fdemo%2Ftemplate.yaml"
        uri = "iac-code-artifact://artifact-1/template.yaml"
        writer.handle(ErrorEvent(error=f"failed at {encoded_path}; see {uri}.", is_retryable=False))
        writer.finalize()

        result = json.loads(stream.getvalue())
        assert result["error"] == f"failed at [PATH]; see {uri}."

    def test_failed_tool_result_is_sanitized(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="bash",
                result="Tool failed: DB_PASSWORD=hunter2 at /Users/alice/.iac-code/settings.yml",
                is_error=True,
            )
        )
        writer.finalize()

        result = json.loads(stream.getvalue())
        tool = result["tool_uses"][0]
        assert tool["is_error"] is True
        assert "hunter2" not in tool["result"]
        assert "/Users/alice" not in tool["result"]

    def test_successful_tool_result_is_sanitized_without_losing_valid_artifact_uri(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        encoded_path = "file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Fprojects%2Fdemo%2Ftemplate.yaml"
        uri = "iac-code-artifact://artifact-1/template.yaml"

        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="bash",
                result={"message": f"wrote {encoded_path}", "artifact": {"filename": "template.yaml", "uri": uri}},
                is_error=False,
            )
        )
        writer.finalize()

        result = json.loads(stream.getvalue())
        tool = result["tool_uses"][0]
        rendered = json.dumps(tool, ensure_ascii=False)
        assert tool["result"]["message"] == "wrote [PATH]"
        assert tool["result"]["artifact"]["uri"] == uri
        assert "%2FUsers" not in rendered
        assert ".iac-code" not in rendered

    def test_synthetic_max_turns_does_not_overwrite_previous_usage(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        writer.handle(MessageEndEvent(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)))
        writer.handle(MessageEndEvent(stop_reason="max_turns", usage=Usage()))
        writer.finalize()

        result = json.loads(stream.getvalue())
        assert result["usage"] == {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }


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

    def test_tool_result_omits_null_metadata_for_field_stability(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(ToolResultEvent(tool_use_id="tu_1", tool_name="bash", result="done", metadata=None))

        data = json.loads(stream.getvalue())
        assert data["type"] == "tool_result"
        assert "metadata" not in data

    def test_tool_result_preserves_non_null_metadata(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="complete_step",
                result="done",
                metadata={"step_result": {"step_id": "x"}},
            )
        )

        data = json.loads(stream.getvalue())
        assert data["type"] == "tool_result"
        assert data["metadata"] == {"step_result": {"step_id": "x"}}

    def test_failed_tool_result_redacts_encoded_malformed_artifact_uri(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        encoded_uri = (
            "iac-code-artifact%3A%2F%2Fartifact-1%2FC%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo%5Ctemplate.yaml"
        )

        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="bash",
                result=f"failed at {encoded_uri}",
                is_error=True,
                metadata={"note": encoded_uri},
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert "[PATH]" in rendered
        assert "%5CUsers" not in rendered
        assert "Users" not in rendered
        assert ".iac-code" not in rendered

    def test_successful_tool_result_and_metadata_are_sanitized(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        encoded_uri = (
            "iac-code-artifact%3A%2F%2Fartifact-1%2FC%3A%5CUsers%5CAlice%20Smith"
            "%5C.iac-code%5Cprojects%5Cdemo%5Ctemplate.yaml"
        )

        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="complete_step",
                result=f"ok {encoded_uri}",
                is_error=False,
                metadata={
                    "artifact": {
                        "filename": r"C:\Users\Alice Smith\.iac-code\projects\demo\template.yaml",
                        "Content": "secret content",
                        "uri": encoded_uri,
                    },
                    "note": r"file:///Users/Alice Smith/.iac-code/projects/demo/template.yaml",
                },
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert "[PATH]" in rendered
        assert "secret content" not in rendered
        assert "Alice Smith" not in rendered
        assert "%5CAlice" not in rendered
        assert ".iac-code" not in rendered

    def test_error_event_is_sanitized(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ErrorEvent(
                error="RuntimeError: Cookie: sid=session-secret; refresh=refresh-secret",
                is_retryable=False,
            )
        )

        data = json.loads(stream.getvalue())
        assert data["type"] == "error"
        assert "session-secret" not in data["error"]
        assert "refresh-secret" not in data["error"]

    def test_error_event_preserves_error_id(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(ErrorEvent(error="boom", is_retryable=False, error_id="err-abc123"))

        data = json.loads(stream.getvalue())
        assert data["type"] == "error"
        assert data["error_id"] == "err-abc123"

    def test_failed_tool_result_is_sanitized(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="bash",
                result="Tool failed: DB_PASSWORD=hunter2 at /Users/alice/.iac-code/settings.yml",
                is_error=True,
            )
        )

        data = json.loads(stream.getvalue())
        assert data["is_error"] is True
        assert "hunter2" not in data["result"]
        assert "/Users/alice" not in data["result"]

    def test_failed_tool_result_metadata_is_sanitized(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="complete_step",
                result="Tool failed: DB_PASSWORD=hunter2",
                is_error=True,
                metadata={
                    "step_result": {
                        "step_id": "x",
                        "error": "Schema failed DB_PASSWORD=hunter2 at /Users/alice/.iac-code/settings.yml",
                    }
                },
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert "hunter2" not in rendered
        assert "/Users/alice" not in rendered
        assert data["metadata"]["step_result"]["error"] == "Schema failed DB_PASSWORD=[REDACTED] at [PATH]"

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
