"""Pipeline terminal event handoff behavior in InlineREPL."""

from __future__ import annotations

import asyncio
import time
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from rich.console import Console

from iac_code.agent.message import Message
from iac_code.pipeline.config import RunMode
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType


async def _empty_stream():
    return
    yield  # noqa: B901


def _pipeline_completed_event(**data) -> PipelineEvent:
    return PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=time.time(),
        data={"total_steps": 2, **data},
    )


def _make_repl_for_handoff(
    terminal_event: PipelineEvent,
    *,
    should_switch_to_normal: bool,
    summary: str = "handoff summary",
    is_complete: bool = True,
    sidecar_status: str | None = None,
):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.PIPELINE
    repl._pipeline_waiting_input = False
    repl._original_cwd = "/workspace"
    repl._session_id = "session-1"
    repl.store = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.record_user_turn = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl._render_pipeline_stream = AsyncMock(return_value=terminal_event)
    repl.current_git_branch = MagicMock(return_value="main")
    repl._session_storage = MagicMock()

    injected = Message(role="user", content=summary)
    repl._agent_loop = MagicMock()
    repl._agent_loop.context_manager = MagicMock()
    repl._agent_loop.context_manager.add_raw_message = MagicMock(return_value=injected)

    pipeline = MagicMock()
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.clear_sidecar = MagicMock()
    pipeline.mark_normal_handoff = MagicMock()
    pipeline.mark_user_aborted = MagicMock()
    pipeline.sidecar_status = sidecar_status
    pipeline.state_machine = MagicMock()
    pipeline.state_machine.is_complete = is_complete
    pipeline.should_switch_to_normal = MagicMock(return_value=should_switch_to_normal)
    pipeline.build_normal_handoff_summary = MagicMock(return_value=summary)
    repl._pipeline = pipeline
    return repl, pipeline, injected


def test_pipeline_runner_mark_normal_handoff_saves_terminal_metadata(tmp_path: Path):
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
    from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession

    runner = PipelineRunner.__new__(PipelineRunner)
    runner.session = PipelineSession(tmp_path / "session" / "pipeline")
    runner._sidecar_status = None
    runner._session_id = "root-session"
    runner._pipeline_identity = PipelineIdentity(
        pipeline_name="support",
        step_ids=["collect"],
        sub_pipeline_step_ids={},
        pipeline_fingerprint="fingerprint",
    )
    runner._execution = {"kind": "step", "active_attempt_id": "att_0001"}
    runner._attempts = {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}
    runner._observability = MagicMock()

    runner.state_machine = MagicMock()
    runner.state_machine.is_complete = True
    runner.state_machine._order = ["collect"]
    runner.state_machine.to_snapshot.return_value = {
        "current_index": 1,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"collect": "completed"},
    }
    runner.context = MagicMock()
    runner.context.to_snapshot.return_value = {"collected": {"value": "yes"}}

    runner.mark_normal_handoff(status="succeeded", failed_reason=None)

    meta = yaml.safe_load(runner.session.meta_path.read_text(encoding="utf-8"))
    context = yaml.safe_load(runner.session.context_path.read_text(encoding="utf-8"))
    assert runner.session.session_dir.exists()
    assert meta["status"] == "completed"
    assert meta["resume_policy"] == "none"
    assert meta["terminal"] is True
    assert meta["current_step"] == "collect"
    assert meta["execution"] == {"kind": "step", "active_attempt_id": "att_0001"}
    assert meta["attempts"] == {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}
    assert meta["normal_handoff"] == {
        "status": "succeeded",
        "switched_to_normal": True,
        "root_session_id": "root-session",
        "summary_message_appended": True,
        "failed_reason": None,
    }
    assert context == {"collected": {"value": "yes"}}


