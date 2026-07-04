"""U-I17: _handle_chat_continue must reject calls in pipeline mode (defensive)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from iac_code.pipeline.config import RunMode
from iac_code.services.session_backup import BackupReason


@pytest.mark.asyncio
async def test_handle_chat_continue_rejects_pipeline_mode(monkeypatch):
    """In pipeline mode, _handle_chat_continue should return without running
    the non-pipeline agent loop (which would produce undefined behavior)."""
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.PIPELINE
    repl.store = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.run_streaming = MagicMock()
    repl.renderer = MagicMock()
    repl._streaming_error_log = []

    await repl._handle_chat_continue()

    # Non-pipeline agent loop must NOT have been invoked.
    repl._agent_loop.run_streaming.assert_not_called()


@pytest.mark.asyncio
async def test_handle_chat_continue_works_in_normal_mode(monkeypatch):
    """Normal mode (no IAC_CODE_MODE set): _handle_chat_continue runs the agent loop."""
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)

    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl.store = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.run_streaming = MagicMock(return_value=[])
    repl._agent_loop.context_manager = MagicMock(get_messages=MagicMock(return_value=[]))
    repl._agent_loop.stamp_last_turn_elapsed = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.run_streaming_output = AsyncMock(return_value=StreamingOutputResult(elapsed=0.0))
    repl.renderer._last_streaming_errors = []
    repl._backup_service = SimpleNamespace(backup_session=Mock())
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._streaming_error_log = []

    await repl._handle_chat_continue()
    repl._agent_loop.run_streaming.assert_called_once()
    repl._backup_service.backup_session.assert_called_once_with(
        "/repo",
        "session-1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )


@pytest.mark.asyncio
async def test_handle_chat_continue_interrupted_result_does_not_backup(monkeypatch):
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)

    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._runtime_mode = RunMode.NORMAL
    repl.store = MagicMock()
    repl._agent_loop = MagicMock()
    repl._agent_loop.run_streaming = MagicMock(return_value=[])
    repl._agent_loop.context_manager = MagicMock(get_messages=MagicMock(return_value=[]))
    repl._agent_loop.stamp_last_turn_elapsed = MagicMock()
    repl.renderer = MagicMock()
    repl.renderer.run_streaming_output = AsyncMock(
        return_value=StreamingOutputResult(
            elapsed=0.2,
            queued_inputs=["next turn"],
            draft_input="half typed",
            interrupted=True,
        )
    )
    repl.renderer._last_streaming_errors = []
    repl._backup_service = SimpleNamespace(backup_session=Mock())
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._streaming_error_log = []
    repl._block_if_cleanup_ledger_unreadable = Mock(return_value=False)
    repl._prune_cleanup_prompts_if_no_pending_cleanup = Mock()

    queued_inputs = await repl._handle_chat_continue()

    assert queued_inputs == ["next turn"]
    assert repl._streaming_draft_input == "half typed"
    repl._backup_service.backup_session.assert_not_called()
    repl._prune_cleanup_prompts_if_no_pending_cleanup.assert_called_once_with()
    repl.store.set_state.assert_any_call(is_busy=False)


@pytest.mark.asyncio
async def test_cleanup_continue_backs_up_after_prune_and_state_reset(tmp_path, monkeypatch):
    """A normal cleanup continuation is also a normal agent-loop turn end."""
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)

    from iac_code.pipeline.engine.cleanup import CleanupPrompt
    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    async def events():
        if False:
            yield None

    order: list[str] = []
    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._pipeline_cleanup_ledger_path = None
    repl._backup_service = SimpleNamespace(
        backup_session=Mock(side_effect=lambda *args, **kwargs: order.append("backup"))
    )
    repl.store = SimpleNamespace(set_state=Mock(side_effect=lambda **kwargs: order.append(f"busy={kwargs['is_busy']}")))
    repl.renderer = SimpleNamespace(
        print_system_message=Mock(),
        prompt_permission=AsyncMock(),
        run_streaming_output=AsyncMock(return_value=StreamingOutputResult(elapsed=1.2)),
        _last_streaming_errors=[],
    )
    repl._agent_loop = SimpleNamespace(
        continue_streaming=Mock(return_value=events()),
        stamp_last_turn_elapsed=Mock(side_effect=lambda elapsed: order.append("stamp")),
        context_manager=SimpleNamespace(
            add_raw_message=Mock(return_value=SimpleNamespace()),
            get_messages=Mock(return_value=[]),
        ),
    )
    repl._session_storage = SimpleNamespace(append=Mock())
    repl.current_git_branch = Mock(return_value=None)
    repl._wrap_cleanup_observer = Mock(side_effect=lambda stream, ledger=None: stream)
    repl._prune_cleanup_prompts_if_no_pending_cleanup = Mock(side_effect=lambda ledger=None: order.append("prune"))
    repl._streaming_error_log = []

    cleanup_prompt = CleanupPrompt(resources=[], prompt="cleanup now", status_message="cleanup status")
    ledger = SimpleNamespace(
        path=tmp_path / "cleanup.yaml",
        load_failed=Mock(return_value=False),
        build_pending_prompt=Mock(return_value=cleanup_prompt),
        record_prompt_queued=Mock(),
    )

    assert await repl._start_pipeline_cleanup_from_ledger(ledger) is True

    repl._backup_service.backup_session.assert_called_once_with(
        "/repo",
        "session-1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )
    assert order == ["busy=True", "stamp", "prune", "busy=False", "backup"]


@pytest.mark.asyncio
async def test_cleanup_continue_interrupted_result_does_not_backup(tmp_path, monkeypatch):
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)

    from iac_code.pipeline.engine.cleanup import CleanupPrompt
    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    async def events():
        if False:
            yield None

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._pipeline_cleanup_ledger_path = None
    repl._backup_service = SimpleNamespace(backup_session=Mock())
    repl.store = SimpleNamespace(set_state=Mock())
    repl.renderer = SimpleNamespace(
        print_system_message=Mock(),
        prompt_permission=AsyncMock(),
        run_streaming_output=AsyncMock(
            return_value=StreamingOutputResult(
                elapsed=0.2,
                queued_inputs=["next turn"],
                draft_input="half typed",
                interrupted=True,
            )
        ),
        _last_streaming_errors=[],
    )
    repl._agent_loop = SimpleNamespace(
        continue_streaming=Mock(return_value=events()),
        stamp_last_turn_elapsed=Mock(),
        context_manager=SimpleNamespace(
            add_raw_message=Mock(return_value=SimpleNamespace()),
            get_messages=Mock(return_value=[]),
        ),
    )
    repl._session_storage = SimpleNamespace(append=Mock())
    repl.current_git_branch = Mock(return_value=None)
    repl._wrap_cleanup_observer = Mock(side_effect=lambda stream, ledger=None: stream)
    repl._prune_cleanup_prompts_if_no_pending_cleanup = Mock()
    repl._streaming_error_log = []

    cleanup_prompt = CleanupPrompt(resources=[], prompt="cleanup now", status_message="cleanup status")
    ledger = SimpleNamespace(
        path=tmp_path / "cleanup.yaml",
        load_failed=Mock(return_value=False),
        build_pending_prompt=Mock(return_value=cleanup_prompt),
        record_prompt_queued=Mock(),
    )

    assert await repl._start_pipeline_cleanup_from_ledger(ledger) is True

    assert repl._streaming_draft_input == "next turn\nhalf typed"
    repl._backup_service.backup_session.assert_not_called()
    repl._prune_cleanup_prompts_if_no_pending_cleanup.assert_called_once_with(ledger)
    repl.store.set_state.assert_any_call(is_busy=False)
