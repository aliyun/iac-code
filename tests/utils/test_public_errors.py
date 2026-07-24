"""Tests for public-error sanitization and the all-redaction suppression switch."""

from __future__ import annotations

from iac_code.utils.public_errors import (
    all_redaction_suppressed,
    sanitize_public_text,
    suppress_all_redaction,
)

_PATH = "/Users/alice/.iac-code/projects/demo/template.yaml"
_SECRET = "authorization: Bearer sk-abcdefgh12345678"


def test_paths_redacted_by_default() -> None:
    assert all_redaction_suppressed() is False
    assert sanitize_public_text("saved to {}".format(_PATH)) == "saved to [PATH]"


def test_suppress_all_redaction_keeps_real_path() -> None:
    with suppress_all_redaction():
        assert all_redaction_suppressed() is True
        assert sanitize_public_text("saved to {}".format(_PATH)) == "saved to {}".format(_PATH)


def test_suppress_all_redaction_keeps_secrets_raw() -> None:
    with suppress_all_redaction():
        assert sanitize_public_text(_SECRET) == _SECRET


def test_suppress_all_redaction_resets_on_exit() -> None:
    with suppress_all_redaction():
        assert all_redaction_suppressed() is True
    assert all_redaction_suppressed() is False
    assert sanitize_public_text("saved to {}".format(_PATH)) == "saved to [PATH]"


def test_suppress_all_redaction_nested_tokens_restore_previous() -> None:
    with suppress_all_redaction():
        with suppress_all_redaction():
            assert all_redaction_suppressed() is True
        assert all_redaction_suppressed() is True
    assert all_redaction_suppressed() is False
