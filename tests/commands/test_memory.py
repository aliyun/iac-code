from __future__ import annotations

from datetime import datetime as real_datetime

import pytest

from iac_code.agent import system_prompt
from iac_code.commands import create_default_registry
from iac_code.commands import memory as memory_module
from iac_code.commands.memory import execute_memory_command
from iac_code.memory.memory_manager import MemoryManager
from iac_code.memory.project_memory import ProjectMemoryRuntime
from iac_code.ui.dialogs.memory_editor import MemoryEditResult


@pytest.fixture
def manager(tmp_path):
    mgr = MemoryManager(memory_dir=str(tmp_path))
    mgr.save("user-role", "Senior cloud engineer", memory_type="user", description="Role")
    mgr.save("feedback-testing", "Prefer integration tests", memory_type="feedback", description="Testing")
    return mgr


class _Context:
    def __init__(self, manager):
        self.repl = type("Repl", (), {"_memory_manager": manager})()


class _ContextWithLegacy:
    def __init__(self, legacy_manager, project_manager):
        self.repl = type(
            "Repl",
            (),
            {
                "_legacy_memory_manager": legacy_manager,
                "_memory_manager": project_manager,
            },
        )()


class _MemoryRuntimeContext:
    def __init__(self, runtime):
        self.repl = type("Repl", (), {"_memory_runtime": runtime})()


class _RefreshingMemoryRuntimeContext:
    def __init__(self, runtime):
        self.refreshed = False

        def refresh_system_prompt():
            self.refreshed = True

        self.repl = type(
            "Repl",
            (),
            {
                "_memory_runtime": runtime,
                "_refresh_system_prompt": staticmethod(refresh_system_prompt),
            },
        )()


def test_execute_memory_command_lists_memories(manager):
    output = execute_memory_command(manager, [])
    assert "Saved memories:" in output
    assert "feedback-testing - Testing" in output
    assert "user-role - Role" in output


def test_execute_memory_command_lists_empty(tmp_path):
    output = execute_memory_command(MemoryManager(memory_dir=str(tmp_path)), [])
    assert output == "No memories saved yet."


def test_execute_memory_command_views_memory(manager):
    output = execute_memory_command(manager, ["user-role"])
    assert output == "[user] Role\n\nSenior cloud engineer"


def test_execute_memory_command_missing_memory(manager):
    output = execute_memory_command(manager, ["missing"])
    assert output == "Memory 'missing' not found."


def test_execute_memory_command_searches_memories(manager):
    output = execute_memory_command(manager, ["search", "integration"])
    assert output == "Matching memories:\n  - feedback-testing - Testing"


def test_execute_memory_command_search_no_matches(manager):
    output = execute_memory_command(manager, ["search", "nope"])
    assert output == "No matching memories."


def test_execute_memory_command_search_without_query_shows_help(manager):
    output = execute_memory_command(manager, ["search"])
    assert "Usage: /memory-folder" in output


def test_execute_memory_command_deletes_memory(manager):
    output = execute_memory_command(manager, ["delete", "user-role"])
    assert output == "Memory 'user-role' deleted."
    assert manager.load("user-role") is None


def test_execute_memory_command_delete_missing(manager):
    output = execute_memory_command(manager, ["delete", "missing"])
    assert output == "Memory 'missing' not found."


def test_execute_memory_command_invalid_name(manager):
    output = execute_memory_command(manager, ["../escape"])
    assert "Invalid memory name" in output


def test_execute_memory_command_help_and_unknown_multi_token(manager):
    assert "Usage: /memory-folder" in execute_memory_command(manager, ["help"])
    assert "Usage: /memory-folder" in execute_memory_command(manager, ["remove", "user-role"])


@pytest.mark.asyncio
async def test_memory_folder_command_uses_repl_memory_manager(manager):
    output = await memory_module.memory_folder_command(context=_Context(manager), args=["user-role"])
    assert output == "[user] Role\n\nSenior cloud engineer"


@pytest.mark.asyncio
async def test_memory_folder_command_prefers_legacy_memory_manager(tmp_path):
    legacy = MemoryManager(memory_dir=str(tmp_path / "legacy"))
    legacy.save("same-name", "Legacy content", memory_type="project", description="Legacy")
    project = MemoryManager(memory_dir=str(tmp_path / "project"))
    project.save("same-name", "Project content", memory_type="project", description="Project")

    output = await memory_module.memory_folder_command(context=_ContextWithLegacy(legacy, project), args=["same-name"])

    assert output == "[project] Legacy\n\nLegacy content"


