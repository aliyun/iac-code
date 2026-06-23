from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from iac_code.agent.message import Message
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineStatePersistenceError
from iac_code.pipeline.engine.user_input import PipelineUserInput


@pytest.fixture
def repl_for_sidecar_restore(tmp_path):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = None
    repl._pipeline_waiting_input = False
    repl._pipeline_state_persistence_failed = False
    repl._session_id = "sid"
    repl._original_cwd = str(tmp_path)
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []
    repl._session_storage = MagicMock()
    repl._session_storage.session_dir.return_value = tmp_path / "sid"
    repl.renderer = MagicMock()
    repl.renderer.record_user_turn = MagicMock()
    repl.renderer.print_system_message = MagicMock()
    repl.console = MagicMock()
    repl.store = MagicMock()
    repl.store.get_state.return_value.permission_context = None
    repl.store.set_state = MagicMock()
    repl._render_pipeline_stream = AsyncMock()
    repl._detect_pipeline_session = InlineREPL._detect_pipeline_session.__get__(repl)
    repl._handle_pipeline_chat = InlineREPL._handle_pipeline_chat.__get__(repl)
    return repl


def test_normal_handoff_save_failure_does_not_switch_or_append(repl_for_sidecar_restore):
    from iac_code.pipeline.config import RunMode

    repl_for_sidecar_restore._runtime_mode = RunMode.PIPELINE
    repl_for_sidecar_restore._agent_loop = MagicMock()
    repl_for_sidecar_restore._agent_loop.context_manager = MagicMock()
    repl_for_sidecar_restore._agent_loop.context_manager.add_raw_message = MagicMock()
    repl_for_sidecar_restore.current_git_branch = MagicMock(return_value="main")
    terminal_event = PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=1.0,
        data={"total_steps": 1},
    )
    pipeline = MagicMock()
    pipeline.should_switch_to_normal.return_value = True
    pipeline.build_normal_handoff_summary.return_value = "handoff summary"
    pipeline.mark_normal_handoff.side_effect = PipelineStatePersistenceError(
        "pipeline state persistence failed during save_normal_handoff"
    )
    repl_for_sidecar_restore._pipeline = pipeline

    result = repl_for_sidecar_restore._handoff_pipeline_to_normal(terminal_event)

    assert result == "persistence_failed"
    assert repl_for_sidecar_restore._pipeline_state_persistence_failed is True
    assert repl_for_sidecar_restore._runtime_mode == RunMode.PIPELINE
    pipeline.mark_normal_handoff.assert_called_once_with(status="pending", failed_reason=None)
    pipeline.build_normal_handoff_summary.assert_not_called()
    repl_for_sidecar_restore._agent_loop.context_manager.add_raw_message.assert_not_called()
    repl_for_sidecar_restore._session_storage.append.assert_not_called()
    repl_for_sidecar_restore.renderer.print_system_message.assert_called_once_with(
        "Pipeline state persistence failed. Normal chat handoff was not marked durable.",
        style="yellow",
    )


def test_finalize_handoff_persistence_failure_keeps_pipeline_paused(repl_for_sidecar_restore):
    from iac_code.pipeline.config import RunMode

    terminal_event = PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=1.0,
        data={"total_steps": 1},
    )
    pipeline = MagicMock()
    pipeline.should_switch_to_normal.return_value = True
    pipeline.mark_normal_handoff.side_effect = PipelineStatePersistenceError(
        "pipeline state persistence failed during save_normal_handoff"
    )
    pipeline.pause_agent_loops = MagicMock()
    pipeline.mark_user_aborted = MagicMock()
    repl_for_sidecar_restore._runtime_mode = RunMode.PIPELINE
    repl_for_sidecar_restore._pipeline = pipeline

    repl_for_sidecar_restore._finalize_pipeline_after_render(terminal_event)

    assert repl_for_sidecar_restore._runtime_mode == RunMode.PIPELINE
    assert repl_for_sidecar_restore._pipeline is pipeline
    assert repl_for_sidecar_restore._pipeline_state_persistence_failed is True
    pipeline.pause_agent_loops.assert_called_once_with()
    pipeline.mark_user_aborted.assert_not_called()


