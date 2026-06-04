"""Tests for InlineREPL integration with ProviderManager."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from iac_code.services.update_checker import PendingUpdate
from iac_code.ui.components.select import SelectLayout
from iac_code.utils.project_paths import format_resume_command


@pytest.fixture(autouse=True)
def _force_stdin_tty(monkeypatch):
    """Default to interactive stdin so _handle_startup_update doesn't short-circuit.

    Pytest captures stdin by default which makes ``sys.stdin.isatty()`` return
    False; the non-TTY guard in ``_handle_startup_update`` would otherwise
    skip the prompt under pytest. Individual tests that exercise the non-TTY
    path explicitly re-patch ``sys.stdin``.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def make_pending_update() -> PendingUpdate:
    return PendingUpdate(
        version="1.2.0",
        current_version="1.1.0",
        source="official_pypi",
        checked_at=123.0,
        update_command=(".venv/bin/python", "-m", "pip", "install", "--upgrade", "iac-code"),
        release_notes_url="https://example.test/releases/1.2.0",
    )


def make_session_entry(session_id: str, cwd: str, name: str | None = None):
    from iac_code.services.session_index import SessionEntry

    return SessionEntry(
        session_id=session_id,
        cwd=cwd,
        project_name="repo",
        git_branch=None,
        title=name or session_id,
        mtime=123.0,
        size_bytes=456,
        name=name,
        is_legacy=False,
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


def test_insert_text_delegates_to_prompt_input():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._prompt_input = SimpleNamespace(insert_text=Mock())

    repl._insert_text("hello from history")

    repl._prompt_input.insert_text.assert_called_once_with("hello from history")


def test_history_search_uses_agent_context_messages():
    from iac_code.agent.message import Message
    from iac_code.state.app_state import AppState
    from iac_code.ui.repl import InlineREPL

    captured: dict[str, object] = {}

    class FakeHistorySearch:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            return None

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = SimpleNamespace(get_state=Mock(return_value=AppState(messages=[])))
    repl._agent_loop = SimpleNamespace(
        context_manager=SimpleNamespace(
            get_messages=Mock(return_value=[Message(role="user", content="prompt from agent context")])
        )
    )
    repl._keybinding_manager = object()
    repl._insert_text = Mock()

    with patch("iac_code.ui.dialogs.history_search.HistorySearch", FakeHistorySearch):
        assert repl._open_history_search() is True

    assert captured["messages"] == [{"role": "user", "content": "prompt from agent context"}]


def test_history_search_uses_input_history_when_context_is_empty():
    from iac_code.state.app_state import AppState
    from iac_code.ui.repl import InlineREPL

    captured: dict[str, object] = {}

    class FakeHistorySearch:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            return None

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = SimpleNamespace(get_state=Mock(return_value=AppState(messages=[])))
    repl._agent_loop = SimpleNamespace(context_manager=SimpleNamespace(get_messages=Mock(return_value=[])))
    repl._history = SimpleNamespace(entries=Mock(return_value=["persisted prompt"]))
    repl._keybinding_manager = object()
    repl._insert_text = Mock()

    with patch("iac_code.ui.dialogs.history_search.HistorySearch", FakeHistorySearch):
        assert repl._open_history_search() is True

    assert captured["messages"] == [{"role": "user", "content": "persisted prompt"}]


@pytest.mark.asyncio
async def test_run_once_routes_shell_escape_before_slash_command():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.command_registry = SimpleNamespace(is_command=Mock(return_value=True))
    repl._handle_shell_escape = AsyncMock()
    repl._handle_command = AsyncMock()
    repl._handle_chat = AsyncMock()

    await repl.run_once("!echo hello")

    repl._handle_shell_escape.assert_awaited_once_with("!echo hello")
    repl._handle_command.assert_not_awaited()
    repl._handle_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_routes_normal_chat_unchanged():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.command_registry = SimpleNamespace(is_command=Mock(return_value=False))
    repl._handle_shell_escape = AsyncMock()
    repl._handle_command = AsyncMock()
    repl._handle_chat = AsyncMock()

    await repl.run_once("hello")

    repl._handle_shell_escape.assert_not_awaited()
    repl._handle_command.assert_not_awaited()
    repl._handle_chat.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_handle_command_reports_disabled_skill():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.command_registry = SimpleNamespace(parse=Mock(return_value=("Disabled", [])), get=Mock(return_value=None))
    repl._disabled_skill_commands = {"disabled": object()}
    repl._agent_loop = SimpleNamespace(context_manager=SimpleNamespace(get_messages=Mock(return_value=[])))
    repl._command_log = []
    repl.renderer = SimpleNamespace(print_system_message=Mock())

    await repl._handle_command("$Disabled")

    repl.renderer.print_system_message.assert_called_once()
    message = repl.renderer.print_system_message.call_args.args[0]
    assert "disabled" in message.lower()
    assert "/skills" in message


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_init_does_not_register_disabled_project_skill(mock_mm, mock_ss, mock_pm, monkeypatch):
    from iac_code.skills.frontmatter import SkillFrontmatter
    from iac_code.skills.skill_definition import SkillDefinition
    from iac_code.types.skill_source import SkillSource
    from iac_code.ui.repl import InlineREPL

    project_skill = SkillDefinition(
        name="project-skill",
        description="Project skill",
        frontmatter=SkillFrontmatter(description="Project skill"),
        content="Body",
        source=SkillSource.PROJECT,
    )
    monkeypatch.setattr("iac_code.skills.discovery.discover_all_skills", lambda cwd: [project_skill])
    monkeypatch.setattr("iac_code.skills.settings.load_disabled_skills", lambda: {"project-skill"})

    repl = InlineREPL(model="test-model")

    assert repl.command_registry.get("project-skill") is None
    assert "project-skill" in repl._disabled_skill_commands


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_refresh_skills_updates_agent_loop_auto_trigger_skills(mock_mm, mock_ss, mock_pm, monkeypatch):
    from iac_code.skills.frontmatter import SkillFrontmatter
    from iac_code.skills.skill_definition import SkillDefinition
    from iac_code.types.skill_source import SkillSource
    from iac_code.ui.repl import InlineREPL

    disabled: set[str] = set()
    project_skill = SkillDefinition(
        name="project-skill",
        description="Project skill",
        frontmatter=SkillFrontmatter(description="Project skill", auto_trigger={"script": "auto_trigger.py"}),
        content="Body",
        source=SkillSource.PROJECT,
    )
    monkeypatch.setattr("iac_code.skills.discovery.discover_all_skills", lambda cwd: [project_skill])
    monkeypatch.setattr("iac_code.skills.settings.load_disabled_skills", lambda: disabled)

    repl = InlineREPL(model="test-model")
    assert any(command.name == "project-skill" for command in repl._agent_loop._auto_trigger_skills)

    disabled.add("project-skill")
    repl.refresh_skills()

    assert all(command.name != "project-skill" for command in repl._agent_loop._auto_trigger_skills)


def test_repl_rename_current_session_updates_storage_and_name():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl._session_id = "session-123"
    repl._session_storage = Mock()
    repl.current_git_branch = Mock(return_value="main")
    repl._load_current_session_name = Mock(return_value="deploy-prod")

    result = repl.rename_current_session("deploy-prod")

    assert result == repl._session_storage.rename_session.return_value
    repl._session_storage.rename_session.assert_called_once_with(
        "/repo",
        "session-123",
        "deploy-prod",
        git_branch="main",
    )
    repl._load_current_session_name.assert_called_once_with()
    assert repl._session_name == "deploy-prod"


def test_swap_session_refreshes_session_name_and_renders_banner():
    from iac_code.state.app_state import AppState
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl._session_id = "old-session"
    repl._session_storage = SimpleNamespace(
        load=Mock(return_value=[]),
        repair_interrupted=Mock(return_value=[]),
    )
    repl._agent_loop = SimpleNamespace(replace_session=Mock())
    repl._load_current_session_name = Mock(return_value="deploy-prod")
    repl.store = SimpleNamespace(get_state=Mock(return_value=AppState(model="test-model", cwd="/repo")))
    repl.console = SimpleNamespace(file=SimpleNamespace(write=Mock(), flush=Mock()), print=Mock())
    repl.renderer = SimpleNamespace(replay_history=Mock())

    with patch("iac_code.ui.repl.render_welcome_banner", return_value="banner") as render_welcome_banner:
        repl.swap_session("new-session")

    assert repl._session_name == "deploy-prod"
    repl._load_current_session_name.assert_called_once_with()
    render_welcome_banner.assert_called_once_with(
        "test-model",
        "/repo",
        session_id="new-session",
        session_name="deploy-prod",
    )
    repl.console.print.assert_called_once_with("banner")


def test_swap_session_refreshes_session_trusted_read_directories(monkeypatch, tmp_path):
    from iac_code.state.app_state import AppState
    from iac_code.types.permissions import ToolPermissionContext
    from iac_code.ui.repl import InlineREPL

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    old_roots = [
        str(tmp_path / "config" / "tool-results" / "old"),
        str(tmp_path / "config" / "image-cache" / "old"),
    ]
    custom_root = "/custom/trusted"
    permission_context = ToolPermissionContext(
        cwd=str(tmp_path),
        trusted_read_directories=[*old_roots, custom_root],
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = str(tmp_path)
    repl._session_id = "old"
    repl._session_storage = SimpleNamespace(
        load=Mock(return_value=[]),
        repair_interrupted=Mock(return_value=[]),
    )
    repl._agent_loop = SimpleNamespace(replace_session=Mock())
    repl._load_current_session_name = Mock(return_value=None)
    repl.store = SimpleNamespace(
        get_state=Mock(
            return_value=AppState(model="test-model", cwd=str(tmp_path), permission_context=permission_context)
        )
    )
    repl.console = SimpleNamespace(file=SimpleNamespace(write=Mock(), flush=Mock()), print=Mock())
    repl.renderer = SimpleNamespace(replay_history=Mock())

    repl.swap_session("new")

    roots = permission_context.trusted_read_directories
    assert old_roots[0] not in roots
    assert old_roots[1] not in roots
    assert str(tmp_path / "config" / "tool-results" / "new") in roots
    assert str(tmp_path / "config" / "image-cache" / "new") in roots
    assert custom_root in roots


def test_print_exit_text_uses_session_name_and_prints_session_id():
    from rich.text import Text

    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._session_id = "abc123"
    repl._session_name = "deploy-prod"
    repl.console = SimpleNamespace(print=Mock())

    repl._print_exit_text()

    printed = [call.args[0] for call in repl.console.print.call_args_list]
    assert "[dim]Goodbye![/dim]" in printed
    assert any(isinstance(item, Text) and "iac-code --resume deploy-prod" in item.plain for item in printed)
    assert any(isinstance(item, Text) and "Session ID: abc123" in item.plain for item in printed)


@pytest.mark.asyncio
async def test_prompt_for_session_name_retries_until_valid():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._prompt_input = SimpleNamespace(get_input=AsyncMock(side_effect=[" ", "bad name", "deploy-prod"]))
    repl.renderer = SimpleNamespace(print_system_message=Mock())

    result = await repl.prompt_for_session_name()

    assert result == "deploy-prod"
    assert repl._prompt_input.get_input.await_count == 3
    assert repl.renderer.print_system_message.call_count == 2
    styles = [call.kwargs["style"] for call in repl.renderer.print_system_message.call_args_list]
    assert styles == ["red", "red"]


def test_resolve_session_id_continue_returns_latest_current_project_session():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl._session_storage = SimpleNamespace(get_latest_session_anywhere=Mock(return_value=("/repo", "latest-id")))

    assert repl._resolve_session_id(True) == "latest-id"
    repl._session_storage.get_latest_session_anywhere.assert_called_once_with()


def test_resolve_session_id_continue_accepts_windows_equivalent_cwd():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = r"C:\Users\Me\Repo"
    repl._session_storage = SimpleNamespace(
        get_latest_session_anywhere=Mock(return_value=("c:/Users/Me/Repo", "latest-id"))
    )

    assert repl._resolve_session_id(True) == "latest-id"


def test_resolve_session_id_continue_cross_project_raises_with_hint():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl._session_storage = SimpleNamespace(
        get_latest_session_anywhere=Mock(return_value=("/elsewhere/repo", "latest-id"))
    )

    with pytest.raises(ValueError) as exc_info:
        repl._resolve_session_id(True)

    assert format_resume_command("/elsewhere/repo", "latest-id") in str(exc_info.value)


def test_cross_project_message_uses_windows_resume_command(monkeypatch):
    import iac_code.utils.project_paths as project_paths
    from iac_code.ui.repl import InlineREPL

    monkeypatch.setattr(project_paths.sys, "platform", "win32")

    message = InlineREPL._cross_project_message(r"C:\Users\Me\iac repo & unsafe", "abc123")

    assert r'cd /d "C:\Users\Me\iac repo & unsafe" && iac-code --resume abc123' in message


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_accepted_when_session_exists(mock_mm, mock_ss, mock_pm):
    from iac_code.services.session_resolver import ResolutionStatus, SessionResolution
    from iac_code.ui.repl import InlineREPL

    existing_id = "99646984-35a9-4850-b72a-4131a1690774"
    mock_ss.return_value.load.return_value = []
    mock_ss.return_value.repair_interrupted.return_value = []
    with patch(
        "iac_code.ui.repl.resolve_session_argument",
        return_value=SessionResolution(
            status=ResolutionStatus.FOUND,
            entry=make_session_entry(existing_id, str(Path.cwd())),
        ),
    ):
        repl = InlineREPL(model="test-model", resume_session_id=existing_id)
    assert repl.session_id == existing_id


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_raises_when_session_missing(mock_mm, mock_ss, mock_pm):
    from iac_code.services.session_resolver import ResolutionStatus, SessionResolution
    from iac_code.ui.repl import InlineREPL

    with (
        patch(
            "iac_code.ui.repl.resolve_session_argument",
            return_value=SessionResolution(status=ResolutionStatus.NOT_FOUND),
        ),
        pytest.raises(ValueError, match="Session not found"),
    ):
        InlineREPL(model="test-model", resume_session_id="no-such-id")


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_cross_project_raises_with_hint(mock_mm, mock_ss, mock_pm):
    """A resume id resolved in a different project must surface the cd command."""
    from iac_code.services.session_resolver import ResolutionStatus, SessionResolution
    from iac_code.ui.repl import InlineREPL

    with (
        patch(
            "iac_code.ui.repl.resolve_session_argument",
            return_value=SessionResolution(
                status=ResolutionStatus.FOUND,
                entry=make_session_entry("some-id", "/elsewhere/repo"),
            ),
        ),
        pytest.raises(ValueError) as exc_info,
    ):
        InlineREPL(model="test-model", resume_session_id="some-id")

    assert format_resume_command("/elsewhere/repo", "some-id") in str(exc_info.value)


def test_resolve_session_id_accepts_current_project_name():
    from iac_code.services.session_resolver import ResolutionStatus, SessionResolution
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl.session_index = object()

    with patch(
        "iac_code.ui.repl.resolve_session_argument",
        return_value=SessionResolution(
            status=ResolutionStatus.FOUND,
            entry=make_session_entry("abc123", repl._original_cwd, name="deploy-prod"),
        ),
    ) as resolve_session_argument:
        result = repl._resolve_session_id("deploy-prod")

    assert result == "abc123"
    resolve_session_argument.assert_called_once_with(repl.session_index, repl._original_cwd, "deploy-prod")


def test_resolve_session_id_ambiguous_name_raises_candidates():
    from iac_code.services.session_resolver import ResolutionStatus, SessionResolution
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl.session_index = object()
    candidates = [
        make_session_entry("abc123", "/repo", name="deploy-prod"),
        make_session_entry("def456", "/elsewhere/repo", name="deploy-prod"),
    ]

    with (
        patch(
            "iac_code.ui.repl.resolve_session_argument",
            return_value=SessionResolution(status=ResolutionStatus.AMBIGUOUS_NAME, candidates=candidates),
        ),
        pytest.raises(ValueError) as exc_info,
    ):
        repl._resolve_session_id("deploy-prod")

    message = str(exc_info.value)
    assert "Multiple sessions match" in message
    assert "abc123" in message
    assert "def456" in message
    assert format_resume_command("/repo", "abc123") in message
    assert format_resume_command("/elsewhere/repo", "def456") in message


def test_printed_session_name_resume_command_resolves_to_session_id():
    from rich.text import Text

    from iac_code.services.session_resolver import ResolutionStatus, SessionResolution
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = "/repo"
    repl._session_id = "abc123"
    repl._session_name = "deploy-prod"
    repl.session_index = object()
    repl.console = SimpleNamespace(print=Mock())

    repl._print_exit_text()
    command = next(
        item.plain
        for call in repl.console.print.call_args_list
        for item in call.args
        if isinstance(item, Text) and item.plain.startswith("iac-code --resume ")
    )
    resume_arg = command.rsplit(" ", 1)[-1]

    with patch(
        "iac_code.ui.repl.resolve_session_argument",
        return_value=SessionResolution(
            status=ResolutionStatus.FOUND,
            entry=make_session_entry("abc123", repl._original_cwd, name="deploy-prod"),
        ),
    ):
        assert repl._resolve_session_id(resume_arg) == "abc123"


@pytest.mark.asyncio
async def test_rename_error_result_prints_red_and_records_error():
    from iac_code.commands.registry import LocalCommand
    from iac_code.commands.rename import rename_command
    from iac_code.state.app_state import AppState
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.command_registry = SimpleNamespace(
        parse=Mock(return_value=("rename", ["-bad"])),
        get=Mock(return_value=LocalCommand(name="rename", description="Rename", handler=rename_command)),
    )
    repl.renderer = SimpleNamespace(print_system_message=Mock(), print_command_result=Mock())
    repl.console = SimpleNamespace()
    repl._agent_loop = SimpleNamespace(context_manager=SimpleNamespace(get_messages=Mock(return_value=[])))
    repl._command_log = []
    repl.store = SimpleNamespace(get_state=Mock(return_value=AppState(model="test-model", cwd="/repo")))
    repl._refresh_banner = Mock()
    repl.rename_current_session = Mock()

    await repl._handle_command("/rename -bad")

    repl.renderer.print_system_message.assert_called_once()
    assert repl.renderer.print_system_message.call_args.kwargs["style"] == "red"
    repl.renderer.print_command_result.assert_not_called()
    assert repl._command_log[-1][0] == "/rename -bad"
    assert repl._command_log[-1][3] is True
    repl._refresh_banner.assert_not_called()


@pytest.mark.asyncio
async def test_rename_success_refreshes_banner():
    from iac_code.commands.registry import LocalCommand
    from iac_code.commands.rename import rename_command
    from iac_code.state.app_state import AppState
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.command_registry = SimpleNamespace(
        parse=Mock(return_value=("rename", ["deploy-prod"])),
        get=Mock(return_value=LocalCommand(name="rename", description="Rename", handler=rename_command)),
    )
    repl.renderer = SimpleNamespace(print_system_message=Mock(), print_command_result=Mock())
    repl.console = SimpleNamespace()
    repl._agent_loop = SimpleNamespace(context_manager=SimpleNamespace(get_messages=Mock(return_value=[])))
    repl._command_log = []
    repl.store = SimpleNamespace(get_state=Mock(return_value=AppState(model="test-model", cwd="/repo")))
    repl._refresh_banner = Mock()
    repl.rename_current_session = Mock(return_value="renamed")

    await repl._handle_command("/rename deploy-prod")

    repl._refresh_banner.assert_called_once_with()
    repl.renderer.print_command_result.assert_not_called()
    assert repl._command_log[-1][0] == "/rename deploy-prod"
    assert repl._command_log[-1][3] is False


@pytest.mark.asyncio
async def test_rename_unchanged_does_not_refresh_banner():
    from iac_code.commands.registry import LocalCommand
    from iac_code.commands.rename import rename_command
    from iac_code.state.app_state import AppState
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.command_registry = SimpleNamespace(
        parse=Mock(return_value=("rename", ["deploy-prod"])),
        get=Mock(return_value=LocalCommand(name="rename", description="Rename", handler=rename_command)),
    )
    repl.renderer = SimpleNamespace(print_system_message=Mock(), print_command_result=Mock())
    repl.console = SimpleNamespace()
    repl._agent_loop = SimpleNamespace(context_manager=SimpleNamespace(get_messages=Mock(return_value=[])))
    repl._command_log = []
    repl.store = SimpleNamespace(get_state=Mock(return_value=AppState(model="test-model", cwd="/repo")))
    repl._refresh_banner = Mock()
    repl.rename_current_session = Mock(return_value="unchanged")

    await repl._handle_command("/rename deploy-prod")

    repl._refresh_banner.assert_not_called()
    repl.renderer.print_command_result.assert_called_once()
    assert repl._command_log[-1][0] == "/rename deploy-prod"
    assert repl._command_log[-1][3] is False


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
        patch("iac_code.services.telemetry.graceful_shutdown") as graceful_shutdown,
    ):
        select.return_value.run.return_value = "update_now"

        with pytest.raises(SystemExit) as exc_info:
            repl._handle_startup_update()

    assert exc_info.value.code == 0
    run_update_command.assert_called_once_with(update)
    graceful_shutdown.assert_called_once_with()


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_handle_startup_update_returns_none_when_stdin_not_tty(mock_mm, mock_ss, mock_pm):
    """Non-TTY callers (CI, container without TTY) must never hit Select.run().

    Without this guard, a cached pending update would block the process
    indefinitely waiting for keyboard input on a closed stdin.
    """
    from iac_code.ui.repl import InlineREPL

    update = make_pending_update()
    repl = InlineREPL(model="test-model")

    with (
        patch("iac_code.ui.repl.sys.stdin") as stdin,
        patch("iac_code.ui.repl.get_pending_update", return_value=update) as get_pending,
        patch("iac_code.ui.repl.Select") as select,
    ):
        stdin.isatty.return_value = False
        assert repl._handle_startup_update() is None

    get_pending.assert_not_called()
    select.assert_not_called()


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
