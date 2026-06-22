"""StateMachine — manages step ordering, advancement, and rollback."""

from __future__ import annotations

import logging

from iac_code.pipeline.engine.step_spec import StepSpec
from iac_code.pipeline.engine.types import StepStatus

logger = logging.getLogger(__name__)


class StateMachine:
    """Generic pipeline state machine.

    Steps are ordered linearly. Each step's config defines its forward
    target. The state machine tracks the current position, step statuses,
    and rollback count.
    """

    def __init__(self, steps: list[StepSpec], max_rollbacks: int = 3, max_interrupt_rollbacks: int = 10) -> None:
        self._steps: dict[str, StepSpec] = {s.step_id: s for s in steps}
        self._order: list[str] = [s.step_id for s in steps]
        self._current_index: int = 0
        self._rollback_count: int = 0
        self._max_rollbacks: int = max_rollbacks
        self._step_statuses: dict[str, StepStatus] = {sid: StepStatus.PENDING for sid in self._order}
        self._interrupt_rollback_count: int = 0
        self._max_interrupt_rollbacks: int = max_interrupt_rollbacks

    @property
    def current_step(self) -> StepSpec:
        return self._steps[self._order[self._current_index]]

    @property
    def current_step_index(self) -> int:
        return self._current_index

    @property
    def total_steps(self) -> int:
        return len(self._order)

    @property
    def rollback_count(self) -> int:
        return self._rollback_count

    @property
    def max_rollbacks(self) -> int:
        return self._max_rollbacks

    @property
    def is_complete(self) -> bool:
        return self._current_index >= len(self._order)

    def advance(self) -> StepSpec | None:
        """Mark current step completed and move to its forward target."""
        step = self.current_step
        self._step_statuses[step.step_id] = StepStatus.COMPLETED
        if step.forward is None:
            self._current_index = len(self._order)
            return None
        self._current_index = self._order.index(step.forward)
        self._step_statuses[step.forward] = StepStatus.RUNNING
        return self.current_step

    def rollback(self, target_step_id: str, reason: str) -> StepSpec:
        """Roll back to target step, marking intermediates as stale."""
        if not self.can_rollback_to(target_step_id):
            raise ValueError(f"Cannot rollback from {self.current_step.step_id} to {target_step_id}")
        if self._rollback_count >= self._max_rollbacks:
            raise ValueError(f"Max rollbacks ({self._max_rollbacks}) exceeded")

        self._rollback_count += 1
        target_index = self._order.index(target_step_id)

        for i in range(target_index + 1, len(self._order)):
            sid = self._order[i]
            self._step_statuses[sid] = StepStatus.STALE

        self._current_index = target_index
        self._step_statuses[target_step_id] = StepStatus.RUNNING
        return self.current_step

    def can_rollback_to(self, target_step_id: str) -> bool:
        return target_step_id in self.completed_non_future_rollback_targets()

    def completed_non_future_rollback_targets(self) -> list[str]:
        """Return completed rollback targets at or before the current position."""
        targets: list[str] = []
        for i, step_id in enumerate(self._order):
            if i > self._current_index:
                break
            if self._step_statuses.get(step_id) == StepStatus.COMPLETED:
                targets.append(step_id)
        return targets

    def can_interrupt_rollback_to(self, target_step_id: str) -> tuple[bool, str | None]:
        """Validate interrupt rollback target without mutating state."""
        if target_step_id not in self._steps:
            return False, "unknown_step"
        if self.is_complete:
            return False, "pipeline_complete"
        target_index = self._order.index(target_step_id)
        if target_index > self._current_index:
            return False, "future_step"
        if self._interrupt_rollback_count >= self._max_interrupt_rollbacks:
            return False, "max_interrupt_rollbacks_exceeded"
        return True, None

    def interrupt_rollback(self, target_step_id: str, reason: str) -> StepSpec:
        """User-interrupt rollback to current or prior steps with its own limit."""
        ok, error = self.can_interrupt_rollback_to(target_step_id)
        if not ok:
            if error == "unknown_step":
                raise ValueError(f"Unknown step: {target_step_id}")
            if error == "pipeline_complete":
                raise ValueError(f"Cannot rollback completed pipeline to {target_step_id}")
            if error == "future_step":
                raise ValueError(f"Cannot rollback forward to {target_step_id}")
            raise ValueError(f"Max interrupt rollbacks ({self._max_interrupt_rollbacks}) exceeded")

        self._interrupt_rollback_count += 1
        target_index = self._order.index(target_step_id)
        logger.info(
            "Interrupt rollback #%d: %s -> %s, reason: %s",
            self._interrupt_rollback_count,
            self._order[self._current_index],
            target_step_id,
            reason,
        )

        for i in range(target_index + 1, len(self._order)):
            self._step_statuses[self._order[i]] = StepStatus.STALE
        self._current_index = target_index
        self._step_statuses[target_step_id] = StepStatus.RUNNING
        return self.current_step

    def jump_to(self, step_id: str) -> None:
        """Jump directly to a step, marking prior steps COMPLETED. For sub-pipeline restart."""
        if step_id not in self._steps:
            raise ValueError(f"Unknown step: {step_id}")
        target_index = self._order.index(step_id)
        for i in range(target_index):
            self._step_statuses[self._order[i]] = StepStatus.COMPLETED
        for i in range(target_index + 1, len(self._order)):
            self._step_statuses[self._order[i]] = StepStatus.STALE
        self._current_index = target_index
        self._step_statuses[step_id] = StepStatus.RUNNING

    def get_interrupt_rollback_options(self) -> list[dict]:
        """Return all valid rollback targets for interrupt (index 0 to current inclusive)."""
        options = []
        for i in range(self._current_index + 1):
            step = self._steps[self._order[i]]
            options.append(
                {
                    "step_id": step.step_id,
                    "description": step.description,
                    "is_current": i == self._current_index,
                }
            )
        return options

    def to_snapshot(self) -> dict:
        return {
            "current_index": self._current_index,
            "rollback_count": self._rollback_count,
            "interrupt_rollback_count": self._interrupt_rollback_count,
            "step_statuses": {k: v.value for k, v in self._step_statuses.items()},
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict,
        steps: list[StepSpec],
        max_rollbacks: int = 3,
        max_interrupt_rollbacks: int = 10,
    ) -> StateMachine:
        sm = cls(steps, max_rollbacks=max_rollbacks, max_interrupt_rollbacks=max_interrupt_rollbacks)
        sm._current_index = snapshot["current_index"]
        sm._rollback_count = snapshot["rollback_count"]
        sm._interrupt_rollback_count = snapshot.get("interrupt_rollback_count", 0)
        sm._step_statuses = {k: StepStatus(v) for k, v in snapshot["step_statuses"].items()}
        return sm