def test_handoff_append_failure_and_failed_metadata_failure_does_not_record_success(repl_for_sidecar_restore):
    from iac_code.pipeline.config import RunMode

    repl_for_sidecar_restore._runtime_mode = RunMode.PIPELINE
    repl_for_sidecar_restore._agent_loop = MagicMock()
    repl_for_sidecar_restore._agent_loop.context_manager = MagicMock()
    repl_for_sidecar_restore._agent_loop.context_manager.add_raw_message.return_value = Message(
        role="user",
        content="handoff summary",
    )
    repl_for_sidecar_restore.current_git_branch = MagicMock(return_value="main")
    repl_for_sidecar_restore._session_storage.append.side_effect = RuntimeError("disk unavailable")
    terminal_event = PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=1.0,
        data={"total_steps": 1},
    )
    pipeline = MagicMock()
    pipeline.should_switch_to_normal.return_value = True
    pipeline.build_normal_handoff_summary.return_value = "handoff summary"
    pipeline.mark_normal_handoff.side_effect = [
        None,
        PipelineStatePersistenceError("pipeline state persistence failed during save_normal_handoff"),
    ]
    repl_for_sidecar_restore._pipeline = pipeline

    result = repl_for_sidecar_restore._handoff_pipeline_to_normal(terminal_event)

    assert result == "persistence_failed"
    assert repl_for_sidecar_restore._pipeline_state_persistence_failed is True
    assert repl_for_sidecar_restore._runtime_mode == RunMode.PIPELINE
    pipeline.mark_normal_handoff.assert_has_calls(
        [
            call(status="pending", failed_reason=None),
            call(status="failed", failed_reason="disk unavailable"),
        ]
    )
    assert pipeline.mark_normal_handoff.call_count == 2


def test_finalize_persistence_failure_event_does_not_mark_user_aborted(repl_for_sidecar_restore):
    event = PipelineEvent(
        type=PipelineEventType.STEP_FAILED,
        step_id="collect",
        timestamp=1.0,
        data={
            "error": "Pipeline state persistence failed.",
            "error_details": {"type": "PipelineStatePersistenceError"},
        },
    )
    pipeline = MagicMock()
    pipeline.sidecar_status = None
    pipeline.state_machine.is_complete = False
    pipeline.pause_agent_loops = MagicMock()
    pipeline.mark_user_aborted = MagicMock()
    repl_for_sidecar_restore._pipeline = pipeline
    repl_for_sidecar_restore._pipeline_waiting_input = False

    repl_for_sidecar_restore._record_pipeline_display_event(event)
    repl_for_sidecar_restore._finalize_pipeline_after_render(None)

    assert repl_for_sidecar_restore._pipeline_state_persistence_failed is True
    assert repl_for_sidecar_restore._pipeline is pipeline
    pipeline.pause_agent_loops.assert_called_once_with()
    pipeline.mark_user_aborted.assert_not_called()


def test_finalize_user_abort_persistence_failure_keeps_pipeline_paused(repl_for_sidecar_restore):
    pipeline = MagicMock()
    pipeline.sidecar_status = None
    pipeline.state_machine.is_complete = False
    pipeline.pause_agent_loops = MagicMock()
    pipeline.mark_user_aborted.side_effect = PipelineStatePersistenceError(
        "pipeline state persistence failed during save_user_aborted_sync"
    )
    repl_for_sidecar_restore._pipeline = pipeline
    repl_for_sidecar_restore._pipeline_waiting_input = False
    repl_for_sidecar_restore._last_interrupt_paused = False

    repl_for_sidecar_restore._finalize_pipeline_after_render(None)

    assert repl_for_sidecar_restore._pipeline_state_persistence_failed is True
    assert repl_for_sidecar_restore._pipeline is pipeline
    pipeline.mark_user_aborted.assert_called_once_with("pipeline interrupted by user or renderer cancellation")
    pipeline.pause_agent_loops.assert_called_once_with()
    repl_for_sidecar_restore.renderer.print_system_message.assert_called_once_with(
        "Pipeline state persistence failed. The pipeline is paused; do not continue until state is durable.",
        style="yellow",
    )


