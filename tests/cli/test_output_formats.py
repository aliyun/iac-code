"""Tests for output format writers."""

from __future__ import annotations

import asyncio
import io
import json

from iac_code.cli.output_formats import (
    JsonWriter,
    OutputFormat,
    StreamJsonWriter,
    TextWriter,
    create_writer,
)
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.tools.cloud.aliyun.result_contract import ALIYUN_HTTP_METADATA_KEY
from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY, ResultStorage
from iac_code.types.permissions import PermissionAuditMetadata, PermissionAuditSettings, PermissionResult
from iac_code.types.stream_events import (
    TOOL_RENDER_METADATA_KEY,
    TOOL_RENDER_RESULT_COMPACT_KEY,
    ErrorEvent,
    MCPProgressEvent,
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    SubAgentToolEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)
from iac_code.types.usage_attribution import UsageAttribution

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
        assert tool["input_summary"] == {"tool_name": "bash", "fields": {"cmd": {"type": "str"}}}
        assert "input" not in tool
        assert tool["result"] == "file.txt"
        assert tool["is_error"] is False
        assert result["usage"] == {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def test_tool_end_replaces_fragmentary_start_name(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        writer.handle(ToolUseStartEvent(tool_use_id="tu_1", name="read_"))
        writer.handle(ToolUseEndEvent(tool_use_id="tu_1", name="read_file", input={"path": "main.py"}))
        writer.finalize()

        tool = json.loads(stream.getvalue())["tool_uses"][0]
        assert tool["name"] == "read_file"
        assert tool["input_summary"]["tool_name"] == "read_file"

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

    def test_error_event_is_preserved_for_local_json(self) -> None:
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
        assert "sk-live" in result["error"]
        assert "/Users/alice" in result["error"]

    def test_error_event_preserves_encoded_path_and_artifact_uri(self) -> None:
        stream = io.StringIO()
        writer = JsonWriter(stream)
        encoded_path = "file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Fprojects%2Fdemo%2Ftemplate.yaml"
        uri = "iac-code-artifact://artifact-1/template.yaml"
        writer.handle(ErrorEvent(error=f"failed at {encoded_path}; see {uri}.", is_retryable=False))
        writer.finalize()

        result = json.loads(stream.getvalue())
        assert result["error"] == f"failed at {encoded_path}; see {uri}."

    def test_failed_tool_result_is_preserved_for_local_json(self) -> None:
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
        assert "hunter2" in tool["result"]
        assert "/Users/alice" in tool["result"]

    def test_successful_tool_result_preserves_path_and_artifact_uri(self) -> None:
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
        assert tool["result"]["message"] == f"wrote {encoded_path}"
        assert tool["result"]["artifact"]["uri"] == uri
        assert "%2FUsers" in rendered
        assert ".iac-code" in rendered

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
    def test_terminal_usage_attribution_is_internal_for_direct_and_subpipeline_events(self) -> None:
        attribution = UsageAttribution(
            logical_provider_key="openai_compatible",
            wire_provider_key="dashscope_token_plan",
            telemetry_provider_name="dashscope",
            adapter_name="qwen",
            requested_model="qwen3.8-max",
            actual_model="qwen3.7-plus",
        )
        terminal = MessageEndEvent(
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=2),
            usage_attribution=attribution,
        )
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(terminal)
        writer.handle(
            SubPipelineStreamEvent(
                sub_pipeline_id="sub-1",
                candidate_index=0,
                inner=terminal,
            )
        )
        direct, nested = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert "usage_attribution" not in direct
        assert "usage_attribution" not in nested["inner"]
        assert "qwen3.7-plus" not in stream.getvalue()

    def test_text_delta_emitted(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(TextDeltaEvent(text="hi"))

        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["type"] == "text_delta"
        assert data["text"] == "hi"

    def test_thinking_delta_omits_internal_provider_metadata(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ThinkingDeltaEvent(
                text="reasoning",
                block_index=2,
                provider_metadata={"provider": "anthropic", "signature": "opaque-signature"},
            )
        )

        data = json.loads(stream.getvalue())
        assert data == {"text": "reasoning", "type": "thinking_delta"}
        assert "opaque-signature" not in stream.getvalue()

    def test_metadata_only_thinking_delta_is_not_emitted(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)

        writer.handle(
            ThinkingDeltaEvent(
                text="",
                provider_metadata={"provider": "gemini", "extra_content": {"google": {"thought_signature": "sig"}}},
            )
        )

        assert stream.getvalue() == ""

    def test_tool_events_emitted(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(ToolUseStartEvent(tool_use_id="tu_1", name="bash"))
        writer.handle(ToolInputDeltaEvent(tool_use_id="tu_1", partial_json='ature":"signature-secret"'))
        writer.handle(
            ToolUseEndEvent(
                tool_use_id="tu_1",
                name="aliyun_api",
                input={"product": "ros", "action": "CreateStack", "params": {"Signature": "signature-secret"}},
            )
        )
        writer.handle(ToolResultEvent(tool_use_id="tu_1", tool_name="bash", result="done"))

        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 4
        first = json.loads(lines[0])
        delta = json.loads(lines[1])
        end = json.loads(lines[2])
        second = json.loads(lines[3])
        assert first["type"] == "tool_use_start"
        assert delta["type"] == "tool_input_delta"
        assert delta["partial_json_length"] == len('ature":"signature-secret"')
        assert "partial_json" not in delta
        assert end["type"] == "tool_use_end"
        assert "input" not in end
        assert end["input_summary"]["tool_name"] == "aliyun_api"
        assert second["type"] == "tool_result"
        assert "signature-secret" not in stream.getvalue()

    def test_tool_use_start_omits_internal_render_metadata(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolUseStartEvent(
                tool_use_id="tu_1",
                name="infraguard_scan",
                metadata={TOOL_RENDER_METADATA_KEY: {"display_name": "InfraGuard scan"}},
            )
        )

        data = json.loads(stream.getvalue())
        assert data == {
            "tool_use_id": "tu_1",
            "name": "infraguard_scan",
            "type": "tool_use_start",
        }

    def test_tool_result_omits_null_metadata_for_field_stability(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(ToolResultEvent(tool_use_id="tu_1", tool_name="bash", result="done", metadata=None))

        data = json.loads(stream.getvalue())
        assert data["type"] == "tool_result"
        assert "metadata" not in data

    def test_tool_result_preserves_paths_without_emitting_projection_roots(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="bash",
                result="/Users/alice/project/src/app.py\n/Users/alice/private/secret.txt",
                public_path_roots=[{"path": "/Users/alice/project", "label": "."}],
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert data["type"] == "tool_result"
        assert data["result"] == "/Users/alice/project/src/app.py\n/Users/alice/private/secret.txt"
        assert "public_path_roots" not in data
        assert "publicPathRoots" not in rendered
        assert "/Users/alice" in rendered

    def test_tool_result_preserves_embedded_file_content_json_string(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="infraguard_scan",
                result=json.dumps(
                    {
                        "file_path": "templates/demo.yml",
                        "file_sha256": "sha256-value",
                        "file_content": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n",
                    },
                    ensure_ascii=False,
                ),
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert "ROSTemplateFormatVersion" in rendered
        assert "file_content" in rendered
        assert "sha256-value" in rendered

    def test_tool_result_preserves_externalized_file_content_preview(self, tmp_path) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        raw_result = json.dumps(
            {
                "file_path": "templates/demo.yml",
                "file_sha256": "sha256-value",
                "file_content": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n" + ("X" * 500),
            },
            ensure_ascii=False,
        )
        preview = (
            ResultStorage(
                storage_dir=str(tmp_path / "tool-results"),
                max_inline_chars=10,
                preview_chars=180,
            )
            .process("tu_1", raw_result)
            .content
        )
        assert "ROSTemplateFormatVersion" in preview
        writer.handle(ToolResultEvent(tool_use_id="tu_1", tool_name="infraguard_scan", result=preview))

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert "ROSTemplateFormatVersion" in rendered
        assert "file_content" in rendered
        assert "sha256-value" in rendered

    def test_subagent_tool_event_omits_raw_child_input(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)

        writer.handle(
            SubAgentToolEvent(
                parent_tool_use_id="parent",
                child_tool_name="aliyun_api",
                child_tool_input={
                    "product": "ROS",
                    "action": "CreateStack",
                    "params": {"Signature": "signature-secret", "AccessKeySecret": "secret-value"},
                    "headers": {"Authorization": "Bearer bearer-secret"},
                },
            )
        )

        data = json.loads(stream.getvalue())
        rendered = stream.getvalue()
        assert data["type"] == "subagent_tool"
        assert data["child_tool_name"] == "aliyun_api"
        assert data["child_input_summary"]["tool_name"] == "aliyun_api"
        assert "child_tool_input" not in data
        assert "signature-secret" not in rendered
        assert "secret-value" not in rendered
        assert "bearer-secret" not in rendered

    def test_sub_pipeline_tool_use_omits_raw_inner_input(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)

        writer.handle(
            SubPipelineStreamEvent(
                sub_pipeline_id="sub-1",
                candidate_index=0,
                inner=ToolUseEndEvent(
                    tool_use_id="tu_aliyun",
                    name="aliyun_api",
                    input={
                        "product": "ros",
                        "action": "CreateStack",
                        "params": {"AccessKeySecret": "secret-value", "StackName": "demo"},
                    },
                ),
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert data["type"] == "sub_pipeline_stream"
        assert data["inner"]["type"] == "tool_use_end"
        assert "input" not in data["inner"]
        assert data["inner"]["input_summary"]["tool_name"] == "aliyun_api"
        assert data["inner"]["input_summary"]["params_fields"] == sorted(
            [fingerprint_text("StackName"), fingerprint_text("AccessKeySecret")]
        )
        assert data["inner"]["input_summary"]["params_field_count"] == 2
        assert "AccessKeySecret" not in rendered
        assert "StackName" not in rendered
        assert "secret-value" not in rendered

    def test_sub_pipeline_permission_request_omits_future_and_uses_summary(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        loop = asyncio.new_event_loop()
        try:
            writer.handle(
                SubPipelineStreamEvent(
                    sub_pipeline_id="sub-1",
                    candidate_index=0,
                    inner=PermissionRequestEvent(
                        tool_name="aliyun_api",
                        tool_input={
                            "product": "ros",
                            "action": "CreateStack",
                            "params": {"AccessKeySecret": "secret-value", "StackName": "demo"},
                        },
                        tool_use_id="tu_aliyun",
                        response_future=loop.create_future(),
                    ),
                )
            )
        finally:
            loop.close()

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert data["type"] == "sub_pipeline_stream"
        assert data["inner"]["type"] == "permission_request"
        assert data["inner"]["tool_name"] == "aliyun_api"
        assert data["inner"]["input_summary"]["tool_name"] == "aliyun_api"
        assert "tool_input" not in data["inner"]
        assert "response_future" not in data["inner"]
        assert "AccessKeySecret" not in rendered
        assert "secret-value" not in rendered

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

    def test_tool_result_omits_internal_externalized_result_path_metadata(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="infraguard_scan",
                result="preview",
                metadata={
                    EXTERNALIZED_RESULT_PATH_METADATA_KEY: "/Users/alice/.iac-code/tool-results/raw.txt",
                    "step_result": {"step_id": "x"},
                },
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert EXTERNALIZED_RESULT_PATH_METADATA_KEY not in rendered
        assert "/Users/alice" not in rendered
        assert data["metadata"] == {"step_result": {"step_id": "x"}}

    def test_tool_result_omits_internal_aliyun_http_metadata_only(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="aliyun_api",
                result='{"Business":"value"}',
                metadata={
                    ALIYUN_HTTP_METADATA_KEY: {
                        "contract_version": "aliyun_body_v1",
                        "header_count": 1,
                    },
                    "ros_validation": {"warning_count": 0},
                },
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert ALIYUN_HTTP_METADATA_KEY not in rendered
        assert "aliyun_body_v1" not in rendered
        assert data["metadata"] == {"ros_validation": {"warning_count": 0}}

    def test_tool_result_omits_internal_render_metadata(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="infraguard_scan",
                result="preview",
                metadata={
                    TOOL_RENDER_METADATA_KEY: {TOOL_RENDER_RESULT_COMPACT_KEY: "passed · 0 findings"},
                    "step_result": {"step_id": "x"},
                },
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert TOOL_RENDER_METADATA_KEY not in rendered
        assert "passed · 0 findings" not in rendered
        assert data["metadata"] == {"step_result": {"step_id": "x"}}

    def test_tool_result_omits_metadata_when_only_internal_render_metadata_remains(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="infraguard_scan",
                result="preview",
                metadata={TOOL_RENDER_METADATA_KEY: {TOOL_RENDER_RESULT_COMPACT_KEY: "passed · 0 findings"}},
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert TOOL_RENDER_METADATA_KEY not in rendered
        assert "passed · 0 findings" not in rendered
        assert "metadata" not in data

    def test_failed_tool_result_preserves_encoded_malformed_artifact_uri(self) -> None:
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
        assert encoded_uri in rendered
        assert "%5CUsers" in rendered
        assert ".iac-code" in rendered

    def test_successful_tool_result_and_metadata_are_preserved(self) -> None:
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
        assert encoded_uri in rendered
        assert "secret content" in rendered
        assert "Alice Smith" in rendered
        assert "%5CAlice" in rendered
        assert ".iac-code" in rendered

    def test_error_event_is_preserved_for_local_stream_json(self) -> None:
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
        assert "session-secret" in data["error"]
        assert "refresh-secret" in data["error"]

    def test_error_event_preserves_error_id(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ErrorEvent(
                error="boom",
                is_retryable=False,
                error_id="err-abc123",
                i18n_message_id="internal message id",
                i18n_message_args={"state": "internal argument"},
            )
        )

        data = json.loads(stream.getvalue())
        assert data["type"] == "error"
        assert data["error_id"] == "err-abc123"
        assert "i18n_message_id" not in data
        assert "i18n_message_args" not in data

    def test_permission_request_omits_internal_audit_context(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        loop = asyncio.new_event_loop()
        try:
            writer.handle(
                PermissionRequestEvent(
                    tool_name="write_file",
                    tool_input={
                        "path": "x",
                        "content": "secret",
                        "customerEmail": "alice@example.com",
                        "customer-prod-123": "tenant-id",
                    },
                    tool_use_id="tu_1",
                    response_future=loop.create_future(),
                    permission_result=PermissionResult(
                        behavior="ask",
                        audit=PermissionAuditMetadata(
                            scope="once",
                            source="permission_pipeline",
                            operation={"action": "DeleteStack"},
                        ),
                    ),
                    audit_context={
                        "session_id": "session-secret",
                        "settings": PermissionAuditSettings(max_file_bytes=123),
                        "metadata": PermissionAuditMetadata(
                            scope="once",
                            source="permission_pipeline",
                            operation={"action": "CreateStack"},
                        ),
                    },
                )
            )
        finally:
            loop.close()

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert data["type"] == "permission_request"
        assert data["tool_name"] == "write_file"
        assert data["tool_use_id"] == "tu_1"
        assert data["input_summary"] == {
            "tool_name": "write_file",
            "fields": {
                "path": {"type": "str"},
                "content": {"type": "str"},
                fingerprint_text("customerEmail"): {"type": "str"},
                fingerprint_text("customer-prod-123"): {"type": "str"},
            },
        }
        assert "tool_input" not in data
        assert "response_future" not in data
        assert "permission_result" not in data
        assert "audit_context" not in data
        assert "secret" not in rendered
        assert "session-secret" not in rendered
        assert "customerEmail" not in rendered
        assert "customer-prod-123" not in rendered
        assert "max_file_bytes" not in rendered
        assert "CreateStack" not in rendered
        assert "DeleteStack" not in rendered

    def test_permission_request_emits_safe_input_summary(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)

        writer.handle(
            PermissionRequestEvent(
                tool_name="aliyun_api",
                tool_input={
                    "product": "ros",
                    "action": "CreateStack",
                    "params": {
                        "StackName": "demo",
                        "AccessKeySecret": "secret-value",
                        "Signature": "signature-secret",
                    },
                    "body": {"PrivateKey": "private-secret"},
                },
                tool_use_id="tu_aliyun",
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert "tool_input" not in data
        assert data["input_summary"]["tool_name"] == "aliyun_api"
        assert data["input_summary"]["product"] == "ros"
        assert data["input_summary"]["action"] == "CreateStack"
        assert "params_fields" in data["input_summary"]
        for forbidden in (
            "secret-value",
            "signature-secret",
            "private-secret",
            "AccessKeySecret",
            "Signature",
            "PrivateKey",
        ):
            assert forbidden not in rendered

    def test_failed_tool_result_is_preserved(self) -> None:
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
        assert "hunter2" in data["result"]
        assert "/Users/alice" in data["result"]

    def test_failed_tool_result_metadata_is_preserved(self) -> None:
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
        assert "hunter2" in rendered
        assert "/Users/alice" in rendered
        assert data["metadata"]["step_result"]["error"].endswith("/Users/alice/.iac-code/settings.yml")

    def test_failed_tool_result_metadata_preserves_public_paths(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="complete_step",
                result="Tool failed",
                is_error=True,
                metadata={
                    "step_result": {
                        "step_id": "x",
                        "error": "Schema failed at /Users/alice/project/logs/result.txt",
                    }
                },
                public_path_roots=[{"path": "/Users/alice/project", "label": "."}],
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert "/Users/alice" in rendered
        assert data["metadata"]["step_result"]["error"] == "Schema failed at /Users/alice/project/logs/result.txt"

    def test_ordinary_mcp_tool_result_is_preserved_for_local_stream(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)
        writer.handle(
            ToolResultEvent(
                tool_use_id="tu_1",
                tool_name="mcp__remote__echo",
                result=(
                    "command=IAC_PRIVATE_COMMAND_ARG_MARKER_56 "
                    "metadata=IAC_PRIVATE_NESTED_METADATA_MARKER_56 "
                    "url=https://example.test/mcp?Signature=IAC_PRIVATE_QUERY_MARKER_56 "
                    "path=file:///Users/alice/.iac-code/settings.yml"
                ),
                metadata={
                    "mcp": {
                        "meta": {
                            "nested": "IAC_PRIVATE_NESTED_METADATA_MARKER_56",
                            "callback": "https://user:pass@example.test/mcp",
                        }
                    }
                },
            )
        )

        data = json.loads(stream.getvalue())
        rendered = json.dumps(data, ensure_ascii=False)
        assert "IAC_PRIVATE_COMMAND_ARG_MARKER_56" in rendered
        assert "IAC_PRIVATE_NESTED_METADATA_MARKER_56" in rendered
        assert "IAC_PRIVATE_QUERY_MARKER_56" in rendered
        assert "/Users/alice" in rendered
        assert "user:pass" in rendered

    def test_mcp_progress_includes_canonical_public_metadata(self) -> None:
        stream = io.StringIO()
        writer = StreamJsonWriter(stream)

        writer.handle(
            MCPProgressEvent(
                server_name="yuque space",
                tool_name="search/docs",
                public_name="mcp__yuque_space__search_docs_8d3f",
                progress=1,
                total=3,
                message="phase api_key=sk-live-secret /Users/alice/.iac-code/settings.yml",
                tool_use_id="tool-59",
            )
        )

        payload = json.loads(stream.getvalue())
        assert payload["mcpProgress"] == {
            "status": "progress",
            "toolUseId": "tool-59",
            "publicName": "mcp__yuque_space__search_docs_8d3f",
            "originalServerName": "yuque space",
            "originalToolName": "search/docs",
            "progress": 1,
            "total": 3,
            "message": "phase api_key=[REDACTED] [PATH]",
        }
        assert payload["public_name"] == "mcp__yuque_space__search_docs_8d3f"
        assert "sk-live-secret" not in stream.getvalue()
        assert "/Users/alice" not in stream.getvalue()

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
