"""Background task state management."""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum


class TaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class TaskInfo:
    id: str
    description: str
    agent_type: str = "general-purpose"
    status: TaskStatus = TaskStatus.RUNNING
    result: str | None = None
    error: str | None = None
    tool_use_count: int = 0
    token_count: int = 0


class TaskManager:
    def __init__(self) -> None:
        self._tasks: OrderedDict[str, TaskInfo] = OrderedDict()

    def register(self, description: str, agent_type: str = "general-purpose") -> str:
        task_id = str(uuid.uuid4())[:8]
        self._tasks[task_id] = TaskInfo(id=task_id, description=description, agent_type=agent_type)
        return task_id

    def get(self, task_id: str) -> TaskInfo | None:
        return self._tasks.get(task_id)

    def complete(self, task_id: str, result: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.COMPLETED
            task.result = result

    def fail(self, task_id: str, error: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.FAILED
            task.error = error

    def stop(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.STOPPED

    def update_progress(self, task_id: str, tool_use_count: int = 0, token_count: int = 0) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.tool_use_count = tool_use_count
            task.token_count = token_count

    def list_all(self) -> list[TaskInfo]:
        return list(self._tasks.values())
