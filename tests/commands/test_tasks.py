import pytest

from iac_code.commands.tasks import TasksCommand
from iac_code.tasks.task_state import TaskManager, TaskStatus


@pytest.fixture
def task_manager():
    mgr = TaskManager()
    mgr.register(description="Explore code", agent_type="explore")
    t2 = mgr.register(description="Plan refactor", agent_type="plan")
    mgr.complete(t2, result="Plan ready")
    return mgr


class TestTasksCommand:
    def test_name(self):
        assert TasksCommand(TaskManager()).name == "tasks"

    def test_description(self):
        assert TasksCommand(TaskManager()).description == "List and manage background tasks. Usage: /tasks [stop <id>]"

    def test_list_tasks(self, task_manager):
        cmd = TasksCommand(task_manager)
        output = cmd.execute("")
        assert "Explore code" in output
        assert "Plan refactor" in output
        assert "running" in output.lower()
        assert "completed" in output.lower()

    def test_stop_subcommand(self, task_manager):
        tasks = task_manager.list_all()
        running_id = tasks[0].id
        cmd = TasksCommand(task_manager)
        output = cmd.execute(f"stop {running_id}")
        assert "stopped" in output.lower()
        assert task_manager.get(running_id).status == TaskStatus.STOPPED

    def test_stop_completed_task_reports_current_status(self, task_manager):
        completed_id = task_manager.list_all()[1].id
        cmd = TasksCommand(task_manager)
        output = cmd.execute(f"stop {completed_id}")
        assert "already completed" in output.lower()
        assert task_manager.get(completed_id).status == TaskStatus.COMPLETED

    def test_stop_nonexistent(self, task_manager):
        cmd = TasksCommand(task_manager)
        output = cmd.execute("stop nonexistent")
        assert "not found" in output.lower()

    def test_empty_list(self):
        cmd = TasksCommand(TaskManager())
        output = cmd.execute("")
        assert "No background tasks" in output
