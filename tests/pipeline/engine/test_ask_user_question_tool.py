from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

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

    def test_validation_error_renders_compact_summary(self):
        long_error = (
            "Invalid input for tool 'ask_user_question': "
            "[{'id': 'tech_stack_nodejs', 'label': 'Node.js'}] is not of type 'object'. "
            "Please provide all required parameters as defined in the tool schema."
        )

        compact = AskUserQuestionTool().render_tool_result_message(long_error, is_error=True)

        assert compact == "ask_user_question validation failed."
        assert "tech_stack_nodejs" not in compact
        assert "not of type" not in compact


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
    async def test_notifies_observer_after_answer_is_submitted(self):
        queue: asyncio.Queue = asyncio.Queue()
        observer = MagicMock()
        tool = AskUserQuestionTool(question_answered_observer=observer)
        task = asyncio.create_task(
            tool.execute(tool_input=_input(), context=ToolContext(event_queue=queue, tool_use_id="tu_1"))
        )

        event = await asyncio.wait_for(queue.get(), timeout=1)
        event.response_future.set_result(
            {"selected_id": "deploy_to_aliyun", "selected_label": "部署到阿里云", "free_text": "预算 500/月"}
        )

        result = await asyncio.wait_for(task, timeout=1)

        assert result.is_error is False
        observer.assert_called_once_with("tu_1", 2, "option_and_free_text")

    @pytest.mark.asyncio
    async def test_observer_failure_does_not_fail_answer_submission(self):
        queue: asyncio.Queue = asyncio.Queue()
        observer = MagicMock(side_effect=RuntimeError("telemetry unavailable"))
        tool = AskUserQuestionTool(question_answered_observer=observer)
        task = asyncio.create_task(tool.execute(tool_input=_input(), context=ToolContext(event_queue=queue)))

        event = await asyncio.wait_for(queue.get(), timeout=1)
        event.response_future.set_result({"selected_id": "not_iac", "selected_label": "不是基础设施需求"})

        result = await asyncio.wait_for(task, timeout=1)

        assert result.is_error is False
        observer.assert_called_once_with(None, 2, "option")

    @pytest.mark.asyncio
    async def test_cancelled_question_returns_error(self):
        queue: asyncio.Queue = asyncio.Queue()
        observer = MagicMock()
        tool = AskUserQuestionTool(question_answered_observer=observer)
        task = asyncio.create_task(tool.execute(tool_input=_input(), context=ToolContext(event_queue=queue)))

        event = await asyncio.wait_for(queue.get(), timeout=1)
        event.response_future.set_result(None)

        result = await asyncio.wait_for(task, timeout=1)
        assert result.is_error is True
        assert "cancelled" in result.content.lower()
        observer.assert_not_called()

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


class TestAskUserQuestionToolGuardRecords:
    """回答必须进入 completion guard 的有序记录，供最终确认绑定真实回答。"""

    @staticmethod
    async def _answer(state, answer, tool_input=None):
        queue: asyncio.Queue = asyncio.Queue()
        tool = AskUserQuestionTool(state)
        task = asyncio.create_task(
            tool.execute(tool_input=tool_input or _input(), context=ToolContext(event_queue=queue))
        )
        event = await asyncio.wait_for(queue.get(), timeout=1)
        event.response_future.set_result(answer)
        return await asyncio.wait_for(task, timeout=1)

    @pytest.mark.asyncio
    async def test_answer_appends_an_ordered_record_with_the_original_question(self):
        state: dict = {}

        result = await self._answer(
            state, {"selected_id": "deploy_to_aliyun", "selected_label": "部署到阿里云", "free_text": "cn-hangzhou"}
        )

        assert result.is_error is False
        payload = {
            "selected_id": "deploy_to_aliyun",
            "selected_label": "部署到阿里云",
            "free_text": "cn-hangzhou",
        }
        assert state["successful_tools"] == {"ask_user_question"}
        assert state["tool_results"]["ask_user_question"] == payload
        assert state["tool_result_records"] == [
            {"tool_name": "ask_user_question", "input": _input(), "result": payload, "is_error": False}
        ]

    @pytest.mark.asyncio
    async def test_records_keep_submission_order_for_repeated_questions(self):
        state: dict = {}
        second_input = {**_input(), "question": "确认部署这份模板？"}

        await self._answer(state, {"selected_id": "not_iac", "selected_label": "不是基础设施需求"})
        await self._answer(state, {"selected_id": "confirm", "selected_label": "确认部署"}, tool_input=second_input)

        records = state["tool_result_records"]
        assert [record["input"]["question"] for record in records] == ["请选择下一步", "确认部署这份模板？"]
        assert records[-1]["result"]["selected_id"] == "confirm"
        # 最近一次回答同时更新聚合视图。
        assert state["tool_results"]["ask_user_question"]["selected_id"] == "confirm"

    @pytest.mark.asyncio
    async def test_record_input_snapshot_is_not_aliased_to_the_tool_input(self):
        state: dict = {}
        tool_input = _input()

        await self._answer(
            state, {"selected_id": "not_iac", "selected_label": "不是基础设施需求"}, tool_input=tool_input
        )
        tool_input["question"] = "被改写的问题"

        assert state["tool_result_records"][0]["input"]["question"] == "请选择下一步"

    @pytest.mark.asyncio
    async def test_cancelled_question_records_nothing(self):
        state: dict = {}

        result = await self._answer(state, None)

        assert result.is_error is True
        assert state == {}

    @pytest.mark.asyncio
    async def test_missing_guard_state_still_returns_the_answer(self):
        result = await self._answer(None, {"selected_id": "not_iac", "selected_label": "不是基础设施需求"})

        assert result.is_error is False
        assert json.loads(result.content)["selected_id"] == "not_iac"
