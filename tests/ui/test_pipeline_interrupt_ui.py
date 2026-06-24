"""Tests for pipeline interrupt UI integration (Esc → judge → action)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.text import Text


@pytest.fixture
def mock_repl():
    """Create a minimal InlineREPL with mocked dependencies for testing interrupt flow."""
    with (
        patch("iac_code.ui.repl.ProviderManager"),
        patch("iac_code.ui.repl.SessionStorage"),
        patch("iac_code.ui.repl.MemoryManager"),
    ):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="test-model")
        repl._pipeline = MagicMock()
        return repl


class TestHandleMidPipelineMessage:
    @pytest.mark.asyncio
    async def test_continue_verdict_returns_false(self, mock_repl):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        verdict = InterruptVerdict(action="continue", reason="not relevant")
        mock_repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)

        needs_restart, feedback = await mock_repl._handle_mid_pipeline_message("hello")
        assert needs_restart is False
        assert feedback

    @pytest.mark.asyncio
    async def test_paused_continue_verdict_saves_waiting_input(self, mock_repl):
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        verdict = InterruptVerdict(
            action="continue",
            reason="judge failed while executing side-effect step 'deploying'; pipeline paused for safety.",
            paused=True,
        )
        pause_event = PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id="deploying",
            timestamp=0,
            data={"kind": "pipeline_pause_confirmation", "options": []},
        )
        mock_repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)
        mock_repl._pipeline.save_interrupt_pause = AsyncMock(return_value=pause_event)
        mock_repl._pipeline_waiting_input = False

        needs_restart, feedback = await mock_repl._handle_mid_pipeline_message("等等", suppress_render=True)

        assert needs_restart is False
        assert "the pipeline was paused" in feedback
        assert mock_repl._pipeline_waiting_input is True
        assert mock_repl._last_interrupt_paused is True
        mock_repl._pipeline.save_interrupt_pause.assert_awaited_once_with(verdict)

    @pytest.mark.asyncio
    async def test_supplement_verdict_returns_false(self, mock_repl):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        verdict = InterruptVerdict(action="supplement", reason="extra info")
        mock_repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)

        needs_restart, feedback = await mock_repl._handle_mid_pipeline_message("补充一下")
        assert needs_restart is False
        assert feedback

    @pytest.mark.asyncio
    async def test_hard_interrupt_parent_returns_true(self, mock_repl):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="user wants cheaper",
            rollback_target="intent_parsing",
        )
        mock_repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)
        mock_repl._pipeline.apply_hard_interrupt = MagicMock(return_value=True)
        mock_repl._pipeline.state_machine = MagicMock()

        needs_restart, feedback = await mock_repl._handle_mid_pipeline_message("我想换个便宜的")
        assert needs_restart is True
        assert feedback
        assert "Intent parsing" in feedback
        assert "intent_parsing" not in feedback
        mock_repl._pipeline.apply_hard_interrupt.assert_called_once_with(verdict)

    @pytest.mark.asyncio
    async def test_hard_interrupt_feedback_uses_applied_verdict(self, mock_repl):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="user requested stale target",
            rollback_target="deleted_step",
        )
        applied_verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="invalid rollback target; falling back to current step: user requested stale target",
            rollback_target="current_step",
        )
        mock_repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)
        mock_repl._pipeline.apply_hard_interrupt = MagicMock(return_value=True)
        mock_repl._pipeline.last_applied_interrupt_verdict = applied_verdict

        needs_restart, feedback = await mock_repl._handle_mid_pipeline_message("换目标", suppress_render=True)

        assert needs_restart is True
        assert "Current step" in feedback
        assert "current_step" not in feedback
        assert "deleted_step" not in feedback
        assert "falling back" in feedback
        mock_repl._pipeline.apply_hard_interrupt.assert_called_once_with(verdict)

    @pytest.mark.asyncio
    async def test_hard_interrupt_candidate_returns_false(self, mock_repl):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        verdict = InterruptVerdict(
            action="hard_interrupt",
            reason="fix template",
            rollback_target="template_generating",
            candidate_scope="candidate:0",
        )
        mock_repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)
        mock_repl._pipeline.apply_hard_interrupt = MagicMock(return_value=False)

        needs_restart, feedback = await mock_repl._handle_mid_pipeline_message("模板有问题")
        assert needs_restart is False
        assert feedback
        assert "Template generation" in feedback
        assert "template_generating" not in feedback
        mock_repl._pipeline.apply_hard_interrupt.assert_called_once_with(verdict)

    @pytest.mark.asyncio
    async def test_no_pipeline_returns_false(self, mock_repl):
        mock_repl._pipeline = None
        needs_restart, feedback = await mock_repl._handle_mid_pipeline_message("test")
        assert needs_restart is False
        assert feedback == ""


class TestCtrlCClearsPipeline:
    """Ctrl+C tears down the in-memory pipeline and marks the sidecar aborted."""

    @pytest.mark.asyncio
    async def test_ctrl_c_clears_pipeline_and_waiting_flag(self, mock_repl):
        mock_repl._pipeline_waiting_input = True
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = False
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        pipeline.clear_sidecar = MagicMock()
        pipeline.mark_user_aborted = MagicMock()
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()
        mock_repl.console = MagicMock()
        mock_repl._render_pipeline_stream = AsyncMock(side_effect=asyncio.CancelledError)

        # CancelledError must propagate (asyncio contract) — the run() loop
        # handles it. The finally still tears the pipeline down.
        with pytest.raises(asyncio.CancelledError):
            await mock_repl._handle_pipeline_chat("hello")
        assert mock_repl._pipeline is None
        # apply_hard_interrupt is NOT used on Ctrl+C anymore — cleanup is purely via aclose().
        pipeline.apply_hard_interrupt.assert_not_called()
        pipeline.mark_user_aborted.assert_called_once()
        pipeline.clear_sidecar.assert_not_called()

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_also_clears(self, mock_repl):
        # Covers the no-signal-handler path (e.g. Windows) where SIGINT surfaces
        # as a raw KeyboardInterrupt from the stream body rather than as a
        # task-level CancelledError. It must propagate too, with the same teardown.
        mock_repl._pipeline_waiting_input = True
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = False
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        pipeline.clear_sidecar = MagicMock()
        pipeline.mark_user_aborted = MagicMock()
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()
        mock_repl.console = MagicMock()
        mock_repl._render_pipeline_stream = AsyncMock(side_effect=KeyboardInterrupt)

        with pytest.raises(KeyboardInterrupt):
            await mock_repl._handle_pipeline_chat("hello")
        assert mock_repl._pipeline is None
        pipeline.mark_user_aborted.assert_called_once()
        pipeline.clear_sidecar.assert_not_called()

    @staticmethod
    async def _fake_stream():
        return
        yield  # noqa: B901


class TestPipelineCompletionClearsWaitingFlag:
    """After PIPELINE_COMPLETED, _pipeline_waiting_input must be cleared even if
    candidate-selection left it stuck True (pre-existing C1 bug)."""

    @pytest.mark.asyncio
    async def test_completion_clears_waiting_flag(self, mock_repl):
        mock_repl._pipeline_waiting_input = True  # set by candidate-selection USER_INPUT_REQUIRED
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = True
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()
        mock_repl._render_pipeline_stream = AsyncMock()

        await mock_repl._handle_pipeline_chat("hello")
        assert mock_repl._pipeline is None
        assert mock_repl._pipeline_waiting_input is False

    @pytest.mark.asyncio
    async def test_incomplete_pipeline_with_waiting_input_kept_alive(self, mock_repl):
        """USER_INPUT_REQUIRED mid-flight → flag re-set True → pipeline preserved."""
        mock_repl._pipeline_waiting_input = True
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = False
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()

        async def fake_render(_stream):
            # Simulates a step yielding USER_INPUT_REQUIRED partway through.
            mock_repl._pipeline_waiting_input = True

        mock_repl._render_pipeline_stream = AsyncMock(side_effect=fake_render)

        await mock_repl._handle_pipeline_chat("hello")
        assert mock_repl._pipeline is not None
        assert mock_repl._pipeline_waiting_input is True

    @staticmethod
    async def _fake_stream():
        return
        yield  # noqa: B901


class TestSidecarCleanupOnTerminalState:
    """Terminal completion preserves sidecars; user aborts persist as user_aborted."""

    @pytest.mark.asyncio
    async def test_completion_preserves_sidecar_and_clears_in_memory_state(self, mock_repl):
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = True
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        pipeline.clear_sidecar = MagicMock()
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()
        mock_repl._render_pipeline_stream = AsyncMock()

        await mock_repl._handle_pipeline_chat("hello")
        pipeline.clear_sidecar.assert_not_called()
        assert mock_repl._pipeline is None
        assert mock_repl._pipeline_waiting_input is False

    @pytest.mark.asyncio
    async def test_failed_sidecar_tears_down_without_user_abort(self, mock_repl):
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = False
        pipeline.sidecar_status = "failed"
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        pipeline.clear_sidecar = MagicMock()
        pipeline.mark_user_aborted = MagicMock()
        mock_repl._pipeline_waiting_input = False
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()
        mock_repl._render_pipeline_stream = AsyncMock()

        await mock_repl._handle_pipeline_chat("hello")

        pipeline.mark_user_aborted.assert_not_called()
        pipeline.clear_sidecar.assert_not_called()
        assert mock_repl._pipeline is None
        assert mock_repl._pipeline_waiting_input is False

    @pytest.mark.asyncio
    async def test_ctrl_c_clears_sidecar(self, mock_repl):
        """Ctrl+C aborts mid-pipeline → sidecar is marked user_aborted."""
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = False
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        pipeline.clear_sidecar = MagicMock()
        pipeline.mark_user_aborted = MagicMock()
        mock_repl._pipeline_waiting_input = False
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()
        mock_repl.console = MagicMock()
        mock_repl._render_pipeline_stream = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await mock_repl._handle_pipeline_chat("hello")
        pipeline.mark_user_aborted.assert_called_once()
        pipeline.clear_sidecar.assert_not_called()
        assert mock_repl._pipeline is None

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_clears_sidecar(self, mock_repl):
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = False
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        pipeline.clear_sidecar = MagicMock()
        pipeline.mark_user_aborted = MagicMock()
        mock_repl._pipeline_waiting_input = False
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()
        mock_repl.console = MagicMock()
        mock_repl._render_pipeline_stream = AsyncMock(side_effect=KeyboardInterrupt)

        with pytest.raises(KeyboardInterrupt):
            await mock_repl._handle_pipeline_chat("hello")
        pipeline.mark_user_aborted.assert_called_once()
        pipeline.clear_sidecar.assert_not_called()
        assert mock_repl._pipeline is None

    @pytest.mark.asyncio
    async def test_pipeline_paused_for_user_input_keeps_sidecar(self, mock_repl):
        """confirm_and_select pauses with USER_INPUT_REQUIRED → pipeline alive,
        sidecar preserved. clear_sidecar must NOT be called."""
        pipeline = mock_repl._pipeline
        pipeline.state_machine = MagicMock()
        pipeline.state_machine.is_complete = False
        pipeline.resume = MagicMock(return_value=self._fake_stream())
        pipeline.clear_sidecar = MagicMock()
        pipeline.mark_user_aborted = MagicMock()
        mock_repl._pipeline_waiting_input = True
        mock_repl.renderer = MagicMock()
        mock_repl.renderer.record_user_turn = MagicMock()
        mock_repl.store = MagicMock()
        mock_repl.store.set_state = MagicMock()

        async def fake_render(_stream):
            mock_repl._pipeline_waiting_input = True

        mock_repl._render_pipeline_stream = AsyncMock(side_effect=fake_render)

        await mock_repl._handle_pipeline_chat("hello")
        pipeline.clear_sidecar.assert_not_called()
        pipeline.mark_user_aborted.assert_not_called()
        assert mock_repl._pipeline is not None

    @staticmethod
    async def _fake_stream():
        return
        yield  # noqa: B901


class TestEventsBetweenSteps:
    """Regression: stream events between STEP_COMPLETED and next STEP_STARTED
    must reach the renderer, not be silently dropped (U-C2)."""

    @pytest.mark.asyncio
    async def test_message_end_event_after_step_completed_reaches_renderer(self, mock_repl):
        import time

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.types.stream_events import (
            MessageEndEvent,
            MessageStartEvent,
            TextDeltaEvent,
            ToolResultEvent,
            Usage,
        )

        # Spy: when _render_pipeline_stream creates a renderer task, the
        # task awaits run_streaming_output(events_iter, ...). We replace
        # run_streaming_output with one that drains events_iter into a
        # captured list, so we can inspect exactly which events the
        # renderer was asked to render.
        captured: list = []

        async def fake_run_streaming_output(events_iter, **kwargs):
            async for ev in events_iter:
                captured.append(ev)

        mock_repl.renderer = MagicMock()
        mock_repl.renderer.run_streaming_output = fake_run_streaming_output
        mock_repl.renderer.prompt_permission = None
        # Stubs for self.* dependencies _render_pipeline_stream touches
        mock_repl._build_progress_bar = MagicMock(return_value="progress")
        mock_repl._render_pipeline_event = MagicMock()
        mock_repl._pipeline_step_names = []
        mock_repl._pipeline_completed_indices = set()
        # Reset _pipeline so pause/resume are no-ops in this test
        mock_repl._pipeline.pause_agent_loops = MagicMock()
        mock_repl._pipeline.resume_agent_loops = MagicMock()

        terminal_event = PipelineEvent(
            type=PipelineEventType.PIPELINE_COMPLETED,
            step_id=None,
            timestamp=time.time(),
            data={"total_steps": 2},
        )

        async def stream():
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_STARTED,
                step_id=None,
                timestamp=time.time(),
                data={"steps": [{"step_id": "a"}, {"step_id": "b"}]},
            )
            yield PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="a",
                timestamp=time.time(),
                data={"index": 1, "total": 2, "name": "a", "step_type": "agent_loop", "ui_mode": "default"},
            )
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="hello")
            # STEP_COMPLETED — under the bug, renderer was torn down here.
            yield PipelineEvent(
                type=PipelineEventType.STEP_COMPLETED,
                step_id="a",
                timestamp=time.time(),
                data={"duration_s": 0.1},
            )
            # These events legitimately follow STEP_COMPLETED. The bug
            # silently drops them; the fix lets them reach the renderer.
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())
            yield ToolResultEvent(tool_use_id="t1", tool_name="read", result="ok")
            # Next STEP_STARTED — at this point the old renderer should
            # have already received the trailing events, and a new renderer
            # spawns for step b.
            yield PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="b",
                timestamp=time.time(),
                data={"index": 2, "total": 2, "name": "b", "step_type": "agent_loop", "ui_mode": "default"},
            )
            yield terminal_event

        result = await mock_repl._render_pipeline_stream(stream())

        # Assertion: the events emitted between STEP_COMPLETED and the next
        # STEP_STARTED must have been delivered to the renderer.
        kinds = [type(e).__name__ for e in captured]
        assert "MessageEndEvent" in kinds, f"MessageEndEvent dropped between steps! captured={kinds}"
        assert "ToolResultEvent" in kinds, f"ToolResultEvent dropped between steps! captured={kinds}"
        assert result is terminal_event


class TestPipelineAskUserQuestion:
    """ask_user_question must be handled by the pipeline stream owner.

    If the renderer task owns this prompt, Ctrl+C can be swallowed while the
    AgentLoop remains blocked on the tool response future.
    """

    @pytest.mark.asyncio
    async def test_pipeline_stream_resolves_ask_user_question_future(self, mock_repl):
        import time

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.types.stream_events import AskUserQuestionEvent

        async def fake_run_streaming_output(events_iter, **kwargs):
            async for _event in events_iter:
                pass

        mock_repl.renderer = MagicMock()
        mock_repl.renderer.run_streaming_output = fake_run_streaming_output
        mock_repl.renderer.prompt_permission = None
        answer = {"selected_id": "deploy_to_aliyun", "selected_label": "部署到阿里云", "free_text": ""}
        mock_repl.renderer.prompt_user_question = AsyncMock(return_value=answer)
        mock_repl._build_progress_bar = MagicMock(return_value="progress")
        mock_repl._render_pipeline_event = MagicMock()
        mock_repl._update_pipeline_state_from_event = MagicMock()
        mock_repl._pipeline_step_names = []
        mock_repl._pipeline_completed_indices = set()
        mock_repl._pipeline.pause_agent_loops = MagicMock()
        mock_repl._pipeline.resume_agent_loops = MagicMock()

        fut: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
        question = AskUserQuestionEvent(
            tool_use_id="tu_1",
            question="确认一下",
            options=[{"id": "deploy_to_aliyun", "label": "部署到阿里云"}],
            response_future=fut,
        )
        terminal_event = PipelineEvent(
            type=PipelineEventType.PIPELINE_COMPLETED,
            step_id=None,
            timestamp=time.time(),
            data={"total_steps": 1},
        )

        async def stream():
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_STARTED,
                step_id=None,
                timestamp=time.time(),
                data={"steps": [{"step_id": "intent"}]},
            )
            yield PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="intent",
                timestamp=time.time(),
                data={"index": 1, "total": 1, "name": "intent", "step_type": "agent_loop", "ui_mode": "default"},
            )
            yield question
            assert fut.done()
            yield terminal_event

        result = await mock_repl._render_pipeline_stream(stream())

        assert fut.result() == answer
        assert result is terminal_event
        mock_repl.renderer.prompt_user_question.assert_awaited_once_with(question)

    @pytest.mark.asyncio
    async def test_resumed_pipeline_stream_without_pipeline_started_initializes_progress_state(self, mock_repl):
        import time

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType

        async def fake_run_streaming_output(events_iter, **kwargs):
            async for _event in events_iter:
                pass

        mock_repl.renderer = MagicMock()
        mock_repl.renderer.run_streaming_output = fake_run_streaming_output
        mock_repl.renderer.prompt_permission = None
        mock_repl._build_progress_bar = MagicMock(return_value="progress")
        mock_repl._pipeline.pause_agent_loops = MagicMock()
        mock_repl._pipeline.resume_agent_loops = MagicMock()
        terminal_event = PipelineEvent(
            type=PipelineEventType.PIPELINE_COMPLETED,
            step_id=None,
            timestamp=time.time(),
            data={"total_steps": 3},
        )

        async def stream():
            yield PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="deploying",
                timestamp=time.time(),
                data={"index": 3, "total": 3, "name": "deploying", "step_type": "agent_loop", "ui_mode": "default"},
            )
            yield terminal_event

        result = await mock_repl._render_pipeline_stream(stream())

        assert result is terminal_event
        assert mock_repl._pipeline_step_names == []
        assert mock_repl._pipeline_completed_indices == set()

    @pytest.mark.asyncio
    async def test_pipeline_stream_ctrl_c_during_ask_user_question_propagates(self, mock_repl):
        import time

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.types.stream_events import AskUserQuestionEvent

        async def fake_run_streaming_output(events_iter, **kwargs):
            async for _event in events_iter:
                pass

        mock_repl.renderer = MagicMock()
        mock_repl.renderer.run_streaming_output = fake_run_streaming_output
        mock_repl.renderer.prompt_permission = None
        mock_repl.renderer.prompt_user_question = AsyncMock(side_effect=KeyboardInterrupt)
        mock_repl._build_progress_bar = MagicMock(return_value="progress")
        mock_repl._render_pipeline_event = MagicMock()
        mock_repl._update_pipeline_state_from_event = MagicMock()
        mock_repl._pipeline_step_names = []
        mock_repl._pipeline_completed_indices = set()
        mock_repl._pipeline.pause_agent_loops = MagicMock()
        mock_repl._pipeline.resume_agent_loops = MagicMock()

        fut: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
        question = AskUserQuestionEvent(
            tool_use_id="tu_1",
            question="确认一下",
            options=[{"id": "deploy_to_aliyun", "label": "部署到阿里云"}],
            response_future=fut,
        )

        async def stream():
            yield PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="intent",
                timestamp=time.time(),
                data={"index": 1, "total": 1, "name": "intent", "step_type": "agent_loop", "ui_mode": "default"},
            )
            yield question

        with pytest.raises(KeyboardInterrupt):
            await mock_repl._render_pipeline_stream(stream())

        assert fut.done()
        assert fut.result() is None


class TestAmbiguousFeedback:
    """P-I18: REPL must surface a yellow warning when judge defaults to continue
    due to ambiguous input (reason starts with [ambiguous])."""

    @pytest.mark.asyncio
    async def test_ambiguous_continue_emits_yellow_warning(self, mock_repl):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        verdict = InterruptVerdict(
            action="continue",
            reason="[ambiguous] 用户输入不清晰，按闲聊处理",
        )
        mock_repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)
        mock_repl.renderer = MagicMock()

        needs_restart, feedback = await mock_repl._handle_mid_pipeline_message("嗯")

        assert needs_restart is False
        mock_repl.renderer.print_system_message.assert_called_once()
        call = mock_repl.renderer.print_system_message.call_args
        msg = call.args[0] if call.args else call.kwargs.get("msg") or call.kwargs.get("message")
        assert "wasn't clearly understood" in msg.lower() or "未被准确理解" in msg
        assert call.kwargs.get("style") == "yellow"

    @pytest.mark.asyncio
    async def test_normal_continue_does_not_emit_warning(self, mock_repl):
        from iac_code.pipeline.engine.interrupt import InterruptVerdict

        verdict = InterruptVerdict(
            action="continue",
            reason="user is just chatting, pipeline should continue",
        )
        mock_repl._pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)
        mock_repl.renderer = MagicMock()

        await mock_repl._handle_mid_pipeline_message("hello")
        mock_repl.renderer.print_system_message.assert_not_called()


class TestRenderDoesNotMutateState:
    """U-I16: _render_pipeline_event must be pure — must not mutate instance state.
    State updates should happen in a separate _update_pipeline_state_from_event step."""

    def test_render_pipeline_event_does_not_mutate_pipeline_step_names(self, mock_repl):
        import time

        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType

        mock_repl._pipeline_step_names = ["a", "b", "c"]
        # Minimal renderer + console mocks
        mock_repl.renderer = MagicMock()
        mock_repl.console = MagicMock()

        event = PipelineEvent(
            type=PipelineEventType.PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={"step_names": ["x", "y"]},
        )

        # Call only the render path — not the dispatcher.
        # If _render_pipeline_event needs more setup, mock minimally.
        try:
            mock_repl._render_pipeline_event(event)
        except Exception:
            pass  # rendering might fail with mocks, but we only care about state mutation

        # Render must NOT have changed instance state.
        assert mock_repl._pipeline_step_names == ["a", "b", "c"], (
            "_render_pipeline_event must not mutate self._pipeline_step_names; "
            "use _update_pipeline_state_from_event instead"
        )


class TestPipelineEventStyles:
    def test_render_pipeline_warning_prints_non_terminal_warning(self, mock_repl):
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType

        printed = []

        class CaptureConsole:
            def print(self, *args, **kwargs):
                if args:
                    printed.extend(str(arg) for arg in args)
                else:
                    printed.append("")

        mock_repl.renderer = SimpleNamespace(console=CaptureConsole())

        mock_repl._render_pipeline_event(
            PipelineEvent(
                type=PipelineEventType.PIPELINE_WARNING,
                step_id="deploying",
                timestamp=0,
                data={"reason": "cleanup_tracking_unavailable"},
            )
        )

        rendered = "\n".join(printed)
        assert "cleanup_tracking_unavailable" in rendered
        assert "yellow" in rendered

    def test_render_pipeline_event_uses_slate_sky_label_styles(self, mock_repl):
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.ui.pipeline_styles import PIPELINE_STEP_HEADER_STYLE, PIPELINE_TITLE_STYLE

        printed = []

        class CaptureConsole:
            def print(self, *args, **kwargs):
                if args:
                    printed.extend(args)
                else:
                    printed.append(None)

        mock_repl.renderer = SimpleNamespace(console=CaptureConsole())
        mock_repl._pipeline_step_names = ["intent_parsing"]

        mock_repl._render_pipeline_event(
            PipelineEvent(
                type=PipelineEventType.PIPELINE_STARTED,
                step_id=None,
                timestamp=0,
                data={"pipeline_type": "selling"},
            )
        )
        mock_repl._render_pipeline_event(
            PipelineEvent(
                type=PipelineEventType.STEP_STARTED,
                step_id="intent_parsing",
                timestamp=0,
                data={"index": 1, "total": 5},
            )
        )

        text_items = [item for item in printed if isinstance(item, Text)]
        title = next(item for item in text_items if item.plain == " AI Selling Pipeline ")
        step = next(item for item in text_items if item.plain == "● Intent parsing (1/5) ")
        assert title.spans[0].style == PIPELINE_TITLE_STYLE
        assert step.spans[0].style == PIPELINE_STEP_HEADER_STYLE
