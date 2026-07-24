from typer.testing import CliRunner

from iac_code.cli.main import app


def test_web_command_runs_local_server_with_safe_defaults(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_web_server(*, host: str, port: int, open_browser: bool) -> None:
        calls.append({"host": host, "port": port, "open_browser": open_browser})

    monkeypatch.setattr("iac_code.web.server.run_web_server", fake_run_web_server)

    result = CliRunner().invoke(app, ["web"])

    assert result.exit_code == 0
    assert calls == [{"host": "127.0.0.1", "port": 8766, "open_browser": True}]


def test_web_command_accepts_host_port_and_open_browser(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_web_server(*, host: str, port: int, open_browser: bool) -> None:
        calls.append({"host": host, "port": port, "open_browser": open_browser})

    monkeypatch.setattr("iac_code.web.server.run_web_server", fake_run_web_server)

    result = CliRunner().invoke(app, ["web", "--host", "127.0.0.2", "--port", "9999", "--open"])

    assert result.exit_code == 0
    assert calls == [{"host": "127.0.0.2", "port": 9999, "open_browser": True}]


def test_web_command_no_open_disables_browser(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_web_server(*, host: str, port: int, open_browser: bool) -> None:
        calls.append({"host": host, "port": port, "open_browser": open_browser})

    monkeypatch.setattr("iac_code.web.server.run_web_server", fake_run_web_server)

    result = CliRunner().invoke(app, ["web", "--no-open"])

    assert result.exit_code == 0
    assert calls == [{"host": "127.0.0.1", "port": 8766, "open_browser": False}]


def test_web_command_bootstraps_and_gracefully_shuts_down_telemetry(monkeypatch) -> None:
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
