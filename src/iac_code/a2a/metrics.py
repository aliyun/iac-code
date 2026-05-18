from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class A2AMetrics(Protocol):
    def record_task_created(self) -> None:
        """Record creation of a protocol task record."""
        ...

    def record_turn_completed(self) -> None:
        """Record successful completion of one agent turn."""
        ...

    def record_task_canceled(self) -> None:
        """Record cancellation of an active task."""
        ...

    def record_task_failed(self) -> None:
        """Record a task reaching a failed terminal state."""
        ...

    def record_context_evicted(self) -> None:
        """Record cleanup of an idle A2A context."""
        ...

    def record_executor_error(self) -> None:
        """Record an executor-level error while handling a task."""
        ...

    def record_push_enqueued(self) -> None:
        """Record enqueueing of a push notification job."""
        ...

    def record_push_delivered(self, *, duration_ms: float) -> None:
        """Record successful push delivery latency."""
        ...

    def record_push_retry_scheduled(self) -> None:
        """Record scheduling of a retry for a transient push failure."""
        ...

    def record_push_dead_lettered(self) -> None:
        """Record a push job moved to the dead-letter queue."""
        ...

    def record_push_permanent_failure(self) -> None:
        """Record a non-retryable push delivery failure."""
        ...

    def record_push_transient_failure(self) -> None:
        """Record a retryable push delivery failure."""
        ...

    def record_push_queue_depth(self, depth: int) -> None:
        """Record the current push queue depth."""
        ...


class NoOpA2AMetrics:
    def record_task_created(self) -> None:
        logger.debug("a2a task created")

    def record_turn_completed(self) -> None:
        logger.debug("a2a turn completed")

    def record_task_canceled(self) -> None:
        logger.debug("a2a task canceled")

    def record_task_failed(self) -> None:
        logger.debug("a2a task failed")

    def record_context_evicted(self) -> None:
        logger.debug("a2a context evicted")

    def record_executor_error(self) -> None:
        logger.debug("a2a executor error")

    def record_push_enqueued(self) -> None:
        logger.debug("a2a push enqueued")

    def record_push_delivered(self, *, duration_ms: float) -> None:
        logger.debug("a2a push delivered in %.2f ms", duration_ms)

    def record_push_retry_scheduled(self) -> None:
        logger.debug("a2a push retry scheduled")

    def record_push_dead_lettered(self) -> None:
        logger.debug("a2a push dead lettered")

    def record_push_permanent_failure(self) -> None:
        logger.debug("a2a push permanent failure")

    def record_push_transient_failure(self) -> None:
        logger.debug("a2a push transient failure")

    def record_push_queue_depth(self, depth: int) -> None:
        logger.debug("a2a push queue depth=%d", depth)
