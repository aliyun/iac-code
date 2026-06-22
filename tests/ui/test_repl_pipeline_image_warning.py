"""Pipeline REPL input should preserve image blocks while keeping text-only feedback."""

from __future__ import annotations

from io import StringIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from iac_code.agent.message import ImageBlock, TextBlock
from iac_code.pipeline.config import RunMode
from iac_code.pipeline.engine.interrupt import InterruptVerdict
from iac_code.pipeline.engine.user_input import PipelineUserInput
from iac_code.ui.core.prompt_input import PromptInputResult
from iac_code.utils.image.pasted_content import PastedContent


def _image_prompt(text: str = "describe [Image #1]") -> PromptInputResult:
    pc = PastedContent(id=1, type="image", content="iVBORw0KGgo=", media_type="image/png")
    return PromptInputResult(text=text, pasted_contents={1: pc})


@pytest.mark.asyncio
async def test_pipeline_prompt_input_forwards_image_blocks() -> None:
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.PIPELINE
    repl._handle_pipeline_chat = AsyncMock()

    await repl._handle_chat(_image_prompt())

    repl._handle_pipeline_chat.assert_awaited_once()
    pipeline_input = repl._handle_pipeline_chat.await_args.args[0]
    assert isinstance(pipeline_input, PipelineUserInput)
    assert pipeline_input.display_text == "describe [Image #1]"
    assert pipeline_input.has_images is True
    assert isinstance(pipeline_input.content, list)
    assert any(isinstance(block, ImageBlock) for block in pipeline_input.content)
    assert any(isinstance(block, TextBlock) for block in pipeline_input.content)


@pytest.mark.asyncio
async def test_pipeline_prompt_input_uses_plain_text_when_no_images() -> None:
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.PIPELINE
    repl._handle_pipeline_chat = AsyncMock()

    user_input = PromptInputResult(
        text="hi",
        pasted_contents={1: PastedContent(id=1, type="text", content="some pasted text")},
    )
    await repl._handle_chat(user_input)

    pipeline_input = repl._handle_pipeline_chat.await_args.args[0]
    assert isinstance(pipeline_input, PipelineUserInput)
    assert pipeline_input.content == "hi"
    assert pipeline_input.display_text == "hi"
    assert pipeline_input.has_images is False


def test_pipeline_visible_user_turn_persists_image_blocks_for_resume() -> None:
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    injected = {"role": "user", "content": "visible"}
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager.add_raw_message = MagicMock(return_value=injected)
    repl._session_storage = MagicMock()
    repl._original_cwd = "/tmp/project"
    repl._session_id = "session-1"
    repl.current_git_branch = MagicMock(return_value="branch")
    pipeline_input = PipelineUserInput(
        content=[
            TextBlock(text="describe "),
            ImageBlock(media_type="image/png", data="base64-bytes"),
        ],
        display_text="describe [Image #1]",
        has_images=True,
    )

    repl._persist_pipeline_visible_user_turn(pipeline_input)

    raw_message = repl._agent_loop.context_manager.add_raw_message.call_args.args[0]
    assert raw_message["role"] == "user"
    assert isinstance(raw_message["content"], list)
    assert any(isinstance(block, TextBlock) for block in raw_message["content"])
    assert any(isinstance(block, ImageBlock) for block in raw_message["content"])
    repl._session_storage.append.assert_called_once_with(
        "/tmp/project",
        "session-1",
        injected,
        git_branch="branch",
    )


@pytest.mark.asyncio
async def test_pipeline_mid_interrupt_forwards_image_blocks() -> None:
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.console = Console(file=StringIO(), width=120, force_terminal=True)
    verdict = InterruptVerdict(action="continue", reason="")
    repl._pipeline = MagicMock()
    repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)

    _needs_restart, feedback = await repl._handle_mid_pipeline_message(_image_prompt(), suppress_render=True)

    pipeline_input = repl._pipeline.handle_user_interrupt.await_args.args[0]
    assert isinstance(pipeline_input, PipelineUserInput)
    assert pipeline_input.display_text == "describe [Image #1]"
    assert pipeline_input.has_images is True
    assert isinstance(pipeline_input.content, list)
    assert any(isinstance(block, ImageBlock) for block in pipeline_input.content)
    assert "describe [Image #1]" in feedback
    assert "iVBORw0KGgo=" not in feedback


@pytest.mark.asyncio
async def test_pipeline_interrupt_reader_preserves_pasted_images() -> None:
    from iac_code.ui.repl import InlineREPL

    class Prompt:
        async def get_input(self, *, prompt: str, transient: bool):
            return "describe [Image #1]"

        def make_result(self):
            return _image_prompt()

    repl = InlineREPL.__new__(InlineREPL)
    repl._prompt_input = Prompt()

    pipeline_input = await repl._read_pipeline_interrupt_input()

    assert pipeline_input.display_text == "describe [Image #1]"
    assert pipeline_input.has_images is True
    assert isinstance(pipeline_input.content, list)
    assert any(isinstance(block, ImageBlock) for block in pipeline_input.content)
