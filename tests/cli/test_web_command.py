import pytest
from typer.testing import CliRunner

from iac_code.cli.main import app


@pytest.fixture(autouse=True)
def setup_logging_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr("iac_code.cli.main.setup_logging", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr("iac_code.web.settings.developer_settings", lambda: {"debug": False})
    return calls


def test_web_command_runs_local_server_with_safe_defaults(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_web_server(*, host: str, port: int, open_browser: bool, access_token_file: str | None) -> None:
        calls.append({"host": host, "port": port, "open_browser": open_browser, "access_token_file": access_token_file})

    monkeypatch.setattr("iac_code.web.server.run_web_server", fake_run_web_server)

    result = CliRunner().invoke(app, ["web"])

    assert result.exit_code == 0
    assert calls == [{"host": "127.0.0.1", "port": 8766, "open_browser": True, "access_token_file": None}]


def test_web_command_configures_logging(monkeypatch, setup_logging_calls) -> None:
    monkeypatch.setattr("iac_code.web.settings.developer_settings", lambda: {"debug": True})
    monkeypatch.setattr("iac_code.web.server.run_web_server", lambda **_kwargs: None)

    result = CliRunner().invoke(app, ["web"])

    assert result.exit_code == 0
    assert len(setup_logging_calls) == 1
    assert str(setup_logging_calls[0]["session_id"]).startswith("web-server-")
    assert setup_logging_calls[0]["debug"] is True


def test_web_command_accepts_host_port_and_open_browser(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_web_server(*, host: str, port: int, open_browser: bool, access_token_file: str | None) -> None:
        calls.append({"host": host, "port": port, "open_browser": open_browser, "access_token_file": access_token_file})

    monkeypatch.setattr("iac_code.web.server.run_web_server", fake_run_web_server)

    result = CliRunner().invoke(app, ["web", "--host", "127.0.0.2", "--port", "9999", "--open"])

    assert result.exit_code == 0
    assert calls == [{"host": "127.0.0.2", "port": 9999, "open_browser": True, "access_token_file": None}]


def test_web_command_no_open_disables_browser(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_web_server(*, host: str, port: int, open_browser: bool, access_token_file: str | None) -> None:
        calls.append({"host": host, "port": port, "open_browser": open_browser, "access_token_file": access_token_file})

    monkeypatch.setattr("iac_code.web.server.run_web_server", fake_run_web_server)

    result = CliRunner().invoke(app, ["web", "--no-open"])

    assert result.exit_code == 0
    assert calls == [{"host": "127.0.0.1", "port": 8766, "open_browser": False, "access_token_file": None}]


def test_web_command_passes_access_token_file(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr("iac_code.web.server.run_web_server", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(
        app,
        ["web", "--host", "0.0.0.0", "--access-token-file", "/run/iac-code/token", "--no-open"],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "host": "0.0.0.0",
            "port": 8766,
            "open_browser": False,
            "access_token_file": "/run/iac-code/token",
        }
    ]


def test_web_command_bootstraps_and_gracefully_shuts_down_telemetry(monkeypatch, setup_logging_calls) -> None:
    lifecycle: list[object] = []

    monkeypatch.setattr(
        "iac_code.services.telemetry.bootstrap_telemetry",
        lambda session_id=None: lifecycle.append(("bootstrap", session_id)),
    )
    monkeypatch.setattr(
        "iac_code.services.telemetry.graceful_shutdown",
        lambda: lifecycle.append("shutdown"),
    )
    monkeypatch.setattr(
        "iac_code.web.server.run_web_server",
        lambda **_kwargs: lifecycle.append("run"),
    )

    result = CliRunner().invoke(app, ["web"])

    assert result.exit_code == 0
    assert lifecycle[0][0] == "bootstrap"
    assert str(lifecycle[0][1]).startswith("web-server-")
    assert setup_logging_calls == [{"session_id": lifecycle[0][1], "debug": False}]
    assert lifecycle[1:] == ["run", "shutdown"]


def test_web_command_gracefully_shuts_down_telemetry_when_server_fails(monkeypatch) -> None:
    lifecycle: list[str] = []

    monkeypatch.setattr(
        "iac_code.services.telemetry.bootstrap_telemetry",
        lambda **_kwargs: lifecycle.append("bootstrap"),
    )
    monkeypatch.setattr("iac_code.services.telemetry.graceful_shutdown", lambda: lifecycle.append("shutdown"))

    def fail_server(**_kwargs) -> None:
        lifecycle.append("run")
        raise RuntimeError("server failed")

    monkeypatch.setattr("iac_code.web.server.run_web_server", fail_server)

    result = CliRunner().invoke(app, ["web"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert lifecycle == ["bootstrap", "run", "shutdown"]


def test_web_command_checks_git_bash_before_starting_on_windows(monkeypatch) -> None:
    from iac_code.utils.platform import GitBashNotFoundError, PlatformInfo

    server_calls: list[str] = []

    def missing_git_bash() -> None:
        raise GitBashNotFoundError("install with: iac-code install-git-bash")

    monkeypatch.setattr("iac_code.cli.main.sys.platform", "win32")
    monkeypatch.setattr(PlatformInfo, "detect", staticmethod(missing_git_bash))
    monkeypatch.setattr(
        "iac_code.web.server.run_web_server",
        lambda **_kwargs: server_calls.append("run"),
    )

    result = CliRunner().invoke(app, ["web"])

    assert result.exit_code == 1
    assert "iac-code install-git-bash" in result.output
    assert server_calls == []
