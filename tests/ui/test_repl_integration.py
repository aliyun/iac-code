"""Tests for InlineREPL integration with ProviderManager."""

from __future__ import annotations

import re
import subprocess
from unittest.mock import patch

from iac_code.services.update_checker import PendingUpdate
from iac_code.ui.components.select import SelectLayout


def make_pending_update() -> PendingUpdate:
    return PendingUpdate(
        version="1.2.0",
        current_version="1.1.0",
        source="official_pypi",
        checked_at=123.0,
        update_command=(".venv/bin/python", "-m", "pip", "install", "--upgrade", "iac-code"),
        release_notes_url="https://example.test/releases/1.2.0",
    )


class TestREPLProviderIntegration:
    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_init_creates_provider_manager(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert hasattr(repl, "_provider_manager")

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_init_creates_task_manager(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert hasattr(repl, "_task_manager")

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_agent_tool_registered(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert repl.tool_registry.get("agent") is not None

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_memory_tools_registered(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert repl.tool_registry.get("read_memory") is not None
        assert repl.tool_registry.get("write_memory") is not None

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_task_tools_registered(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert repl.tool_registry.get("task_list") is not None
        assert repl.tool_registry.get("task_stop") is not None


UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_new_session_id_is_full_uuid(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL(model="test-model")
    assert UUID4_RE.match(repl.session_id), f"expected UUID4, got {repl.session_id!r}"


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_accepted_when_session_exists(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    existing_id = "99646984-35a9-4850-b72a-4131a1690774"
    mock_ss.return_value.exists.return_value = True
    mock_ss.return_value.load.return_value = []
    mock_ss.return_value.repair_interrupted.return_value = []
    repl = InlineREPL(model="test-model", resume_session_id=existing_id)
    assert repl.session_id == existing_id


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_raises_when_session_missing(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    mock_ss.return_value.exists.return_value = False
    mock_ss.return_value.find_session_anywhere.return_value = None
    import pytest

    with pytest.raises(ValueError, match="Session not found"):
        InlineREPL(model="test-model", resume_session_id="no-such-id")


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_cross_project_raises_with_hint(mock_mm, mock_ss, mock_pm, tmp_path):
    """A resume id resolved in a different project must surface the cd command."""
    from iac_code.ui.repl import InlineREPL

    mock_ss.return_value.exists.return_value = False
    mock_ss.return_value.find_session_anywhere.return_value = (
        "/elsewhere/repo",
        tmp_path / "fake.jsonl",
    )
    import pytest

    with pytest.raises(ValueError, match=r"cd /elsewhere/repo && iac-code --resume"):
        InlineREPL(model="test-model", resume_session_id="some-id")


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_dollar_local_command_shows_error(mock_mm, mock_ss, mock_pm):
    """Typing $help (a built-in command) under the $ trigger errors clearly."""
    import asyncio

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL(model="test-model")
    asyncio.run(repl._handle_command("$help"))
    assert repl._command_log
    user_input, message, _count, is_error = repl._command_log[-1]
    assert user_input == "$help"
    assert is_error is True
    assert "/help" in message


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_dollar_unknown_skill_shows_error(mock_mm, mock_ss, mock_pm):
    """Typing $<unknown> under the $ trigger reports an unknown-skill error."""
    import asyncio

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL(model="test-model")
    asyncio.run(repl._handle_command("$nosuchskillxyz"))
    assert repl._command_log
    user_input, message, _count, is_error = repl._command_log[-1]
    assert user_input == "$nosuchskillxyz"
    assert is_error is True
    assert "nosuchskillxyz" in message


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_handle_startup_update_returns_none_without_pending_update(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL(model="test-model")

    with (
        patch("iac_code.ui.repl.get_pending_update", return_value=None) as get_pending,
        patch("iac_code.ui.repl.Select") as select,
    ):
        assert repl._handle_startup_update() is None

    get_pending.assert_called_once_with()
    select.assert_not_called()


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_handle_startup_update_returns_update_when_skipped(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    update = make_pending_update()
    repl = InlineREPL(model="test-model")

    with (
        patch("iac_code.ui.repl.get_pending_update", return_value=update),
        patch("iac_code.ui.repl.render_update_prompt_header", return_value="update prompt"),
        patch("iac_code.ui.repl.Select") as select,
        patch("iac_code.ui.repl.start_background_update_check") as start_background,
    ):
        select.return_value.run.return_value = "skip"

        assert repl._handle_startup_update() == update

    select.assert_called_once()
    assert select.call_args.kwargs["default_value"] == "skip"
    assert select.call_args.kwargs["layout"] == SelectLayout.EXPANDED
    assert select.call_args.kwargs["visible_count"] == 3
    start_background.assert_not_called()


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_handle_startup_update_suppresses_version_when_skipped_until_next(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    update = make_pending_update()
    repl = InlineREPL(model="test-model")

    with (
        patch("iac_code.ui.repl.get_pending_update", return_value=update),
        patch("iac_code.ui.repl.render_update_prompt_header", return_value="update prompt"),
        patch("iac_code.ui.repl.Select") as select,
        patch("iac_code.ui.repl.suppress_version") as suppress_version,
    ):
        select.return_value.run.return_value = "skip_until_next"

        assert repl._handle_startup_update() is None

    suppress_version.assert_called_once_with(update.version)


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_handle_startup_update_exits_after_successful_update(mock_mm, mock_ss, mock_pm):
    import pytest

    from iac_code.ui.repl import InlineREPL

    update = make_pending_update()
    repl = InlineREPL(model="test-model")
    completed = subprocess.CompletedProcess(update.update_command, 0)

    with (
        patch("iac_code.ui.repl.get_pending_update", return_value=update),
        patch("iac_code.ui.repl.render_update_prompt_header", return_value="update prompt"),
        patch("iac_code.ui.repl.Select") as select,
        patch("iac_code.ui.repl.run_update_command", return_value=completed) as run_update_command,
    ):
        select.return_value.run.return_value = "update_now"

        with pytest.raises(SystemExit) as exc_info:
            repl._handle_startup_update()

    assert exc_info.value.code == 0
    run_update_command.assert_called_once_with(update)


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_handle_startup_update_returns_update_after_failed_update_command(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    update = make_pending_update()
    repl = InlineREPL(model="test-model")
    completed = subprocess.CompletedProcess(update.update_command, 1)

    with (
        patch("iac_code.ui.repl.get_pending_update", return_value=update),
        patch("iac_code.ui.repl.render_update_prompt_header", return_value="update prompt"),
        patch("iac_code.ui.repl.Select") as select,
        patch("iac_code.ui.repl.run_update_command", return_value=completed) as run_update_command,
    ):
        select.return_value.run.return_value = "update_now"

        assert repl._handle_startup_update() == update

    run_update_command.assert_called_once_with(update)


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_handle_startup_update_returns_update_when_update_command_raises(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    update = make_pending_update()
    repl = InlineREPL(model="test-model")

    with (
        patch("iac_code.ui.repl.get_pending_update", return_value=update),
        patch("iac_code.ui.repl.render_update_prompt_header", return_value="update prompt"),
        patch("iac_code.ui.repl.Select") as select,
        patch("iac_code.ui.repl.run_update_command", side_effect=OSError("missing executable")) as run_update_command,
    ):
        select.return_value.run.return_value = "update_now"

        assert repl._handle_startup_update() == update

    run_update_command.assert_called_once_with(update)


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_handle_startup_update_recovers_from_unexpected_exception(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    update = make_pending_update()
    repl = InlineREPL(model="test-model")

    with (
        patch("iac_code.ui.repl.get_pending_update", return_value=update),
        patch("iac_code.ui.repl.render_update_prompt_header", return_value="update prompt"),
        patch("iac_code.ui.repl.Select") as select,
        patch("iac_code.ui.repl.run_update_command", side_effect=RuntimeError("unexpected")) as run_update_command,
    ):
        select.return_value.run.return_value = "update_now"

        assert repl._handle_startup_update() == update

    run_update_command.assert_called_once_with(update)


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_start_background_update_checker_delegates_once(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL(model="test-model")

    with patch("iac_code.ui.repl.start_background_update_check") as start_background:
        repl._start_background_update_checker()

    start_background.assert_called_once_with()


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_run_reads_pending_update_then_renders_banner_then_starts_background(mock_mm, mock_ss, mock_pm):
    import asyncio
    from unittest.mock import AsyncMock

    from rich.text import Text

    from iac_code.ui.repl import ExitREPLError, InlineREPL

    repl = InlineREPL(model="test-model")
    repl._prompt_input.get_input = AsyncMock(side_effect=ExitREPLError())

    call_order: list[str] = []

    def _record_get_pending():
        call_order.append("get_pending_update")
        return None

    def _record_render_banner(*args, **kwargs):
        call_order.append("render_welcome_banner")
        return Text("welcome")

    def _record_start_background():
        call_order.append("start_background_update_check")

    with (
        patch("iac_code.ui.repl.get_pending_update", side_effect=_record_get_pending),
        patch("iac_code.ui.repl.render_welcome_banner", side_effect=_record_render_banner),
        patch("iac_code.ui.repl.start_background_update_check", side_effect=_record_start_background),
        patch("iac_code.ui.repl.start_background_housekeeping"),
    ):
        asyncio.run(repl.run())

    assert call_order == [
        "get_pending_update",
        "render_welcome_banner",
        "start_background_update_check",
    ]
