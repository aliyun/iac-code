"""Regression test for U-C1: transient cleanup must not erase content above the prompt."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def prompt_input():
    """Construct a PromptInput with minimal mocked dependencies."""
    from iac_code.ui.core.prompt_input import PromptInput

    pi = PromptInput(
        keybinding_manager=MagicMock(),
        suggestion_aggregator=None,
        history=None,
        console=MagicMock(),
        paste_handler=None,
        image_store=None,
    )
    return pi


def test_transient_cleanup_uses_cursor_row_not_content_extra(prompt_input, capsys):
    """When cursor is mid-buffer and content has more lines below it, transient
    cleanup must move up by the cursor row, not the total content extra lines."""
    # Setup: cursor on physical row 1, content extends to row 3.
    # The bug would move up by 3 → terminal clamps at row 0 → \033[J wipes
    # everything above the prompt. The fix moves up by 1 → only wipes the
    # prompt frame.
    prompt_input._prev_cursor_physical_row = 1
    prompt_input._prev_content_extra_lines = 3
    prompt_input._prev_suggestion_lines = 0

    prompt_input._render_transient_cleanup()

    out = capsys.readouterr().out
    # Must move up by the cursor row (1), NOT the content extra (3)
    assert "\033[1A" in out
    assert "\033[3A" not in out
    # Must clear from the resulting cursor position
    assert "\033[J" in out


def test_transient_cleanup_skips_move_when_cursor_at_top(prompt_input, capsys):
    """When cursor is already on physical row 0, no upward move is needed."""
    prompt_input._prev_cursor_physical_row = 0
    prompt_input._prev_content_extra_lines = 0
    prompt_input._prev_suggestion_lines = 0

    prompt_input._render_transient_cleanup()

    out = capsys.readouterr().out
    # No upward move
    assert "\033[" not in out or "A" not in out.split("\033[J")[0]
    # But still clears
    assert "\033[J" in out