@pytest.mark.asyncio
async def test_memory_folder_command_missing_context_manager():
    output = await memory_module.memory_folder_command(context=object(), args=[])
    assert output == "Memory manager is unavailable."


def test_default_registry_exposes_new_memory_and_hides_memory_folder():
    registry = create_default_registry()

    memory = registry.get("memory")
    memory_folder = registry.get("memory-folder")

    assert memory is not None
    assert memory.description == "Edit memory files"
    assert memory.hidden is False
    assert memory.arg_hint is None
    assert memory_folder is not None
    assert memory_folder.hidden is True
    assert memory_folder.arg_hint == "[<name>|search <query>|delete <name>|help]"
    assert "memory-folder" not in {cmd.name for cmd in registry.get_all()}


@pytest.mark.asyncio
async def test_memory_command_opens_project_iac_code_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    edited: list[object] = []
    monkeypatch.setattr(
        memory_module,
        "_select_memory_action",
        lambda runtime, **kwargs: "project",
        raising=False,
    )
    monkeypatch.setattr(
        memory_module,
        "_edit_memory_file",
        lambda path, title: edited.append((path, title)) or MemoryEditResult("saved", "Project rules\n"),
        raising=False,
    )

    output = await memory_module.memory_command(context=_MemoryRuntimeContext(runtime), args=[])

    assert runtime.project_instruction_path.exists()
    assert edited == [(runtime.project_instruction_path, "Project memory")]
    assert runtime.project_instruction_path.read_text(encoding="utf-8") == "Project rules\n"
    assert output == "Saved project memory: {}".format(runtime.project_instruction_path)


