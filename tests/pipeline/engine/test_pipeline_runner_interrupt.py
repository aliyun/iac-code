"""Tests for PipelineRunner interrupt coordination."""

from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iac_code.agent.message import ImageBlock, Message, TextBlock, ToolResultBlock
from iac_code.pipeline.engine.events import PipelineEventType
from iac_code.pipeline.engine.interrupt import InterruptVerdict
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner, RestartInfo
from iac_code.pipeline.engine.types import StepStatus


class FakeSessionStorage:
    def __init__(self):
        self.meta_entries = []
        self._path = MagicMock()

    def append_meta(self, cwd, session_id, meta):
        self.meta_entries.append(meta)

    def session_path(self, cwd, session_id):
        return self._path


class FakeTranscriptStorage:
    def __init__(self, messages_by_id=None):
        self.messages_by_id = messages_by_id or {}

    def load(self, cwd, session_id):
        return list(self.messages_by_id.get(session_id, []))

    @staticmethod
    def repair_interrupted(messages):
        return list(messages)


class RecordingPipelineSession:
    def __init__(self):
        self.calls = []

    def save_rollback_sync(
        self,
        from_step,
        to_step,
        reason,
        state_machine_snapshot,
        context_snapshot,
        pipeline_identity,
        **kwargs,
    ):
        self.calls.append(
            (
                "rollback_sync",
                from_step,
                to_step,
                state_machine_snapshot["current_index"],
                reason,
                pipeline_identity.pipeline_name,
            )
        )


class CapturingPipelineSession(RecordingPipelineSession):
    def __init__(self):
        super().__init__()
        self.saved = []

    def save_running_sync(
        self,
        current_step,
        state_machine_snapshot,
        context_snapshot,
        pipeline_identity,
        reason=None,
        **kwargs,
    ):
        self.saved.append(("running", current_step, reason, kwargs))

    def save_failed_sync(
        self,
        current_step,
        state_machine_snapshot,
        context_snapshot,
        pipeline_identity,
        reason=None,
        **kwargs,
    ):
        self.saved.append(("failed", current_step, reason, kwargs))
        self.calls.append(("failed_sync", current_step, state_machine_snapshot["current_index"], reason))

    def save_rollback_sync(
        self,
        from_step,
        to_step,
        reason,
        state_machine_snapshot,
        context_snapshot,
        pipeline_identity,
        **kwargs,
    ):
        self.saved.append(("rollback", from_step, reason, kwargs))
        super().save_rollback_sync(
            from_step,
            to_step,
            reason,
            state_machine_snapshot,
            context_snapshot,
            pipeline_identity,
            **kwargs,
        )


@pytest.fixture
def pipeline_runner(tmp_path):
    """Create a PipelineRunner with mocked dependencies."""
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "a.md").write_text("do A", encoding="utf-8")
    (tmp_path / "prompts" / "b.md").write_text("do B", encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        dedent("""\
        name: test
        context_dependencies:
          a_out: []
          b_out: [a_out]
        max_rollbacks: 3
        steps:
          - id: a
            conclusion_field: a_out
            forward: b
            prompt: prompts/a.md
            description: Step A
          - id: b
            conclusion_field: b_out
            forward: null
            prompt: prompts/b.md
            description: Step B
    """),
        encoding="utf-8",
    )

    pm = MagicMock()
    pm.get_model_name.return_value = "test-model"
    storage = FakeSessionStorage()

    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=pm,
        base_tool_registry=MagicMock(),
        session_storage=storage,
        session_id="test-session",
        cwd=str(tmp_path),
    )
    runner.session = RecordingPipelineSession()
    return runner


def _seed_restored_parallel_judge_state(pipeline_runner):
    from iac_code.pipeline.engine.step_spec import StepSpec, SubPipelineSpec
    from iac_code.pipeline.engine.ui_contract import PipelineStepType

    parallel_step = StepSpec(
        step_id="evaluate",
        conclusion_field="evaluate_out",
        forward=None,
        prompt_file="prompts/a.md",
        step_type=PipelineStepType.PARALLEL_SUB_PIPELINE.value,
        sub_pipeline_name="evaluate_candidate",
        description="Evaluate candidate plans",
    )
    sub_steps = [
        StepSpec(
            step_id="template_generating",
            conclusion_field="template",
            forward="reviewing",
            prompt_file="prompts/a.md",
            description="Generate template",
        ),
        StepSpec(
            step_id="reviewing",
            conclusion_field="review",
            forward=None,
            prompt_file="prompts/b.md",
            description="Review template",
        ),
    ]
    pipeline_runner._loaded.steps = [parallel_step]
    pipeline_runner._loaded.sub_pipelines = {
        "evaluate_candidate": SubPipelineSpec(
            name="evaluate_candidate",
            steps=sub_steps,
            max_rollbacks=2,
            iterate_over="candidates",
        )
    }
    pipeline_runner.state_machine = MagicMock(current_step=parallel_step, current_step_index=0)
    pipeline_runner.context = MagicMock()
    pipeline_runner.context.get_conclusion.return_value = None
    pipeline_runner._active_candidates = {}
    pipeline_runner._execution = {
        "kind": "parallel_sub_pipeline",
        "step_id": "evaluate",
        "sub_pipeline_name": "evaluate_candidate",
        "candidates": {
            "0": {
                "status": "running",
                "candidate": {"name": "基础方案"},
                "current_sub_step": "template_generating",
            },
            "1": {
                "status": "running",
                "candidate": {"name": "高可用方案"},
                "current_sub_step": "reviewing",
            },
        },
    }


class TestHandleUserInterrupt:
    @pytest.mark.asyncio
    async def test_supplement_injects_message(self, pipeline_runner):
        """Supplement verdict should inject message into current agent loop."""
        verdict = InterruptVerdict(action="supplement", reason="extra info")

        with patch.object(pipeline_runner, "_interrupt_controller") as mock_ctrl:
            mock_ctrl.judge = AsyncMock(return_value=verdict)
            result = await pipeline_runner.handle_user_interrupt("add more memory")

        assert result.action == "supplement"

    @pytest.mark.asyncio
    async def test_continue_does_nothing(self, pipeline_runner):
        verdict = InterruptVerdict(action="continue", reason="irrelevant")

        with patch.object(pipeline_runner, "_interrupt_controller") as mock_ctrl:
            mock_ctrl.judge = AsyncMock(return_value=verdict)
            result = await pipeline_runner.handle_user_interrupt("hello")

        assert result.action == "continue"
        assert result.paused is False

    @pytest.mark.asyncio
    async def test_judge_failure_on_pause_policy_step_keeps_pipeline_paused(self, pipeline_runner):
        pipeline_runner.state_machine.advance()  # a -> b
        pipeline_runner.state_machine.current_step.interrupt_judge_failure = "pause"
        verdict = InterruptVerdict(action="continue", reason="judge failed: timeout after 90.0s")

        with patch.object(pipeline_runner, "_interrupt_controller") as mock_ctrl:
            mock_ctrl.judge = AsyncMock(return_value=verdict)
            result = await pipeline_runner.handle_user_interrupt("stop deploying")

        assert result.action == "continue"
        assert result.paused is True
        assert "paused" in result.reason
        assert "timeout" in result.reason
        assert not pipeline_runner._agent_pause_event.is_set()


class TestApplyHardInterrupt:
    def test_parent_level_rollback(self, pipeline_runner):
        """Hard interrupt should rollback state machine and mark context stale."""
        pipeline_runner.state_machine.advance()  # a -> b
        pipeline_runner.context.set_conclusion("a_out", {"data": "test"})

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target="a",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        assert result is True
        assert pipeline_runner.state_machine.current_step.step_id == "a"
        assert pipeline_runner.state_machine._step_statuses["a"] == StepStatus.RUNNING
        assert pipeline_runner.state_machine._step_statuses["b"] == StepStatus.STALE

    def test_parent_hard_interrupt_discards_active_attempt_and_creates_fresh_target_attempt(self, pipeline_runner):
        previous = pipeline_runner._ensure_parent_attempt("a")
        pipeline_runner.session = CapturingPipelineSession()

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="retry",
            rollback_target="a",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        new_attempt_id = pipeline_runner._execution["active_attempt_id"]
        assert result is True
        assert pipeline_runner._attempts["items"][previous["attempt_id"]]["status"] == "discarded"
        assert new_attempt_id != previous["attempt_id"]
        assert pipeline_runner._attempts["items"][new_attempt_id]["step_id"] == "a"
        assert pipeline_runner._attempts["items"][new_attempt_id]["status"] == "running"
        rollback_save = next(save for save in pipeline_runner.session.saved if save[0] == "rollback")
        assert rollback_save[3]["execution"]["active_attempt_id"] == new_attempt_id

    def test_invalid_hard_interrupt_fallback_failure_marks_active_attempt_failed(self, pipeline_runner, monkeypatch):
        previous = pipeline_runner._ensure_parent_attempt("a")
        pipeline_runner.session = CapturingPipelineSession()
        monkeypatch.setattr(pipeline_runner, "_can_interrupt_rollback_to", lambda target: (False, "nope"))

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target="missing",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        failed_save = next(save for save in pipeline_runner.session.saved if save[0] == "failed")
        assert result is False
        assert pipeline_runner._attempts["items"][previous["attempt_id"]]["status"] == "failed"
        assert pipeline_runner._execution == {}
        assert failed_save[3]["execution"] is None
        assert failed_save[3]["attempts"]["items"][previous["attempt_id"]]["status"] == "failed"

    def test_rollback_to_current_step(self, pipeline_runner):
        """Hard interrupt can rollback to the current step itself."""
        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="retry",
            rollback_target="a",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)
        assert result is True
        assert pipeline_runner.state_machine.current_step.step_id == "a"

    def test_invalid_parent_hard_interrupt_target_falls_back_to_current_step(self, pipeline_runner):
        pipeline_runner.state_machine.advance()  # a -> b

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target="missing",
            rollback_context="用户想重新调整",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        assert result is True
        assert pipeline_runner.state_machine.current_step.step_id == "b"
        assert pipeline_runner._rollback_context == "用户想重新调整"
        assert any(
            call[0] == "rollback_sync" and call[1] == "b" and call[2] == "b" for call in pipeline_runner.session.calls
        )

    def test_future_parent_hard_interrupt_target_falls_back_to_current_step(self, pipeline_runner):
        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target="b",
            rollback_context="用户想继续但judge错了",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        assert result is True
        assert pipeline_runner.state_machine.current_step.step_id == "a"
        assert pipeline_runner._rollback_context == "用户想继续但judge错了"
        assert any(
            call[0] == "rollback_sync" and call[1] == "a" and call[2] == "a" for call in pipeline_runner.session.calls
        )

    def test_parent_hard_interrupt_persists_rollback_sidecar(self, pipeline_runner):
        pipeline_runner.state_machine.advance()  # a -> b

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target="a",
            rollback_context="改成低成本",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        assert result is True
        assert ("rollback_sync", "b", "a", 0, "changed mind", "test") in pipeline_runner.session.calls
        assert pipeline_runner.sidecar_status == "running"

    def test_completed_pipeline_hard_interrupt_fails_gracefully(self, pipeline_runner):
        pipeline_runner.state_machine.advance()  # a -> b
        pipeline_runner.state_machine.advance()  # b -> complete
        pipeline_runner._rollback_context = "previous interrupt"

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="too late",
            rollback_target="a",
            rollback_context="ignored",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        assert result is False
        assert pipeline_runner._rollback_context is None
        assert pipeline_runner.last_applied_interrupt_verdict is None

    @pytest.mark.parametrize(
        ("start_at_b", "requested_target", "expected_target"),
        [
            (True, "missing", "b"),
            (False, "b", "a"),
        ],
    )
    def test_parent_hard_interrupt_fallback_is_observable(
        self, pipeline_runner, start_at_b, requested_target, expected_target
    ):
        if start_at_b:
            pipeline_runner.state_machine.advance()  # a -> b

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target=requested_target,
            rollback_context="用户想重新调整",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        assert result is True
        assert pipeline_runner.last_applied_interrupt_verdict is not None
        assert pipeline_runner.last_applied_interrupt_verdict.rollback_target == expected_target
        assert pipeline_runner.last_applied_interrupt_verdict.reason == (
            f"invalid rollback target {requested_target!r}; "
            f"falling back to current step {expected_target!r}: changed mind"
        )


