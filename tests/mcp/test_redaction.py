from __future__ import annotations

from iac_code.mcp.redaction import strip_mcp_terminal_control_sequences


def test_strip_mcp_terminal_control_sequences_removes_ansi_osc_and_c1_controls() -> None:
    text = "a\x1b[2Jb\x1b]0;owned\x07c\x1b]1;gone\x1b\\d\x9b31me\x9dtitle\x9cf\x08g"

    assert strip_mcp_terminal_control_sequences(text) == "abcdefg"


def test_strip_mcp_terminal_control_sequences_removes_unterminated_osc_payload() -> None:
    assert strip_mcp_terminal_control_sequences("prefix\x1b]0;owned") == "prefix"
