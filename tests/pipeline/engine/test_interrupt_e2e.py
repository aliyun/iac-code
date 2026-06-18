"""End-to-end test for pipeline interrupt flow."""

from unittest.mock import MagicMock

import pytest

from iac_code.pipeline.engine.interrupt import InterruptVerdict
from iac_code.pipeline.engine.types import StepStatus


@pytest.fixture
def pipeline_dir(tmp_path):
    """Create a minimal pipeline directory for testing."""
    d = tmp_path / "pipeline"
    d.mkdir()
    (d / "pipeline.yaml").write_text(
        "name: test\n"
        "context_dependencies:\n"
        "  intent: []\n"
        "  arch: [intent]\n"
        "max_rollbacks: 3\n"
        "steps:\n"
        "  - id: step_a\n"
        "    conclusion_field: intent\n"
        "    forward: step_b\n"
        "    description: First step\n"
        "    prompt: a.md\n"
        "  - id: step_b\n"
        "    conclusion_field: arch\n"
        "    forward: null\n"
        "    description: Second step\n"
        "    prompt: b.md\n",
        encoding="utf-8",
    )
    (d / "a.md").write_text("Do step A: {intent}", encoding="utf-8")
    (d / "b.md").write_text("Do step B: {arch}", encoding="utf-8")
    return d


@pytest.fixture
def runner(pipeline_dir, tmp_path):
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    pm = MagicMock()
    pm.get_model_name.return_value = "test-model"
    registry = MagicMock()
    registry.clone.return_value = registry
    registry.filter.return_value = registry
    registry.exclude.return_value = registry
    registry.list_tools.return_value = []
    registry.register = MagicMock()

    storage = MagicMock()
    storage.session_path.return_value = tmp_path / "sessions" / "e2e.jsonl"
    storage.append_meta = MagicMock()

    return PipelineRunner(
        pipeline_dir=pipeline_dir,
        provider_manager=pm,
        base_tool_registry=registry,
        session_storage=storage,
        session_id="e2e-test",
        cwd=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_interrupt_rollback_resets_to_target(runner):
    """Full flow: advance → interrupt → verify rollback state."""
    runner.state_machine.advance()
    runner.context.set_conclusion("intent", {"summary": "deploy nginx"})

    assert runner.state_machine.current_step.step_id == "step_b"

    verdict = InterruptVerdict(
        action="hard_interrupt",
        reason="changed mind",
        rollback_target="step_a",
    )
    runner.apply_hard_interrupt(verdict)

    assert runner.state_machine.current_step.step_id == "step_a"
    assert runner.state_machine._step_statuses["step_a"] == StepStatus.RUNNING
    assert runner.state_machine._step_statuses["step_b"] == StepStatus.STALE


@pytest.mark.asyncio
async def test_supplement_does_not_change_state(runner):
    """Supplement verdict should not alter state machine position."""
    runner.state_machine.advance()
    runner.context.set_conclusion("intent", {"summary": "deploy nginx"})

    mock_loop = MagicMock()
    runner._step_executor._current_agent_loop = mock_loop

    verdict = InterruptVerdict(action="supplement", reason="extra info", supplement_target=None)
    runner._inject_supplement(verdict, "add 8GB RAM")

    assert runner.state_machine.current_step.step_id == "step_b"
    mock_loop.inject_user_message.assert_called_once_with("add 8GB RAM")


@pytest.mark.asyncio
async def test_continue_after_interrupt_produces_events(runner):
    """After interrupt, continue_after_interrupt should yield pipeline events."""
    runner.state_machine.advance()
    runner.context.set_conclusion("intent", {"summary": "deploy nginx"})

    verdict = InterruptVerdict(
        action="hard_interrupt",
        reason="changed mind",
        rollback_target="step_a",
    )
    runner.apply_hard_interrupt(verdict)

    gen = runner.continue_after_interrupt()
    assert hasattr(gen, "__aiter__")
    await gen.aclose()


@pytest.mark.asyncio
async def test_get_state_for_judge_complete_picture(runner):
    """State for judge should include all pipeline info."""
    runner.state_machine.advance()
    runner.context.set_conclusion("intent", {"summary": "deploy nginx"})

    state = runner._get_state_for_judge()

    assert state["pipeline_name"] == "test"
    assert state["current_step_id"] == "step_b"
    assert state["current_step_index"] == 1
    assert len(state["steps"]) == 2
    assert state["steps"][0]["step_id"] == "step_a"
    assert state["steps"][0]["is_current"] is False
    assert state["steps"][1]["step_id"] == "step_b"
    assert state["steps"][1]["is_current"] is True
    assert "intent" in state["conclusions"]
    assert state["conclusions"]["intent"]["summary"] == "deploy nginx"