class TestContinueAfterInterrupt:
    @pytest.mark.asyncio
    async def test_returns_generator(self, pipeline_runner):
        """continue_after_interrupt should return an async generator."""
        gen = pipeline_runner.continue_after_interrupt()
        assert hasattr(gen, "__aiter__")
        await gen.aclose()


class TestContinueFromSidecarInterruptRouting:
    @pytest.mark.asyncio
    async def test_supplement_verdict_passes_input_to_current_step(self, pipeline_runner):
        verdict = InterruptVerdict(action="supplement", reason="extra context")

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock(return_value=verdict)) as judge,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input="use a smaller instance"):
                pass

        judge.assert_awaited_once_with("use a smaller instance")
        cont.assert_called_once_with(user_input="use a smaller instance", resume_running_step=True)

    @pytest.mark.asyncio
    async def test_supplement_verdict_preserves_image_blocks_for_current_step(self, pipeline_runner):
        verdict = InterruptVerdict(action="supplement", reason="extra context")
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock(return_value=verdict)) as judge,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input=image_input):
                pass

        judge.assert_awaited_once()
        judged_input = judge.await_args.args[0]
        assert judged_input.content == image_input
        assert judged_input.display_text == "参考这张图"
        assert judged_input.has_images is True
        cont.assert_called_once_with(
            user_input=image_input,
            user_input_display_text="参考这张图",
            resume_running_step=True,
        )

    @pytest.mark.asyncio
    async def test_restored_parallel_continuation_judges_with_persisted_candidate_state(self, pipeline_runner):
        _seed_restored_parallel_judge_state(pipeline_runner)
        captured_states = []

        async def judge_with_state_capture(_message):
            captured_states.append(pipeline_runner._get_state_for_judge())
            return InterruptVerdict(action="continue", reason="keep going")

        with (
            patch.object(
                pipeline_runner._interrupt_controller,
                "judge",
                AsyncMock(side_effect=judge_with_state_capture),
            ) as judge,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input="继续执行"):
                pass

        judge.assert_awaited_once_with("继续执行")
        cont.assert_called_once_with(resume_running_step=True)
        assert captured_states[0]["candidate_states"][0]["name"] == "基础方案"
        assert captured_states[0]["candidate_states"][0]["current_sub_step"] == "template_generating"
        assert captured_states[0]["sub_pipeline_steps"][0]["step_id"] == "template_generating"

    @pytest.mark.asyncio
    async def test_restored_running_judge_failure_pause_yields_waiting_input(self, pipeline_runner):
        pipeline_runner.state_machine.advance()  # a -> b
        pipeline_runner.state_machine.current_step.interrupt_judge_failure = "pause"
        pipeline_runner.session = MagicMock()
        pipeline_runner.session.save_waiting_input = AsyncMock()
        pipeline_runner.mark_user_aborted = MagicMock()
        verdict = InterruptVerdict(action="continue", reason="judge failed: timeout after 90.0s")

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock(return_value=verdict)) as judge,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            events = [event async for event in pipeline_runner.continue_from_sidecar(user_input="stop deploying")]

        judge.assert_awaited_once_with("stop deploying")
        cont.assert_not_called()
        pipeline_runner.mark_user_aborted.assert_not_called()
        assert pipeline_runner.sidecar_status == "waiting_input"
        pipeline_runner.session.save_waiting_input.assert_awaited_once()
        assert len(events) == 1
        event = events[0]
        assert event.type == PipelineEventType.USER_INPUT_REQUIRED
        assert event.step_id == "b"
        assert event.data["kind"] == "pipeline_pause_confirmation"
        assert event.data["paused"] is True
        assert "timeout" in event.data["reason"]
        assert not pipeline_runner._agent_pause_event.is_set()

    @pytest.mark.asyncio
    async def test_restored_running_judge_failure_hard_interrupt_restarts_parent(self, pipeline_runner):
        pipeline_runner.state_machine.advance()  # a -> b
        pipeline_runner.state_machine.current_step.interrupt_judge_failure = "hard_interrupt"
        verdict = InterruptVerdict(action="continue", reason="judge failed: timeout after 90.0s")

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock(return_value=verdict)) as judge,
            patch.object(pipeline_runner, "apply_hard_interrupt", MagicMock(return_value=True)) as apply_interrupt,
            patch.object(
                pipeline_runner, "continue_after_interrupt", MagicMock(return_value=_empty_stream())
            ) as restart,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input="stop deploying"):
                pass

        judge.assert_awaited_once_with("stop deploying")
        cont.assert_not_called()
        restart.assert_called_once_with()
        applied_verdict = apply_interrupt.call_args.args[0]
        assert applied_verdict.action == "hard_interrupt"
        assert applied_verdict.rollback_target == "b"
        assert "timeout" in applied_verdict.reason

    @pytest.mark.asyncio
    async def test_restored_running_judge_exception_hard_interrupt_restarts_parent(self, pipeline_runner):
        pipeline_runner.state_machine.advance()  # a -> b
        pipeline_runner.state_machine.current_step.interrupt_judge_failure = "hard_interrupt"

        with (
            patch.object(
                pipeline_runner._interrupt_controller,
                "judge",
                AsyncMock(side_effect=RuntimeError("judge exploded")),
            ),
            patch.object(pipeline_runner, "apply_hard_interrupt", MagicMock(return_value=True)) as apply_interrupt,
            patch.object(
                pipeline_runner, "continue_after_interrupt", MagicMock(return_value=_empty_stream())
            ) as restart,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input="stop deploying"):
                pass

        cont.assert_not_called()
        restart.assert_called_once_with()
        applied_verdict = apply_interrupt.call_args.args[0]
        assert applied_verdict.action == "hard_interrupt"
        assert applied_verdict.rollback_target == "b"
        assert "judge exploded" in applied_verdict.reason

    @pytest.mark.asyncio
    async def test_restored_running_continue_after_pause_resumes_agent_loops(self, pipeline_runner):
        pipeline_runner.pause_agent_loops()
        verdict = InterruptVerdict(action="continue", reason="continue")

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock(return_value=verdict)),
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input="continue"):
                pass

        cont.assert_called_once_with(resume_running_step=True)
        assert pipeline_runner._agent_pause_event.is_set()

    @pytest.mark.asyncio
    async def test_resume_pause_confirmation_continue_bypasses_judge(self, pipeline_runner):
        pipeline_runner.state_machine.advance()  # a -> b
        pipeline_runner._set_pending_input_kind("pipeline_pause_confirmation")

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock()) as judge,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            events = [event async for event in pipeline_runner.resume("continue")]

        judge.assert_not_awaited()
        cont.assert_called_once_with(user_input=None, resume_running_step=True)
        assert pipeline_runner.pending_input_kind() is None
        assert events[0].type == PipelineEventType.USER_INPUT_RECEIVED
        assert events[0].step_id == "b"
        assert events[0].data == {
            "kind": "pipeline_pause_confirmation",
            "user_input_length": len("continue"),
        }

    @pytest.mark.asyncio
    async def test_restored_running_judge_exception_after_pause_resumes_agent_loops(self, pipeline_runner):
        pipeline_runner.pause_agent_loops()

        with (
            patch.object(
                pipeline_runner._interrupt_controller,
                "judge",
                AsyncMock(side_effect=RuntimeError("judge exploded")),
            ),
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input="add detail"):
                pass

        cont.assert_called_once_with(user_input="add detail", resume_running_step=True)
        assert pipeline_runner._agent_pause_event.is_set()

    @pytest.mark.asyncio
    async def test_restored_parallel_supplement_verdict_preserves_candidate_target(self, pipeline_runner):
        _seed_restored_parallel_judge_state(pipeline_runner)
        verdict = InterruptVerdict(
            action="supplement",
            reason="extra context",
            supplement_target="candidate:1",
        )
        captured = {}

        async def capture_continue(*, user_input=None, **kwargs):
            captured["user_input"] = user_input
            captured["restored_supplement"] = getattr(pipeline_runner, "_restored_supplement", None)
            captured["kwargs"] = kwargs
            return
            yield  # noqa: B901

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock(return_value=verdict)) as judge,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(side_effect=capture_continue)) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input="only adjust HA plan"):
                pass

        judge.assert_awaited_once_with("only adjust HA plan")
        cont.assert_called_once_with(user_input=None, resume_running_step=True)
        assert captured["user_input"] is None
        assert captured["kwargs"] == {"resume_running_step": True}
        assert captured["restored_supplement"] == {
            "message": "only adjust HA plan",
            "target": "candidate:1",
        }

    @pytest.mark.asyncio
    async def test_hard_interrupt_verdict_applies_interrupt_and_restarts_parent(self, pipeline_runner):
        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="change architecture",
            rollback_target="a",
            rollback_context="use a managed database",
        )

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock(return_value=verdict)) as judge,
            patch.object(pipeline_runner, "apply_hard_interrupt", MagicMock(return_value=True)) as apply_interrupt,
            patch.object(pipeline_runner, "continue_after_interrupt", MagicMock(return_value=_empty_stream())) as cont,
        ):
            async for _event in pipeline_runner.continue_from_sidecar(user_input="use a managed database"):
                pass

        judge.assert_awaited_once_with("use a managed database")
        apply_interrupt.assert_called_once_with(verdict)
        cont.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_hard_interrupt_fallback_failure_does_not_resume_current_step(self, pipeline_runner):
        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="change architecture",
            rollback_target="missing",
        )
        pipeline_runner.session = CapturingPipelineSession()

        with (
            patch.object(pipeline_runner._interrupt_controller, "judge", AsyncMock(return_value=verdict)) as judge,
            patch.object(pipeline_runner, "_can_interrupt_rollback_to", MagicMock(return_value=(False, "nope"))),
            patch.object(
                pipeline_runner, "continue_after_interrupt", MagicMock(return_value=_empty_stream())
            ) as restart,
            patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont,
        ):
            events = [event async for event in pipeline_runner.continue_from_sidecar(user_input="change architecture")]

        judge.assert_awaited_once_with("change architecture")
        restart.assert_not_called()
        cont.assert_not_called()
        assert pipeline_runner.sidecar_status == "failed"
        assert len(events) == 1
        assert events[0].type == PipelineEventType.PIPELINE_COMPLETED
        assert events[0].data["failed"] is True