def test_pipeline_runner_mark_normal_handoff_preserves_existing_execution_when_empty(tmp_path: Path):
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
    from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession

    identity = PipelineIdentity(
        pipeline_name="support",
        step_ids=["collect"],
        sub_pipeline_step_ids={},
        pipeline_fingerprint="fingerprint",
    )
    session = PipelineSession(tmp_path / "session" / "pipeline")
    existing_execution = {"kind": "step", "step_id": "collect", "active_attempt_id": "att_0001"}
    session.save_running_sync(
        "collect",
        {
            "current_index": 0,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {"collect": "running"},
        },
        {"collected": {"value": "in-progress"}},
        identity,
        reason="step started",
        execution=existing_execution,
        attempts={"next_attempt_number": 2, "items": {"att_0001": {"status": "running"}}},
    )

    runner = PipelineRunner.__new__(PipelineRunner)
    runner.session = session
    runner._sidecar_status = None
    runner._session_id = "root-session"
    runner._pipeline_identity = identity
    runner._execution = {}
    runner._attempts = {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}
    runner._observability = MagicMock()

    runner.state_machine = MagicMock()
    runner.state_machine.is_complete = True
    runner.state_machine._order = ["collect"]
    runner.state_machine.to_snapshot.return_value = {
        "current_index": 1,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"collect": "completed"},
    }
    runner.context = MagicMock()
    runner.context.to_snapshot.return_value = {"collected": {"value": "yes"}}

    runner.mark_normal_handoff(status="succeeded", failed_reason=None)

    meta = yaml.safe_load(runner.session.meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["execution"] == existing_execution
    assert meta["attempts"] == {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}
    assert meta["normal_handoff"]["status"] == "succeeded"


def test_pipeline_runner_mark_user_aborted_marks_active_attempt_failed(tmp_path: Path):
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
    from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession

    runner = PipelineRunner.__new__(PipelineRunner)
    runner.session = PipelineSession(tmp_path / "session" / "pipeline")
    runner._sidecar_status = None
    runner._pipeline_identity = PipelineIdentity(
        pipeline_name="support",
        step_ids=["collect"],
        sub_pipeline_step_ids={},
        pipeline_fingerprint="fingerprint",
    )
    runner._execution = {"kind": "step", "step_id": "collect", "active_attempt_id": "att_0001"}
    runner._attempts = {
        "next_attempt_number": 2,
        "items": {"att_0001": {"attempt_id": "att_0001", "step_id": "collect", "status": "running"}},
    }

    runner.state_machine = MagicMock()
    runner.state_machine.is_complete = False
    runner.state_machine.current_step.step_id = "collect"
    runner.state_machine.to_snapshot.return_value = {
        "current_index": 0,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"collect": "pending"},
    }
    runner.context = MagicMock()
    runner.context.to_snapshot.return_value = {"collected": {"value": None}}

    runner.mark_user_aborted("ctrl-c")

    meta = yaml.safe_load(runner.session.meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "user_aborted"
    assert meta["terminal"] is True
    assert meta["execution"] == {"kind": "step", "step_id": "collect", "active_attempt_id": "att_0001"}
    assert meta["attempts"]["items"]["att_0001"]["status"] == "failed"


@pytest.mark.asyncio
async def test_user_aborted_pipeline_switches_to_normal_and_persists_notice():
    from iac_code.ui.repl import InlineREPL

    terminal_event = _pipeline_completed_event()
    repl, pipeline, _injected = _make_repl_for_handoff(
        terminal_event,
        should_switch_to_normal=False,
        is_complete=False,
    )
    root_message = Message(role="user", content="start")
    notice_message = Message(role="assistant", content=InlineREPL._pipeline_abort_notice_text())
    repl._render_pipeline_stream = AsyncMock(side_effect=asyncio.CancelledError)
    repl._session_storage.load.return_value = [root_message]
    repl._session_storage.repair_interrupted.side_effect = lambda messages: messages
    repl._agent_loop.replace_session = MagicMock()
    repl._agent_loop.context_manager.add_raw_message.return_value = notice_message

    with pytest.raises(asyncio.CancelledError):
        await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    pipeline.mark_user_aborted.assert_called_once_with("pipeline interrupted by user or renderer cancellation")
    repl._agent_loop.replace_session.assert_called_once_with("session-1", [root_message])
    repl._agent_loop.context_manager.add_raw_message.assert_called_once_with(
        {"role": "assistant", "content": InlineREPL._pipeline_abort_notice_text()}
    )
    repl.renderer.print_system_message.assert_called_once_with(
        InlineREPL._pipeline_abort_notice_text(),
        style="yellow",
    )
    repl._session_storage.append.assert_called_once_with(
        "/workspace",
        "session-1",
        notice_message,
        git_branch="main",
    )
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_completed_pipeline_handoff_switches_to_normal_and_clears_state():
    terminal_event = _pipeline_completed_event()
    repl, pipeline, injected = _make_repl_for_handoff(terminal_event, should_switch_to_normal=True)

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    repl._agent_loop.context_manager.add_raw_message.assert_called_once_with(
        {"role": "user", "content": "handoff summary"}
    )
    repl._session_storage.append.assert_called_once_with(
        "/workspace",
        "session-1",
        injected,
        git_branch="main",
    )
    pipeline.mark_normal_handoff.assert_called_once_with(status="succeeded", failed_reason=None)
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_early_exit_handoff_clears_incomplete_state_without_user_abort():
    terminal_event = _pipeline_completed_event(early_exit=True)
    repl, pipeline, _injected = _make_repl_for_handoff(
        terminal_event,
        should_switch_to_normal=True,
        is_complete=False,
    )

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    pipeline.mark_normal_handoff.assert_called_once_with(status="succeeded", failed_reason=None)
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_failed_terminal_event_does_not_handoff_and_failed_cleanup_remains():
    terminal_event = _pipeline_completed_event(failed=True)
    repl, pipeline, _injected = _make_repl_for_handoff(
        terminal_event,
        should_switch_to_normal=False,
        is_complete=False,
        sidecar_status="failed",
    )

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.PIPELINE
    repl._agent_loop.context_manager.add_raw_message.assert_not_called()
    repl._session_storage.append.assert_not_called()
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_normal_handoff.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_handoff_injection_failure_still_switches_to_normal_and_preserves_sidecar():
    terminal_event = _pipeline_completed_event()
    repl, pipeline, _injected = _make_repl_for_handoff(terminal_event, should_switch_to_normal=True)
    repl._agent_loop.context_manager.add_raw_message.side_effect = RuntimeError("context unavailable")

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    repl._session_storage.append.assert_not_called()
    repl.renderer.print_system_message.assert_called()
    pipeline.mark_normal_handoff.assert_called_once_with(
        status="failed",
        failed_reason="context unavailable",
    )
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    pipeline.resume.assert_called_once_with("start")
    pipeline.run.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_handoff_persistence_failure_still_switches_to_normal_and_preserves_sidecar():
    terminal_event = _pipeline_completed_event()
    repl, pipeline, _injected = _make_repl_for_handoff(terminal_event, should_switch_to_normal=True)
    repl._session_storage.append.side_effect = RuntimeError("disk unavailable")

    await repl._handle_pipeline_chat("start")

    assert repl._runtime_mode == RunMode.NORMAL
    repl._agent_loop.context_manager.add_raw_message.assert_called_once()
    repl.renderer.print_system_message.assert_called()
    pipeline.mark_normal_handoff.assert_called_once_with(
        status="failed",
        failed_reason="disk unavailable",
    )
    pipeline.clear_sidecar.assert_not_called()
    pipeline.mark_user_aborted.assert_not_called()
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_outer_stream_returns_candidate_selection_terminal_event():
    from iac_code.ui.repl import InlineREPL

    terminal_event = _pipeline_completed_event()
    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_step_names = []
    repl._pipeline_completed_indices = set()
    repl._update_pipeline_state_from_event = MagicMock()
    repl._render_pipeline_event = MagicMock()
    repl._render_candidate_selection_tabs = AsyncMock(return_value=terminal_event)
    repl._restart_pipeline_stream_after_interrupt = AsyncMock(return_value=_empty_stream())

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="select",
            timestamp=time.time(),
            data={"index": 1, "total": 1, "ui_mode": "candidate_selection"},
        )

    result = await repl._render_pipeline_stream(stream())

    assert result is terminal_event
    repl._restart_pipeline_stream_after_interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_stream_returns_parallel_tabs_terminal_event():
    from iac_code.ui.repl import InlineREPL

    terminal_event = _pipeline_completed_event()
    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_step_names = []
    repl._pipeline_completed_indices = set()
    repl._update_pipeline_state_from_event = MagicMock()
    repl._render_pipeline_event = MagicMock()
    repl._render_parallel_tabs = AsyncMock(return_value=terminal_event)
    repl._restart_pipeline_stream_after_interrupt = AsyncMock(return_value=_empty_stream())

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="parallel",
            timestamp=time.time(),
            data={
                "index": 1,
                "total": 1,
                "ui_mode": "default",
                "step_type": "parallel_sub_pipeline",
            },
        )

    result = await repl._render_pipeline_stream(stream())

    assert result is terminal_event
    repl._restart_pipeline_stream_after_interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_render_parallel_tabs_returns_consumed_pipeline_completed_event(monkeypatch):
    from iac_code.ui.repl import InlineREPL

    terminal_event = _pipeline_completed_event()
    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True)
    repl._pipeline = MagicMock()

    class FakeLive:
        def start(self):
            pass

        def stop(self):
            pass

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr(InlineREPL, "_create_parallel_live", lambda self: FakeLive(), raising=False)

    async def stream():
        yield terminal_event

    result = await repl._render_parallel_tabs(stream())

    assert result is terminal_event
