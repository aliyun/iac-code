"""Optional transient progress before the first effective A2A display content."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict, ParseDict

from iac_code.a2a.display_timing import DisplayEventClassifier

ENV = "IAC_CODE_A2A_PROGRESS_ENABLED"
_STAGES = {
    "session_load_started": (1, "loading_history"),
    "wiki_keyword_extract_started": (2, "searching_knowledge"),
    "wiki_search_started": (2, "searching_knowledge"),
    "llm_request_started": (3, "analyzing"),
}
_TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "completed", "failed", "canceled"}


def progress_enabled() -> bool:
    return os.environ.get(ENV, "").strip().casefold() in {"1", "true", "yes", "on"}


class ProgressEventQueue:
    """Serialize optional progress with business events; never add text to Task history."""

    def __init__(self, queue: Any) -> None:
        self._progress_delegate = queue
        self._progress_loop = asyncio.get_running_loop()
        self._progress_lock = asyncio.Lock()
        self._progress_classifier = DisplayEventClassifier()
        self._progress_id = uuid.uuid4().hex
        self._progress_task_id = self._progress_context_id = ""
        self._progress_rank = 0
        self._progress_stage = "preparing"
        self._progress_sent_stage = ""
        self._progress_sequence = 0
        self._progress_finished = False
        self._progress_task: asyncio.Task[None] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._progress_delegate, name)

    def on_stage(self, stage: str) -> None:
        if stage in _STAGES and not self._progress_finished:
            self._progress_loop.call_soon_threadsafe(self._update, *_STAGES[stage])

    def _update(self, rank: int, stage: str) -> None:
        if self._progress_finished or rank < self._progress_rank:
            return
        self._progress_rank, self._progress_stage = rank, stage
        if self._progress_stage != self._progress_sent_stage and (
            self._progress_task is None or self._progress_task.done()
        ):
            self._progress_task = self._progress_loop.create_task(self._flush())

    async def _flush(self) -> None:
        while not self._progress_finished:
            await asyncio.sleep(0.15)
            async with self._progress_lock:
                await self._publish()
            if not self._progress_task_id or self._progress_stage == self._progress_sent_stage:
                return

    async def _publish(self) -> None:
        if (
            self._progress_finished
            or not self._progress_task_id
            or not self._progress_context_id
            or self._progress_stage == self._progress_sent_stage
        ):
            return
        self._progress_sequence += 1
        stage = self._progress_stage
        update = TaskStatusUpdateEvent(
            task_id=self._progress_task_id,
            context_id=self._progress_context_id,
            status=TaskStatus(state=TaskState.Name(TaskState.TASK_STATE_WORKING)),
        )
        ParseDict(
            {
                "iac_code": {
                    "progress": {
                        "schemaVersion": 1,
                        "id": self._progress_id,
                        "sequence": self._progress_sequence,
                        "stage": stage,
                    }
                }
            },
            update.metadata,
        )
        try:
            await self._progress_delegate.enqueue_event(update)
        except Exception:
            self._progress_finished = True
            return
        self._progress_sent_stage = stage

    async def enqueue_event(self, event: Any) -> None:
        async with self._progress_lock:
            if self._progress_finished:
                await self._progress_delegate.enqueue_event(event)
                return
            data = event if isinstance(event, dict) else MessageToDict(event)
            status = data.get("status") or {}
            if self._progress_classifier.classify(data) or status.get("state") in _TERMINAL or data.get("final"):
                self._progress_finished = True
            await self._progress_delegate.enqueue_event(event)
            if not self._progress_finished:
                self._progress_task_id = data.get("taskId") or data.get("id") or self._progress_task_id
                self._progress_context_id = data.get("contextId") or self._progress_context_id
                await self._publish()

    async def finish_progress(self) -> None:
        self._progress_finished = True
        if self._progress_task is not None:
            self._progress_task.cancel()
            await asyncio.gather(self._progress_task, return_exceptions=True)
