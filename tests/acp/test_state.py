"""Tests for acp/state.py — targeting uncovered lines (partial JSON fallback)."""

from __future__ import annotations

import time

from iac_code.acp.state import ToolCallState, _extract_key_argument

# ---------------------------------------------------------------------------
# _extract_key_argument — partial JSON fallback path
# ---------------------------------------------------------------------------


class TestExtractKeyArgumentFallback:
    """Cover the partial/invalid JSON fallback branch (lines 86-98)."""

    def test_partial_json_extracts_value(self) -> None:
        # Incomplete JSON triggers JSONDecodeError, fallback should still extract
        partial = '{"command": "echo hello'
        result = _extract_key_argument("bash", partial)
        assert result == "echo hello"

    def test_partial_json_no_key_found(self) -> None:
        partial = '{"other": "stuff'
        result = _extract_key_argument("bash", partial)
        assert result == ""

    def test_partial_json_key_present_but_no_quote(self) -> None:
        # Key exists but value doesn't start with quote
        partial = '{"command": 123'
        result = _extract_key_argument("bash", partial)
        assert result == ""

    def test_partial_json_value_truncated_at_max_len(self) -> None:
        long_cmd = "x" * 100
        partial = f'{{"command": "{long_cmd}'
        result = _extract_key_argument("bash", partial)
        assert len(result) == 60

    def test_tool_not_in_key_arg_map(self) -> None:
        result = _extract_key_argument("unknown_tool", '{"anything": "value"}')
        assert result == ""

    def test_valid_json_extracts_value(self) -> None:
        result = _extract_key_argument("bash", '{"command": "ls -la"}')
        assert result == "ls -la"

    def test_valid_json_value_not_string(self) -> None:
        result = _extract_key_argument("bash", '{"command": 42}')
        assert result == ""

    def test_valid_json_key_missing(self) -> None:
        result = _extract_key_argument("bash", '{"other": "value"}')
        assert result == ""

    def test_valid_json_truncates_long_value(self) -> None:
        long_path = "/" + "a" * 100
        result = _extract_key_argument("read_file", f'{{"file_path": "{long_path}"}}')
        assert len(result) == 60


# ---------------------------------------------------------------------------
# ToolCallState.__post_init__ edge case
# ---------------------------------------------------------------------------


class TestToolCallStatePostInit:
    def test_explicit_start_time_preserved(self) -> None:
        """When start_time is explicitly provided non-zero, it should not be overwritten."""
        tc = ToolCallState(tool_call_id="tc-1", tool_name="bash", start_time=42.0)
        assert tc.start_time == 42.0

    def test_default_start_time_set(self) -> None:
        before = time.monotonic()
        tc = ToolCallState(tool_call_id="tc-2", tool_name="bash")
        after = time.monotonic()
        assert before <= tc.start_time <= after


# ---------------------------------------------------------------------------
# ToolCallState._update_title for tool without key arg
# ---------------------------------------------------------------------------


class TestToolCallStateTitleNoKeyArg:
    def test_tool_without_key_arg_has_tool_name_only_title(self) -> None:
        tc = ToolCallState(tool_call_id="tc-1", tool_name="unknown_tool")
        tc.update_input('{"arg": "value"}')
        assert tc.title == "unknown_tool"
