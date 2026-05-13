"""The /tasks command — view and manage background agents."""

from __future__ import annotations

from iac_code.tasks.task_state import TaskManager


class TasksCommand:
    def __init__(self, task_manager: TaskManager):
        self._manager = task_manager

    @property
    def name(self) -> str:
        return "tasks"

    @property
    def description(self) -> str:
        return "List and manage background tasks. Usage: /tasks [stop <id>]"

    def execute(self, args: str) -> str:
        parts = args.strip().split()
        if parts and parts[0] == "stop" and len(parts) >= 2:
            return self._stop_task(parts[1])
        return self._list_tasks()

    def _list_tasks(self) -> str:
        tasks = self._manager.list_all()
        if not tasks:
            return "No background tasks."
        lines = []
        for t in tasks:
            icon = {"running": "*", "completed": "+", "failed": "!", "stopped": "-"}.get(t.status.value, "?")
            lines.append(f"  [{icon}] {t.id}  {t.status.value:<10}  [{t.agent_type}] {t.description}")
        return f"Background Tasks ({len(tasks)}):\n" + "\n".join(lines)

    def _stop_task(self, task_id: str) -> str:
        task = self._manager.get(task_id)
        if not task:
            return f"Task '{task_id}' not found."
        self._manager.stop(task_id)
        return f"Task '{task_id}' stopped."