class TestCandidateRestart:
    @pytest.mark.asyncio
    async def test_schedule_candidate_restart_sets_pending(self, pipeline_runner):
        """Scheduling a candidate restart should populate _pending_candidate_restarts."""
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        pipeline_runner._active_candidates[0] = {
            "task": mock_task,
            "current_sub_step": "template_generating",
            "conclusions": {"template": {"content": "..."}},
            "name": "基础方案",
            "agent_loop": None,
        }

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="fix template",
            rollback_target="template_generating",
            candidate_scope="candidate:0",
        )
        pipeline_runner._schedule_candidate_restart(verdict)

        assert 0 in pipeline_runner._pending_candidate_restarts
        assert pipeline_runner._pending_candidate_restarts[0].start_from_step == "template_generating"
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_schedule_all_candidates_restart(self, pipeline_runner):
        """scope='all' should cancel and restart all candidates."""
        tasks = []
        for i in range(3):
            mock_task = MagicMock()
            mock_task.done.return_value = False
            mock_task.cancel = MagicMock()
            tasks.append(mock_task)
            pipeline_runner._active_candidates[i] = {
                "task": mock_task,
                "current_sub_step": "reviewing",
                "conclusions": {},
                "name": f"方案{i}",
                "agent_loop": None,
            }
        # scope="all" expands to range(_parallel_candidates_total); the real
        # parallel step sets this whenever candidates are live.
        pipeline_runner._parallel_candidates_total = 3

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="redo all",
            rollback_target="template_generating",
            candidate_scope="all",
        )
        pipeline_runner._schedule_candidate_restart(verdict)

        assert len(pipeline_runner._pending_candidate_restarts) == 3
        for t in tasks:
            t.cancel.assert_called_once()

    def test_candidate_hard_interrupt_uses_persisted_running_candidate_state(self, pipeline_runner):
        """Restored parallel steps have no live tasks yet, but sidecar state still marks candidates running."""
        _seed_restored_parallel_judge_state(pipeline_runner)
        pipeline_runner._schedule_candidate_restart = MagicMock()

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="fix restored candidate",
            rollback_target="template_generating",
            candidate_scope="candidate:0",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        assert result is False
        pipeline_runner._schedule_candidate_restart.assert_called_once_with(verdict)

    def test_parent_attempt_creation_preserves_restored_parallel_execution(self, pipeline_runner):
        _seed_restored_parallel_judge_state(pipeline_runner)

        attempt = pipeline_runner._ensure_parent_attempt("evaluate")

        assert attempt["scope"] == "parent"
        assert pipeline_runner._execution["kind"] == "parallel_sub_pipeline"
        assert pipeline_runner._execution["active_attempt_id"] == attempt["attempt_id"]
        assert pipeline_runner._execution["candidates"]["0"]["status"] == "running"

    def test_restored_all_scope_with_completed_candidate_escalates_to_parent_rollback(self, pipeline_runner):
        _seed_restored_parallel_judge_state(pipeline_runner)
        pipeline_runner._execution["candidates"]["1"]["status"] = "completed"
        pipeline_runner._schedule_candidate_restart = MagicMock()

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="redo every candidate",
            rollback_target="template_generating",
            candidate_scope="all",
        )
        result = pipeline_runner.apply_hard_interrupt(verdict)

        assert result is True
        pipeline_runner._schedule_candidate_restart.assert_not_called()
        pipeline_runner.state_machine.interrupt_rollback.assert_called_once_with("evaluate", "redo every candidate")


class TestGetStateForJudge:
    def test_basic_state(self, pipeline_runner):
        """_get_state_for_judge returns expected dict keys."""
        state = pipeline_runner._get_state_for_judge()
        assert state["pipeline_name"] == "test"
        assert state["current_step_id"] == "a"
        assert len(state["steps"]) == 2
        assert state["steps"][0]["is_current"] is True

    def test_includes_conclusions(self, pipeline_runner):
        """Should include completed conclusions."""
        pipeline_runner.context.set_conclusion("a_out", {"intent": "deploy nginx"})
        state = pipeline_runner._get_state_for_judge()
        assert "a_out" in state["conclusions"]

    def test_includes_candidate_states(self, pipeline_runner):
        """Should include candidate states when active."""
        pipeline_runner._active_candidates[0] = {
            "name": "Test方案",
            "current_sub_step": "generating",
        }
        state = pipeline_runner._get_state_for_judge()
        assert "candidate_states" in state
        assert state["candidate_states"][0]["name"] == "Test方案"

    def test_includes_persisted_candidate_states_for_restored_parallel_step(self, pipeline_runner):
        """Restored parallel execution should expose persisted candidates before live state is rehydrated."""
        _seed_restored_parallel_judge_state(pipeline_runner)

        state = pipeline_runner._get_state_for_judge()

        assert state["candidate_states"] == [
            {
                "index": 0,
                "name": "基础方案",
                "current_sub_step": "template_generating",
                "status": "running",
            },
            {
                "index": 1,
                "name": "高可用方案",
                "current_sub_step": "reviewing",
                "status": "running",
            },
        ]
        assert state["sub_pipeline_steps"] == [
            {"step_id": "template_generating", "description": "Generate template"},
            {"step_id": "reviewing", "description": "Review template"},
        ]


