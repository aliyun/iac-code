import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iac_code.agent.agent_tool import AgentProgress, AgentTool, run_sub_agent
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import TextDeltaEvent, ToolResultEvent, ToolUseEndEvent, ToolUseStartEvent


@pytest.fixture
def agent_tool():
    return AgentTool()


class TestAgentToolMetadata:
    def test_name(self, agent_tool):
        assert agent_tool.name == "agent"

    def test_schema_required(self, agent_tool):
        schema = agent_tool.input_schema
        assert "prompt" in schema["required"]

    def test_schema_has_subagent_type(self, agent_tool):
        assert "subagent_type" in agent_tool.input_schema["properties"]

    def test_schema_has_run_in_background(self, agent_tool):
        assert "run_in_background" in agent_tool.input_schema["properties"]

    def test_is_concurrency_safe(self, agent_tool):
        assert agent_tool.is_concurrency_safe({}) is True

    def test_user_facing_name(self, agent_tool):
        assert agent_tool.user_facing_name() == "Agent"

    def test_activity_description(self, agent_tool):
        assert agent_tool.get_activity_description({"description": "search tree"}) == "Running agent: search tree"
        assert agent_tool.get_activity_description(None) is None

    def test_render_tool_result_error(self, agent_tool):
        msg = agent_tool.render_tool_result_message("boom happened", is_error=True)
        assert msg == "Agent error: boom happened"

    def test_render_tool_result_without_stats_returns_none(self, agent_tool):
        assert agent_tool.render_tool_result_message("plain output") is None


@pytest.mark.asyncio
class TestRunSubAgent:
    async def test_unknown_agent_type_raises(self):
        with pytest.raises(ValueError, match="Unknown agent type"):
            await run_sub_agent(prompt="x", agent_type="missing")

    async def test_collects_text_progress_and_event_queue(self, monkeypatch):
        async def fake_stream(_prompt):
            yield TextDeltaEvent(text="hello ")
            yield ToolUseStartEvent(tool_use_id="t1", name="read_file")
            yield ToolUseEndEvent(tool_use_id="t1", name="read_file", input={"path": "a.txt"})
            yield ToolResultEvent(tool_use_id="t1", tool_name="read_file", result="ok")
            yield TextDeltaEvent(text="world")

        class FakeAgentLoop:
            def __init__(self, **kwargs):
                self.context_manager = SimpleNamespace(get_total_tokens=lambda: 321)

            def run_streaming(self, prompt):
                return fake_stream(prompt)

        monkeypatch.setattr(
            "iac_code.agent.agent_tool.get_agent_definition", lambda agent_type: SimpleNamespace(max_turns=3)
        )
        monkeypatch.setattr("iac_code.agent.agent_tool.filter_tools", lambda registry, defn: "filtered-tools")
        monkeypatch.setattr("iac_code.agent.system_prompt.build_system_prompt", lambda cwd=None: "built prompt")
        monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)

        queue = asyncio.Queue()
        text, progress = await run_sub_agent(
            prompt="demo",
            parent_provider_manager="pm",
            parent_tool_registry="registry",
            event_queue=queue,
        )

        assert text == "hello world"
        assert progress.tool_use_count == 1
        assert progress.last_activity == "read_file"
        assert progress.token_count == 321
        assert await queue.get() == {
            "child_tool_name": "read_file",
            "child_tool_input": {"path": "a.txt"},
            "is_done": False,
        }
        assert await queue.get() == {
            "child_tool_name": "read_file",
            "child_tool_input": {"path": "a.txt"},
            "is_done": True,
            "is_error": False,
        }

    async def test_truncates_long_output_to_500_words(self, monkeypatch):
        long_text = " ".join(f"w{i}" for i in range(550))

        async def fake_stream(_prompt):
            yield TextDeltaEvent(text=long_text)

        class FakeAgentLoop:
            def __init__(self, **kwargs):
                self.context_manager = SimpleNamespace(get_total_tokens=lambda: 10)

            def run_streaming(self, prompt):
                return fake_stream(prompt)

        monkeypatch.setattr(
            "iac_code.agent.agent_tool.get_agent_definition", lambda agent_type: SimpleNamespace(max_turns=3)
        )
        monkeypatch.setattr("iac_code.agent.system_prompt.build_system_prompt", lambda cwd=None: "built prompt")
        monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)

        text, progress = await run_sub_agent(prompt="demo")

        assert text.endswith("[... truncated to 500 words]")
        assert len(text.split()) > 500
        assert progress.token_count == 10