@pytest.mark.asyncio
async def test_memory_command_refreshes_full_system_prompt_after_open(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    context = _RefreshingMemoryRuntimeContext(runtime)
    monkeypatch.setattr(memory_module, "_select_memory_action", lambda runtime, **kwargs: "project", raising=False)
    monkeypatch.setattr(
        memory_module,
        "_edit_memory_file",
        lambda path, title: MemoryEditResult("saved", "Project rules\n"),
        raising=False,
    )

    await memory_module.memory_command(context=context, args=[])

    assert context.refreshed is True


@pytest.mark.asyncio
async def test_memory_command_fallback_refresh_reuses_runtime_current_time(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    captured: dict[str, str] = {}

    class FakeDateTime:
        calls = 0

        @classmethod
        def now(cls):
            cls.calls += 1
            return real_datetime(2026, 6, 5, 10, cls.calls, 0)

    class FakeAgentLoop:
        def set_provider(self, provider_manager, *, system_prompt):
            captured["system_prompt"] = system_prompt

    class FakeRepl:
        _memory_runtime = runtime
        _agent_loop = FakeAgentLoop()
        _provider_manager = object()
        _runtime_current_time = "2026-06-05 10:00:00"
        _skill_listing = ""

        @staticmethod
        def _refresh_memory_context():
            return runtime.build_memory_context()

    repl = type(
        "Repl",
        (FakeRepl,),
        {},
    )()
    context = type("Context", (), {"repl": repl})()
    monkeypatch.setattr(system_prompt, "datetime", FakeDateTime)
    monkeypatch.setattr(memory_module, "_select_memory_action", lambda runtime, **kwargs: "project", raising=False)
    monkeypatch.setattr(
        memory_module,
        "_edit_memory_file",
        lambda path, title: MemoryEditResult("saved", "Project rules\n"),
        raising=False,
    )

    await memory_module.memory_command(context=context, args=[])

    assert "- Current time: 2026-06-05 10:00:00" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_memory_command_opens_user_iac_code_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    edited: list[object] = []
    monkeypatch.setattr(memory_module, "_select_memory_action", lambda runtime, **kwargs: "user", raising=False)
    monkeypatch.setattr(
        memory_module,
        "_edit_memory_file",
        lambda path, title: edited.append((path, title)) or MemoryEditResult("saved", "User rules\n"),
        raising=False,
    )

    output = await memory_module.memory_command(context=_MemoryRuntimeContext(runtime), args=[])

    assert runtime.user_instruction_path.exists()
    assert edited == [(runtime.user_instruction_path, "User memory")]
    assert runtime.user_instruction_path.read_text(encoding="utf-8") == "User rules\n"
    assert output == "Saved user memory: {}".format(runtime.user_instruction_path)


@pytest.mark.asyncio
async def test_memory_command_does_not_refresh_when_editor_reports_unchanged(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    context = _RefreshingMemoryRuntimeContext(runtime)
    monkeypatch.setattr(memory_module, "_select_memory_action", lambda runtime, **kwargs: "project", raising=False)
    monkeypatch.setattr(
        memory_module,
        "_edit_memory_file",
        lambda path, title: MemoryEditResult("unchanged", ""),
        raising=False,
    )

    output = await memory_module.memory_command(context=context, args=[])

    assert context.refreshed is False
    assert output == "No changes made to project memory: {}".format(runtime.project_instruction_path)
    assert not runtime.project_instruction_path.exists()


@pytest.mark.asyncio
async def test_memory_command_unchanged_user_edit_does_not_create_empty_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    context = _RefreshingMemoryRuntimeContext(runtime)
    monkeypatch.setattr(memory_module, "_select_memory_action", lambda runtime, **kwargs: "user", raising=False)
    monkeypatch.setattr(
        memory_module,
        "_edit_memory_file",
        lambda path, title: MemoryEditResult("unchanged", ""),
        raising=False,
    )

    output = await memory_module.memory_command(context=context, args=[])

    assert context.refreshed is False
    assert output == "No changes made to user memory: {}".format(runtime.user_instruction_path)
    assert not runtime.user_instruction_path.exists()


@pytest.mark.asyncio
async def test_memory_command_cancelled_project_edit_does_not_create_empty_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    monkeypatch.setattr(memory_module, "_select_memory_action", lambda runtime, **kwargs: "project", raising=False)
    monkeypatch.setattr(
        memory_module,
        "_edit_memory_file",
        lambda path, title: MemoryEditResult("cancelled", ""),
        raising=False,
    )

    output = await memory_module.memory_command(context=_MemoryRuntimeContext(runtime), args=[])

    assert output is None
    assert not runtime.project_instruction_path.exists()


@pytest.mark.asyncio
async def test_memory_command_cancelled_user_edit_does_not_create_empty_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    monkeypatch.setattr(memory_module, "_select_memory_action", lambda runtime, **kwargs: "user", raising=False)
    monkeypatch.setattr(
        memory_module,
        "_edit_memory_file",
        lambda path, title: MemoryEditResult("cancelled", ""),
        raising=False,
    )

    output = await memory_module.memory_command(context=_MemoryRuntimeContext(runtime), args=[])

    assert output is None
    assert not runtime.user_instruction_path.exists()


@pytest.mark.asyncio
async def test_memory_command_opens_auto_memory_folder(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    opened: list[object] = []
    initial_actions: list[str | None] = []
    actions = iter(["folder", None])

    def select_memory_action(runtime, **kwargs):
        initial_actions.append(kwargs.get("initial_action"))
        return next(actions)

    monkeypatch.setattr(memory_module, "_select_memory_action", select_memory_action, raising=False)
    monkeypatch.setattr(memory_module, "_open_folder", lambda path: opened.append(path), raising=False)

    output = await memory_module.memory_command(context=_MemoryRuntimeContext(runtime), args=[])

    assert runtime.auto_memory_dir.exists()
    assert opened == [runtime.auto_memory_dir]
    assert initial_actions == [None, "folder"]
    assert output is None


@pytest.mark.asyncio
async def test_memory_command_cancel_returns_none(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))
    monkeypatch.setattr(memory_module, "_select_memory_action", lambda runtime, **kwargs: None, raising=False)

    assert await memory_module.memory_command(context=_MemoryRuntimeContext(runtime), args=[]) is None


@pytest.mark.asyncio
async def test_memory_command_toggle_auto_memory_persists_setting(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runtime = ProjectMemoryRuntime(str(project))

    def select_and_toggle(runtime, *, auto_memory_enabled, on_toggle, initial_action=None):
        assert auto_memory_enabled is True
        assert initial_action is None
        on_toggle(False)
        return None

    monkeypatch.setattr(memory_module, "_select_memory_action", select_and_toggle, raising=False)

    assert await memory_module.memory_command(context=_MemoryRuntimeContext(runtime), args=[]) is None
    assert memory_module.is_auto_memory_enabled() is False
