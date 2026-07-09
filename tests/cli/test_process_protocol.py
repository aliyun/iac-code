from __future__ import annotations

import json

import pytest

from iac_code.cli.process_protocol import (
    ProcessFrameParser,
    ProcessFrameValidationError,
    SDKControlRequest,
    SDKControlResponse,
    SDKUpdateEnvironmentVariables,
    SDKUserMessage,
)


def test_parse_claude_style_initialize_control_request(tmp_path) -> None:
    parser = ProcessFrameParser()
    frame = parser.parse_line(
        json.dumps(
            {
                "type": "control_request",
                "request_id": "req-1",
                "request": {
                    "subtype": "initialize",
                    "cwd": str(tmp_path),
                    "model": "qwen3.7-max",
                    "max_turns": 10,
                },
            }
        )
    )

    assert isinstance(frame, SDKControlRequest)
    assert frame.request_id == "req-1"
    assert frame.subtype == "initialize"
    assert frame.payload["cwd"] == str(tmp_path)
    assert frame.payload["model"] == "qwen3.7-max"
    assert frame.payload["max_turns"] == 10


def test_parse_user_frame_with_text_content() -> None:
    parser = ProcessFrameParser()
    frame = parser.parse_line(
        json.dumps(
            {
                "type": "user",
                "session_id": "session-1",
                "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                "metadata": {"iac_code": {"iac_code_model": "qwen3.6-plus"}},
            }
        )
    )

    assert isinstance(frame, SDKUserMessage)
    assert frame.session_id == "session-1"
    assert frame.text == "hello"
    assert frame.metadata["iac_code"]["iac_code_model"] == "qwen3.6-plus"


def test_parse_legacy_user_message_alias(tmp_path) -> None:
    parser = ProcessFrameParser()
    frame = parser.parse_line(
        json.dumps(
            {
                "type": "user_message",
                "id": "legacy-1",
                "session_id": "session-legacy",
                "content": [{"type": "text", "text": "legacy hello"}],
                "metadata": {"iac_code": {"cwd": str(tmp_path)}},
            }
        )
    )

    assert isinstance(frame, SDKUserMessage)
    assert frame.request_id == "legacy-1"
    assert frame.session_id == "session-legacy"
    assert frame.text == "legacy hello"
    assert frame.cwd == str(tmp_path)


def test_parse_legacy_control_alias() -> None:
    parser = ProcessFrameParser()
    frame = parser.parse_line(json.dumps({"type": "control", "id": "legacy-2", "subtype": "interrupt"}))

    assert isinstance(frame, SDKControlRequest)
    assert frame.request_id == "legacy-2"
    assert frame.subtype == "interrupt"


def test_parse_claude_style_control_response() -> None:
    parser = ProcessFrameParser()
    frame = parser.parse_line(
        json.dumps(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "req-permission",
                    "response": {"behavior": "allow"},
                },
            }
        )
    )

    assert isinstance(frame, SDKControlResponse)
    assert frame.request_id == "req-permission"
    assert frame.subtype == "success"
    assert frame.payload["response"] == {"behavior": "allow"}


def test_parse_keep_alive_as_noop() -> None:
    parser = ProcessFrameParser()

    assert parser.parse_line(json.dumps({"type": "keep_alive"})) is None


def test_parse_update_environment_variables() -> None:
    parser = ProcessFrameParser()
    frame = parser.parse_line(
        json.dumps({"type": "update_environment_variables", "variables": {"IAC_CODE_TEST_ENV": "1"}})
    )

    assert isinstance(frame, SDKUpdateEnvironmentVariables)
    assert frame.variables == {"IAC_CODE_TEST_ENV": "1"}


def test_rejects_malformed_json() -> None:
    parser = ProcessFrameParser()

    with pytest.raises(ProcessFrameValidationError) as exc_info:
        parser.parse_line("{not-json")

    assert exc_info.value.code == "invalid_json"
    assert exc_info.value.request_id is None


def test_rejects_unknown_type() -> None:
    parser = ProcessFrameParser()

    with pytest.raises(ProcessFrameValidationError) as exc_info:
        parser.parse_line(json.dumps({"type": "unknown", "id": "req-bad"}))

    assert exc_info.value.code == "invalid_frame"
    assert exc_info.value.request_id == "req-bad"


def test_rejects_relative_cwd_in_initialize() -> None:
    parser = ProcessFrameParser()

    with pytest.raises(ProcessFrameValidationError) as exc_info:
        parser.parse_line(
            json.dumps(
                {
                    "type": "control_request",
                    "request_id": "req-relative",
                    "request": {"subtype": "initialize", "cwd": "relative/path"},
                }
            )
        )

    assert exc_info.value.code == "invalid_frame"
    assert exc_info.value.request_id == "req-relative"
    assert "cwd" in exc_info.value.message


def test_rejects_non_text_content_block() -> None:
    parser = ProcessFrameParser()

    with pytest.raises(ProcessFrameValidationError) as exc_info:
        parser.parse_line(
            json.dumps(
                {
                    "type": "user",
                    "session_id": "session-1",
                    "message": {"role": "user", "content": [{"type": "image", "data": "..."}]},
                }
            )
        )

    assert exc_info.value.code == "invalid_frame"
    assert "text" in exc_info.value.message


def test_rejects_update_environment_variables_with_non_string_value() -> None:
    parser = ProcessFrameParser()

    with pytest.raises(ProcessFrameValidationError) as exc_info:
        parser.parse_line(json.dumps({"type": "update_environment_variables", "variables": {"A": 1}}))

    assert exc_info.value.code == "invalid_frame"
