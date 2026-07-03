"""Tests for content_serializer module."""

import json
from dataclasses import dataclass
from typing import Any

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


@dataclass
class FakeToolDef:
    name: str
    description: str
    input_schema: dict | None = None


@dataclass
class FakeToolResult:
    content: Any


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


def test_truncation_for_large_content():
    big = "x" * 10000
    result = serialize_tool_arguments(big)
    assert len(result.encode("utf-8")) <= 4096 + 20  # margin for [truncated]
    assert result.endswith("...[truncated]")
