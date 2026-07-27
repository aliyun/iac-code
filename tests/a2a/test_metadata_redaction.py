"""Tests for A2AMetadataEchoRedactor sensitive-key handling and the all-redaction switch."""

from __future__ import annotations

from iac_code.a2a.metadata_redaction import A2AMetadataEchoRedactor
from iac_code.utils.public_errors import suppress_all_redaction


def test_redact_masks_sensitive_key_by_default() -> None:
    redactor = A2AMetadataEchoRedactor()
    out = redactor.redact({"password": "p@ss", "note": "hello"})
    assert out == {"password": "***", "note": "hello"}


def test_redact_keeps_everything_raw_under_suppress_all() -> None:
    redactor = A2AMetadataEchoRedactor()
    payload = {"password": "p@ss", "path": "/Users/alice/.iac-code/t.yaml"}
    with suppress_all_redaction():
        assert redactor.redact(payload) == payload