class TestInjectSupplement:
    def test_inject_to_current_step(self, pipeline_runner):
        """Supplement without target should inject to current step's agent loop."""
        mock_loop = MagicMock()
        pipeline_runner._step_executor._current_agent_loop = mock_loop

        verdict = InterruptVerdict(action="supplement", reason="extra", supplement_target=None)
        pipeline_runner._inject_supplement(verdict, "add 4GB RAM")

        mock_loop.inject_user_message.assert_called_once_with("add 4GB RAM")

    def test_inject_to_specific_candidate(self, pipeline_runner):
        """Supplement to candidate_index:N should inject to that candidate."""
        mock_loop = MagicMock()
        pipeline_runner._active_candidates[1] = {
            "agent_loop": mock_loop,
            "name": "方案2",
        }

        verdict = InterruptVerdict(action="supplement", reason="extra", supplement_target="candidate_index:1")
        pipeline_runner._inject_supplement(verdict, "use t5.large")

        mock_loop.inject_user_message.assert_called_once_with("use t5.large")

    def test_inject_to_all_candidates(self, pipeline_runner):
        """Supplement to 'all' should inject to all candidates."""
        loops = [MagicMock(), MagicMock()]
        for i, loop in enumerate(loops):
            pipeline_runner._active_candidates[i] = {
                "agent_loop": loop,
                "name": f"方案{i}",
            }

        verdict = InterruptVerdict(action="supplement", reason="extra", supplement_target="all")
        pipeline_runner._inject_supplement(verdict, "add SLB")

        for loop in loops:
            loop.inject_user_message.assert_called_once_with("add SLB")

    def test_inject_to_current_step_rejects_closed_loop(self, pipeline_runner):
        mock_loop = MagicMock()
        mock_loop.can_accept_injected_user_message = False
        pipeline_runner._step_executor._current_agent_loop = mock_loop

        verdict = InterruptVerdict(action="supplement", reason="extra", supplement_target=None)
        injected = pipeline_runner._inject_supplement(verdict, "add 4GB RAM")

        assert injected is False
        mock_loop.inject_user_message.assert_not_called()

    def test_inject_to_specific_candidate_rejects_closed_loop(self, pipeline_runner):
        mock_loop = MagicMock()
        mock_loop.can_accept_injected_user_message = False
        pipeline_runner._active_candidates[1] = {
            "agent_loop": mock_loop,
            "name": "方案2",
        }

        verdict = InterruptVerdict(action="supplement", reason="extra", supplement_target="candidate:1")
        injected = pipeline_runner._inject_supplement(verdict, "use t5.large")

        assert injected is False
        mock_loop.inject_user_message.assert_not_called()

    def test_inject_to_all_candidates_succeeds_when_one_loop_accepts(self, pipeline_runner):
        closed_loop = MagicMock()
        closed_loop.can_accept_injected_user_message = False
        open_loop = MagicMock()
        open_loop.can_accept_injected_user_message = True
        pipeline_runner._active_candidates[0] = {"agent_loop": closed_loop, "name": "方案1"}
        pipeline_runner._active_candidates[1] = {"agent_loop": open_loop, "name": "方案2"}

        verdict = InterruptVerdict(action="supplement", reason="extra", supplement_target="all")
        injected = pipeline_runner._inject_supplement(verdict, "add SLB")

        assert injected is True
        closed_loop.inject_user_message.assert_not_called()
        open_loop.inject_user_message.assert_called_once_with("add SLB")

    def test_inject_to_all_candidates_tolerates_active_candidate_mutation(self, pipeline_runner):
        class MutatingLoop:
            can_accept_injected_user_message = True

            def __init__(self) -> None:
                self.messages: list[str] = []

            def inject_user_message(self, message: str) -> None:
                self.messages.append(message)
                pipeline_runner._active_candidates.pop(1, None)

        mutating_loop = MutatingLoop()
        other_loop = MagicMock()
        other_loop.can_accept_injected_user_message = True
        pipeline_runner._active_candidates[0] = {"agent_loop": mutating_loop, "name": "方案1"}
        pipeline_runner._active_candidates[1] = {"agent_loop": other_loop, "name": "方案2"}

        verdict = InterruptVerdict(action="supplement", reason="extra", supplement_target="all")
        injected = pipeline_runner._inject_supplement(verdict, "add SLB")

        assert injected is True
        assert mutating_loop.messages == ["add SLB"]
        other_loop.inject_user_message.assert_called_once_with("add SLB")

    def test_cancel_active_candidates_tolerates_active_candidate_mutation(self, pipeline_runner):
        class MutatingTask:
            def __init__(self) -> None:
                self.cancelled = False

            def done(self) -> bool:
                pipeline_runner._active_candidates.pop(1, None)
                return False

            def cancel(self) -> None:
                self.cancelled = True

        mutating_task = MutatingTask()
        other_task = MagicMock()
        other_task.done.return_value = False
        pipeline_runner._active_candidates[0] = {"task": mutating_task, "name": "方案1"}
        pipeline_runner._active_candidates[1] = {"task": other_task, "name": "方案2"}

        cancelled = pipeline_runner._cancel_active_candidates(reason="cleanup")

        assert mutating_task in cancelled
        assert mutating_task.cancelled is True
        other_task.cancel.assert_called_once()
        assert pipeline_runner._active_candidates == {}

    def test_try_inject_helper_prefers_checked_api(self):
        class CheckedLoop:
            def __init__(self, accepted):
                self.accepted = accepted
                self.try_injected = []
                self.legacy_injected = []

            def try_inject_user_message(self, message):
                self.try_injected.append(message)
                return self.accepted

            def inject_user_message(self, message):
                self.legacy_injected.append(message)

        accepting_loop = CheckedLoop(True)
        rejected_loop = CheckedLoop(False)

        assert PipelineRunner._try_inject_into_agent_loop(accepting_loop, "add SLB") is True
        assert PipelineRunner._try_inject_into_agent_loop(rejected_loop, "add SLB") is False

        assert accepting_loop.try_injected == ["add SLB"]
        assert rejected_loop.try_injected == ["add SLB"]
        assert accepting_loop.legacy_injected == []
        assert rejected_loop.legacy_injected == []


