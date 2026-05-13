import pytest

from iac_code.tasks.task_state import TaskManager
from iac_code.tasks.task_tools import TaskGetTool, TaskListTool, TaskStopTool
from iac_code.tools.base import ToolContext


@pytest.fixture
def task_manager():
    mgr = TaskManager()
    mgr.register(description="Explore codebase", agent_type="explore")
    mgr.register(description="Plan architecture", agent_type="plan")
    return mgr


@pytest.mark.asyncio
class TestTaskTools:
    async def test_tool_metadata_and_read_only_flags(self, task_manager):
        list_tool = TaskListTool(task_manager)
        get_tool = TaskGetTool(task_manager)
        stop_tool = TaskStopTool(task_manager)

        assert list_tool.name == "task_list"
        assert "List all background tasks" in list_tool.description
        assert list_tool.input_schema == {"type": "object", "properties": {}}
        assert list_tool.is_read_only() is True

        assert get_tool.name == "task_get"
        assert get_tool.input_schema["required"] == ["task_id"]
        assert get_tool.is_read_only() is True

        assert stop_tool.name == "task_stop"
        assert stop_tool.input_schema["required"] == ["task_id"]
        assert stop_tool.is_read_only() is False

    async def test_task_list(self, task_manager):
        tool = TaskListTool(task_manager)
        result = await tool.execute(tool_input={}, context=ToolContext())
        assert "Explore codebase" in result.content
        assert "Plan architecture" in result.content

    async def test_task_list_includes_result_and_error_snippets(self, task_manager):
        tasks = task_manager.list_all()
        task_manager.complete(tasks[0].id, "x" * 250)
        task_manager.fail(tasks[1].id, "y" * 250)

        tool = TaskListTool(task_manager)
        result = await tool.execute(tool_input={}, context=ToolContext())

        assert "Result: " + ("x" * 200) in result.content
        assert "Error: " + ("y" * 200) in result.content

    async def test_task_get_existing(self, task_manager):
        tasks = task_manager.list_all()
        task_manager.update_progress(tasks[0].id, tool_use_count=3, token_count=120)
        task_manager.complete(tasks[0].id, "done")
        tool = TaskGetTool(task_manager)
        result = await tool.execute(tool_input={"task_id": tasks[0].id}, context=ToolContext())
        assert "Explore codebase" in result.content
        assert "Tool uses: 3" in result.content
        assert "Tokens: 120" in result.content
        assert "Result: done" in result.content
        assert result.is_error is False

    async def test_task_get_existing_with_error(self, task_manager):
        task = task_manager.list_all()[0]
        task_manager.fail(task.id, "failed badly")

        tool = TaskGetTool(task_manager)
        result = await tool.execute(tool_input={"task_id": task.id}, context=ToolContext())

        assert "Error: failed badly" in result.content

    async def test_task_get_nonexistent(self, task_manager):
        tool = TaskGetTool(task_manager)
        result = await tool.execute(tool_input={"task_id": "xxx"}, context=ToolContext())
        assert result.is_error is True

    async def test_task_stop(self, task_manager):
        tasks = task_manager.list_all()
        tool = TaskStopTool(task_manager)
        result = await tool.execute(tool_input={"task_id": tasks[0].id}, context=ToolContext())
        assert result.is_error is False
        assert task_manager.get(tasks[0].id).status.value == "stopped"

    async def test_task_stop_nonexistent(self, task_manager):
        tool = TaskStopTool(task_manager)
        result = await tool.execute(tool_input={"task_id": "missing"}, context=ToolContext())
        assert result.is_error is True
        assert "not found" in result.content

    async def test_task_list_empty(self):
        tool = TaskListTool(TaskManager())
        result = await tool.execute(tool_input={}, context=ToolContext())
        assert "No background tasks" in result.content
