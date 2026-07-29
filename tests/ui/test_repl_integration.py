"""Tests for InlineREPL integration with ProviderManager."""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from iac_code.services.session_backup import BackupReason
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


def _current_time_line(prompt: str) -> str:
    return next(line for line in prompt.splitlines() if line.startswith("- Current time: "))


@pytest.mark.asyncio
async def test_thinking_enabled_command_reconfigures_active_repl_provider(tmp_path, monkeypatch):
    from iac_code.commands.thinking_enabled import thinking_enabled_command
    from iac_code.config import get_settings_path, load_active_provider_config
    from iac_code.state.app_state import AppState, AppStateStore
    from iac_code.ui.repl import InlineREPL

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = get_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        "activeProvider: openai\nproviders:\n  openai:\n    name: OpenAI\n    model: gpt-5.5\n",
        encoding="utf-8",
    )
    repl = InlineREPL.__new__(InlineREPL)
    repl.store = AppStateStore(AppState(model="gpt-5.5"))
    repl._current_model = "gpt-5.5"
    repl._current_provider_config = load_active_provider_config()
    repl._provider_key_override = None
    repl._base_url_override = None
    repl._load_credentials = Mock(return_value={"openai": "key"})
    repl._provider_manager = Mock()
    repl._refresh_system_prompt = Mock()
    repl.store.subscribe(repl._on_state_change)
    context = SimpleNamespace(store=repl.store, console=None, repl=repl)

    result = await thinking_enabled_command(context=context, args=["off"])

    assert "disabled" in result
    assert load_active_provider_config()["thinkingEnabled"] is False
    repl._provider_manager.reconfigure.assert_called_once()


@pytest.mark.asyncio
async def test_repl_handle_command_routes_thinking_enabled_toggle(tmp_path, monkeypatch):
    from iac_code.commands import create_default_registry
    from iac_code.config import get_settings_path, load_active_provider_config
    from iac_code.state.app_state import AppState, AppStateStore
    from iac_code.ui.repl import InlineREPL

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = get_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        "activeProvider: openai\nproviders:\n  openai:\n    name: OpenAI\n    model: gpt-5.5\n",
        encoding="utf-8",
    )
    repl = InlineREPL.__new__(InlineREPL)
    repl.command_registry = create_default_registry()
    repl._disabled_skill_commands = {}
    repl._command_log = []
    repl.console = None
    repl.store = AppStateStore(AppState(model="gpt-5.5"))
    repl._current_model = "gpt-5.5"
    repl._current_provider_config = load_active_provider_config()
    repl._provider_key_override = None
    repl._base_url_override = None
    repl._load_credentials = Mock(return_value={"openai": "key"})
    repl._provider_manager = Mock()
    repl._refresh_system_prompt = Mock()
    repl.renderer = SimpleNamespace(print_system_message=Mock(), print_command_result=Mock())
    repl.store.subscribe(repl._on_state_change)

    queued = await repl._handle_command("/thinking_enabled off")

    assert queued == []
    assert load_active_provider_config()["thinkingEnabled"] is False
    repl._provider_manager.reconfigure.assert_called_once()
    repl.renderer.print_command_result.assert_called_once()