class TestParentRollbackDuringParallelStep:
    """Tests that parent-level rollback is forced when rollback_target is a parent step,
    even if candidate_scope is set (e.g. judge LLM hallucination)."""

    @pytest.fixture
    def parallel_pipeline_runner(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "a.md").write_text("do A", encoding="utf-8")
        (tmp_path / "prompts" / "b.md").write_text("do B", encoding="utf-8")
        (tmp_path / "prompts" / "t.md").write_text("do T", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              a_out: []
              b_out: [a_out]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: a_out.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: null
                    prompt: prompts/t.md
            steps:
              - id: a
                conclusion_field: a_out
                forward: b
                prompt: prompts/a.md
                description: Step A
              - id: b
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: b_out
                forward: null
                prompt: prompts/b.md
                description: Step B
        """),
            encoding="utf-8",
        )
        pm = MagicMock()
        pm.get_model_name.return_value = "test-model"
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=pm,
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test-session",
            cwd=str(tmp_path),
        )
        return runner

    def test_candidate_scope_with_parent_target_forces_parent_rollback(self, parallel_pipeline_runner):
        """candidate_scope='all' + parent rollback_target should force parent rollback."""
        runner = parallel_pipeline_runner
        runner.state_machine.advance()  # a -> b (parallel step)
        runner.context.set_conclusion("a_out", {"candidates": [{"name": "Plan A"}]})

        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        runner._active_candidates[0] = {
            "task": mock_task,
            "current_sub_step": "template_gen",
            "conclusions": {},
            "name": "Plan A",
            "agent_loop": None,
        }

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="user wants FC instead of ECS",
            rollback_target="a",
            candidate_scope="all",
            rollback_context="用户要求改用FC实现",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is True
        assert runner.state_machine.current_step.step_id == "a"
        assert len(runner._active_candidates) == 0
        assert runner._rollback_context == "用户要求改用FC实现"

    def test_candidate_scope_with_sub_step_target_does_candidate_restart(self, parallel_pipeline_runner):
        """candidate_scope='all' + sub-pipeline rollback_target should do candidate restart."""
        runner = parallel_pipeline_runner
        runner.state_machine.advance()  # a -> b (parallel step)

        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        runner._active_candidates[0] = {
            "task": mock_task,
            "current_sub_step": "template_gen",
            "conclusions": {},
            "name": "Plan A",
            "agent_loop": None,
        }
        # All requested candidates still active → no escalation to parent rollback.
        runner._parallel_candidates_total = 1

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="fix template",
            rollback_target="template_gen",
            candidate_scope="all",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is False
        assert 0 in runner._pending_candidate_restarts
        assert runner.state_machine.current_step.step_id == "b"

    def test_candidate_scope_with_future_sub_step_target_forces_parent_rollback(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "a.md").write_text("do A", encoding="utf-8")
        (tmp_path / "prompts" / "b.md").write_text("do B", encoding="utf-8")
        (tmp_path / "prompts" / "template.md").write_text("do template", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("do cost", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              a_out: []
              b_out: [a_out]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: a_out.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: cost_estimate
                    prompt: prompts/template.md
                  - id: cost_estimate
                    conclusion_field: cost
                    forward: null
                    prompt: prompts/cost.md
            steps:
              - id: a
                conclusion_field: a_out
                forward: b
                prompt: prompts/a.md
                description: Step A
              - id: b
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: b_out
                forward: null
                prompt: prompts/b.md
                description: Step B
        """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test-session",
            cwd=str(tmp_path),
        )
        runner.state_machine.advance()  # a -> b (parallel step)
        mock_task = MagicMock()
        mock_task.done.return_value = False
        runner._active_candidates[0] = {
            "task": mock_task,
            "current_sub_step": "template_gen",
            "conclusions": {},
            "name": "Plan A",
            "agent_loop": None,
        }
        runner._parallel_candidates_total = 1

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="user asks to change later cost assumptions",
            rollback_target="cost_estimate",
            candidate_scope="candidate:0",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is True
        assert runner.state_machine.current_step.step_id == "b"
        assert runner._pending_candidate_restarts == {}

    def test_candidate_restart_persists_fresh_target_attempt_before_cancelling(self, parallel_pipeline_runner):
        runner = parallel_pipeline_runner
        runner.state_machine.advance()  # a -> b (parallel step)
        runner.session = CapturingPipelineSession()
        old_attempt = runner._create_sub_step_attempt(
            parent_step_id="b",
            candidate_index=0,
            sub_pipeline_id="evaluate_candidate_a",
            sub_step_id="review",
        )
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        runner._active_candidates[0] = {
            "task": mock_task,
            "current_sub_step": "review",
            "conclusions": {"template": {"body": "stale"}},
            "name": "Plan A",
            "agent_loop": None,
            "sub_pipeline_id": "evaluate_candidate_a",
            "state_machine": {
                "current_index": 0,
                "rollback_count": 0,
                "interrupt_rollback_count": 0,
                "step_statuses": {"template_gen": "completed"},
            },
            "context": {
                "candidate": {
                    "value": {"name": "Plan A"},
                    "version": 1,
                    "stale": False,
                    "updated_at": None,
                    "history": [],
                },
                "template": {
                    "value": {"body": "stale"},
                    "version": 1,
                    "stale": False,
                    "updated_at": None,
                    "history": [],
                },
            },
            "active_attempt_id": old_attempt["attempt_id"],
            "transcript_id": old_attempt["transcript_id"],
        }
        runner._execution = {
            "kind": "parallel_sub_pipeline",
            "step_id": "b",
            "sub_pipeline_name": "evaluate_candidate",
            "candidates": {
                "0": {
                    "status": "running",
                    "candidate": {"name": "Plan A"},
                    "sub_pipeline_id": "evaluate_candidate_a",
                    "current_sub_step": "review",
                    "active_attempt_id": old_attempt["attempt_id"],
                    "transcript_id": old_attempt["transcript_id"],
                }
            },
        }
        runner._parallel_candidates_total = 1

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="fix template",
            rollback_target="template_gen",
            candidate_scope="candidate:0",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is False
        mock_task.cancel.assert_called_once()
        candidate_state = runner._execution["candidates"]["0"]
        new_attempt_id = candidate_state["active_attempt_id"]
        assert candidate_state["status"] == "running"
        assert candidate_state["current_sub_step"] == "template_gen"
        assert candidate_state["state_machine"]["current_index"] == 0
        assert candidate_state["pending_restart"] == {
            "start_from_step": "template_gen",
            "preserved_conclusions": {},
            "rollback_context": None,
        }
        assert new_attempt_id != old_attempt["attempt_id"]
        assert runner._attempts["items"][old_attempt["attempt_id"]]["status"] == "discarded"
        new_attempt = runner._attempts["items"][new_attempt_id]
        assert new_attempt["status"] == "running"
        assert new_attempt["sub_step_id"] == "template_gen"
        saved_execution = runner.session.saved[-1][3]["execution"]
        assert saved_execution["candidates"]["0"]["active_attempt_id"] == new_attempt_id
        assert saved_execution["candidates"]["0"]["current_sub_step"] == "template_gen"
        assert saved_execution["candidates"]["0"]["pending_restart"]["start_from_step"] == "template_gen"

    def test_candidate_restart_marks_target_and_downstream_context_stale(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "a.md").write_text("do A", encoding="utf-8")
        (tmp_path / "prompts" / "b.md").write_text("do B", encoding="utf-8")
        (tmp_path / "prompts" / "template.md").write_text("template", encoding="utf-8")
        (tmp_path / "prompts" / "review.md").write_text("review", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("cost", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              a_out: []
              b_out: [a_out]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: a_out.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: review
                    prompt: prompts/template.md
                  - id: review
                    conclusion_field: review
                    forward: cost
                    prompt: prompts/review.md
                  - id: cost
                    conclusion_field: cost
                    forward: null
                    prompt: prompts/cost.md
            steps:
              - id: a
                conclusion_field: a_out
                forward: b
                prompt: prompts/a.md
              - id: b
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: b_out
                forward: null
                prompt: prompts/b.md
        """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test-session",
            cwd=str(tmp_path),
        )
        runner.state_machine.advance()  # a -> b
        runner.session = CapturingPipelineSession()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        runner._active_candidates[0] = {
            "task": mock_task,
            "current_sub_step": "review",
            "conclusions": {
                "template": {"body": "old"},
                "review": {"ok": True},
                "cost": {"total": 1},
            },
            "sub_pipeline_id": "evaluate_candidate_a",
            "state_machine": {
                "current_index": 1,
                "rollback_count": 0,
                "interrupt_rollback_count": 0,
                "step_statuses": {
                    "template_gen": "completed",
                    "review": "running",
                    "cost": "pending",
                },
            },
            "context": {
                "candidate": {"value": {"name": "Plan A"}, "version": 1, "stale": False, "updated_at": None},
                "template": {"value": {"body": "old"}, "version": 1, "stale": False, "updated_at": None},
                "review": {"value": {"ok": True}, "version": 1, "stale": False, "updated_at": None},
                "cost": {"value": {"total": 1}, "version": 1, "stale": False, "updated_at": None},
            },
        }
        runner._parallel_candidates_total = 1

        result = runner.apply_hard_interrupt(
            InterruptVerdict(
                action="hard_interrupt",
                reason="redo template",
                rollback_target="template_gen",
                candidate_scope="candidate:0",
            )
        )

        assert result is False
        context = runner._execution["candidates"]["0"]["context"]
        assert context["template"]["stale"] is True
        assert context["review"]["stale"] is True
        assert context["cost"]["stale"] is True

    def test_null_string_candidate_scope_with_parent_target(self, parallel_pipeline_runner):
        """Even if _parse_verdict somehow passes a non-null scope, parent target wins."""
        runner = parallel_pipeline_runner
        runner.state_machine.advance()

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target="a",
            candidate_scope="candidate:0",
        )
        result = runner.apply_hard_interrupt(verdict)

        assert result is True
        assert runner.state_machine.current_step.step_id == "a"


class TestRollbackContextPropagation:
    def test_apply_hard_interrupt_stores_rollback_context(self, pipeline_runner):
        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target="a",
            rollback_context="用户要求改为WordPress网站",
        )
        pipeline_runner.apply_hard_interrupt(verdict)
        assert pipeline_runner._rollback_context == "用户要求改为WordPress网站"

    @pytest.mark.asyncio
    async def test_continue_after_interrupt_passes_context(self, pipeline_runner):
        pipeline_runner._rollback_context = "用户要求改为WordPress"
        gen = pipeline_runner.continue_after_interrupt()
        assert pipeline_runner._rollback_context is None
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_continue_after_interrupt_preserves_image_source_input(self, pipeline_runner):
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="changed mind",
            rollback_target="a",
            rollback_context="用户要求改为WordPress网站",
        )
        pipeline_runner.apply_hard_interrupt(verdict, source_input=image_input)

        expected_display = "用户要求改为WordPress网站\n\n参考这张图"
        expected_content = [TextBlock(text="用户要求改为WordPress网站"), *image_input]
        with patch.object(pipeline_runner, "_continue_from_current", MagicMock(return_value=_empty_stream())) as cont:
            gen = pipeline_runner.continue_after_interrupt()
            await gen.aclose()

        assert pipeline_runner._rollback_context is None
        cont.assert_called_once_with(user_input=expected_content, user_input_display_text=expected_display)

    def test_schedule_candidate_restart_stores_rollback_context(self, pipeline_runner):
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        pipeline_runner._active_candidates[0] = {
            "task": mock_task,
            "current_sub_step": "template_generating",
            "conclusions": {},
            "name": "基础方案",
            "agent_loop": None,
        }

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="fix template",
            rollback_target="template_generating",
            candidate_scope="candidate:0",
            rollback_context="用户要求模板使用WordPress",
        )
        pipeline_runner._schedule_candidate_restart(verdict)

        assert pipeline_runner._pending_candidate_restarts[0].rollback_context == "用户要求模板使用WordPress"

    def test_schedule_candidate_restart_preserves_image_source_input(self, pipeline_runner):
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        pipeline_runner._active_candidates[0] = {
            "task": mock_task,
            "current_sub_step": "template_generating",
            "conclusions": {},
            "name": "基础方案",
            "agent_loop": None,
        }
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="fix template",
            rollback_target="template_generating",
            candidate_scope="candidate:0",
            rollback_context="用户要求模板使用WordPress",
        )
        pipeline_runner._schedule_candidate_restart(verdict, source_input=image_input)

        info = pipeline_runner._pending_candidate_restarts[0]
        assert info.rollback_context == "用户要求模板使用WordPress\n\n参考这张图"
        assert info.rollback_input == [TextBlock(text="用户要求模板使用WordPress"), *image_input]

    def test_candidate_ask_user_question_keeps_tool_result_before_image_message(self, pipeline_runner):
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        tool_result = ToolResultBlock(
            tool_use_id="toolu_1",
            content='{"selected_id":"","selected_label":"","free_text":"see image"}',
        )
        tool_result_message = Message(role="user", content=[tool_result])
        pipeline_runner._restored_ask_user_question = {
            "candidate_index": 0,
            "resume_messages": [tool_result_message],
            "user_message": image_input,
            "precompleted_tools": {"ask_user_question": {"free_text": "see image"}},
        }

        assert pipeline_runner._candidate_resume_messages_for_restored_ask_user_question(0) == [tool_result_message]
        assert pipeline_runner._candidate_user_message_for_restored_ask_user_question(0) == image_input

    def test_candidate_ask_user_question_resume_state_survives_execution_snapshot(self, pipeline_runner):
        _seed_restored_parallel_judge_state(pipeline_runner)
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        tool_result = ToolResultBlock(
            tool_use_id="toolu_1",
            content='{"selected_id":"","selected_label":"","free_text":"see image"}',
        )
        tool_result_message = Message(role="user", content=[tool_result])
        precompleted_tools = {"ask_user_question": {"free_text": "see image"}}

        pipeline_runner._set_candidate_ask_user_question_resume_state(
            0,
            user_message=image_input,
            resume_messages=[tool_result_message],
            precompleted_tools=precompleted_tools,
        )

        restored = PipelineRunner(
            pipeline_dir=pipeline_runner._pipeline_dir,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="restored",
            cwd=pipeline_runner._cwd,
        )
        restored._execution = dict(pipeline_runner._execution)

        assert restored._candidate_user_message_for_restored_ask_user_question(0) == image_input
        assert restored._candidate_resume_messages_for_restored_ask_user_question(0) == [tool_result_message]
        assert restored._candidate_precompleted_tools_for_restored_ask_user_question(0) == precompleted_tools

    @pytest.mark.asyncio
    async def test_restored_candidate_ask_user_question_image_resume_reaches_sub_pipeline(self, tmp_path):
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType

        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "sub_a.md").write_text("sub A", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              candidates_done: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              per_candidate:
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                max_rollbacks: 3
                steps:
                  - id: sub_a
                    conclusion_field: sub_a_out
                    forward: null
                    prompt: prompts/sub_a.md
                    description: Sub A
            steps:
              - id: parallel
                conclusion_field: candidates_done
                type: parallel_sub_pipeline
                sub_pipeline: per_candidate
                forward: null
                description: Parallel
        """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test",
            cwd=str(tmp_path),
        )
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        tool_result = ToolResultBlock(
            tool_use_id="toolu_1",
            content='{"selected_id":"","selected_label":"","free_text":"see image"}',
        )
        tool_result_message = Message(role="user", content=[tool_result])
        precompleted_tools = {"ask_user_question": {"free_text": "see image"}}
        candidates = [{"name": "方案1"}]
        runner.context.set_conclusion("architecture", {"candidates": candidates})
        runner._execution = {
            "kind": "parallel_sub_pipeline",
            "step_id": "parallel",
            "sub_pipeline_name": "per_candidate",
            "active_attempt_id": "attempt_parent",
            "transcript_id": "transcript_parent",
            "candidates": {
                "0": {
                    "status": "running",
                    "candidate": candidates[0],
                    "current_sub_step": "sub_a",
                    "state_machine": {"current_index": 0, "completed": [], "rollback_count": 0},
                    "context": {"fields": {}},
                    "pending_ask_user_question_resume": {
                        "user_message": [
                            {"type": "text", "text": "参考这张图"},
                            {"type": "image", "media_type": "image/png", "data": "aW1hZ2U="},
                        ],
                        "resume_messages": [tool_result_message.to_dict()],
                        "precompleted_tools": precompleted_tools,
                    },
                },
            },
        }
        runner._attempts["items"]["attempt_parent"] = {
            "attempt_id": "attempt_parent",
            "scope": "parent",
            "step_id": "parallel",
            "status": "running",
            "transcript_id": "transcript_parent",
        }
        captured: dict[str, object] = {}

        from iac_code.pipeline.engine import sub_pipeline_executor as spe_module

        original_execute_streaming = spe_module.SubPipelineExecutor.execute_streaming

        async def spy_execute_streaming(self, *args, **kwargs):
            captured.update(kwargs)
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "candidate_name": "方案",
                    "total_steps": 1,
                    "sub_pipeline_name": "per_candidate",
                },
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "failed": False,
                    "conclusions": {},
                },
            )

        spe_module.SubPipelineExecutor.execute_streaming = spy_execute_streaming
        try:
            async for _event in runner._continue_from_current(user_input=None):
                pass
        finally:
            spe_module.SubPipelineExecutor.execute_streaming = original_execute_streaming

        assert captured["user_message"] == image_input
        assert captured["resume_messages"] == [tool_result_message]
        assert captured["precompleted_tools"] == precompleted_tools

    @pytest.mark.asyncio
    async def test_parent_ask_user_question_image_resume_state_survives_sidecar_restore(self, pipeline_runner):
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        tool_result = ToolResultBlock(
            tool_use_id="toolu_1",
            content='{"selected_id":"","selected_label":"","free_text":"see image"}',
        )
        tool_result_message = Message(role="user", content=[tool_result])
        precompleted_tools = {"ask_user_question": {"free_text": "see image"}}

        pipeline_runner._set_current_step_user_input(image_input, display_text="参考这张图")
        pipeline_runner._set_current_step_resume_state(
            resume_messages=[tool_result_message],
            precompleted_tools=precompleted_tools,
        )
        snapshot = pipeline_runner._state_machine_snapshot_for_sidecar()

        restored = PipelineRunner(
            pipeline_dir=pipeline_runner._pipeline_dir,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="restored",
            cwd=pipeline_runner._cwd,
        )
        restored.state_machine = type(pipeline_runner.state_machine).from_snapshot(
            snapshot,
            restored._loaded.steps,
            max_rollbacks=restored._loaded.max_rollbacks,
        )
        restored._restore_current_step_user_input_from_snapshot(snapshot)

        captured: dict[str, object] = {}

        async def capture_execute(*args, **kwargs):
            captured.update(kwargs)
            if False:
                yield None

        restored._step_executor.execute = capture_execute

        async for _event in restored._continue_from_current(user_input=None):
            pass

        assert captured["user_message"] == image_input
        assert captured["resume_messages"] == [tool_result_message]
        assert captured["precompleted_tools"] == precompleted_tools

    @pytest.mark.asyncio
    async def test_parent_ask_user_question_restore_prefers_transcript_with_answer(self, pipeline_runner):
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        tool_result = ToolResultBlock(
            tool_use_id="toolu_1",
            content='{"selected_id":"","selected_label":"","free_text":"see image"}',
        )
        tool_result_message = Message(role="user", content=[tool_result])
        post_answer_message = Message(role="assistant", content=[TextBlock(text="我已经读取图片")])
        precompleted_tools = {"ask_user_question": {"free_text": "see image"}}

        pipeline_runner._set_current_step_user_input(image_input, display_text="参考这张图")
        pipeline_runner._set_current_step_resume_state(
            resume_messages=[tool_result_message],
            precompleted_tools=precompleted_tools,
        )
        snapshot = pipeline_runner._state_machine_snapshot_for_sidecar()

        restored = PipelineRunner(
            pipeline_dir=pipeline_runner._pipeline_dir,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="restored",
            cwd=pipeline_runner._cwd,
        )
        restored.state_machine = type(pipeline_runner.state_machine).from_snapshot(
            snapshot,
            restored._loaded.steps,
            max_rollbacks=restored._loaded.max_rollbacks,
        )
        restored._restore_current_step_user_input_from_snapshot(snapshot)
        restored._attempts["items"]["attempt_parent"] = {
            "attempt_id": "attempt_parent",
            "scope": "parent",
            "step_id": "a",
            "status": "running",
            "transcript_id": "transcript_parent",
        }
        restored._execution = {
            "kind": "step",
            "step_id": "a",
            "active_attempt_id": "attempt_parent",
            "transcript_id": "transcript_parent",
        }
        restored._transcript_storage = FakeTranscriptStorage(
            {"transcript_parent": [tool_result_message, post_answer_message]}
        )
        captured: dict[str, object] = {}

        async def capture_execute(*args, **kwargs):
            captured.update(kwargs)
            if False:
                yield None

        restored._step_executor.execute = capture_execute

        async for _event in restored._continue_from_current(user_input=None):
            pass

        assert captured["resume_messages"] == [tool_result_message, post_answer_message]
        assert captured["precompleted_tools"] == precompleted_tools

    @pytest.mark.asyncio
    async def test_parent_ask_user_question_restore_does_not_replay_transcript_image_user_message(
        self,
        pipeline_runner,
    ):
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        tool_result = ToolResultBlock(
            tool_use_id="toolu_1",
            content='{"selected_id":"","selected_label":"","free_text":"see image"}',
        )
        tool_result_message = Message(role="user", content=[tool_result])
        image_message = Message(role="user", content=image_input)
        precompleted_tools = {"ask_user_question": {"free_text": "see image"}}

        pipeline_runner._set_current_step_user_input(image_input, display_text="参考这张图")
        pipeline_runner._set_current_step_resume_state(
            resume_messages=[tool_result_message],
            precompleted_tools=precompleted_tools,
        )
        snapshot = pipeline_runner._state_machine_snapshot_for_sidecar()

        restored = PipelineRunner(
            pipeline_dir=pipeline_runner._pipeline_dir,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="restored",
            cwd=pipeline_runner._cwd,
        )
        restored.state_machine = type(pipeline_runner.state_machine).from_snapshot(
            snapshot,
            restored._loaded.steps,
            max_rollbacks=restored._loaded.max_rollbacks,
        )
        restored._restore_current_step_user_input_from_snapshot(snapshot)
        restored._attempts["items"]["attempt_parent"] = {
            "attempt_id": "attempt_parent",
            "scope": "parent",
            "step_id": "a",
            "status": "running",
            "transcript_id": "transcript_parent",
        }
        restored._execution = {
            "kind": "step",
            "step_id": "a",
            "active_attempt_id": "attempt_parent",
            "transcript_id": "transcript_parent",
        }
        restored._transcript_storage = FakeTranscriptStorage(
            {"transcript_parent": [tool_result_message, image_message]}
        )
        captured: dict[str, object] = {}

        async def capture_execute(*args, **kwargs):
            captured.update(kwargs)
            if False:
                yield None

        restored._step_executor.execute = capture_execute

        async for _event in restored._continue_from_current(user_input=None):
            pass

        assert captured["user_message"] is None
        assert captured["resume_messages"] == [tool_result_message, image_message]
        assert captured["precompleted_tools"] == precompleted_tools

    @pytest.mark.asyncio
    async def test_sub_pipeline_ask_user_question_restore_does_not_duplicate_transcript_answer(self, tmp_path):
        from iac_code.pipeline.engine.context import PipelineContext
        from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec, SubPipelineSpec
        from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        step = StepSpec(
            step_id="sub_a",
            conclusion_field="sub_a_out",
            forward=None,
            prompt_file="prompts/sub_a.md",
            description="Sub A",
        )
        sub_spec = SubPipelineSpec(
            name="per_candidate",
            steps=[step],
            max_rollbacks=3,
            iterate_over="architecture.candidates",
        )
        loaded = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={},
            max_rollbacks=3,
            skills={},
            sub_pipelines={"per_candidate": sub_spec},
        )
        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            pipeline=loaded,
            pipeline_dir=tmp_path,
            session_storage=FakeSessionStorage(),
            cwd=str(tmp_path),
        )
        tool_result = ToolResultBlock(
            tool_use_id="toolu_1",
            content='{"selected_id":"","selected_label":"","free_text":"see image"}',
        )
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        tool_result_message = Message(role="user", content=[tool_result])
        post_answer_message = Message(role="assistant", content=[TextBlock(text="我已经读取图片")])
        captured: dict[str, object] = {}

        class CapturingStepExecutor:
            async def execute(self, step, context, session_id, **kwargs):
                captured.update(kwargs)
                yield StepResult(
                    step_id=step.step_id,
                    status=StepStatus.COMPLETED,
                    conclusion={"ok": True},
                )

        executor._make_step_executor = lambda: CapturingStepExecutor()

        def allocate_sub_step_attempt(request):
            return {
                "attempt_id": "attempt_sub",
                "transcript_id": "transcript_sub",
                "resume_messages": [tool_result_message, post_answer_message],
            }

        async for _event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "方案1"},
            candidate_index=0,
            parent_context=PipelineContext({}),
            session_id="session",
            user_message=image_input,
            resume_messages=[tool_result_message],
            parent_step_id="parallel",
            resume_state={
                "sub_pipeline_id": "sub",
                "state_machine": {
                    "current_index": 0,
                    "rollback_count": 0,
                    "interrupt_rollback_count": 0,
                    "step_statuses": {},
                },
                "context": {"fields": {}},
                "active_attempt_id": "attempt_sub",
                "transcript_id": "transcript_sub",
                "current_sub_step": "sub_a",
            },
            sub_step_attempt_allocator=allocate_sub_step_attempt,
            precompleted_tools={"ask_user_question": {"free_text": "see image"}},
        ):
            pass

        assert captured["resume_messages"] == [tool_result_message, post_answer_message]

    @pytest.mark.asyncio
    async def test_sub_pipeline_ask_user_question_restore_does_not_replay_transcript_image_user_message(
        self,
        tmp_path,
    ):
        from iac_code.pipeline.engine.context import PipelineContext
        from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec, SubPipelineSpec
        from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        step = StepSpec(
            step_id="sub_a",
            conclusion_field="sub_a_out",
            forward=None,
            prompt_file="prompts/sub_a.md",
            description="Sub A",
        )
        sub_spec = SubPipelineSpec(
            name="per_candidate",
            steps=[step],
            max_rollbacks=3,
            iterate_over="architecture.candidates",
        )
        loaded = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={},
            max_rollbacks=3,
            skills={},
            sub_pipelines={"per_candidate": sub_spec},
        )
        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            pipeline=loaded,
            pipeline_dir=tmp_path,
            session_storage=FakeSessionStorage(),
            cwd=str(tmp_path),
        )
        tool_result = ToolResultBlock(
            tool_use_id="toolu_1",
            content='{"selected_id":"","selected_label":"","free_text":"see image"}',
        )
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        tool_result_message = Message(role="user", content=[tool_result])
        image_message = Message(role="user", content=image_input)
        captured: dict[str, object] = {}

        class CapturingStepExecutor:
            async def execute(self, step, context, session_id, **kwargs):
                captured.update(kwargs)
                yield StepResult(
                    step_id=step.step_id,
                    status=StepStatus.COMPLETED,
                    conclusion={"ok": True},
                )

        executor._make_step_executor = lambda: CapturingStepExecutor()

        def allocate_sub_step_attempt(request):
            return {
                "attempt_id": "attempt_sub",
                "transcript_id": "transcript_sub",
                "resume_messages": [tool_result_message, image_message],
            }

        async for _event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "方案1"},
            candidate_index=0,
            parent_context=PipelineContext({}),
            session_id="session",
            user_message=image_input,
            resume_messages=[tool_result_message],
            parent_step_id="parallel",
            resume_state={
                "sub_pipeline_id": "sub",
                "state_machine": {
                    "current_index": 0,
                    "rollback_count": 0,
                    "interrupt_rollback_count": 0,
                    "step_statuses": {},
                },
                "context": {"fields": {}},
                "active_attempt_id": "attempt_sub",
                "transcript_id": "transcript_sub",
                "current_sub_step": "sub_a",
            },
            sub_step_attempt_allocator=allocate_sub_step_attempt,
            precompleted_tools={"ask_user_question": {"free_text": "see image"}},
        ):
            pass

        assert captured["user_message"] is None
        assert captured["resume_messages"] == [tool_result_message, image_message]

    def test_inject_pending_question_supplement_preserves_image_blocks(self, pipeline_runner):
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        injected_messages = []

        class AgentLoop:
            def try_inject_user_message(self, message):
                injected_messages.append(message)
                return True

        agent_loop = AgentLoop()
        pipeline_runner._step_executor._current_agent_loop = agent_loop

        injected = pipeline_runner.inject_pending_question_supplement(
            image_input,
            envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
        )

        assert injected is True
        assert injected_messages == [image_input]

    def test_inject_pending_question_supplement_treats_none_return_as_success(self, pipeline_runner):
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        injected_messages = []

        class AgentLoop:
            def try_inject_user_message(self, message):
                injected_messages.append(message)

        agent_loop = AgentLoop()
        pipeline_runner._step_executor._current_agent_loop = agent_loop

        injected = pipeline_runner.inject_pending_question_supplement(
            image_input,
            envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
        )

        assert injected is True
        assert injected_messages == [image_input]


