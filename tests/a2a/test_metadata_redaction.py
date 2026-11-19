"""Tests for the canonical A2A metadata-copy compatibility wrapper."""

from __future__ import annotations

import copy

import pytest
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


@pytest.mark.parametrize("wrapped", [False, True])
def test_redact_removes_runtime_secrets_without_changing_input(wrapped: bool) -> None:
    metadata = {
        "iac_code": {
            "llm_headers": {"Authorization": "Bearer top-secret"},
            "iac_code_api_key": "fake-provider-api-key",
            "alibaba_cloud_access_key_id": "fake-access-key-id",
            "alibaba_cloud_access_key_secret": "fake-access-key-secret",
            "alibaba_cloud_security_token": "fake-security-token",
            "alibaba_cloud_region_id": "cn-hangzhou",
        }
    }
    payload = {"metadata": metadata} if wrapped else metadata
    original = copy.deepcopy(payload)

    result = A2AMetadataEchoRedactor().redact(payload)

    expected = {"iac_code": {"alibaba_cloud_region_id": "cn-hangzhou"}}
    assert result == ({"metadata": expected} if wrapped else expected)
    assert payload == original


def test_redact_message_echo_removes_runtime_secrets_but_preserves_other_metadata() -> None:
    redactor = A2AMetadataEchoRedactor()
    message = Message(
        role=Role.ROLE_USER,
        parts=[Part(text="hello")],
        message_id="message-1",
        metadata={
            "iac_code": {
                "llm_headers": {"Authorization": "Bearer top-secret"},
                "iac_code_api_key": "fake-provider-api-key",
                "alibaba_cloud_access_key_id": "fake-access-key-id",
                "alibaba_cloud_access_key_secret": "fake-access-key-secret",
                "alibaba_cloud_security_token": "fake-security-token",
                "preferredLanguage": "zh-CN",
            },
            "caller": "test-client",
        },
    )
    original = MessageToDict(message)

    echoed = MessageToDict(redactor.redact_message_echo(message), preserving_proto_field_name=False)

    assert echoed["metadata"] == {
        "iac_code": {"preferredLanguage": "zh-CN"},
        "caller": "test-client",
    }
    assert "top-secret" not in str(echoed)
    assert MessageToDict(message) == original
