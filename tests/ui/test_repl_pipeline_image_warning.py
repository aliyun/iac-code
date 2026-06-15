"""U-I4: pipeline mode must warn before dropping pasted images."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_code.pipeline.config import RunMode
from iac_code.ui.core.prompt_input import PromptInputResult
from iac_code.utils.image.pasted_content import PastedContent


@pytest.mark.asyncio
async def test_pipeline_mode_warns_when_pasted_image_present(monkeypatch):
    """Image in pasted_contents should trigger a yellow print_system_message before drop."""
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    pc = PastedContent(id=1, type="image", content="iVBORw0KGgo=", media_type="image/png")
    user_input = PromptInputResult(text="describe this image", pasted_contents={1: pc})

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.PIPELINE
    repl.renderer = MagicMock()
    repl._handle_pipeline_chat = AsyncMock()

    await repl._handle_chat(user_input)

    # Verify the warning went to renderer.print_system_message (consistent with
    # other image warnings at repl.py:2250, 2258, 2275).
    repl.renderer.print_system_message.assert_called_once()
    call = repl.renderer.print_system_message.call_args
    # First positional arg is the message (or `msg` kwarg).
    msg_arg = call.args[0] if call.args else call.kwargs.get("msg") or call.kwargs.get("message")
    assert msg_arg is not None, f"could not extract message from call: {call!r}"
    assert "image" in msg_arg.lower(), f"image warning text missing: {msg_arg!r}"
    # Style must be yellow.
    assert call.kwargs.get("style") == "yellow", f"expected style='yellow', got {call.kwargs!r}"

    # Pipeline handler still invoked (warning is non-blocking).
    repl._handle_pipeline_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_mode_no_warning_when_no_images(monkeypatch):
    """Text-only pipeline input should NOT trigger the image warning."""
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    pc = PastedContent(id=1, type="text", content="some pasted text")
    user_input = PromptInputResult(text="hi", pasted_contents={1: pc})

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.PIPELINE
    repl.renderer = MagicMock()
    repl._handle_pipeline_chat = AsyncMock()

    await repl._handle_chat(user_input)

    repl.renderer.print_system_message.assert_not_called()
    repl._handle_pipeline_chat.assert_awaited_once()