@pytest.mark.asyncio
async def test_mid_pipeline_pause_save_failure_warns_and_stays_paused(repl_for_sidecar_restore):
    from iac_code.pipeline.engine.interrupt import InterruptVerdict

    verdict = InterruptVerdict(action="continue", reason="judge failed", paused=True)
    pipeline = MagicMock()
    pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)
    pipeline.save_interrupt_pause = AsyncMock(
        side_effect=PipelineStatePersistenceError("pipeline state persistence failed during save_waiting_input")
    )
    pipeline.pause_agent_loops = MagicMock()
    pipeline.resume_agent_loops = MagicMock()
    repl_for_sidecar_restore._pipeline = pipeline
    repl_for_sidecar_restore._last_interrupt_paused = False
    repl_for_sidecar_restore._pipeline_waiting_input = False

    needs_restart, feedback = await repl_for_sidecar_restore._handle_mid_pipeline_message(
        "等等",
        suppress_render=True,
    )

    assert needs_restart is False
    assert feedback == ""
    assert repl_for_sidecar_restore._pipeline_waiting_input is False
    assert repl_for_sidecar_restore._last_interrupt_paused is True
    pipeline.save_interrupt_pause.assert_awaited_once_with(verdict)
    pipeline.resume_agent_loops.assert_not_called()
    repl_for_sidecar_restore.renderer.print_system_message.assert_called_once_with(
        "Pipeline state persistence failed. The pipeline is paused; do not continue until state is durable.",
        style="yellow",
    )


@pytest.mark.asyncio
async def test_mid_pipeline_hard_interrupt_save_failure_warns_and_stays_paused(repl_for_sidecar_restore):
    from iac_code.pipeline.engine.interrupt import InterruptVerdict

    verdict = InterruptVerdict(action="hard_interrupt", reason="changed mind", rollback_target="intent")
    pipeline = MagicMock()
    pipeline.handle_user_interrupt = AsyncMock(return_value=verdict)
    pipeline.apply_hard_interrupt = MagicMock(
        side_effect=PipelineStatePersistenceError("pipeline state persistence failed during save_rollback_sync")
    )
    pipeline.pause_agent_loops = MagicMock()
    pipeline.resume_agent_loops = MagicMock()
    repl_for_sidecar_restore._pipeline = pipeline
    repl_for_sidecar_restore._last_interrupt_paused = False

    needs_restart, feedback = await repl_for_sidecar_restore._handle_mid_pipeline_message(
        "换方案",
        suppress_render=True,
    )

    assert needs_restart is False
    assert feedback == ""
    assert repl_for_sidecar_restore._last_interrupt_paused is True
    pipeline.apply_hard_interrupt.assert_called_once_with(verdict)
    pipeline.resume_agent_loops.assert_not_called()
    repl_for_sidecar_restore.renderer.print_system_message.assert_called_once_with(
        "Pipeline state persistence failed. The pipeline is paused; do not continue until state is durable.",
        style="yellow",
    )


