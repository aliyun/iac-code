"""Tests for the canonical A2A metadata-copy compatibility wrapper."""

from __future__ import annotations

from a2a.types import Message, Part, Role
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.metadata_redaction import A2AMetadataEchoRedactor
from iac_code.utils.public_errors import suppress_all_redaction


def test_redact_preserves_sensitive_key_by_default() -> None:
    redactor = A2AMetadataEchoRedactor()
    out = redactor.redact({"password": "p@ss", "note": "hello"})
    assert out == {"password": "p@ss", "note": "hello"}


def test_redact_keeps_everything_raw_under_suppress_all() -> None:
    redactor = A2AMetadataEchoRedactor()
    payload = {"password": "p@ss", "path": "/Users/alice/.iac-code/t.yaml"}
    with suppress_all_redaction():
        assert redactor.redact(payload) == payload


def test_redact_message_echo_removes_llm_headers_but_preserves_other_metadata() -> None:
    redactor = A2AMetadataEchoRedactor()
    message = Message(
        role=Role.ROLE_USER,
        parts=[Part(text="hello")],
        message_id="message-1",
        metadata={
            "iac_code": {
                "llm_headers": {"Authorization": "Bearer top-secret"},
                "preferredLanguage": "zh-CN",
            },
            "caller": "test-client",
        },
    )

    echoed = MessageToDict(redactor.redact_message_echo(message), preserving_proto_field_name=False)

    assert echoed["metadata"] == {
        "iac_code": {"preferredLanguage": "zh-CN"},
        "caller": "test-client",
    }
    assert "top-secret" not in str(echoed)
