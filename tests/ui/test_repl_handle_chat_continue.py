"""U-I17: _handle_chat_continue must reject calls in pipeline mode (defensive)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_code.pipeline.config import RunMode


@pytest.mark.asyncio
async def test_handle_chat_continue_rejects_pipeline_mode(monkeypatch):
    """In pipeline mode, _handle_chat_continue should return without running
    the non-pipeline agent loop (which would produce undefined behavior)."""
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.PIPELINE
    repl.store = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.run_streaming = MagicMock()
    repl.renderer = MagicMock()
    repl._streaming_error_log = []

    await repl._handle_chat_continue()

    # Non-pipeline agent loop must NOT have been invoked.
    repl._agent_loop.run_streaming.assert_not_called()


@pytest.mark.asyncio
async def test_handle_chat_continue_works_in_normal_mode(monkeypatch):
    """Normal mode (no IAC_CODE_MODE set): _handle_chat_continue runs the agent loop."""
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl.store = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.run_streaming = MagicMock(return_value=[])
    repl._agent_loop.context_manager = MagicMock(get_messages=MagicMock(return_value=[]))
    repl._agent_loop.stamp_last_turn_elapsed = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.run_streaming_output = AsyncMock(return_value=0.0)
    repl.renderer._last_streaming_errors = []
    repl._streaming_error_log = []

    await repl._handle_chat_continue()
    repl._agent_loop.run_streaming.assert_called_once()
