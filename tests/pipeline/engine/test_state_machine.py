import pytest

from iac_code.pipeline.engine.state_machine import StateMachine
from iac_code.pipeline.engine.step_spec import StepSpec
from iac_code.pipeline.engine.types import RollbackRule, StepStatus


def _make_three_steps():
    """A → B → C pipeline."""
    return [
        StepSpec(step_id="a", conclusion_field="a", forward="b", prompt_file="a.md"),
        StepSpec(
            step_id="b",
            conclusion_field="b",
            forward="c",
            prompt_file="b.md",
            rollback_rules=[RollbackRule(target_step="a", condition="fix")],
        ),
        StepSpec(
            step_id="c",
            conclusion_field="c",
            forward=None,
            prompt_file="c.md",
            rollback_rules=[
                RollbackRule(target_step="a", condition="restart"),
                RollbackRule(target_step="b", condition="revise"),
            ],
        ),
    ]


class TestStateMachineBasics:
    def test_initial_step(self):
        sm = StateMachine(_make_three_steps())
        assert sm.current_step.step_id == "a"
        assert not sm.is_complete

    def test_advance_through_all(self):
        sm = StateMachine(_make_three_steps())
        sm._step_statuses["a"] = StepStatus.RUNNING
        next_step = sm.advance()
        assert next_step.step_id == "b"
        sm._step_statuses["b"] = StepStatus.RUNNING
        next_step = sm.advance()
        assert next_step.step_id == "c"
        sm._step_statuses["c"] = StepStatus.RUNNING
        result = sm.advance()
        assert result is None
        assert sm.is_complete

    def test_step_statuses_on_advance(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()
        assert sm._step_statuses["a"] == StepStatus.COMPLETED
        assert sm._step_statuses["b"] == StepStatus.RUNNING


class TestStateMachineRollback:
    def test_rollback_to_allowed_target(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()  # a → b
        sm.advance()  # b → c
        step = sm.rollback("a", "restart")
        assert step.step_id == "a"
        assert sm._step_statuses["a"] == StepStatus.RUNNING
        assert sm._step_statuses["b"] == StepStatus.STALE
        assert sm._step_statuses["c"] == StepStatus.STALE

    def test_rollback_to_disallowed_target_raises(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()  # a → b
        with pytest.raises(ValueError, match="Cannot rollback"):
            sm.rollback("c", "invalid")  # b can only roll back to a

    def test_max_rollbacks_exceeded(self):
        sm = StateMachine(_make_three_steps(), max_rollbacks=1)
        sm.advance()  # a → b
        sm.advance()  # b → c
        sm.rollback("a", "first")
        sm.advance()  # a → b
        sm.advance()  # b → c
        with pytest.raises(ValueError, match="Max rollbacks"):
            sm.rollback("a", "second")

    def test_rollback_increments_count(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()
        sm.advance()
        assert sm._rollback_count == 0
        sm.rollback("a", "reason")
        assert sm._rollback_count == 1

    def test_can_rollback_to(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()
        sm.advance()
        assert sm.can_rollback_to("a")
        assert sm.can_rollback_to("b")
        assert not sm.can_rollback_to("nonexistent")

    def test_get_rollback_options(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()
        sm.advance()
        options = sm.get_rollback_options()
        assert len(options) == 2
        targets = {r.target_step for r in options}
        assert targets == {"a", "b"}

    def test_completed_non_future_rollback_targets_ignore_static_rules(self):
        steps = [
            StepSpec(step_id="a", conclusion_field="a", forward="b", prompt_file="a.md"),
            StepSpec(step_id="b", conclusion_field="b", forward="c", prompt_file="b.md"),
            StepSpec(step_id="c", conclusion_field="c", forward=None, prompt_file="c.md"),
        ]
        sm = StateMachine(steps)
        sm.advance()  # a -> b
        sm.advance()  # b -> c

        assert sm.completed_non_future_rollback_targets() == ["a", "b"]
        step = sm.rollback("b", "revise completed step", allow_completed_non_future=True)

        assert step.step_id == "b"

    def test_completed_non_future_rollback_rejects_future_and_uncompleted_targets(self):
        steps = [
            StepSpec(step_id="a", conclusion_field="a", forward="b", prompt_file="a.md"),
            StepSpec(step_id="b", conclusion_field="b", forward="c", prompt_file="b.md"),
            StepSpec(step_id="c", conclusion_field="c", forward=None, prompt_file="c.md"),
        ]
        sm = StateMachine(steps)
        sm.advance()  # a -> b

        assert sm.completed_non_future_rollback_targets() == ["a"]
        with pytest.raises(ValueError, match="Cannot rollback"):
            sm.rollback("c", "future", allow_completed_non_future=True)


class TestInterruptRollback:
    def test_can_interrupt_rollback_to_accepts_current_and_prior_steps(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()
        sm.advance()

        assert sm.can_interrupt_rollback_to("a") == (True, None)
        assert sm.can_interrupt_rollback_to("b") == (True, None)
        assert sm.can_interrupt_rollback_to("c") == (True, None)

    def test_can_interrupt_rollback_to_rejects_unknown_and_future_steps(self):
        sm = StateMachine(_make_three_steps())

        assert sm.can_interrupt_rollback_to("missing") == (False, "unknown_step")
        assert sm.can_interrupt_rollback_to("b") == (False, "future_step")

    def test_can_interrupt_rollback_to_rejects_limit_exhaustion(self):
        sm = StateMachine(_make_three_steps(), max_interrupt_rollbacks=1)
        sm.interrupt_rollback("a", "retry once")

        assert sm.can_interrupt_rollback_to("a") == (False, "max_interrupt_rollbacks_exceeded")

    def test_can_interrupt_rollback_to_rejects_completed_pipeline(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()
        sm.advance()
        sm.advance()

        assert sm.can_interrupt_rollback_to("a") == (False, "pipeline_complete")

    def test_interrupt_rollback_to_earlier_step(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()  # a → b
        sm.advance()  # b → c
        step = sm.interrupt_rollback("a", "user changed mind")
        assert step.step_id == "a"
        assert sm._step_statuses["a"] == StepStatus.RUNNING
        assert sm._step_statuses["b"] == StepStatus.STALE
        assert sm._step_statuses["c"] == StepStatus.STALE

    def test_interrupt_rollback_to_current_step(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()  # a → b
        step = sm.interrupt_rollback("b", "retry")
        assert step.step_id == "b"
        assert sm._step_statuses["b"] == StepStatus.RUNNING

    def test_interrupt_rollback_ignores_rollback_rules(self):
        """interrupt_rollback should work even without rollback_rules."""
        sm = StateMachine(_make_three_steps())
        sm.advance()  # a → b (b can only roll back to a via rules)
        sm.advance()  # b → c
        step = sm.interrupt_rollback("b", "no rule needed")
        assert step.step_id == "b"

    def test_interrupt_rollback_ignores_max_rollbacks(self):
        sm = StateMachine(_make_three_steps(), max_rollbacks=1)
        sm.advance()  # a → b
        sm.advance()  # b → c
        sm.interrupt_rollback("a", "first")
        sm.advance()  # a → b
        sm.advance()  # b → c
        step = sm.interrupt_rollback("a", "second interrupt")
        assert step.step_id == "a"

    def test_interrupt_rollback_forward_raises(self):
        sm = StateMachine(_make_three_steps())
        with pytest.raises(ValueError, match="Cannot rollback forward"):
            sm.interrupt_rollback("b", "invalid")

    def test_interrupt_rollback_unknown_step_raises(self):
        sm = StateMachine(_make_three_steps())
        with pytest.raises(ValueError, match="Unknown step"):
            sm.interrupt_rollback("nonexistent", "bad")

    def test_interrupt_rollback_max_limit_raises(self):
        sm = StateMachine(_make_three_steps(), max_interrupt_rollbacks=2)
        sm.advance()  # a → b
        sm.advance()  # b → c
        sm.interrupt_rollback("a", "first")
        sm.advance()
        sm.advance()
        sm.interrupt_rollback("a", "second")
        sm.advance()
        sm.advance()
        with pytest.raises(ValueError, match="Max interrupt rollbacks"):
            sm.interrupt_rollback("a", "third should fail")

    def test_interrupt_rollback_completed_pipeline_raises_value_error(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()
        sm.advance()
        sm.advance()

        with pytest.raises(ValueError, match="Cannot rollback completed pipeline"):
            sm.interrupt_rollback("a", "after done")


class TestJumpTo:
    def test_jump_to_later_step(self):
        steps = _make_three_steps()
        sm = StateMachine(steps)
        sm.jump_to("b")
        assert sm.current_step.step_id == "b"
        assert sm._step_statuses["a"] == StepStatus.COMPLETED
        assert sm._step_statuses["b"] == StepStatus.RUNNING

    def test_jump_to_marks_downstream_steps_stale(self):
        sm = StateMachine(_make_three_steps())
        sm.advance()
        sm.advance()
        sm.advance()

        sm.jump_to("b")

        assert sm.current_step.step_id == "b"
        assert sm._step_statuses["a"] == StepStatus.COMPLETED
        assert sm._step_statuses["b"] == StepStatus.RUNNING
        assert sm._step_statuses["c"] == StepStatus.STALE

    def test_jump_to_first_step(self):
        steps = _make_three_steps()
        sm = StateMachine(steps)
        sm.jump_to("a")
        assert sm.current_step.step_id == "a"
        assert sm._step_statuses["a"] == StepStatus.RUNNING


class TestGetInterruptRollbackOptions:
    def test_returns_options_up_to_current(self):
        steps = [
            StepSpec(step_id="a", conclusion_field="a", forward="b", prompt_file="a.md", description="Step A"),
            StepSpec(step_id="b", conclusion_field="b", forward="c", prompt_file="b.md", description="Step B"),
            StepSpec(step_id="c", conclusion_field="c", forward=None, prompt_file="c.md", description="Step C"),
        ]
        sm = StateMachine(steps)
        sm.advance()  # a → b
        sm.advance()  # b → c
        options = sm.get_interrupt_rollback_options()
        assert len(options) == 3
        assert options[0] == {"step_id": "a", "description": "Step A", "is_current": False}
        assert options[1] == {"step_id": "b", "description": "Step B", "is_current": False}
        assert options[2] == {"step_id": "c", "description": "Step C", "is_current": True}

    def test_returns_only_first_when_at_start(self):
        steps = _make_three_steps()
        sm = StateMachine(steps)
        options = sm.get_interrupt_rollback_options()
        assert len(options) == 1
        assert options[0]["step_id"] == "a"
        assert options[0]["is_current"] is True


class TestStateMachineSnapshot:
    def test_roundtrip(self):
        steps = _make_three_steps()
        sm = StateMachine(steps)
        sm.advance()
        sm.advance()
        sm.rollback("a", "reason")

        snapshot = sm.to_snapshot()
        restored = StateMachine.from_snapshot(snapshot, steps)

        assert restored.current_step.step_id == "a"
        assert restored._rollback_count == 1
        assert restored._step_statuses["b"] == StepStatus.STALE

    def test_snapshot_has_all_fields(self):
        sm = StateMachine(_make_three_steps())
        snapshot = sm.to_snapshot()
        assert "current_index" in snapshot
        assert "rollback_count" in snapshot
        assert "interrupt_rollback_count" in snapshot
        assert "step_statuses" in snapshot

    def test_snapshot_preserves_interrupt_rollback_count(self):
        steps = _make_three_steps()
        sm = StateMachine(steps)
        sm.advance()
        sm.advance()
        sm.interrupt_rollback("a", "reason")

        snapshot = sm.to_snapshot()
        restored = StateMachine.from_snapshot(snapshot, steps)
        assert restored._interrupt_rollback_count == 1
