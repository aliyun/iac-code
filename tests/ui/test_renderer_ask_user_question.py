from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from iac_code.types.stream_events import AskUserQuestionEvent
from iac_code.ui.renderer import Renderer


def _renderer() -> Renderer:
    return Renderer(Console(record=True), MagicMock())


def _event(*, allow_free_text: bool = True) -> AskUserQuestionEvent:
    fut: asyncio.Future[dict[str, str] | None] = asyncio.get_event_loop().create_future()
    return AskUserQuestionEvent(
        tool_use_id="tu_1",
        question="请选择下一步",
        options=[
            {"id": "deploy_to_aliyun", "label": "部署到阿里云", "description": "生成 IaC 方案"},
            {"id": "not_iac", "label": "不是基础设施需求"},
        ],
        allow_free_text=allow_free_text,
        free_text_prompt="可选补充：",
        response_future=fut,
    )


def _details_event() -> AskUserQuestionEvent:
    fut: asyncio.Future[dict[str, str] | None] = asyncio.get_event_loop().create_future()
    return AskUserQuestionEvent(
        tool_use_id="tu_1",
        question="请补充这个项目是什么，以及希望怎么上线。",
        options=[
            {"id": "not_deployment", "label": "暂不处理部署", "description": "当前不是部署或云资源需求"},
        ],
        allow_free_text=True,
        free_text_prompt="可以直接输入：例如 nginx 网站。",
        response_future=fut,
    )


@pytest.mark.asyncio
async def test_prompt_user_question_returns_choice_from_number():
    renderer = _renderer()
    renderer.console.input = MagicMock(return_value="2")

    result = await renderer.prompt_user_question(_event())

    assert result == {
        "selected_id": "not_iac",
        "selected_label": "不是基础设施需求",
        "free_text": "",
    }


@pytest.mark.asyncio
async def test_prompt_user_question_accepts_direct_free_text_without_prior_select():
    renderer = _renderer()
    renderer.console.input = MagicMock(return_value="nginx 网站，日访问 1 万")

    result = await renderer.prompt_user_question(_event())

    assert result == {
        "selected_id": "",
        "selected_label": "",
        "free_text": "nginx 网站，日访问 1 万",
    }
    assert renderer.console.input.call_args.args[0] == "  > "
    output = renderer.console.export_text()
    assert "1. 部署到阿里云" in output
    assert "2. 不是基础设施需求" in output
    assert "可选补充：" in output
    assert "nginx 网站" not in output


@pytest.mark.asyncio
async def test_prompt_user_question_does_not_require_free_text_placeholder_option():
    renderer = _renderer()
    renderer.console.input = MagicMock(return_value="1")

    result = await renderer.prompt_user_question(_details_event())

    assert result == {"selected_id": "not_deployment", "selected_label": "暂不处理部署", "free_text": ""}
    output = renderer.console.export_text()
    assert "1. 补充项目信息" not in output
    assert "1. 暂不处理部署" in output
    assert "可以直接输入：例如 nginx 网站。" in output


@pytest.mark.asyncio
async def test_prompt_user_question_direct_details_use_hidden_free_text_placeholder():
    renderer = _renderer()
    renderer.console.input = MagicMock(return_value="nginx 网站，日访问 1 万")

    result = await renderer.prompt_user_question(_details_event())

    assert result == {
        "selected_id": "",
        "selected_label": "",
        "free_text": "nginx 网站，日访问 1 万",
    }


@pytest.mark.asyncio
async def test_prompt_user_question_free_text_ctrl_c_propagates():
    renderer = _renderer()
    renderer.console.input = MagicMock(side_effect=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        await renderer.prompt_user_question(_event())


@pytest.mark.asyncio
async def test_prompt_user_question_cancel_returns_none():
    renderer = _renderer()
    renderer.console.input = MagicMock(side_effect=EOFError)

    result = await renderer.prompt_user_question(_event())

    assert result is None


@pytest.mark.asyncio
async def test_prompt_user_question_matches_choice_text_when_free_text_disabled():
    renderer = _renderer()
    renderer.console.input = MagicMock(return_value="不是基础设施需求")

    result = await renderer.prompt_user_question(_event(allow_free_text=False))

    assert result == {"selected_id": "not_iac", "selected_label": "不是基础设施需求", "free_text": ""}
    output = renderer.console.export_text()
    assert "可选补充：" not in output


@pytest.mark.asyncio
async def test_prompt_user_question_reprompts_unmatched_text_when_free_text_disabled():
    renderer = _renderer()
    renderer.console.input = MagicMock(side_effect=["随便写点", "2"])

    result = await renderer.prompt_user_question(_event(allow_free_text=False))

    assert result == {"selected_id": "not_iac", "selected_label": "不是基础设施需求", "free_text": ""}


@pytest.mark.asyncio
async def test_streaming_output_resolves_question_future():
    renderer = _renderer()
    event = _event()
    renderer.prompt_user_question = AsyncMock(
        return_value={"selected_id": "deploy_to_aliyun", "selected_label": "部署到阿里云", "free_text": ""}
    )

    async def events():
        yield event

    await renderer.run_streaming_output(events(), permission_handler=AsyncMock(return_value=True))

    assert event.response_future is not None
    assert event.response_future.done()
    assert event.response_future.result()["selected_id"] == "deploy_to_aliyun"


@pytest.mark.asyncio
async def test_streaming_output_resolves_question_future_when_prompt_raises():
    renderer = _renderer()
    event = _event()
    renderer.prompt_user_question = AsyncMock(side_effect=RuntimeError("prompt failed"))

    async def events():
        yield event

    await renderer.run_streaming_output(events(), permission_handler=AsyncMock(return_value=True))

    assert event.response_future is not None
    assert event.response_future.done()
    assert event.response_future.result() is None
    assert renderer._last_streaming_errors == ["Error: prompt failed"]
