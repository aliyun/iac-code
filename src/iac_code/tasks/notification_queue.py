"""Notification queue for background agent completion events."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class TaskNotification:
    task_id: str
    message: str


class NotificationQueue:
    def __init__(self, max_pending: int = 100):
        self._queue: deque[TaskNotification] = deque(maxlen=max_pending)

    def enqueue(self, task_id: str, message: str) -> None:
        self._queue.append(TaskNotification(task_id=task_id, message=message))

    def dequeue(self) -> TaskNotification | None:
        if self._queue:
            return self._queue.popleft()
        return None

    def has_pending(self) -> bool:
        return len(self._queue) > 0
