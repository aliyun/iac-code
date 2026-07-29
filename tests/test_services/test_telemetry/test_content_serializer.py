"""Tests for content_serializer module."""

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from iac_code.agent.message import Message as AgentMessage
from iac_code.agent.message import ToolResultBlock, ToolUseBlock
from iac_code.providers.base import ContentBlock, Message
from iac_code.services.telemetry.content_serializer import (
    serialize_input_messages,
    serialize_output_messages,
    serialize_system_instructions,
    serialize_tool_arguments,
    serialize_tool_definitions,
    serialize_tool_result,
)
from iac_code.tools.result_storage import ResultStorage


@dataclass
class FakeMessage:
    role: str
    content: Any


@dataclass
class FakeContentBlock:
    type: str
    text: str | None = None
    tool_use_id: str | None = None
    name: str | None = None
    content: str | None = None
    is_error: bool = False
    metadata: dict[str, Any] | None = None


@dataclass
class FakeToolDef:
    name: str
    description: str
    input_schema: dict | None = None


@dataclass
class FakeToolResult:
    content: Any


def _aliyun_http_metadata() -> dict[str, Any]:
    return {
        "contract_version": "aliyun_body_v1",
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "status": 200,
        "status_class": "2xx",
        "response_mode": "json",
        "body_format": "json",
        "headers_present": True,
        "body_present": True,
        "content_type_present": True,
        "size_present": True,
        "content_encoding_present": False,
        "headers_nonempty": True,
        "header_count": 1,
        "content_state": "inline_final",
    }


def test_serialize_input_messages_text():
    msgs = [FakeMessage(role="user", content="Hello")]
    result = json.loads(serialize_input_messages(msgs))
    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert result[0]["parts"][0]["type"] == "text"
    assert result[0]["parts"][0]["content"] == "Hello"


def test_serialize_input_messages_with_blocks():
    blocks = [
        FakeContentBlock(type="text", text="Hi"),
        FakeContentBlock(type="tool_use", name="bash", tool_use_id="t1"),
    ]
    msgs = [FakeMessage(role="assistant", content=blocks)]
    result = json.loads(serialize_input_messages(msgs))
    assert result[0]["parts"][0] == {"type": "text", "content": "Hi"}
    assert result[0]["parts"][1]["type"] == "tool_call"
    assert result[0]["parts"][1]["name"] == "bash"
    assert result[0]["parts"][1]["id"] == "t1"


def test_serialize_input_messages_tool_result():
    blocks = [
        FakeContentBlock(type="tool_result", tool_use_id="t1", text="result output"),
    ]
    msgs = [FakeMessage(role="tool", content=blocks)]
    result = json.loads(serialize_input_messages(msgs))
    part = result[0]["parts"][0]
    assert part["type"] == "tool_call_response"
    assert part["id"] == "t1"
    assert part["response"] == "result output"


def test_serialize_input_messages_tool_result_redacts_embedded_file_content():
    blocks = [
        FakeContentBlock(
            type="tool_result",
            tool_use_id="t1",
            text=json.dumps(
                {
                    "file_content": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n",
                    "file_sha256": "sha256-value",
                },
                ensure_ascii=False,
            ),
        ),
    ]
    result = json.loads(serialize_input_messages([FakeMessage(role="tool", content=blocks)]))

    response = result[0]["parts"][0]["response"]
    assert "ROSTemplateFormatVersion" not in response
    assert "file_content" in response
    assert "sha256-value" in response


