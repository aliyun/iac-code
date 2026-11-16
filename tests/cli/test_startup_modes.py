"""Default Web routing and the frozen terminal entry point."""

import io
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from iac_code.cli.main import app


class TerminalInput(io.BytesIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def web_calls(monkeypatch):
    calls = []
    monkeypatch.setattr("iac_code.cli.main.setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr("iac_code.web.settings.developer_settings", lambda: {"debug": False})
    monkeypatch.setattr("iac_code.services.telemetry.bootstrap_telemetry", lambda **_kwargs: None)
    monkeypatch.setattr("iac_code.services.telemetry.graceful_shutdown", lambda: None)
    monkeypatch.setattr("iac_code.web.server.run_web_server", lambda **kwargs: calls.append(kwargs))
    return calls


def test_default_starts_web_without_loading_model_configuration(monkeypatch, web_calls):
    def unexpected_model_load():
        raise AssertionError("Web must open before a provider is configured")

    monkeypatch.setattr("iac_code.cli.main.load_saved_model", unexpected_model_load)
    monkeypatch.setenv("IAC_CODE_PROVIDER", "NotAProvider")
    result = CliRunner().invoke(app, [], input=TerminalInput())

    assert result.exit_code == 0, result.output
    assert web_calls == [{"host": "127.0.0.1", "port": 8766, "open_browser": True, "access_token_file": None}]
    assert "no longer updated" not in result.output


def test_default_without_terminal_gives_explicit_entry_points(web_calls):
    result = CliRunner().invoke(app, [], input="a prompt from a pipe")

    assert result.exit_code == 2
    assert "iac-code web --no-open" in result.output
    assert "--prompt -" in result.output
    assert web_calls == []


@pytest.mark.parametrize(
    "args",
    [["--model", "test"], ["--resume", "session"], ["--continue"], ["--debug"], ["--output-format", "json"]],
)
def test_root_options_require_explicit_execution_mode(args, web_calls):
    result = CliRunner().invoke(app, args, input=TerminalInput())

    assert result.exit_code == 2
    assert "iac-code tui" in result.output
    assert "--prompt" in result.output
    assert web_calls == []


def test_tui_options_before_subcommand_are_rejected():
    result = CliRunner().invoke(app, ["--model", "test", "tui"])

    assert result.exit_code == 2
    assert "Place terminal options after the subcommand" in result.output


@pytest.mark.parametrize("resume_args, expected", [(["--resume", "session"], "session"), (["--continue"], True)])
def test_tui_preserves_terminal_options(monkeypatch, resume_args, expected):
    repl = MagicMock(session_id="test-session")
    repl.run = AsyncMock()
    constructor = MagicMock(return_value=repl)
    monkeypatch.setattr("iac_code.ui.repl.InlineREPL", constructor)
    monkeypatch.setattr("iac_code.cli.main.setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr("iac_code.services.telemetry.bootstrap_telemetry", lambda **_kwargs: None)
    monkeypatch.setattr("iac_code.services.telemetry.log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("iac_code.services.telemetry.add_metric", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr("signal.signal", lambda *_args: None)

    result = CliRunner().invoke(
        app,
        ["tui", "--model", "test", "--allowed-tools", "read_file", "--disallowed-tools", "bash", *resume_args],
        input=TerminalInput(),
    )

    assert result.exit_code == 0, result.output
    constructor.assert_called_once_with(
        model="test",
        resume_session_id=expected,
        cli_allowed_tools=["read_file"],
        cli_disallowed_tools=["bash"],
        cli_permission_mode=None,
    )
    repl.run.assert_awaited_once_with(initial_prompt=None)


def test_tui_rejects_conflicting_resume_options():
    result = CliRunner().invoke(app, ["tui", "--resume", "session", "--continue"])

    assert result.exit_code == 1
    assert "--resume and --continue cannot be used together" in result.output


def test_tui_help_explains_maintenance_status():
    result = CliRunner().invoke(app, ["tui", "--help"])

    assert result.exit_code == 0
    assert "unmaintained terminal UI" in result.output


def test_web_debug_flag_is_available(web_calls, monkeypatch):
    logging = []
    monkeypatch.setattr("iac_code.cli.main.setup_logging", lambda **kwargs: logging.append(kwargs))
    result = CliRunner().invoke(app, ["web", "--debug", "--no-open"])

    assert result.exit_code == 0
    assert logging[0]["debug"] is True
    assert web_calls[0]["open_browser"] is False