@pytest.mark.asyncio
async def test_fresh_pipeline_persists_visible_user_turn_to_root_session(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    prompt = "帮我在阿里云部署一个低成本 Nginx 网站，需要公网访问。"
    persisted = Message(role="user", content=prompt)
    repl_for_sidecar_restore._agent_loop = MagicMock()
    repl_for_sidecar_restore._agent_loop.context_manager.add_raw_message.return_value = persisted
    repl_for_sidecar_restore.current_git_branch = MagicMock(return_value="main")

    pipeline = MagicMock()
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = None
    pipeline.mark_user_aborted = MagicMock()

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        await repl_for_sidecar_restore._handle_pipeline_chat(prompt)

    repl_for_sidecar_restore._agent_loop.context_manager.add_raw_message.assert_any_call(
        {"role": "user", "content": prompt}
    )
    repl_for_sidecar_restore._session_storage.append.assert_any_call(
        str(repl_for_sidecar_restore._original_cwd),
        "sid",
        persisted,
        git_branch="main",
    )


def test_terminal_pipeline_resume_falls_back_to_initial_transcript_user_message(tmp_path, monkeypatch):
    monkeypatch.delenv("IAC_CODE_CWD", raising=False)
    from iac_code.pipeline.config import RunMode
    from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession
    from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    cwd = str(tmp_path / "workspace")
    session_id = "sid"
    prompt = "帮我在阿里云部署一个低成本 Nginx 网站，需要公网访问。"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    storage.append_meta(cwd, session_id, {"type": "pipeline_init", "pipeline_type": "selling"})

    sidecar = PipelineSession(storage.session_dir(cwd, session_id) / "pipeline")
    sidecar.save_user_aborted_sync(
        "intent_parsing",
        {
            "current_index": 0,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {"intent_parsing": "pending"},
        },
        {},
        PipelineIdentity(
            pipeline_name="selling",
            step_ids=["intent_parsing"],
            sub_pipeline_step_ids={},
            pipeline_fingerprint="fingerprint",
        ),
        reason="pipeline interrupted by user or renderer cancellation",
        execution={"kind": "step", "active_attempt_id": "att_0001", "transcript_id": "transcript_att_0001"},
        attempts={
            "next_attempt_number": 2,
            "items": {
                "att_0001": {
                    "attempt_id": "att_0001",
                    "scope": "parent",
                    "status": "running",
                    "step_id": "intent_parsing",
                    "transcript_id": "transcript_att_0001",
                }
            },
        },
    )
    transcript_storage = PipelineTranscriptStorage(sidecar.session_dir)
    transcript_storage.append(cwd, "transcript_att_0001", Message(role="user", content=prompt))
    transcript_storage.append(cwd, "transcript_att_0001", Message(role="assistant", content="internal step output"))

    repl = InlineREPL.__new__(InlineREPL)
    repl._session_storage = storage
    repl._original_cwd = cwd
    repl._session_id = session_id
    repl._runtime_mode = RunMode.NORMAL

    messages = repl._load_resume_messages(session_id)

    assert [(message.role, message.content) for message in messages] == [
        ("user", prompt),
        ("assistant", InlineREPL._pipeline_abort_notice_text()),
    ]


@pytest.mark.asyncio
async def test_restored_waiting_input_routes_current_message_to_resume(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline = MagicMock()
    pipeline.restore_from_sidecar = AsyncMock(return_value=MagicMock(ok=True, status="waiting_input", reason=None))
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = "waiting_input"
    pipeline.mark_user_aborted = MagicMock()
    _seed_sidecar(repl_for_sidecar_restore, "waiting_input")

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        await repl_for_sidecar_restore._handle_pipeline_chat("方案一")

    pipeline.resume.assert_called_once_with(
        PipelineUserInput(content="方案一", display_text="方案一", has_images=False)
    )
    pipeline.run.assert_not_called()
    pipeline.continue_from_sidecar.assert_not_called()


@pytest.mark.asyncio
async def test_restored_waiting_candidate_selection_reenters_selection_ui(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline = MagicMock()
    pipeline.restore_from_sidecar = AsyncMock(return_value=MagicMock(ok=True, status="waiting_input", reason=None))
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.current_step.step_id = "confirm_and_select"
    pipeline.state_machine.current_step.ui_mode = "candidate_selection"
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = "waiting_input"
    pipeline.mark_user_aborted = MagicMock()
    repl_for_sidecar_restore._resume_waiting_candidate_selection_from_sidecar = AsyncMock(return_value=None)
    _seed_sidecar(repl_for_sidecar_restore, "waiting_input")

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        await repl_for_sidecar_restore._handle_pipeline_chat("继续")

    repl_for_sidecar_restore._resume_waiting_candidate_selection_from_sidecar.assert_awaited_once()
    pipeline.resume.assert_not_called()
    pipeline.run.assert_not_called()
    pipeline.continue_from_sidecar.assert_not_called()


@pytest.mark.asyncio
async def test_prompt_restore_rehydrates_pipeline_without_continuing(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline = MagicMock()
    pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    pipeline.restore_from_sidecar = AsyncMock()
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    pipeline.display_transcript_path = None
    _seed_sidecar(repl_for_sidecar_restore, "running")

    from iac_code.ui.repl import InlineREPL

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        restored = await InlineREPL.ensure_pipeline_restored_for_prompt(repl_for_sidecar_restore)

    assert restored is True
    assert repl_for_sidecar_restore._pipeline is pipeline
    assert repl_for_sidecar_restore._pipeline_restored_status == "running"
    pipeline.restore_from_sidecar.assert_not_called()
    pipeline.resume.assert_not_called()
    pipeline.run.assert_not_called()
    pipeline.continue_from_sidecar.assert_not_called()


@pytest.mark.asyncio
async def test_startup_waiting_candidate_selection_reenters_selection_ui(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    from iac_code.pipeline.config import RunMode
    from iac_code.ui.repl import InlineREPL

    repl_for_sidecar_restore._runtime_mode = RunMode.PIPELINE
    pipeline = MagicMock()
    pipeline.sidecar_restore_result = MagicMock(ok=True, status="waiting_input", reason=None)
    pipeline.restore_from_sidecar = AsyncMock()
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.current_step.step_id = "confirm_and_select"
    pipeline.state_machine.current_step.ui_mode = "candidate_selection"
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = "waiting_input"
    pipeline.mark_user_aborted = MagicMock()
    pipeline.display_transcript_path = None
    call_order: list[str] = []
    repl_for_sidecar_restore._render_pipeline_display_replay_on_startup = MagicMock(
        side_effect=lambda: call_order.append("replay")
    )

    async def fake_resume_selection():
        call_order.append("selection")
        return None

    repl_for_sidecar_restore._resume_waiting_candidate_selection_from_sidecar = AsyncMock(
        side_effect=fake_resume_selection
    )
    _seed_sidecar(repl_for_sidecar_restore, "waiting_input")

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        handled = await InlineREPL._resume_pipeline_sidecar_on_startup(repl_for_sidecar_restore)

    assert handled is True
    repl_for_sidecar_restore._render_pipeline_display_replay_on_startup.assert_called_once()
    repl_for_sidecar_restore._resume_waiting_candidate_selection_from_sidecar.assert_awaited_once()
    assert call_order == ["replay", "selection"]
    pipeline.restore_from_sidecar.assert_not_called()
    pipeline.resume.assert_not_called()
    pipeline.run.assert_not_called()
    pipeline.continue_from_sidecar.assert_not_called()
    assert repl_for_sidecar_restore._pipeline_waiting_input is True


@pytest.mark.asyncio
async def test_startup_waiting_candidate_selection_starts_cleanup_after_terminal_handoff(
    monkeypatch,
    repl_for_sidecar_restore,
):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    from iac_code.pipeline.config import RunMode
    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.ui.repl import InlineREPL

    repl_for_sidecar_restore._runtime_mode = RunMode.PIPELINE
    terminal_event = PipelineEvent(
        type=PipelineEventType.PIPELINE_COMPLETED,
        step_id=None,
        timestamp=1.0,
        data={"total_steps": 1},
    )
    pipeline = MagicMock()
    pipeline.sidecar_restore_result = MagicMock(ok=True, status="waiting_input", reason=None)
    pipeline.restore_from_sidecar = AsyncMock()
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.current_step.step_id = "confirm_and_select"
    pipeline.state_machine.current_step.ui_mode = "candidate_selection"
    pipeline.state_machine.is_complete = True
    pipeline.sidecar_status = "completed"
    pipeline.should_switch_to_normal = MagicMock(return_value=True)
    pipeline.build_normal_handoff_summary = MagicMock(return_value="handoff summary")
    pipeline.mark_normal_handoff = MagicMock()
    pipeline.mark_user_aborted = MagicMock()
    pipeline.display_transcript_path = None
    repl_for_sidecar_restore.current_git_branch = MagicMock(return_value="main")
    repl_for_sidecar_restore._agent_loop = MagicMock()
    repl_for_sidecar_restore._agent_loop.context_manager = MagicMock()
    repl_for_sidecar_restore._agent_loop.context_manager.add_raw_message = MagicMock(
        return_value=Message(role="user", content="handoff summary")
    )
    repl_for_sidecar_restore._render_pipeline_display_replay_on_startup = MagicMock()
    repl_for_sidecar_restore._resume_waiting_candidate_selection_from_sidecar = AsyncMock(return_value=terminal_event)
    repl_for_sidecar_restore._maybe_start_pipeline_cleanup = AsyncMock(return_value=True)
    _seed_sidecar(repl_for_sidecar_restore, "waiting_input")

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        handled = await InlineREPL._resume_pipeline_sidecar_on_startup(repl_for_sidecar_restore)

    assert handled is True
    repl_for_sidecar_restore._maybe_start_pipeline_cleanup.assert_awaited_once_with(pipeline)


@pytest.mark.asyncio
async def test_startup_running_pipeline_replays_history_without_continuing(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    from iac_code.pipeline.config import RunMode
    from iac_code.ui.repl import InlineREPL

    repl_for_sidecar_restore._runtime_mode = RunMode.PIPELINE
    pipeline = MagicMock()
    pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    pipeline.restore_from_sidecar = AsyncMock()
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.current_step.step_id = "deploying"
    pipeline.state_machine.current_step.ui_mode = ""
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = "running"
    pipeline.mark_user_aborted = MagicMock()
    pipeline.display_transcript_path = None
    repl_for_sidecar_restore._render_pipeline_display_replay_on_startup = MagicMock()
    repl_for_sidecar_restore._resume_waiting_candidate_selection_from_sidecar = AsyncMock(return_value=None)
    _seed_sidecar(repl_for_sidecar_restore, "running")

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        handled = await InlineREPL._resume_pipeline_sidecar_on_startup(repl_for_sidecar_restore)

    assert handled is False
    repl_for_sidecar_restore._render_pipeline_display_replay_on_startup.assert_called_once()
    repl_for_sidecar_restore._resume_waiting_candidate_selection_from_sidecar.assert_not_awaited()
    pipeline.restore_from_sidecar.assert_not_called()
    pipeline.resume.assert_not_called()
    pipeline.run.assert_not_called()
    pipeline.continue_from_sidecar.assert_not_called()
    assert repl_for_sidecar_restore._pipeline_restored_status == "running"


@pytest.mark.asyncio
async def test_restored_running_routes_to_continue_without_user_prompt(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline = MagicMock()
    pipeline.restore_from_sidecar = AsyncMock(return_value=MagicMock(ok=True, status="running", reason=None))
    pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = "running"
    pipeline.mark_user_aborted = MagicMock()
    _seed_sidecar(repl_for_sidecar_restore, "running")

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        await repl_for_sidecar_restore._handle_pipeline_chat("hello after crash")

    pipeline.continue_from_sidecar.assert_called_once_with(
        user_input=PipelineUserInput(content="hello after crash", display_text="hello after crash", has_images=False)
    )
    pipeline.run.assert_not_called()
    pipeline.resume.assert_not_called()


@pytest.mark.asyncio
async def test_restored_running_uses_pipeline_working_directory(monkeypatch, tmp_path, repl_for_sidecar_restore):
    pipeline_cwd = tmp_path / "pipeline-cwd"
    original_cwd = tmp_path / "original-cwd"
    repl_for_sidecar_restore._original_cwd = str(original_cwd)
    repl_for_sidecar_restore._session_storage.session_dir.side_effect = lambda cwd, sid: tmp_path / cwd / sid
    _seed_sidecar_path(tmp_path / str(pipeline_cwd) / "sid" / "pipeline", "running")

    pipeline = MagicMock()
    pipeline.restore_from_sidecar = AsyncMock(return_value=MagicMock(ok=True, status="running", reason=None))
    pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.resume = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = "running"
    pipeline.mark_user_aborted = MagicMock()

    with (
        patch("iac_code.pipeline.config.get_working_directory", return_value=str(pipeline_cwd)),
        patch("iac_code.pipeline.create_pipeline", return_value=pipeline),
    ):
        await repl_for_sidecar_restore._handle_pipeline_chat("hello after crash")

    pipeline.restore_from_sidecar.assert_awaited_once()
    pipeline.continue_from_sidecar.assert_called_once_with(
        user_input=PipelineUserInput(content="hello after crash", display_text="hello after crash", has_images=False)
    )
    pipeline.run.assert_not_called()
    pipeline.resume.assert_not_called()


@pytest.mark.asyncio
async def test_corrupt_sidecar_starts_fresh(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline = MagicMock()
    pipeline.restore_from_sidecar = AsyncMock(return_value=MagicMock(ok=False, status=None, reason="corrupt_meta"))
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = None
    pipeline.mark_user_aborted = MagicMock()
    _seed_sidecar(repl_for_sidecar_restore, "running")

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        await repl_for_sidecar_restore._handle_pipeline_chat("fresh")

    pipeline.run.assert_called_once_with(PipelineUserInput(content="fresh", display_text="fresh", has_images=False))
    repl_for_sidecar_restore.renderer.print_system_message.assert_called()


@pytest.mark.asyncio
async def test_confirm_pipeline_resume_handles_corrupt_meta(tmp_path, repl_for_sidecar_restore):
    meta_path = tmp_path / "meta.yaml"
    meta_path.write_text("[broken", encoding="utf-8")

    choice = await repl_for_sidecar_restore._confirm_pipeline_resume(meta_path)

    assert choice == "discard"
    repl_for_sidecar_restore.renderer.print_system_message.assert_called()


@pytest.mark.asyncio
async def test_confirm_pipeline_resume_discards_non_mapping_meta(tmp_path, repl_for_sidecar_restore):
    meta_path = tmp_path / "meta.yaml"
    meta_path.write_text("- not\n- metadata\n", encoding="utf-8")

    choice = await repl_for_sidecar_restore._confirm_pipeline_resume(meta_path)

    assert choice == "discard"
    repl_for_sidecar_restore.renderer.print_system_message.assert_called()


@pytest.mark.asyncio
async def test_discarded_sidecar_starts_fresh_without_restore(monkeypatch, repl_for_sidecar_restore):
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline = MagicMock()
    pipeline.restore_from_sidecar = AsyncMock()
    pipeline.run = MagicMock(return_value=_empty_stream())
    pipeline.state_machine.is_complete = False
    pipeline.sidecar_status = None
    pipeline.mark_user_aborted = MagicMock()
    _seed_sidecar(repl_for_sidecar_restore, "discarded")

    with patch("iac_code.pipeline.create_pipeline", return_value=pipeline):
        await repl_for_sidecar_restore._handle_pipeline_chat("fresh")

    pipeline.restore_from_sidecar.assert_not_called()
    pipeline.run.assert_called_once_with(PipelineUserInput(content="fresh", display_text="fresh", has_images=False))


def _seed_sidecar(repl, status: str) -> None:
    _seed_sidecar_path(repl._session_storage.session_dir.return_value / "pipeline", status)


def _seed_sidecar_path(sidecar, status: str) -> None:
    sidecar.mkdir(parents=True, exist_ok=True)
    (sidecar / "meta.yaml").write_text(
        f"status: {status}\ncurrent_step: step1\nstate_machine: {{}}\nupdated_at: 0.0\n",
        encoding="utf-8",
    )


async def _empty_stream():
    return
    yield  # noqa: B901
