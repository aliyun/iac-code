"""Verify the image pipeline assembles content blocks for the chat path."""

import base64
import io

from PIL import Image

from iac_code.agent.message import ImageBlock, TextBlock
from iac_code.utils.image.pasted_content import PastedContent
from iac_code.utils.image.processor import process_user_input


def _b64_png(w: int = 4, h: int = 4) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color=(0, 0, 0)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_processor_emits_blocks_for_chat_path():
    pc = {1: PastedContent(id=1, type="image", content=_b64_png(), media_type="image/png")}
    blocks = process_user_input("see [Image #1]", pasted_contents=pc)
    assert any(isinstance(b, ImageBlock) for b in blocks)
    assert any(isinstance(b, TextBlock) for b in blocks)


def test_handle_chat_with_blocks_calls_run_streaming(monkeypatch):
    """Smoke test: PromptInputResult with images flows through _handle_chat → blocks → agent loop."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from iac_code.ui.core.prompt_input import PromptInputResult
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.run_streaming_output = AsyncMock(return_value=0.0)
    fake_loop = MagicMock()
    fake_loop.run_streaming = MagicMock(return_value=iter([]))
    fake_loop.stamp_last_turn_elapsed = MagicMock()
    repl._agent_loop = fake_loop

    pc = {1: PastedContent(id=1, type="image", content=_b64_png(), media_type="image/png")}
    result = PromptInputResult(text="see [Image #1]", pasted_contents=pc)
    asyncio.run(repl._handle_chat(result))

    # The agent loop should have been called with a list[ContentBlock] containing both block types
    args, _kwargs = fake_loop.run_streaming.call_args
    payload = args[0]
    assert isinstance(payload, list)
    assert any(isinstance(b, ImageBlock) for b in payload)
    assert any(isinstance(b, TextBlock) for b in payload)


def test_handle_chat_with_string_passes_through(monkeypatch):
    """Backward-compat: plain string user input still works."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.run_streaming_output = AsyncMock(return_value=0.0)
    fake_loop = MagicMock()
    fake_loop.run_streaming = MagicMock(return_value=iter([]))
    fake_loop.stamp_last_turn_elapsed = MagicMock()
    repl._agent_loop = fake_loop

    asyncio.run(repl._handle_chat("plain text"))
    args, _kwargs = fake_loop.run_streaming.call_args
    payload = args[0]
    assert payload == "plain text"