class TestParallelSubPipelineUserMessagePropagation:
    """Regression: `user_input` from `_continue_from_current` must reach the
    first step of every fresh candidate when re-entering a parallel step (P-C1)."""

    @pytest.mark.asyncio
    async def test_user_message_propagates_to_fresh_candidates(self, tmp_path):
        from textwrap import dedent
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        # Minimal pipeline with one parallel sub-pipeline step.
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "a.md").write_text("do A", encoding="utf-8")
        (tmp_path / "prompts" / "sub_a.md").write_text("sub A", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              candidates_done: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              per_candidate:
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                max_rollbacks: 3
                steps:
                  - id: sub_a
                    conclusion_field: sub_a_out
                    forward: null
                    prompt: prompts/sub_a.md
                    description: Sub A
            steps:
              - id: parallel
                conclusion_field: candidates_done
                type: parallel_sub_pipeline
                sub_pipeline: per_candidate
                forward: null
                description: Parallel
        """),
            encoding="utf-8",
        )
        storage = FakeSessionStorage()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test",
            cwd=str(tmp_path),
        )
        # Pre-populate the architecture conclusion so iterate_over works.
        runner.context.set_conclusion(
            "architecture",
            {
                "candidates": [
                    {"name": "方案1"},
                    {"name": "方案2"},
                ]
            },
        )

        # Capture user_message values seen by SubPipelineExecutor.execute_streaming
        captured_user_messages: list[str | None] = []

        from iac_code.pipeline.engine import sub_pipeline_executor as spe_module

        original_execute_streaming = spe_module.SubPipelineExecutor.execute_streaming

        async def spy_execute_streaming(self, *args, **kwargs):
            captured_user_messages.append(kwargs.get("user_message"))
            # Yield a minimal completion event so the parent moves on.
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "candidate_name": "方案",
                    "total_steps": 1,
                    "sub_pipeline_name": "per_candidate",
                },
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "failed": False,
                    "conclusions": {},
                },
            )

        spe_module.SubPipelineExecutor.execute_streaming = spy_execute_streaming
        try:
            events = []
            async for ev in runner._continue_from_current(user_input="我想要更便宜的方案"):
                events.append(ev)
                if len(events) > 30:
                    break
        finally:
            spe_module.SubPipelineExecutor.execute_streaming = original_execute_streaming

        # All fresh candidates should have received the user_message
        assert captured_user_messages == ["我想要更便宜的方案", "我想要更便宜的方案"], (
            f"Expected both candidates to receive the rollback_context. Got: {captured_user_messages}"
        )

    @pytest.mark.asyncio
    async def test_restored_supplement_targets_only_matching_candidate(self, tmp_path):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "sub_a.md").write_text("sub A", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              candidates_done: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              per_candidate:
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                max_rollbacks: 3
                steps:
                  - id: sub_a
                    conclusion_field: sub_a_out
                    forward: null
                    prompt: prompts/sub_a.md
                    description: Sub A
            steps:
              - id: parallel
                conclusion_field: candidates_done
                type: parallel_sub_pipeline
                sub_pipeline: per_candidate
                forward: null
                description: Parallel
        """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test",
            cwd=str(tmp_path),
        )
        runner.context.set_conclusion(
            "architecture",
            {
                "candidates": [
                    {"name": "方案1"},
                    {"name": "方案2"},
                ]
            },
        )
        runner._restored_supplement = {
            "message": "only adjust the second candidate",
            "target": "candidate:1",
        }
        captured_user_messages: list[str | None] = []

        from iac_code.pipeline.engine import sub_pipeline_executor as spe_module

        original_execute_streaming = spe_module.SubPipelineExecutor.execute_streaming

        async def spy_execute_streaming(self, *args, **kwargs):
            captured_user_messages.append(kwargs.get("user_message"))
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "candidate_name": "方案",
                    "total_steps": 1,
                    "sub_pipeline_name": "per_candidate",
                },
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "failed": False,
                    "conclusions": {},
                },
            )

        spe_module.SubPipelineExecutor.execute_streaming = spy_execute_streaming
        try:
            events = []
            async for ev in runner._continue_from_current(user_input=None):
                events.append(ev)
                if len(events) > 30:
                    break
        finally:
            spe_module.SubPipelineExecutor.execute_streaming = original_execute_streaming

        assert captured_user_messages == [None, "only adjust the second candidate"]

    @pytest.mark.asyncio
    async def test_restored_candidate_restart_is_consumed_during_initial_parallel_scheduling(self, tmp_path):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "sub_a.md").write_text("sub A", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              candidates_done: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              per_candidate:
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                max_rollbacks: 3
                steps:
                  - id: sub_a
                    conclusion_field: sub_a_out
                    forward: null
                    prompt: prompts/sub_a.md
                    description: Sub A
            steps:
              - id: parallel
                conclusion_field: candidates_done
                type: parallel_sub_pipeline
                sub_pipeline: per_candidate
                forward: null
                description: Parallel
        """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test",
            cwd=str(tmp_path),
        )
        candidates = [{"name": "方案1"}, {"name": "方案2"}]
        runner.context.set_conclusion("architecture", {"candidates": candidates})
        runner._execution = {
            "kind": "parallel_sub_pipeline",
            "step_id": "parallel",
            "sub_pipeline_name": "per_candidate",
            "active_attempt_id": "attempt_parent",
            "transcript_id": "transcript_parent",
            "candidates": {
                "0": {"status": "running", "candidate": candidates[0], "current_sub_step": "sub_a"},
                "1": {"status": "running", "candidate": candidates[1], "current_sub_step": "sub_a"},
            },
        }
        runner._attempts["items"]["attempt_parent"] = {
            "attempt_id": "attempt_parent",
            "scope": "parent",
            "step_id": "parallel",
            "status": "running",
            "transcript_id": "transcript_parent",
        }
        runner._pending_candidate_restarts[1] = RestartInfo(
            start_from_step="sub_a",
            preserved_conclusions={},
            rollback_context="restart just second",
        )
        captured: dict[int, dict[str, object]] = {}

        from iac_code.pipeline.engine import sub_pipeline_executor as spe_module

        original_execute_streaming = spe_module.SubPipelineExecutor.execute_streaming

        async def spy_execute_streaming(self, *args, **kwargs):
            captured[kwargs["candidate_index"]] = {
                "start_from_step": kwargs.get("start_from_step"),
                "user_message": kwargs.get("user_message"),
                "resume_state": kwargs.get("resume_state"),
            }
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "candidate_name": "方案",
                    "total_steps": 1,
                    "sub_pipeline_name": "per_candidate",
                },
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "failed": False,
                    "conclusions": {},
                },
            )

        spe_module.SubPipelineExecutor.execute_streaming = spy_execute_streaming
        try:
            events = []
            async for ev in runner._continue_from_current(user_input=None):
                events.append(ev)
                if len(events) > 30:
                    break
        finally:
            spe_module.SubPipelineExecutor.execute_streaming = original_execute_streaming

        assert captured[0]["start_from_step"] is None
        assert captured[0]["user_message"] is None
        assert captured[1]["start_from_step"] == "sub_a"
        assert captured[1]["user_message"] == "restart just second"
        assert isinstance(captured[1]["resume_state"], dict)

    @pytest.mark.asyncio
    async def test_persisted_candidate_restart_is_consumed_after_process_restore(self, tmp_path):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "sub_a.md").write_text("sub A", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              candidates_done: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              per_candidate:
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                max_rollbacks: 3
                steps:
                  - id: sub_a
                    conclusion_field: sub_a_out
                    forward: null
                    prompt: prompts/sub_a.md
                    description: Sub A
            steps:
              - id: parallel
                conclusion_field: candidates_done
                type: parallel_sub_pipeline
                sub_pipeline: per_candidate
                forward: null
                description: Parallel
        """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test",
            cwd=str(tmp_path),
        )
        candidates = [{"name": "方案1"}, {"name": "方案2"}]
        runner.context.set_conclusion("architecture", {"candidates": candidates})
        runner._execution = {
            "kind": "parallel_sub_pipeline",
            "step_id": "parallel",
            "sub_pipeline_name": "per_candidate",
            "active_attempt_id": "attempt_parent",
            "transcript_id": "transcript_parent",
            "candidates": {
                "0": {"status": "running", "candidate": candidates[0], "current_sub_step": "sub_a"},
                "1": {
                    "status": "running",
                    "candidate": candidates[1],
                    "current_sub_step": "sub_a",
                    "pending_restart": {
                        "start_from_step": "sub_a",
                        "preserved_conclusions": {},
                        "rollback_context": "restored restart feedback",
                    },
                },
            },
        }
        runner._attempts["items"]["attempt_parent"] = {
            "attempt_id": "attempt_parent",
            "scope": "parent",
            "step_id": "parallel",
            "status": "running",
            "transcript_id": "transcript_parent",
        }
        captured: dict[int, dict[str, object]] = {}

        from iac_code.pipeline.engine import sub_pipeline_executor as spe_module

        original_execute_streaming = spe_module.SubPipelineExecutor.execute_streaming

        async def spy_execute_streaming(self, *args, **kwargs):
            captured[kwargs["candidate_index"]] = {
                "start_from_step": kwargs.get("start_from_step"),
                "user_message": kwargs.get("user_message"),
                "resume_state": kwargs.get("resume_state"),
            }
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "candidate_name": "方案",
                    "total_steps": 1,
                    "sub_pipeline_name": "per_candidate",
                },
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "failed": False,
                    "conclusions": {},
                },
            )

        spe_module.SubPipelineExecutor.execute_streaming = spy_execute_streaming
        try:
            events = []
            async for ev in runner._continue_from_current(user_input=None):
                events.append(ev)
                if len(events) > 30:
                    break
        finally:
            spe_module.SubPipelineExecutor.execute_streaming = original_execute_streaming

        assert captured[0]["start_from_step"] is None
        assert captured[0]["user_message"] is None
        assert captured[1]["start_from_step"] == "sub_a"
        assert captured[1]["user_message"] == "restored restart feedback"
        assert isinstance(captured[1]["resume_state"], dict)

    @pytest.mark.asyncio
    async def test_restored_candidate_restart_preserves_image_rollback_input(self, tmp_path):
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "sub_a.md").write_text("sub A", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              candidates_done: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              per_candidate:
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                max_rollbacks: 3
                steps:
                  - id: sub_a
                    conclusion_field: sub_a_out
                    forward: null
                    prompt: prompts/sub_a.md
                    description: Sub A
            steps:
              - id: parallel
                conclusion_field: candidates_done
                type: parallel_sub_pipeline
                sub_pipeline: per_candidate
                forward: null
                description: Parallel
        """),
            encoding="utf-8",
        )
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=FakeSessionStorage(),
            session_id="test",
            cwd=str(tmp_path),
        )
        image_input = [
            TextBlock(text="参考这张图"),
            ImageBlock(media_type="image/png", data="aW1hZ2U="),
        ]
        candidates = [{"name": "方案1"}]
        runner.context.set_conclusion("architecture", {"candidates": candidates})
        runner._execution = {
            "kind": "parallel_sub_pipeline",
            "step_id": "parallel",
            "sub_pipeline_name": "per_candidate",
            "active_attempt_id": "attempt_parent",
            "transcript_id": "transcript_parent",
            "candidates": {
                "0": {
                    "status": "running",
                    "candidate": candidates[0],
                    "current_sub_step": "sub_a",
                    "pending_restart": {
                        "start_from_step": "sub_a",
                        "preserved_conclusions": {},
                        "rollback_context": "用户要求改架构\n\n参考这张图",
                        "rollback_input": [
                            {"type": "text", "text": "用户要求改架构"},
                            {"type": "text", "text": "参考这张图"},
                            {"type": "image", "media_type": "image/png", "data": "aW1hZ2U="},
                        ],
                    },
                },
            },
        }
        runner._attempts["items"]["attempt_parent"] = {
            "attempt_id": "attempt_parent",
            "scope": "parent",
            "step_id": "parallel",
            "status": "running",
            "transcript_id": "transcript_parent",
        }
        captured: dict[int, dict[str, object]] = {}

        from iac_code.pipeline.engine import sub_pipeline_executor as spe_module

        original_execute_streaming = spe_module.SubPipelineExecutor.execute_streaming

        async def spy_execute_streaming(self, *args, **kwargs):
            captured[kwargs["candidate_index"]] = {"user_message": kwargs.get("user_message")}
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "candidate_name": "方案",
                    "total_steps": 1,
                    "sub_pipeline_name": "per_candidate",
                },
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0.0,
                data={
                    "sub_pipeline_id": "x",
                    "candidate_index": kwargs.get("candidate_index", 0),
                    "failed": False,
                    "conclusions": {},
                },
            )

        spe_module.SubPipelineExecutor.execute_streaming = spy_execute_streaming
        try:
            async for _ev in runner._continue_from_current(user_input=None):
                if captured:
                    break
        finally:
            spe_module.SubPipelineExecutor.execute_streaming = original_execute_streaming

        assert captured[0]["user_message"] == [TextBlock(text="用户要求改架构"), *image_input]


class TestSupplementTargetUnifiedFormat:
    """P-I5: _inject_supplement should accept both 'candidate:N' (new unified)
    and 'candidate_index:N' (legacy, in-flight sessions)."""

    @pytest.mark.parametrize(
        "target_str,expected_idx",
        [
            ("candidate:0", 0),
            ("candidate:2", 2),
            ("candidate_index:0", 0),  # legacy
            ("candidate_index:1", 1),  # legacy
        ],
    )
    def test_supplement_target_both_formats_resolve_same_candidate(self, target_str, expected_idx):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.interrupt import InterruptVerdict
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner._step_executor = MagicMock(current_agent_loop=None)
        runner._active_candidates = {
            0: {"agent_loop": MagicMock(inject_user_message=MagicMock())},
            1: {"agent_loop": MagicMock(inject_user_message=MagicMock())},
            2: {"agent_loop": MagicMock(inject_user_message=MagicMock())},
        }
        verdict = InterruptVerdict(
            action="supplement",
            reason="extra info",
            supplement_target=target_str,
        )
        injected = runner._inject_supplement(verdict, "extra hint")
        assert injected is True
        runner._active_candidates[expected_idx]["agent_loop"].inject_user_message.assert_called_once_with("extra hint")
        # Other candidates not touched.
        for other_idx in {0, 1, 2} - {expected_idx}:
            runner._active_candidates[other_idx]["agent_loop"].inject_user_message.assert_not_called()


class TestParallelSupplementBroadcast:
    """P-I19: when in parallel_sub_pipeline and supplement_target is None,
    broadcast to all candidate agent loops instead of silently dropping."""

    def test_supplement_target_none_in_parallel_broadcasts(self):
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.interrupt import InterruptVerdict
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner._step_executor = MagicMock(current_agent_loop=None)
        parallel_step = MagicMock(step_type="parallel_sub_pipeline")
        runner.state_machine = MagicMock()
        runner.state_machine.current_step = parallel_step
        runner._active_candidates = {
            0: {"agent_loop": MagicMock(inject_user_message=MagicMock())},
            1: {"agent_loop": MagicMock(inject_user_message=MagicMock())},
        }

        verdict = InterruptVerdict(action="supplement", reason="x", supplement_target=None)
        result = runner._inject_supplement(verdict, "switch to python")

        assert result is True
        runner._active_candidates[0]["agent_loop"].inject_user_message.assert_called_once_with("switch to python")
        runner._active_candidates[1]["agent_loop"].inject_user_message.assert_called_once_with("switch to python")

    def test_supplement_target_none_in_non_parallel_uses_current_loop(self):
        """Existing behavior preserved when current step is NOT parallel."""
        from unittest.mock import MagicMock

        from iac_code.pipeline.engine.interrupt import InterruptVerdict
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        current_loop = MagicMock(inject_user_message=MagicMock())
        runner._step_executor = MagicMock(current_agent_loop=current_loop)
        normal_step = MagicMock(step_type="agent_loop")
        runner.state_machine = MagicMock()
        runner.state_machine.current_step = normal_step
        runner._active_candidates = {}

        verdict = InterruptVerdict(action="supplement", reason="x", supplement_target=None)
        result = runner._inject_supplement(verdict, "tweak this")
        assert result is True
        current_loop.inject_user_message.assert_called_once_with("tweak this")


async def _empty_stream():
    return
    yield  # noqa: B901
