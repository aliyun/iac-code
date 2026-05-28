"""Tests for HeadlessRunner."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from iac_code.cli.headless import EXIT_ERROR, EXIT_MAX_TURNS, EXIT_OK, HeadlessRunner
from iac_code.cli.output_formats import OutputFormat
from iac_code.providers.manager import ProviderNotConfiguredError
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    TextDeltaEvent,
    Usage,
)


def _make_runner(
    output_format: OutputFormat = OutputFormat.TEXT,
    output_stream: io.StringIO | None = None,
) -> HeadlessRunner:
    return HeadlessRunner(
        model="test-model",
        output_format=output_format,
        output_stream=output_stream or io.StringIO(),
    )


async def _fake_stream(*events):
    """Create an async generator that yields the given events."""
    for event in events:
        yield event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_output():
    """TextDeltaEvent + MessageEndEvent produces text output and exit code 0."""
    buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)

    events = [
        TextDeltaEvent(text="Hello, world!"),
        MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5)),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    output = buf.getvalue()
    assert "Hello, world!" in output


@pytest.mark.asyncio
async def test_json_output():
    """TextDeltaEvent + MessageEndEvent produces valid JSON output and exit code 0."""
    buf = io.StringIO()
    runner = _make_runner(OutputFormat.JSON, buf)

    events = [
        TextDeltaEvent(text="result text"),
        MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5)),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    output = buf.getvalue()
    parsed = json.loads(output)
    assert parsed["text"] == "result text"


@pytest.mark.asyncio
async def test_permission_auto_approved():
    """PermissionRequestEvent future is auto-approved with True."""
    buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()

    events = [
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"command": "ls"},
            tool_use_id="tu_1",
            response_future=future,
        ),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    assert future.result() is True


@pytest.mark.asyncio
async def test_error_returns_exit_code_1():
    """ErrorEvent causes exit code 1."""
    buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)

    events = [
        ErrorEvent(error="something went wrong", is_retryable=False),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_ERROR


@pytest.mark.asyncio
async def test_max_turns_returns_exit_code_2():
    """MessageEndEvent with stop_reason='max_turns' causes exit code 2."""
    buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)

    events = [
        TextDeltaEvent(text="partial"),
        MessageEndEvent(stop_reason="max_turns", usage=Usage()),
    ]

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_MAX_TURNS


# ---------------------------------------------------------------------------
# CLI Flag Wiring Tests
# ---------------------------------------------------------------------------

runner_cli = CliRunner()


class TestCLIFlags:
    def test_prompt_flag_triggers_headless(self):
        from iac_code.cli.main import app

        with patch("iac_code.cli.headless.HeadlessRunner") as mock_runner:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=0)
            mock_runner.return_value = mock_instance

            runner_cli.invoke(app, ["-p", "hello"])

            mock_runner.assert_called_once()
            mock_instance.run.assert_called_once_with("hello")

    def test_output_format_passed_to_headless(self):
        from iac_code.cli.main import app

        with patch("iac_code.cli.headless.HeadlessRunner") as mock_runner:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=0)
            mock_runner.return_value = mock_instance

            runner_cli.invoke(app, ["-p", "hello", "--output-format", "json"])

            call_kwargs = mock_runner.call_args[1]
            assert call_kwargs["output_format"] == OutputFormat.JSON

    def test_max_turns_passed_to_headless(self):
        from iac_code.cli.main import app

        with patch("iac_code.cli.headless.HeadlessRunner") as mock_runner:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=0)
            mock_runner.return_value = mock_instance

            runner_cli.invoke(app, ["-p", "hello", "--max-turns", "5"])

            call_kwargs = mock_runner.call_args[1]
            assert call_kwargs["max_turns"] == 5

    def test_stdin_prompt(self):
        from iac_code.cli.main import app

        with patch("iac_code.cli.headless.HeadlessRunner") as mock_runner:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value=0)
            mock_runner.return_value = mock_instance

            runner_cli.invoke(app, ["-p", "-"], input="hello from stdin")

            mock_instance.run.assert_called_once_with("hello from stdin")

    def test_invalid_provider_env_prints_error_not_traceback(self, monkeypatch):
        from iac_code.cli.main import app

        monkeypatch.setenv("IAC_CODE_PROVIDER", "NotAProvider")
        result = runner_cli.invoke(app, [])
        assert result.exit_code != 0
        assert "Invalid IAC_CODE_PROVIDER" in result.output


@pytest.mark.asyncio
async def test_permission_without_response_future_is_ignored_and_writer_finalized():
    buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)

    events = [
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"command": "ls"},
            tool_use_id="tu_1",
            response_future=None,
        ),
        MessageEndEvent(stop_reason="end_turn", usage=Usage()),
    ]

    with (
        patch("iac_code.cli.headless.start_background_housekeeping") as housekeeping,
        patch("iac_code.cli.headless.create_writer") as create_writer,
        patch.object(runner, "_create_agent_loop") as mock_create,
    ):
        writer = MagicMock()
        create_writer.return_value = writer
        mock_loop = AsyncMock()
        mock_loop.run_streaming = lambda prompt: _fake_stream(*events)
        mock_create.return_value = mock_loop

        exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_OK
    housekeeping.assert_called_once()
    writer.finalize.assert_called_once()


class FakeToolRegistry:
    def __init__(self):
        self.registered = []
        self.default_registered = False

    def register_default_tools(self):
        self.default_registered = True

    def register(self, tool):
        self.registered.append(tool)


class FakeCommandRegistry:
    def __init__(self, existing=None):
        self.existing = existing or {}
        self.registered = []
        self.skill_commands = ["skill-cmd"]

    def get(self, name):
        return self.existing.get(name)

    def register(self, cmd):
        self.registered.append(cmd)
        self.existing[cmd.name] = cmd

    def get_model_invocable_skills(self):
        return self.skill_commands


def _install_headless_fakes(monkeypatch, *, creds=None, skills=None, existing_command=None):
    captured = {}
    fake_registry = FakeToolRegistry()
    fake_command_registry = FakeCommandRegistry(existing=existing_command or {})
    fake_session_dir = Path("/tmp/iac-config")

    class FakeProviderManager:
        def __init__(self, *, model, credentials, provider_key_override=None, base_url_override=None):
            captured["provider_manager"] = {"model": model, "credentials": credentials}

    class FakeSessionStorage:
        def __init__(self, projects_dir=None):
            captured["projects_dir"] = projects_dir

    class FakeMemoryManager:
        def __init__(self, *, memory_dir):
            captured["memory_dir"] = memory_dir

        def get_prompt_content(self):
            return "memory prompt"

    class FakeTaskManager:
        pass

    class FakeNotificationQueue:
        pass

    class FakeReadMemoryTool:
        def __init__(self, manager):
            self.kind = "read_memory"
            self.manager = manager

    class FakeWriteMemoryTool:
        def __init__(self, manager):
            self.kind = "write_memory"
            self.manager = manager

    class FakeTaskListTool:
        def __init__(self, manager):
            self.kind = "task_list"

    class FakeTaskGetTool:
        def __init__(self, manager):
            self.kind = "task_get"

    class FakeTaskStopTool:
        def __init__(self, manager):
            self.kind = "task_stop"

    class FakeAgentTool:
        def __init__(self, **kwargs):
            self.kind = "agent"
            captured["agent_tool_kwargs"] = kwargs

    class FakeSkillTool:
        def __init__(self, **kwargs):
            self.kind = "skill"
            captured["skill_tool_kwargs"] = kwargs

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            captured["agent_loop_kwargs"] = kwargs

    class FakeCloudCredentials:
        pass

    class FakePromptCommand:
        def __init__(self, name="prompt", **kwargs):
            self.name = name
            for key, value in kwargs.items():
                setattr(self, key, value)

    monkeypatch.setattr("iac_code.config._load_yaml", lambda path: creds)
    monkeypatch.setattr("iac_code.config.get_credentials_path", lambda: Path("/tmp/creds.yml"))
    monkeypatch.setattr("iac_code.config.get_config_dir", lambda: fake_session_dir)
    monkeypatch.setattr("iac_code.providers.manager.ProviderManager", FakeProviderManager)
    monkeypatch.setattr("iac_code.tools.base.ToolRegistry", lambda: fake_registry)
    monkeypatch.setattr(
        "iac_code.tools.cloud.registry.register_cloud_tools",
        lambda registry, creds: captured.setdefault("cloud_tools", []).append((registry, creds)),
    )
    monkeypatch.setattr("iac_code.services.cloud_credentials.CloudCredentials", FakeCloudCredentials)
    monkeypatch.setattr("iac_code.services.session_storage.SessionStorage", FakeSessionStorage)
    monkeypatch.setattr("iac_code.memory.memory_manager.MemoryManager", FakeMemoryManager)
    monkeypatch.setattr("iac_code.memory.memory_tools.ReadMemoryTool", FakeReadMemoryTool)
    monkeypatch.setattr("iac_code.memory.memory_tools.WriteMemoryTool", FakeWriteMemoryTool)
    monkeypatch.setattr("iac_code.tasks.task_state.TaskManager", FakeTaskManager)
    monkeypatch.setattr("iac_code.tasks.notification_queue.NotificationQueue", FakeNotificationQueue)
    monkeypatch.setattr("iac_code.tasks.task_tools.TaskListTool", FakeTaskListTool)
    monkeypatch.setattr("iac_code.tasks.task_tools.TaskGetTool", FakeTaskGetTool)
    monkeypatch.setattr("iac_code.tasks.task_tools.TaskStopTool", FakeTaskStopTool)
    monkeypatch.setattr("iac_code.agent.agent_tool.AgentTool", FakeAgentTool)
    monkeypatch.setattr("iac_code.skills.skill_tool.SkillTool", FakeSkillTool)
    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)
    monkeypatch.setattr("iac_code.commands.create_default_registry", lambda: fake_command_registry)
    monkeypatch.setattr("iac_code.commands.registry.PromptCommand", FakePromptCommand)
    monkeypatch.setattr("iac_code.skills.bundled.init_bundled_skills", lambda: captured.setdefault("bundled_init", 0))
    monkeypatch.setattr("iac_code.skills.discovery.discover_all_skills", lambda cwd: skills or [])
    monkeypatch.setattr(
        "iac_code.skills.discovery.skill_to_command",
        lambda skill: SimpleNamespace(name=skill.name),
    )
    monkeypatch.setattr("iac_code.skills.listing.build_skill_listing", lambda skill_commands: "skill listing")
    monkeypatch.setattr(
        "iac_code.agent.system_prompt.build_system_prompt",
        lambda **kwargs: f"prompt:{kwargs.get('cwd')}:{kwargs.get('memory_content')}:{kwargs.get('skill_listing')}",
    )
    monkeypatch.setattr("os.getcwd", lambda: "/worktree")
    monkeypatch.setattr("uuid.uuid4", lambda: SimpleNamespace(__str__=lambda self: "12345678-aaaa"))

    return captured, fake_registry, fake_command_registry


def test_create_agent_loop_builds_expected_dependencies(monkeypatch):
    runner = _make_runner()
    captured, fake_registry, fake_command_registry = _install_headless_fakes(
        monkeypatch,
        creds={
            "anthropic": "ak",
            "openai": "ok",
            "bailian": "bk",
            "openapi_compatible": "compat",
        },
        skills=[],
    )

    loop = runner._create_agent_loop()

    assert loop is not None
    assert fake_registry.default_registered is True
    pm = captured["provider_manager"]
    assert pm["model"] == "test-model"
    assert pm["credentials"]["anthropic"] == "ak"
    assert pm["credentials"]["openai"] == "ok"
    assert pm["credentials"]["dashscope"] == "bk"
    assert pm["credentials"]["openapi_compatible"] == "compat"
    # Session storage is now project-partitioned and constructs its own
    # default projects_dir from get_config_dir(), so we just assert the
    # storage was instantiated rather than checking a specific path.
    assert "projects_dir" in captured
    assert captured["memory_dir"] == str(Path("/tmp/iac-config/memory"))
    assert any(getattr(tool, "kind", "") == "agent" for tool in fake_registry.registered)
    assert any(getattr(tool, "kind", "") == "skill" for tool in fake_registry.registered)
    assert captured["agent_loop_kwargs"]["max_turns"] == 100
    assert captured["agent_loop_kwargs"]["session_storage"] is not None
    assert fake_command_registry.registered == []


def test_create_agent_loop_handles_credential_load_failure_and_skill_conflict(monkeypatch):
    runner = _make_runner()
    existing_cmd = {"skill-one": object()}
    skill = SimpleNamespace(name="skill-one")
    captured, fake_registry, fake_command_registry = _install_headless_fakes(
        monkeypatch,
        creds=None,
        skills=[skill],
        existing_command=existing_cmd,
    )
    monkeypatch.setattr("iac_code.config._load_yaml", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))

    with patch("iac_code.cli.headless.logger.warning") as warning:
        runner._create_agent_loop()

    creds = captured["provider_manager"]["credentials"]
    for key in ("anthropic", "openai", "dashscope", "dashscope_token_plan", "deepseek", "openapi_compatible"):
        assert creds[key] == ""
    warning.assert_called_once()
    assert fake_command_registry.registered == []
    assert any(getattr(tool, "kind", "") == "skill" for tool in fake_registry.registered)


@pytest.mark.asyncio
async def test_provider_not_configured_prints_friendly_error():
    """ValueError from _ensure_provider prints a friendly message, not a traceback."""
    buf = io.StringIO()
    err_buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)

    async def _raise_on_stream(prompt):
        raise ProviderNotConfiguredError("Cannot determine provider for model: custom-model. Run /auth to configure.")
        yield  # make it an async generator

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = _raise_on_stream
        mock_create.return_value = mock_loop

        with patch("sys.stderr", err_buf):
            exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_ERROR
    err_output = err_buf.getvalue()
    assert "Cannot determine provider for model: custom-model" in err_output
    assert "IAC_CODE_API_KEY" in err_output
    assert "/auth" in err_output


@pytest.mark.asyncio
async def test_no_api_key_prints_friendly_error():
    """ValueError about missing API key also shows the friendly message."""
    buf = io.StringIO()
    err_buf = io.StringIO()
    runner = _make_runner(OutputFormat.TEXT, buf)

    async def _raise_on_stream(prompt):
        raise ProviderNotConfiguredError(
            "No API key configured for provider 'anthropic' (model: claude-sonnet-4-6). Run /auth to configure."
        )
        yield  # make it an async generator

    with patch.object(runner, "_create_agent_loop") as mock_create:
        mock_loop = AsyncMock()
        mock_loop.run_streaming = _raise_on_stream
        mock_create.return_value = mock_loop

        with patch("sys.stderr", err_buf):
            exit_code = await runner.run("test prompt")

    assert exit_code == EXIT_ERROR
    err_output = err_buf.getvalue()
    assert "No API key configured for provider" in err_output
    assert "/auth" in err_output