@pytest.mark.asyncio
class TestAgentToolExecution:
    async def test_unknown_agent_type(self, agent_tool):
        result = await agent_tool.execute(
            tool_input={"prompt": "x", "description": "x", "subagent_type": "nonexistent"},
            context=ToolContext(),
        )
        assert result.is_error is True
        assert "Unknown agent type" in result.content

    async def test_sync_execution(self, agent_tool):
        with patch(
            "iac_code.agent.agent_tool.run_sub_agent",
            new_callable=AsyncMock,
            return_value=("Done", AgentProgress()),
        ):
            result = await agent_tool.execute(
                tool_input={"prompt": "Find files", "description": "Find"},
                context=ToolContext(),
            )
            assert result.is_error is False
            assert "Done" in result.content

    async def test_background_execution(self):
        from iac_code.tasks.task_state import TaskManager

        tm = TaskManager()
        tool = AgentTool(task_manager=tm)
        with patch(
            "iac_code.agent.agent_tool.run_sub_agent",
            new_callable=AsyncMock,
            return_value=("bg done", AgentProgress()),
        ):
            result = await tool.execute(
                tool_input={"prompt": "task", "description": "bg", "run_in_background": True},
                context=ToolContext(),
            )
            assert "task_id" in result.content
            assert len(tm.list_all()) == 1

    async def test_render_tool_use(self, agent_tool):
        msg = agent_tool.render_tool_use_message({"description": "Search", "subagent_type": "explore"})
        assert msg == "Search"

    async def test_user_facing_name(self, agent_tool):
        assert agent_tool.user_facing_name({"subagent_type": "explore"}) == "Explore"
        assert agent_tool.user_facing_name({"subagent_type": "plan"}) == "Plan"
        assert agent_tool.user_facing_name({"subagent_type": "general-purpose"}) == "Agent"
        assert agent_tool.user_facing_name(None) == "Agent"

    async def test_render_tool_result_with_stats(self, agent_tool):
        output = "some text\n\n[Agent stats: 5 tool calls, 2500 tokens]"
        msg = agent_tool.render_tool_result_message(output)
        assert msg == "Done (5 tool uses · 2.5k tokens)"

    async def test_render_tool_result_verbose(self, agent_tool):
        output = "full output text\n\n[Agent stats: 3 tool calls, 500 tokens]"
        msg = agent_tool.render_tool_result_message(output, verbose=True)
        assert msg == output

    async def test_execute_closes_event_queue_on_success(self):
        tool = AgentTool()
        tool._event_queue = asyncio.Queue()

        with patch(
            "iac_code.agent.agent_tool.run_sub_agent",
            new_callable=AsyncMock,
            return_value=("Done", AgentProgress(tool_use_count=2, token_count=99)),
        ):
            result = await tool.execute(
                tool_input={"prompt": "Find files", "description": "Find"},
                context=ToolContext(),
            )

        assert result.is_error is False
        assert await tool._event_queue.get() is None

    async def test_execute_closes_event_queue_on_failure(self):
        tool = AgentTool()
        tool._event_queue = asyncio.Queue()

        with patch("iac_code.agent.agent_tool.run_sub_agent", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            result = await tool.execute(
                tool_input={"prompt": "Find files", "description": "Find"},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "boom" in result.content
        assert await tool._event_queue.get() is None

    async def test_run_background_success_updates_task_manager_and_notifications(self):
        task_manager = MagicMock()
        notifications = MagicMock()
        tool = AgentTool(task_manager=task_manager, notification_queue=notifications)

        with patch(
            "iac_code.agent.agent_tool.run_sub_agent",
            new_callable=AsyncMock,
            return_value=("background done", AgentProgress(tool_use_count=3, token_count=450)),
        ):
            await tool._run_background("task-1", "prompt", "general-purpose", ToolContext(cwd="/tmp"))

        task_manager.complete.assert_called_once_with("task-1", result="background done")
        task_manager.update_progress.assert_called_once_with("task-1", tool_use_count=3, token_count=450)
        notifications.enqueue.assert_called_once()

    async def test_run_background_failure_marks_task_failed(self):
        task_manager = MagicMock()
        notifications = MagicMock()
        tool = AgentTool(task_manager=task_manager, notification_queue=notifications)

        with patch("iac_code.agent.agent_tool.run_sub_agent", new_callable=AsyncMock, side_effect=RuntimeError("bad")):
            await tool._run_background("task-1", "prompt", "general-purpose", ToolContext(cwd="/tmp"))

        task_manager.fail.assert_called_once_with("task-1", error="bad")
        notifications.enqueue.assert_called_once()