class TestREPLProviderIntegration:
    def test_init_failure_closes_only_owned_aliyun_services(self, monkeypatch):
        from iac_code.tools.cloud.aliyun import runtime as aliyun_runtime
        from iac_code.ui.repl import InlineREPL

        owned_services = SimpleNamespace(aclose=AsyncMock())
        external_services = SimpleNamespace(aclose=AsyncMock())
        monkeypatch.setattr(aliyun_runtime, "create_aliyun_runtime_services", lambda **kwargs: owned_services)

        def fail_initialize(self, **kwargs):
            del self, kwargs
            raise RuntimeError("init failed")

        monkeypatch.setattr(InlineREPL, "_initialize", fail_initialize)

        with pytest.raises(RuntimeError, match="init failed"):
            InlineREPL(model="test-model")
        with pytest.raises(RuntimeError, match="init failed"):
            InlineREPL(model="test-model", aliyun_services=external_services)

        owned_services.aclose.assert_awaited_once()
        external_services.aclose.assert_not_awaited()

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_init_creates_provider_manager(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert hasattr(repl, "_provider_manager")

    @patch("iac_code.ui.repl.SessionBackupService")
    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_init_default_backup_service_reuses_session_storage(self, mock_mm, mock_ss, mock_pm, mock_backup):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")

        mock_backup.assert_called_once_with(session_storage=mock_ss.return_value)
        assert repl._backup_service is mock_backup.return_value

    @patch("iac_code.ui.repl.SessionBackupService")
    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_init_keeps_injected_backup_service(self, mock_mm, mock_ss, mock_pm, mock_backup):
        from iac_code.ui.repl import InlineREPL

        backup_service = Mock()

        repl = InlineREPL(model="claude-sonnet-4-6", backup_service=backup_service)

        mock_backup.assert_not_called()
        assert repl._backup_service is backup_service

    @patch("iac_code.ui.repl.SessionBackupService")
    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_init_keeps_falsey_injected_backup_service(self, mock_mm, mock_ss, mock_pm, mock_backup):
        from iac_code.ui.repl import InlineREPL

        class FalseyBackupService:
            def __bool__(self) -> bool:
                return False

        backup_service = FalseyBackupService()

        repl = InlineREPL(model="claude-sonnet-4-6", backup_service=backup_service)

        mock_backup.assert_not_called()
        assert repl._backup_service is backup_service

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

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_mcp_tools_registered_in_interactive_repl(self, mock_mm, mock_ss, mock_pm, monkeypatch, tmp_path):
        from iac_code.mcp.types import MCPToolRecord
        from iac_code.ui.repl import InlineREPL

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        (tmp_path / ".iac-code").mkdir()
        (tmp_path / ".iac-code" / "settings.local.yml").write_text(
            "mcpServers:\n  ros:\n    command: uvx\n",
            encoding="utf-8",
        )
        manager = SimpleNamespace(
            connect_all=AsyncMock(),
            list_tools=Mock(
                return_value=[
                    MCPToolRecord(
                        server_name="ros",
                        tool_name="plan",
                        public_name="mcp__ros__plan",
                        input_schema={"type": "object"},
                    )
                ]
            ),
            list_resources=Mock(return_value=[]),
            list_prompts=Mock(return_value=[]),
            needs_auth_servers=Mock(return_value=[]),
            set_elicitation_handler=Mock(),
        )
        monkeypatch.setattr("iac_code.mcp.manager.MCPManager", lambda configs, roots, **kwargs: manager)

        repl = InlineREPL(model="claude-sonnet-4-6")

        assert manager.connect_all.await_count == 1
        assert repl.tool_registry.get("mcp__ros__plan") is not None

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_mcp_init_failure_disconnects_manager(self, mock_mm, mock_ss, mock_pm, monkeypatch, tmp_path):
        from iac_code.ui.repl import InlineREPL

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        (tmp_path / ".iac-code").mkdir()
        (tmp_path / ".iac-code" / "settings.local.yml").write_text(
            "mcpServers:\n  ros:\n    command: uvx\n",
            encoding="utf-8",
        )
        manager = SimpleNamespace(
            connect_all=AsyncMock(),
            disconnect_all=AsyncMock(),
            list_tools=Mock(side_effect=RuntimeError("tool sync failed")),
            list_resources=Mock(return_value=[]),
            list_prompts=Mock(return_value=[]),
            needs_auth_servers=Mock(return_value=[]),
            set_elicitation_handler=Mock(),
        )
        monkeypatch.setattr("iac_code.mcp.manager.MCPManager", lambda configs, roots, **kwargs: manager)

        with pytest.raises(RuntimeError, match="tool sync failed"):
            InlineREPL(model="claude-sonnet-4-6")

        manager.disconnect_all.assert_awaited_once()

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_refresh_skills_preserves_mcp_prompt_commands(self, mock_mm, mock_ss, mock_pm, monkeypatch, tmp_path):
        from iac_code.mcp.types import MCPPromptRecord
        from iac_code.ui.repl import InlineREPL

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        (tmp_path / ".iac-code").mkdir()
        (tmp_path / ".iac-code" / "settings.local.yml").write_text(
            "mcpServers:\n  ros:\n    command: uvx\n",
            encoding="utf-8",
        )
        manager = SimpleNamespace(
            connect_all=AsyncMock(),
            list_tools=Mock(return_value=[]),
            list_resources=Mock(return_value=[]),
            list_prompts=Mock(
                return_value=[
                    MCPPromptRecord(
                        server_name="ros",
                        prompt_name="review",
                        public_name="mcp__ros__review",
                        description="Review template",
                    )
                ]
            ),
            needs_auth_servers=Mock(return_value=[]),
            add_change_listener=Mock(),
            set_elicitation_handler=Mock(),
        )
        monkeypatch.setattr("iac_code.mcp.manager.MCPManager", lambda configs, roots, **kwargs: manager)

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert repl.command_registry.get("mcp__ros__review") is not None

        repl.refresh_skills()

        assert repl.command_registry.get("mcp__ros__review") is not None
        assert "mcp__ros__review" in repl._skill_listing

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_mcp_prompts_list_changed_removes_deleted_prompt_command(
        self,
        mock_mm,
        mock_ss,
        mock_pm,
        monkeypatch,
        tmp_path,
    ):
        from iac_code.mcp.types import MCPPromptRecord
        from iac_code.ui.repl import InlineREPL

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        (tmp_path / ".iac-code").mkdir()
        (tmp_path / ".iac-code" / "settings.local.yml").write_text(
            "mcpServers:\n  ros:\n    command: uvx\n",
            encoding="utf-8",
        )
        listener_holder = {}
        manager = SimpleNamespace(
            connect_all=AsyncMock(),
            list_tools=Mock(return_value=[]),
            list_resources=Mock(return_value=[]),
            list_prompts=Mock(
                return_value=[
                    MCPPromptRecord(
                        server_name="ros",
                        prompt_name="review",
                        public_name="mcp__ros__review",
                        description="Review template",
                    )
                ]
            ),
            list_connections=Mock(return_value=[]),
            needs_auth_servers=Mock(return_value=[]),
            add_change_listener=Mock(side_effect=lambda listener: listener_holder.setdefault("listener", listener)),
            set_elicitation_handler=Mock(),
        )
        monkeypatch.setattr("iac_code.mcp.manager.MCPManager", lambda configs, roots, **kwargs: manager)

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert repl.command_registry.get("mcp__ros__review") is not None
        assert "mcp__ros__review" in repl.command_registry.get_completions("mcp__ros")

        manager.list_prompts.return_value = []
        asyncio.run(listener_holder["listener"]("ros", "prompts"))

        assert repl.command_registry.get("mcp__ros__review") is None
        assert "mcp__ros__review" not in repl.command_registry.get_completions("mcp__ros")

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_repl_prompts_for_project_mcp_approval_before_connecting(
        self,
        mock_mm,
        mock_ss,
        mock_pm,
        monkeypatch,
        tmp_path,
    ):
        from iac_code.mcp.types import MCPToolRecord
        from iac_code.ui.repl import InlineREPL

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        (tmp_path / ".git").mkdir()
        (tmp_path / ".mcp.json").write_text(
            '{"mcpServers": {"project-ros": {"command": "uvx"}}}',
            encoding="utf-8",
        )
        connected_names = []
        prompts = []
        manager = SimpleNamespace(
            connect_all=AsyncMock(),
            list_tools=Mock(
                return_value=[
                    MCPToolRecord(
                        server_name="project-ros",
                        tool_name="plan",
                        public_name="mcp__project_ros__plan",
                        input_schema={"type": "object"},
                    )
                ]
            ),
            list_resources=Mock(return_value=[]),
            list_prompts=Mock(return_value=[]),
            needs_auth_servers=Mock(return_value=[]),
            set_elicitation_handler=Mock(),
        )

        def make_manager(configs, roots, **kwargs):
            connected_names.extend(config.name for config in configs)
            return manager

        prompt_kwargs = []

        def approve_input(console, prompt, **kwargs):
            prompts.append(prompt)
            prompt_kwargs.append(kwargs)
            return "y"

        monkeypatch.setattr("iac_code.mcp.manager.MCPManager", make_manager)
        monkeypatch.setattr("iac_code.ui.repl.Console.input", approve_input)

        repl = InlineREPL(model="claude-sonnet-4-6")

        assert prompts == ["Approve project MCP server 'project-ros' from {}? [y/N] ".format(tmp_path / ".mcp.json")]
        assert prompt_kwargs == [{"markup": False}]
        assert connected_names == ["project-ros"]
        assert manager.connect_all.await_count == 1
        assert repl.tool_registry.get("mcp__project_ros__plan") is not None


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
def test_repl_init_reuses_runtime_current_time_for_refresher(mock_mm, mock_ss, mock_pm, tmp_path, monkeypatch):
    from datetime import datetime as real_datetime

    from iac_code.agent import system_prompt
    from iac_code.ui import repl as repl_module
    from iac_code.ui.repl import InlineREPL

    class FakeDateTime:
        calls = 0

        @classmethod
        def now(cls):
            cls.calls += 1
            return real_datetime(2026, 6, 5, 10, cls.calls, 0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(system_prompt, "datetime", FakeDateTime)
    monkeypatch.setattr(repl_module, "datetime", FakeDateTime, raising=False)

    repl = InlineREPL(model="qwen3.7-max")

    initial_line = _current_time_line(repl._agent_loop.system_prompt)
    refreshed_line = _current_time_line(repl._build_current_system_prompt())

    assert refreshed_line == initial_line


def test_insert_text_delegates_to_prompt_input():
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._prompt_input = SimpleNamespace(insert_text=Mock())

    repl._insert_text("hello from history")

    repl._prompt_input.insert_text.assert_called_once_with("hello from history")


def test_session_dir_for_artifacts_returns_none_for_legacy_directory_without_layout(tmp_path, monkeypatch):
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    cwd = str(tmp_path)
    storage = SessionStorage()
    session_dir = storage.session_dir(cwd, "legacy-session")
    session_dir.mkdir(parents=True)
    repl = InlineREPL.__new__(InlineREPL)
    repl._session_storage = storage
    repl._original_cwd = cwd
    repl._session_id = "legacy-session"

    assert repl._session_dir_for_artifacts() is None
    assert repl._result_storage_dir_for_session() is None


def test_session_dir_for_artifacts_ignores_mock_storage_paths(tmp_path, monkeypatch):
    from iac_code.ui.repl import InlineREPL

    monkeypatch.chdir(tmp_path)
    repl = InlineREPL.__new__(InlineREPL)
    repl._session_storage = MagicMock()
    repl._original_cwd = str(tmp_path)
    repl._session_id = "mock-session"

    assert repl._session_dir_for_artifacts() is None
    assert repl._raw_session_dir_for_trusted_roots() is None
    assert not (tmp_path / "MagicMock").exists()


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


def test_history_search_hides_internal_skill_context_messages():
    from iac_code.agent.message import Message
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._history = None
    repl._agent_loop = SimpleNamespace(
        context_manager=SimpleNamespace(
            get_messages=Mock(
                return_value=[
                    Message(role="user", content="继续"),
                    Message(
                        role="user",
                        content="<skill-name>iac-aliyun</skill-name>\n\nBase directory for this skill: /tmp/skill",
                    ),
                ]
            )
        )
    )

    assert repl._history_search_messages() == [{"role": "user", "content": "继续"}]


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
async def test_handle_chat_returns_queued_inputs_from_streaming_renderer():
    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    async def events():
        if False:
            yield None

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = SimpleNamespace(set_state=Mock())
    repl.renderer = SimpleNamespace(
        record_user_turn=Mock(),
        run_streaming_output=AsyncMock(return_value=StreamingOutputResult(elapsed=0.2, queued_inputs=["next turn"])),
        prompt_permission=AsyncMock(),
        _last_streaming_errors=[],
    )
    repl._agent_loop = SimpleNamespace(
        run_streaming=Mock(return_value=events()),
        stamp_last_turn_elapsed=Mock(),
        context_manager=SimpleNamespace(get_messages=Mock(return_value=[])),
    )
    repl._backup_service = SimpleNamespace(backup_session=Mock())
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._streaming_error_log = []

    queued_inputs = await repl._handle_chat("first turn")

    assert queued_inputs == ["next turn"]
    args, kwargs = repl._agent_loop.run_streaming.call_args
    assert args == ("first turn",)
    assert callable(kwargs["queued_input_provider"])
    _, renderer_kwargs = repl.renderer.run_streaming_output.call_args
    assert renderer_kwargs["streaming_input"] is not None
    repl.renderer.record_user_turn.assert_called_once_with("first turn")
    repl._backup_service.backup_session.assert_called_once_with(
        "/repo",
        "session-1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )


@pytest.mark.asyncio
async def test_handle_chat_backup_failure_warns_and_preserves_queued_inputs():
    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    async def events():
        if False:
            yield None

    def raise_backup(*args, **kwargs):
        raise RuntimeError("/mnt/oss/customer-bucket/repl-session source failed")

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = SimpleNamespace(set_state=Mock())
    repl.renderer = SimpleNamespace(
        record_user_turn=Mock(),
        run_streaming_output=AsyncMock(return_value=StreamingOutputResult(elapsed=0.2, queued_inputs=["next turn"])),
        prompt_permission=AsyncMock(),
        _last_streaming_errors=[],
    )
    repl._agent_loop = SimpleNamespace(
        run_streaming=Mock(return_value=events()),
        stamp_last_turn_elapsed=Mock(),
        context_manager=SimpleNamespace(get_messages=Mock(return_value=[])),
    )
    repl._backup_service = SimpleNamespace(backup_session=Mock(side_effect=raise_backup))
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._streaming_error_log = []

    with patch("iac_code.ui.repl.logger.warning") as warning:
        queued_inputs = await repl._handle_chat("first turn")

    assert queued_inputs == ["next turn"]
    repl.store.set_state.assert_any_call(is_busy=False)
    warning.assert_called_once()
    assert warning.call_args.args == (
        "Normal REPL session backup failed (reason={}, retry_count={}, error_type={})",
        "normal_turn_end",
        0,
        "RuntimeError",
    )
    assert "/mnt/oss/customer-bucket" not in " ".join(str(arg) for arg in warning.call_args.args)


@pytest.mark.asyncio
async def test_handle_chat_non_critical_backup_result_failure_warns():
    from iac_code.services.session_backup import BackupResult
    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    async def events():
        if False:
            yield None

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = SimpleNamespace(set_state=Mock())
    repl.renderer = SimpleNamespace(
        record_user_turn=Mock(),
        run_streaming_output=AsyncMock(return_value=StreamingOutputResult(elapsed=0.2, queued_inputs=[])),
        prompt_permission=AsyncMock(),
        _last_streaming_errors=[],
    )
    repl._agent_loop = SimpleNamespace(
        run_streaming=Mock(return_value=events()),
        stamp_last_turn_elapsed=Mock(),
        context_manager=SimpleNamespace(get_messages=Mock(return_value=[])),
    )
    repl._backup_service = SimpleNamespace(
        backup_session=Mock(return_value=BackupResult(enabled=True, succeeded=False, error="[PATH]", retry_count=2))
    )
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._streaming_error_log = []

    with patch("iac_code.ui.repl.logger.warning") as warning:
        queued_inputs = await repl._handle_chat("first turn")

    assert queued_inputs == []
    warning.assert_called_once()
    assert warning.call_args.args == (
        "Normal REPL session backup failed (reason={}, retry_count={}): {}",
        "normal_turn_end",
        2,
        "[PATH]",
    )


@pytest.mark.asyncio
async def test_handle_chat_interrupted_result_does_not_backup():
    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    async def events():
        if False:
            yield None

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = SimpleNamespace(set_state=Mock())
    repl.renderer = SimpleNamespace(
        record_user_turn=Mock(),
        run_streaming_output=AsyncMock(
            return_value=StreamingOutputResult(
                elapsed=0.2,
                queued_inputs=["next turn"],
                draft_input="half typed",
                interrupted=True,
            )
        ),
        prompt_permission=AsyncMock(),
        _last_streaming_errors=[],
    )
    repl._agent_loop = SimpleNamespace(
        run_streaming=Mock(return_value=events()),
        stamp_last_turn_elapsed=Mock(),
        context_manager=SimpleNamespace(get_messages=Mock(return_value=[])),
    )
    repl._backup_service = SimpleNamespace(backup_session=Mock())
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._streaming_error_log = []
    repl._prune_cleanup_prompts_if_no_pending_cleanup = Mock()

    queued_inputs = await repl._handle_chat("first turn")

    assert queued_inputs == ["next turn"]
    assert repl._streaming_draft_input == "half typed"
    repl._backup_service.backup_session.assert_not_called()
    repl._prune_cleanup_prompts_if_no_pending_cleanup.assert_called_once_with()
    repl.store.set_state.assert_any_call(is_busy=False)


@pytest.mark.parametrize("exc_type", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.asyncio
async def test_handle_chat_propagates_interrupts_without_warning_or_backup(exc_type):
    from iac_code.ui.repl import InlineREPL

    async def events():
        if False:
            yield None

    repl = InlineREPL.__new__(InlineREPL)
    repl.store = SimpleNamespace(set_state=Mock())
    repl.renderer = SimpleNamespace(
        record_user_turn=Mock(),
        run_streaming_output=AsyncMock(side_effect=exc_type()),
        prompt_permission=AsyncMock(),
        _last_streaming_errors=[],
    )
    repl._agent_loop = SimpleNamespace(
        run_streaming=Mock(return_value=events()),
        stamp_last_turn_elapsed=Mock(),
        context_manager=SimpleNamespace(get_messages=Mock(return_value=[])),
    )
    repl._backup_service = SimpleNamespace(backup_session=Mock())
    repl._original_cwd = "/repo"
    repl._session_id = "session-1"
    repl._streaming_error_log = []

    with patch("iac_code.ui.repl.logger.opt") as logger_opt, pytest.raises(exc_type):
        await repl._handle_chat("first turn")

    repl.store.set_state.assert_any_call(is_busy=False)
    repl._backup_service.backup_session.assert_not_called()
    logger_opt.assert_not_called()


def test_normalize_streaming_output_result_includes_draft_input():
    from iac_code.ui.renderer import StreamingOutputResult
    from iac_code.ui.repl import InlineREPL

    result = InlineREPL._normalize_streaming_output_result(
        StreamingOutputResult(elapsed=0.2, queued_inputs=["next"], draft_input="half typed")
    )

    assert result == (0.2, ["next"], "half typed")


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


@pytest.mark.asyncio
async def test_handle_mcp_prompt_command_error_is_raw_for_local_repl():
    from iac_code.commands.registry import CommandRegistry, PromptCommand
    from iac_code.skills.frontmatter import SkillFrontmatter
    from iac_code.skills.skill_definition import SkillDefinition
    from iac_code.types.skill_source import SkillSource
    from iac_code.ui.repl import InlineREPL

    class FailingPromptProvider:
        async def get_prompt(self, args, context):
            raise RuntimeError(
                "MCP prompt failed with IAC_PRIVATE_COMMAND_ARG_MARKER_56 at https://user:pass@example.test/mcp"
            )

    registry = CommandRegistry()
    registry.register(
        PromptCommand(
            name="mcp__ros__review",
            description="Review with MCP",
            skill=SkillDefinition(
                name="mcp__ros__review",
                description="Review with MCP",
                frontmatter=SkillFrontmatter(description="Review with MCP"),
                content="",
                source=SkillSource.PROJECT,
                file_path="mcp://ros/prompt/review",
                content_length=0,
                _prompt_provider=FailingPromptProvider(),
            ),
            source=SkillSource.PROJECT,
        )
    )
    repl = InlineREPL.__new__(InlineREPL)
    repl.command_registry = registry
    repl._disabled_skill_commands = {}
    repl.renderer = SimpleNamespace(print_system_message=Mock())

    await repl._handle_command("/mcp__ros__review template=vpc")

    message = repl.renderer.print_system_message.call_args.args[0]
    assert "IAC_PRIVATE_COMMAND_ARG_MARKER_56" in message
    assert "https://user:pass@example.test/mcp" in message


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


def test_print_mcp_config_warnings_prints_each_warning_once():
    from iac_code.mcp.types import MCPConfigWarning
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.console = SimpleNamespace(print=Mock())
    repl._mcp_warnings_printed_count = 0
    repl.mcp_config_warnings = [
        MCPConfigWarning(source="mcp", server_name="broken", code="connection_failed", message="first warning")
    ]

    repl._print_mcp_config_warnings()
    repl._print_mcp_config_warnings()
    repl.mcp_config_warnings.append(
        MCPConfigWarning(source="mcp", server_name="broken", code="resources_failed", message="second warning")
    )
    repl._print_mcp_config_warnings()

    printed = [call.args[0] for call in repl.console.print.call_args_list]
    assert len(printed) == 2
    assert "first warning" in printed[0]
    assert "second warning" in printed[1]


def test_swap_session_marks_completed_cleanup_prompt(tmp_path: Path):
    from iac_code.agent.message import Message
    from iac_code.pipeline.engine.cleanup import (
        CleanupLedger,
        CleanupResource,
        create_cleanup_prompt_message,
    )
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    cwd = str(tmp_path / "repo")
    Path(cwd).mkdir()
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    new_session_id = "new-session"
    ledger = CleanupLedger(storage.session_dir(cwd, new_session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-deleted",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(
        cwd,
        new_session_id,
        create_cleanup_prompt_message(cleanup_prompt.prompt, cleanup_ledger_path=ledger.path, cleanup_status="pending"),
    )
    storage.append(cwd, new_session_id, Message(role="assistant", content="cleanup finished"))
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-deleted",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = cwd
    repl._session_id = "old-session"
    repl._session_storage = storage
    repl._agent_loop = SimpleNamespace(replace_session=Mock())
    repl._load_current_session_name = Mock(return_value=None)
    repl._load_pipeline_display_replay_model = Mock(return_value=None)
    repl.current_git_branch = Mock(return_value="main")
    repl.store = SimpleNamespace(get_state=Mock(return_value=SimpleNamespace(model="test-model", cwd=cwd)))
    repl.console = SimpleNamespace(file=SimpleNamespace(write=Mock(), flush=Mock()), print=Mock())
    repl.renderer = SimpleNamespace(replay_history=Mock())

    repl.swap_session(new_session_id)

    messages = storage.load(cwd, new_session_id)
    cleanup_messages = [message for message in messages if message.metadata.get("type") == "pipeline_cleanup_prompt"]
    assert cleanup_messages[0].metadata["cleanupStatus"] == "completed"


def test_swap_session_prints_cleanup_resume_summary(tmp_path: Path):
    from iac_code.agent.message import Message
    from iac_code.pipeline.engine.cleanup import (
        CleanupLedger,
        CleanupResource,
        create_cleanup_prompt_message,
    )
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    cwd = str(tmp_path / "repo")
    Path(cwd).mkdir()
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    new_session_id = "new-session"
    ledger = CleanupLedger(storage.session_dir(cwd, new_session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-deleted",
                resource_name="demo-stack",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(
        cwd,
        new_session_id,
        create_cleanup_prompt_message(cleanup_prompt.prompt, cleanup_ledger_path=ledger.path, cleanup_status="pending"),
    )
    storage.append(cwd, new_session_id, Message(role="assistant", content="cleanup finished"))
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-deleted",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = cwd
    repl._session_id = "old-session"
    repl._session_storage = storage
    repl._agent_loop = SimpleNamespace(replace_session=Mock())
    repl._load_current_session_name = Mock(return_value=None)
    repl._load_pipeline_display_replay_model = Mock(return_value=None)
    repl.current_git_branch = Mock(return_value="main")
    repl.store = SimpleNamespace(get_state=Mock(return_value=SimpleNamespace(model="test-model", cwd=cwd)))
    repl.console = SimpleNamespace(file=SimpleNamespace(write=Mock(), flush=Mock()), print=Mock())
    repl.renderer = SimpleNamespace(replay_history=Mock(), print_system_message=Mock())

    repl.swap_session(new_session_id)

    rendered = "\n".join(call.args[0] for call in repl.renderer.print_system_message.call_args_list)
    assert "↺ Rollback cleanup resume: all 1 records are completed." in rendered
    assert "Rollback cleanup [Completed] demo-stack" not in rendered
    assert "stack-deleted" not in rendered
    assert "status=" not in rendered
    assert "progress=" not in rendered
    replayed = repl.renderer.replay_history.call_args.args[0]
    assert all(message.metadata.get("type") != "pipeline_cleanup_prompt" for message in replayed)


def test_swap_session_prints_cleanup_resume_summary_for_completed_prompt(tmp_path: Path):
    from iac_code.agent.message import Message
    from iac_code.pipeline.engine.cleanup import (
        CleanupLedger,
        CleanupResource,
        create_cleanup_prompt_message,
    )
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    cwd = str(tmp_path / "repo")
    Path(cwd).mkdir()
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    new_session_id = "new-session"
    ledger = CleanupLedger(storage.session_dir(cwd, new_session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-deleted",
                resource_name="demo-stack",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(
        cwd,
        new_session_id,
        create_cleanup_prompt_message(
            cleanup_prompt.prompt,
            cleanup_ledger_path=ledger.path,
            cleanup_status="completed",
        ),
    )
    storage.append(cwd, new_session_id, Message(role="assistant", content="cleanup finished"))
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-deleted",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = cwd
    repl._session_id = "old-session"
    repl._session_storage = storage
    repl._agent_loop = SimpleNamespace(replace_session=Mock())
    repl._load_current_session_name = Mock(return_value=None)
    repl._load_pipeline_display_replay_model = Mock(return_value=None)
    repl.current_git_branch = Mock(return_value="main")
    repl.store = SimpleNamespace(get_state=Mock(return_value=SimpleNamespace(model="test-model", cwd=cwd)))
    repl.console = SimpleNamespace(file=SimpleNamespace(write=Mock(), flush=Mock()), print=Mock())
    repl.renderer = SimpleNamespace(replay_history=Mock(), print_system_message=Mock())

    repl.swap_session(new_session_id)

    rendered = "\n".join(call.args[0] for call in repl.renderer.print_system_message.call_args_list)
    assert "↺ Rollback cleanup resume: all 1 records are completed." in rendered
    assert "Rollback cleanup [Completed] demo-stack" not in rendered
    assert "DELETE_COMPLETE" not in rendered


def test_swap_session_prints_failed_cleanup_resume_summary(tmp_path: Path):
    from iac_code.agent.message import Message
    from iac_code.pipeline.engine.cleanup import (
        CleanupLedger,
        CleanupResource,
        create_cleanup_prompt_message,
    )
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    cwd = str(tmp_path / "repo")
    Path(cwd).mkdir()
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    new_session_id = "new-session"
    ledger = CleanupLedger(storage.session_dir(cwd, new_session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-failed",
                resource_name="failed-stack",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(
        cwd,
        new_session_id,
        create_cleanup_prompt_message(cleanup_prompt.prompt, cleanup_ledger_path=ledger.path, cleanup_status="pending"),
    )
    storage.append(cwd, new_session_id, Message(role="assistant", content="cleanup failed"))
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-failed",
        region_id="cn-hangzhou",
        cleanup_status="failed",
        progress_status="DELETE_FAILED",
        last_error="DELETE_FAILED: stack still has dependency",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = cwd
    repl._session_id = "old-session"
    repl._session_storage = storage
    repl._agent_loop = SimpleNamespace(replace_session=Mock())
    repl._load_current_session_name = Mock(return_value=None)
    repl._load_pipeline_display_replay_model = Mock(return_value=None)
    repl.current_git_branch = Mock(return_value="main")
    repl.store = SimpleNamespace(get_state=Mock(return_value=SimpleNamespace(model="test-model", cwd=cwd)))
    repl.console = SimpleNamespace(file=SimpleNamespace(write=Mock(), flush=Mock()), print=Mock())
    repl.renderer = SimpleNamespace(replay_history=Mock(), print_system_message=Mock())

    repl.swap_session(new_session_id)

    rendered = "\n".join(call.args[0] for call in repl.renderer.print_system_message.call_args_list)
    assert "↺ Rollback cleanup resume: 1 records, 1 failed." in rendered
    assert "  [Failed] failed-stack" in rendered
    assert "↺ Rollback cleanup [Failed] failed-stack" not in rendered
    assert "stack stack-failed · cn-hangzhou" in rendered
    assert "DELETE_FAILED" in rendered
    assert "dependency" in rendered
    assert "status=" not in rendered
    assert "progress=" not in rendered
    replayed = repl.renderer.replay_history.call_args.args[0]
    assert all(message.metadata.get("type") != "pipeline_cleanup_prompt" for message in replayed)


def test_pipeline_visible_resume_messages_hides_pending_cleanup_prompt():
    from iac_code.agent.message import Message
    from iac_code.pipeline.engine.cleanup import create_cleanup_prompt_message
    from iac_code.ui.repl import InlineREPL

    cleanup = create_cleanup_prompt_message("hidden cleanup prompt", cleanup_status="pending")
    messages = [Message(role="user", content="visible"), cleanup, Message(role="assistant", content="answer")]

    visible = InlineREPL._pipeline_visible_resume_messages(messages)

    assert [message.content for message in visible] == ["visible", "answer"]


def test_swap_session_clears_stale_cleanup_ledger_path_before_pruning(tmp_path: Path):
    from iac_code.agent.message import Message
    from iac_code.pipeline.engine.cleanup import (
        CleanupLedger,
        CleanupResource,
        ObservedResource,
        create_cleanup_prompt_message,
    )
    from iac_code.services.session_storage import SessionStorage
    from iac_code.ui.repl import InlineREPL

    cwd = str(tmp_path / "repo")
    Path(cwd).mkdir()
    storage = SessionStorage(projects_dir=tmp_path / "projects")

    old_ledger = CleanupLedger(tmp_path / "old-cleanup.yaml")
    old_ledger.record_observed(
        ObservedResource(
            provider="ros",
            resource_type="stack",
            resource_id="old-stack",
            region_id="cn-hangzhou",
            observed_action="CreateStack",
        )
    )

    new_session_id = "new-session"
    new_ledger = CleanupLedger(storage.session_dir(cwd, new_session_id) / "pipeline" / "cleanup.yaml")
    new_ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-deleted",
                region_id="cn-hangzhou",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = new_ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(
        cwd,
        new_session_id,
        create_cleanup_prompt_message(
            cleanup_prompt.prompt,
            cleanup_ledger_path=new_ledger.path,
            cleanup_status="pending",
        ),
    )
    storage.append(cwd, new_session_id, Message(role="assistant", content="cleanup finished"))
    new_ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-deleted",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )

    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = cwd
    repl._session_id = "old-session"
    repl._pipeline_cleanup_ledger_path = old_ledger.path
    repl._session_storage = storage
    repl._agent_loop = SimpleNamespace(replace_session=Mock())
    repl._load_current_session_name = Mock(return_value=None)
    repl._load_pipeline_display_replay_model = Mock(return_value=None)
    repl.current_git_branch = Mock(return_value="main")
    repl.store = SimpleNamespace(get_state=Mock(return_value=SimpleNamespace(model="test-model", cwd=cwd)))
    repl.console = SimpleNamespace(file=SimpleNamespace(write=Mock(), flush=Mock()), print=Mock())
    repl.renderer = SimpleNamespace(replay_history=Mock())

    repl.swap_session(new_session_id)

    messages = storage.load(cwd, new_session_id)
    cleanup_messages = [message for message in messages if message.metadata.get("type") == "pipeline_cleanup_prompt"]
    assert cleanup_messages[0].metadata["cleanupStatus"] == "completed"
    assert not hasattr(repl, "_pipeline_cleanup_ledger_path")


def test_swap_session_refreshes_session_trusted_read_directories(monkeypatch, tmp_path):
    from iac_code.mcp.types import MCPToolRecord
    from iac_code.services.agent_factory import _sync_mcp_tool_registry
    from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
    from iac_code.state.app_state import AppState
    from iac_code.tools.base import ToolRegistry
    from iac_code.types.permissions import ToolPermissionContext
    from iac_code.ui.repl import InlineREPL
    from iac_code.utils.image.pasted_content import PastedContent

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    sessions_root = tmp_path / "sessions"
    write_session_metadata(
        sessions_root / "new",
        SessionMetadata(session_id="new", cwd=str(tmp_path), layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    old_roots = [
        str(tmp_path / "config" / "tool-results" / "old"),
        str(tmp_path / "config" / "image-cache" / "old"),
        str(sessions_root / "old" / "tool-results"),
        str(sessions_root / "old" / "image-cache"),
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
        session_dir=lambda _cwd, session_id: sessions_root / session_id,
    )
    repl._agent_loop = SimpleNamespace(replace_session=Mock())
    repl._load_current_session_name = Mock(return_value=None)
    repl.tool_registry = ToolRegistry()
    repl._mcp_manager = SimpleNamespace(
        list_tools=Mock(
            return_value=[
                MCPToolRecord(
                    server_name="ros",
                    tool_name="render",
                    public_name="mcp__ros__render",
                    input_schema={"type": "object"},
                )
            ]
        ),
        list_resources=Mock(return_value=[]),
    )
    repl._registered_mcp_tool_names = _sync_mcp_tool_registry(
        repl.tool_registry,
        repl._mcp_manager,
        "old",
        set(),
        session_dir=sessions_root / "old",
    )
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
    assert old_roots[2] not in roots
    assert old_roots[3] not in roots
    assert str(tmp_path / "config" / "tool-results" / "new") in roots
    assert str(tmp_path / "config" / "image-cache" / "new") in roots
    assert str(sessions_root / "new" / "tool-results") in roots
    assert str(sessions_root / "new" / "image-cache") in roots
    assert custom_root in roots
    stored_path = repl._image_store.store(PastedContent(id=1, type="image", content="aGk=", media_type="image/png"))
    assert stored_path == str(sessions_root / "new" / "image-cache" / "1.png")
    mcp_tool = repl.tool_registry.get("mcp__ros__render")
    assert mcp_tool._session_id == "new"
    assert mcp_tool._session_dir == sessions_root / "new"


def test_repl_mcp_change_listener_syncs_connection_and_auth_like_agent_factory(monkeypatch, tmp_path):
    from iac_code.mcp.types import MCPConfigScope, MCPServerConfig, ScopedMCPServerConfig
    from iac_code.ui.repl import InlineREPL

    scoped = ScopedMCPServerConfig(
        config=MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"}),
        scope=MCPConfigScope.USER,
    )
    listener_holder = {}

    class FakeMCPManager:
        def __init__(self, configs, *, roots, session_id):
            self.configs = configs
            self.roots = roots
            self.session_id = session_id

        def set_elicitation_handler(self, handler):
            self.elicitation_handler = handler

        async def connect_all(self):
            return None

        def add_change_listener(self, listener):
            listener_holder["listener"] = listener

    monkeypatch.setattr(
        "iac_code.mcp.config.load_mcp_configs",
        lambda **kwargs: SimpleNamespace(servers=[scoped], warnings=[]),
    )
    monkeypatch.setattr("iac_code.mcp.config.resolve_mcp_workspace_root", lambda cwd: tmp_path)
    monkeypatch.setattr("iac_code.mcp.manager.MCPManager", FakeMCPManager)
    monkeypatch.setattr("iac_code.services.agent_factory._mcp_connection_warnings", lambda manager: [])
    monkeypatch.setattr(
        "iac_code.services.agent_factory._append_new_mcp_connection_warnings",
        lambda warnings, manager: None,
    )
    monkeypatch.setattr(
        "iac_code.services.agent_factory._sync_mcp_auth_tools",
        lambda *args, **kwargs: {"auth-tool"},
    )
    tool_sync_calls = []
    command_sync_calls = []

    def sync_tools(*args, **kwargs):
        tool_sync_calls.append(True)
        return {"mcp__remote__search"}

    async def sync_commands(*args, **kwargs):
        command_sync_calls.append(True)
        return {"mcp__remote__review"}, []

    monkeypatch.setattr("iac_code.services.agent_factory._sync_mcp_tool_registry", sync_tools)
    monkeypatch.setattr("iac_code.services.agent_factory._sync_mcp_command_registry", sync_commands)

    repl = InlineREPL.__new__(InlineREPL)
    repl._prompt_for_pending_project_mcp_servers = Mock()
    repl._original_cwd = str(tmp_path)
    repl._session_id = "session-1"
    repl._mcp_auth_tasks = set()
    repl._mcp_auth_flows = set()
    repl.tool_registry = object()
    repl.command_registry = object()
    repl.mcp_config_warnings = []
    repl._registered_mcp_tool_names = set()
    repl._registered_mcp_auth_tool_names = set()
    repl._registered_mcp_command_names = set()
    repl._session_dir_for_artifacts = Mock(return_value=tmp_path / "tool-results")
    repl._request_mcp_elicitation = Mock()
    repl._refresh_model_skill_listing = Mock()
    repl._print_mcp_config_warnings = Mock()

    repl._register_mcp_integrations()
    tool_sync_calls.clear()
    command_sync_calls.clear()

    asyncio.run(listener_holder["listener"]("remote", "connection"))

    assert tool_sync_calls == [True]
    assert command_sync_calls == [True]

    tool_sync_calls.clear()
    command_sync_calls.clear()
    asyncio.run(listener_holder["listener"]("remote", "auth"))

    assert tool_sync_calls == [True]
    assert command_sync_calls == [True]


@pytest.mark.asyncio
async def test_repl_pipeline_restore_passes_mcp_status_metadata_to_pipeline_factory(monkeypatch, tmp_path):
    from iac_code.pipeline.config import RunMode
    from iac_code.ui.repl import InlineREPL

    mcp_manager = SimpleNamespace(list_connections=Mock(return_value=[]))
    warnings = [SimpleNamespace(server_name="broken", code="connection_failed", message="MCP server failed")]
    captured_kwargs = {}

    class FakePipeline:
        sidecar_restore_result = SimpleNamespace(ok=True, status="running")

    def fake_create_pipeline(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakePipeline()

    repl = InlineREPL.__new__(InlineREPL)
    repl._pipeline = None
    repl._get_runtime_mode = Mock(return_value=RunMode.PIPELINE)
    repl._original_cwd = str(tmp_path)
    repl._session_id = "session-1"
    repl._detect_pipeline_session = Mock(return_value=True)
    repl._provider_manager = object()
    repl.tool_registry = object()
    repl._session_storage = object()
    repl.store = SimpleNamespace(get_state=Mock(return_value=SimpleNamespace(permission_context=object())))
    repl._pipeline_memory_content_getter = Mock(return_value=lambda: "")
    repl.command_registry = SimpleNamespace(get_model_invocable_skills=Mock(return_value=[]))
    repl._refresh_pipeline_display_recorder = Mock()
    repl._mcp_manager = mcp_manager
    repl.mcp_config_warnings = warnings

    monkeypatch.setattr("iac_code.pipeline.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.pipeline.config.get_pipeline_name", lambda: "selling")
    monkeypatch.setattr("iac_code.pipeline.config.get_working_directory", lambda: str(tmp_path))

    assert await repl.ensure_pipeline_restored_for_prompt() is True
    assert captured_kwargs["mcp_manager"] is mcp_manager
    assert captured_kwargs["mcp_config_warnings"] == warnings


def test_extract_last_user_text_skips_recalled_memory_message():
    from iac_code.agent.message import Message, create_recalled_memory_message
    from iac_code.pipeline.engine.cleanup import create_cleanup_prompt_message
    from iac_code.ui.repl import InlineREPL

    text = InlineREPL._extract_last_user_text(
        [
            Message(role="user", content="real prompt"),
            Message(role="assistant", content="answer"),
            create_recalled_memory_message("# Recalled Memory\nhidden prompt", ["topic.md"]),
            create_cleanup_prompt_message("cleanup hidden prompt"),
        ]
    )

    assert text == "real prompt"


def test_history_search_messages_skips_recalled_memory_messages_and_leaked_entries():
    from iac_code.agent.message import RECALLED_MEMORY_MARKER, Message, create_recalled_memory_message
    from iac_code.pipeline.engine.cleanup import create_cleanup_prompt_message
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._history = SimpleNamespace(
        entries=Mock(
            return_value=[
                "normal history",
                f"<system-reminder>\n{RECALLED_MEMORY_MARKER}:\n\nhidden\n</system-reminder>",
            ]
        )
    )
    repl._agent_loop = SimpleNamespace(
        context_manager=SimpleNamespace(
            get_messages=Mock(
                return_value=[
                    create_recalled_memory_message("# Recalled Memory\nhidden context", ["topic.md"]),
                    create_cleanup_prompt_message("cleanup hidden prompt"),
                    Message(role="user", content="context prompt"),
                ]
            )
        )
    )

    messages = repl._history_search_messages()

    assert messages == [
        {"role": "user", "content": "normal history"},
        {"role": "user", "content": "context prompt"},
    ]


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


@patch("iac_code.ui.repl.AgentLoop")
@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_continue_without_history_prepares_new_session_layout(
    mock_mm,
    mock_ss,
    mock_pm,
    mock_agent_loop,
    monkeypatch,
    tmp_path,
):
    from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
    from iac_code.ui.repl import InlineREPL

    session_id = "fresh-continue-session"
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / session_id
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("uuid.uuid4", lambda: session_id)
    storage = mock_ss.return_value
    storage.get_latest_session_anywhere.return_value = None
    storage.load.return_value = []
    storage.repair_interrupted.return_value = []
    storage.session_dir.side_effect = lambda _cwd, selected_session_id: sessions_root / selected_session_id

    def ensure_v2_session(cwd, selected_session_id, **_kwargs):
        write_session_metadata(
            sessions_root / selected_session_id,
            SessionMetadata(session_id=selected_session_id, cwd=cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
        )

    storage.ensure_v2_session_dir_for_new_session.side_effect = ensure_v2_session
    storage.v2_session_dir.side_effect = lambda _cwd, selected_session_id: (
        sessions_root / selected_session_id if storage.ensure_v2_session_dir_for_new_session.called else None
    )

    repl = InlineREPL(model="test-model", resume_session_id=True)

    assert repl.session_id == session_id
    assert storage.ensure_v2_session_dir_for_new_session.call_args.args[:2] == (str(tmp_path), session_id)
    assert mock_agent_loop.call_args.kwargs["result_storage_dir"] == session_dir / "tool-results"


def test_prepare_pipeline_session_layout_uses_pipeline_cwd() -> None:
    from iac_code.ui.repl import InlineREPL

    storage = Mock()
    repl = InlineREPL.__new__(InlineREPL)
    repl._session_storage = storage
    repl.current_git_branch = Mock(return_value="feature")

    repl._prepare_pipeline_session_layout("/pipeline-workspace", "session-1")

    storage.ensure_v2_session_dir_for_new_session.assert_called_once_with(
        "/pipeline-workspace",
        "session-1",
        git_branch="feature",
    )


def test_cross_project_message_uses_windows_resume_command(monkeypatch):
    import iac_code.utils.project_paths as project_paths
    from iac_code.ui.repl import InlineREPL

    monkeypatch.setattr(project_paths.sys, "platform", "win32")

    message = InlineREPL._cross_project_message(r"C:\Users\Me\iac repo & unsafe", "abc & unsafe")

    assert r'cd /d "C:\Users\Me\iac repo & unsafe" && iac-code --resume "abc & unsafe"' in message


@patch("iac_code.ui.repl.AgentLoop")
@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_inline_repl_agent_loop_uses_session_tool_results(
    mock_mm,
    mock_ss,
    mock_pm,
    mock_agent_loop,
    monkeypatch,
    tmp_path,
):
    from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
    from iac_code.ui.repl import InlineREPL

    session_id = "session-v2"
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / session_id
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd=str(tmp_path), layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("uuid.uuid4", lambda: session_id)
    mock_ss.return_value.load.return_value = []
    mock_ss.return_value.repair_interrupted.return_value = []
    mock_ss.return_value.session_dir.side_effect = lambda _cwd, selected_session_id: sessions_root / selected_session_id

    repl = InlineREPL(model="test-model")

    assert repl.session_id == session_id
    assert mock_agent_loop.call_args.kwargs["result_storage_dir"] == session_dir / "tool-results"


@patch("iac_code.ui.repl.AgentLoop")
@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_inline_repl_agent_loop_uses_legacy_tool_results_without_session_metadata(
    mock_mm,
    mock_ss,
    mock_pm,
    mock_agent_loop,
    monkeypatch,
    tmp_path,
):
    from iac_code.ui.repl import InlineREPL

    session_id = "legacy-session"
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / session_id
    session_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("uuid.uuid4", lambda: session_id)
    mock_ss.return_value.load.return_value = []
    mock_ss.return_value.repair_interrupted.return_value = []
    mock_ss.return_value.session_dir.side_effect = lambda _cwd, selected_session_id: sessions_root / selected_session_id

    repl = InlineREPL(model="test-model")

    assert repl.session_id == session_id
    assert mock_agent_loop.call_args.kwargs["result_storage_dir"] is None


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

    async def _record_startup_pipeline_restore():
        call_order.append("resume_pipeline_sidecar_on_startup")
        return False

    repl._resume_pipeline_sidecar_on_startup = AsyncMock(side_effect=_record_startup_pipeline_restore)

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
        "resume_pipeline_sidecar_on_startup",
    ]


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_run_does_not_render_second_update_notice_after_startup_prompt(mock_mm, mock_ss, mock_pm):
    import asyncio
    from io import StringIO
    from unittest.mock import AsyncMock

    from rich.console import Console
    from rich.text import Text

    from iac_code.ui.repl import ExitREPLError, InlineREPL

    update = make_pending_update()
    repl = InlineREPL(model="test-model")
    output = StringIO()
    repl.console = Console(file=output, force_terminal=False, width=120)
    repl._prompt_input.get_input = AsyncMock(side_effect=ExitREPLError())

    with (
        patch("iac_code.ui.repl.get_pending_update", return_value=update),
        patch("iac_code.ui.repl.render_welcome_banner", return_value=Text("welcome")),
        patch("iac_code.ui.repl.Select") as select,
        patch("iac_code.ui.repl.start_background_housekeeping"),
        patch("iac_code.ui.repl.start_background_update_check"),
    ):
        select.return_value.run.return_value = "skip"
        asyncio.run(repl.run())

    assert output.getvalue().count("Update available!") == 1
