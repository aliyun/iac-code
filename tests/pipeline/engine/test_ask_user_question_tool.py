from __future__ import annotations

import asyncio
import json

import pytest

from iac_code.pipeline.engine.ask_user_question_tool import AskUserQuestionTool
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import AskUserQuestionEvent


def _input() -> dict:
    return {
        "question": "请选择下一步",
        "options": [
            {"id": "deploy_to_aliyun", "label": "部署到阿里云", "description": "生成 IaC 部署方案"},
            {"id": "not_iac", "label": "不是基础设施需求"},
        ],
        "allow_free_text": True,
        "free_text_prompt": "可选补充规模、预算、地域：",
    }


class TestAskUserQuestionToolMeta:
    def test_metadata(self):
        tool = AskUserQuestionTool()

        assert tool.name == "ask_user_question"
        assert "pipeline" in tool.description.lower()
        assert tool.needs_event_queue() is True
        assert tool.is_read_only({}) is True
        assert tool.is_concurrency_safe({}) is False
        assert tool.timeout >= 3600

    def test_schema_requires_question_and_options(self):
        schema = AskUserQuestionTool().input_schema

        assert schema["required"] == ["question", "options"]
        assert schema["properties"]["options"]["minItems"] == 1
        option_schema = schema["properties"]["options"]["items"]
        assert option_schema["required"] == ["id", "label"]
        assert schema["additionalProperties"] is False


class TestAskUserQuestionToolExecute:
    @pytest.mark.asyncio
    async def test_emits_event_and_returns_selected_answer(self):
        queue: asyncio.Queue = asyncio.Queue()
        tool = AskUserQuestionTool()
        task = asyncio.create_task(
            tool.execute(tool_input=_input(), context=ToolContext(cwd="/tmp", event_queue=queue, tool_use_id="tu_1"))
        )

        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert isinstance(event, AskUserQuestionEvent)
        assert event.tool_use_id == "tu_1"
        assert event.question == "请选择下一步"
        assert event.options[0]["id"] == "deploy_to_aliyun"
        assert event.allow_free_text is True
        assert event.free_text_prompt == "可选补充规模、预算、地域："

        assert event.response_future is not None
        event.response_future.set_result(
            {"selected_id": "deploy_to_aliyun", "selected_label": "部署到阿里云", "free_text": "预算 500/月"}
        )

        result = await asyncio.wait_for(task, timeout=1)
        assert result.is_error is False
        assert json.loads(result.content) == {
            "selected_id": "deploy_to_aliyun",
            "selected_label": "部署到阿里云",
            "free_text": "预算 500/月",
        }

    @pytest.mark.asyncio
    async def test_cancelled_question_returns_error(self):
        queue: asyncio.Queue = asyncio.Queue()
        tool = AskUserQuestionTool()
        task = asyncio.create_task(tool.execute(tool_input=_input(), context=ToolContext(event_queue=queue)))

        event = await asyncio.wait_for(queue.get(), timeout=1)
        event.response_future.set_result(None)

        result = await asyncio.wait_for(task, timeout=1)
        assert result.is_error is True
        assert "cancelled" in result.content.lower()

    @pytest.mark.asyncio
    async def test_cancelled_task_resolves_response_future(self):
        queue: asyncio.Queue = asyncio.Queue()
        tool = AskUserQuestionTool()
        task = asyncio.create_task(tool.execute(tool_input=_input(), context=ToolContext(event_queue=queue)))

        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event.response_future is not None

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert event.response_future.done()
        assert event.response_future.result() is None

    @pytest.mark.asyncio
    async def test_missing_event_queue_returns_error(self):
        result = await AskUserQuestionTool().execute(tool_input=_input(), context=ToolContext(event_queue=None))

        assert result.is_error is True
        assert "event queue" in result.content.lower()