def test_serialize_input_messages_tool_result_uses_provider_content_field():
    result = json.loads(
        serialize_input_messages(
            [
                Message(
                    role="user",
                    content=[
                        ContentBlock(
                            type="tool_result",
                            tool_use_id="t1",
                            content=json.dumps(
                                {
                                    "file_content": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n",
                                    "file_sha256": "sha256-value",
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ],
                )
            ]
        )
    )

    response = result[0]["parts"][0]["response"]
    assert response
    assert "ROSTemplateFormatVersion" not in response
    assert "file_content" in response
    assert "sha256-value" in response


def test_serialize_input_messages_does_not_infer_http_fields_from_unmarked_aliyun_result():
    secret_result = json.dumps(
        {
            "status": 200,
            "headers": {"authorization": "credential-secret", "host": "private.example.com"},
            "body": {"bucket": "bucket-secret", "value": "marker-secret"},
            "content_type": "application/json",
            "content_encoding": None,
            "size": 99,
            "artifact_path": "/private/customer/artifact.bin",
        }
    )
    messages = [
        Message.assistant_tool_use(
            tool_use_id="aliyun-call",
            name="aliyun_api",
            input={"params": {"bucket": "input-bucket-secret"}},
        ),
        Message.tool_result(tool_use_id="aliyun-call", content=secret_result),
    ]

    serialized = serialize_input_messages(messages)
    result = json.loads(serialized)
    response = json.loads(result[1]["parts"][0]["response"])

    assert response == {
        "is_error": False,
        "headers_present": False,
        "body_present": False,
        "content_type_present": False,
        "content_encoding_present": False,
        "size_present": False,
        "artifact_present": False,
    }
    for forbidden in (
        "credential-secret",
        "private.example.com",
        "bucket-secret",
        "marker-secret",
        "/private/customer",
    ):
        assert forbidden not in serialized


def test_serialize_input_messages_does_not_infer_agent_loop_unmarked_aliyun_result():
    messages = [
        AgentMessage(
            role="assistant",
            content=[ToolUseBlock(id="aliyun-call", name="aliyun_api", input={})],
        ),
        AgentMessage(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="aliyun-call",
                    content=json.dumps(
                        {
                            "status": 200,
                            "headers": {"authorization": "credential-secret"},
                            "body": {"value": "business-secret"},
                        }
                    ),
                )
            ],
        ),
    ]

    serialized = serialize_input_messages(messages)
    result = json.loads(serialized)

    assert result[0]["parts"][0]["id"] == "aliyun-call"
    response = json.loads(result[1]["parts"][0]["response"])
    assert response["headers_present"] is False
    assert "status" not in response
    assert "credential-secret" not in serialized
    assert "business-secret" not in serialized


def test_serialize_direct_unmarked_aliyun_error_uses_fixed_false_presence():
    result = SimpleNamespace(
        content=json.dumps(
            {
                "status": 503,
                "headers": {"authorization": "secret"},
                "body": {"artifact_path": "business-value"},
                "content_type": "application/json",
                "content_encoding": "gzip",
                "size": 123,
                "artifact_path": "business-value",
            }
        ),
        is_error=True,
        metadata={},
    )

    serialized = json.loads(serialize_tool_result(result, tool_name="aliyun_api"))

    assert serialized == {
        "is_error": True,
        "headers_present": False,
        "body_present": False,
        "content_type_present": False,
        "content_encoding_present": False,
        "size_present": False,
        "artifact_present": False,
    }


def test_serialize_direct_unmarked_aliyun_error_trusts_only_artifacts_metadata():
    result = SimpleNamespace(
        content='{"artifact_path":"business-value"}',
        is_error=True,
        metadata={"artifacts": [{"name": "trusted"}]},
    )

    serialized = json.loads(serialize_tool_result(result, tool_name="ros_validate_template"))

    assert serialized["artifact_present"] is True
    assert "status" not in serialized


def test_marked_aliyun_provider_input_uses_sidecar_metadata_and_never_reports_artifact():
    blocks = [
        FakeContentBlock(type="tool_use", tool_use_id="tool-1", name="aliyun_api"),
        FakeContentBlock(
            type="tool_result",
            tool_use_id="tool-1",
            content='{"artifact_path":"business-value"}',
            metadata={"aliyun_http": _aliyun_http_metadata(), "artifacts": [{"name": "not-in-sidecar"}]},
        ),
    ]

    serialized = json.loads(serialize_input_messages([FakeMessage(role="user", content=blocks)]))
    response = json.loads(serialized[0]["parts"][1]["response"])

    assert response == {
        "is_error": False,
        "headers_present": True,
        "body_present": True,
        "content_type_present": True,
        "content_encoding_present": False,
        "size_present": True,
        "headers_nonempty": True,
        "header_count": 1,
        "artifact_present": False,
        "status": 200,
        "status_class": "2xx",
    }


def test_marked_aliyun_direct_result_trusts_artifacts_metadata_not_business_collision():
    without_artifact = SimpleNamespace(
        content='{"artifact_path":"business-value"}',
        is_error=False,
        metadata={"aliyun_http": _aliyun_http_metadata()},
    )
    with_artifact = SimpleNamespace(
        content='{"artifact_path":"business-value"}',
        is_error=False,
        metadata={"aliyun_http": _aliyun_http_metadata(), "artifacts": [{"name": "trusted"}]},
    )

    assert json.loads(serialize_tool_result(without_artifact, tool_name="aliyun_api"))["artifact_present"] is False
    assert json.loads(serialize_tool_result(with_artifact, tool_name="aliyun_api"))["artifact_present"] is True


def test_non_migrated_aliyun_tool_keeps_legacy_envelope_projection():
    content = json.dumps(
        {
            "status": 200,
            "headers": {},
            "body": {"ok": True},
            "content_type": "application/json",
            "content_encoding": None,
            "size": 11,
            "artifact_path": "legacy",
        }
    )
    result = SimpleNamespace(content=content, is_error=False, metadata={})

    serialized = json.loads(serialize_tool_result(result, tool_name="ros_stack"))

    assert serialized == {
        "is_error": False,
        "headers_present": True,
        "body_present": True,
        "content_type_present": True,
        "content_encoding_present": False,
        "size_present": True,
        "artifact_present": True,
        "status": 200,
        "status_class": "2xx",
    }


def test_serialize_input_messages_keeps_associated_non_aliyun_tool_result_unchanged():
    messages = [
        Message.assistant_tool_use(tool_use_id="custom-call", name="custom_tool", input={}),
        Message.tool_result(tool_use_id="custom-call", content="ordinary result output"),
    ]

    result = json.loads(serialize_input_messages(messages))

    assert result[1]["parts"][0]["response"] == "ordinary result output"


def test_serialize_input_messages_tool_result_redacts_externalized_file_content_preview(tmp_path):
    raw_result = json.dumps(
        {
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
        .process("t1", raw_result)
        .content
    )
    assert "ROSTemplateFormatVersion" in preview
    blocks = [FakeContentBlock(type="tool_result", tool_use_id="t1", text=preview)]
    result = json.loads(serialize_input_messages([FakeMessage(role="tool", content=blocks)]))

    response = result[0]["parts"][0]["response"]
    assert "ROSTemplateFormatVersion" not in response
    assert "file_content" in response
    assert "sha256-value" in response


def test_serialize_output_messages():
    result = json.loads(serialize_output_messages("Done!", "end_turn"))
    assert result[0]["role"] == "assistant"
    assert result[0]["finish_reason"] == "end_turn"
    assert result[0]["parts"][0]["type"] == "text"
    assert result[0]["parts"][0]["content"] == "Done!"


def test_serialize_system_instructions():
    result = json.loads(serialize_system_instructions("You are helpful."))
    assert result[0]["type"] == "text"
    assert result[0]["content"] == "You are helpful."


def test_serialize_tool_definitions():
    tools = [FakeToolDef(name="bash", description="Run a command")]
    result = json.loads(serialize_tool_definitions(tools))
    assert result[0]["name"] == "bash"
    assert result[0]["type"] == "function"
    assert result[0]["description"] == "Run a command"


def test_serialize_tool_definition_description_keeps_diagnostic_metadata_raw():
    description = "Run /Users/alice/private/tool with token=tool-owned-description"
    result = json.loads(serialize_tool_definitions([FakeToolDef(name="bash", description=description)]))

    assert result[0]["description"] == description


def test_serialize_tool_definitions_empty():
    assert serialize_tool_definitions(None) == "[]"
    assert serialize_tool_definitions([]) == "[]"


def test_serialize_tool_arguments_dict():
    result = json.loads(serialize_tool_arguments({"cmd": "ls"}))
    assert result["cmd"] == "ls"


def test_serialize_tool_result_object():
    result = serialize_tool_result(FakeToolResult(content="output"))
    assert "output" in result


def test_serialize_tool_result_redacts_embedded_file_content():
    result = serialize_tool_result(
        FakeToolResult(
            content=json.dumps(
                {
                    "file_path": "templates/demo.yml",
                    "file_sha256": "sha256-value",
                    "file_content": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n",
                },
                ensure_ascii=False,
            )
        )
    )

    assert "ROSTemplateFormatVersion" not in result
    assert "file_content" in result
    assert "sha256-value" in result


def test_serialize_tool_result_redacts_non_string_content():
    result = serialize_tool_result(
        FakeToolResult(
            content={
                "file_path": "templates/demo.yml",
                "file_sha256": "sha256-value",
                "file_content": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n",
            }
        )
    )

    assert "ROSTemplateFormatVersion" not in result
    assert "file_content" in result
    assert "sha256-value" in result


def test_content_serializers_strictly_sanitize_before_serialization_and_truncation(monkeypatch):
    from iac_code.utils.public_errors import suppress_all_redaction

    class SensitiveLeaf:
        def __str__(self) -> str:
            return "password=hunter2 /Users/alice/private.txt"

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    with suppress_all_redaction():
        arguments = serialize_tool_arguments(
            {
                "/Users/alice/key": SensitiveLeaf(),
                "nested_json": '{"token":"secret-value","path":"/Users/alice/result.json"}',
            }
        )

    parsed = json.loads(arguments)
    assert "/Users/alice" not in arguments
    assert "hunter2" not in arguments
    assert "secret-value" not in arguments
    assert parsed["[PATH]"] == "password=[REDACTED] [PATH]"
    assert "[REDACTED]" in parsed["nested_json"]


def test_content_serializers_redact_structured_sensitive_mapping_values():
    payload = {
        "password": "hunter2",
        "apiKey": "plain-secret",
        "config": {
            "access_key_secret": "aliyun-secret",
            "token": 123456,
            "region_id": "cn-hangzhou",
        },
        "credentials": {"profile": "production"},
    }

    arguments = json.loads(serialize_tool_arguments(payload))
    result = json.loads(serialize_tool_result(FakeToolResult(content=payload)))

    expected = {
        "password": "[REDACTED]",
        "apiKey": "[REDACTED]",
        "config": {
            "access_key_secret": "[REDACTED]",
            "token": "[REDACTED]",
            "region_id": "cn-hangzhou",
        },
        "credentials": "[REDACTED]",
    }
    assert arguments == expected
    assert result == expected
    assert payload["password"] == "hunter2"
    assert payload["config"]["token"] == 123456


def test_truncation_for_large_content():
    big = "x" * 10000
    result = serialize_tool_arguments(big)
    assert len(result.encode("utf-8")) <= 4096 + 20  # margin for [truncated]
    assert result.endswith("...[truncated]")
