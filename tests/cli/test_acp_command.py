from typer.testing import CliRunner

from iac_code.cli.main import app


def test_acp_command_bootstraps_telemetry_around_acp_main(monkeypatch) -> None:
    boot_calls: list[str | None] = []
    events: list[tuple[str, dict]] = []
    metrics: list[tuple[str, int | float, dict]] = []
    shutdown_calls: list[int] = []
    main_calls: list[dict] = []

    def fake_bootstrap(session_id=None):
        boot_calls.append(session_id)

    def fake_log_event(name, payload=None):
        events.append((name, dict(payload or {})))

    def fake_add_metric(name, value, attributes=None):
        metrics.append((name, value, dict(attributes or {})))

    def fake_shutdown():
        shutdown_calls.append(1)

    def fake_acp_main(*, debug: bool = False) -> None:
        main_calls.append({"debug": debug})
        # Telemetry must already be wired when acp_main runs, otherwise per-call
        # spans/events from the agent loop are silently dropped.
        assert len(boot_calls) == 1
        assert any(name == "iac.session.started" for name, _ in events)

    monkeypatch.setattr("iac_code.services.telemetry.bootstrap_telemetry", fake_bootstrap)
    monkeypatch.setattr("iac_code.services.telemetry.log_event", fake_log_event)
    monkeypatch.setattr("iac_code.services.telemetry.add_metric", fake_add_metric)
    monkeypatch.setattr("iac_code.services.telemetry.graceful_shutdown", fake_shutdown)
    monkeypatch.setattr("iac_code.acp.acp_main", fake_acp_main)

    result = CliRunner().invoke(app, ["acp"])

    assert result.exit_code == 0
    assert len(boot_calls) == 1
    assert boot_calls[0] is not None
    assert boot_calls[0].startswith("acp-server-")

    started = [payload for name, payload in events if name == "iac.session.started"]
    assert started == [{"mode": "acp-server", "transport": "stdio"}]

    exited = [payload for name, payload in events if name == "iac.session.exited"]
    assert len(exited) == 1
    assert exited[0]["mode"] == "acp-server"
    assert exited[0]["reason"] == "normal"
    assert isinstance(exited[0]["duration_s"], int)

    assert ("iac.session.count", 1, {}) in metrics
    assert main_calls == [{"debug": False}]
    assert shutdown_calls == [1]


def test_acp_command_http_transport_routes_to_acp_main_http(monkeypatch) -> None:
    captured: dict = {}

    monkeypatch.setattr("iac_code.services.telemetry.bootstrap_telemetry", lambda session_id=None: None)
    monkeypatch.setattr("iac_code.services.telemetry.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.services.telemetry.add_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.services.telemetry.graceful_shutdown", lambda: None)

    def fake_acp_main_http(*, host: str, port: int, debug: bool) -> None:
        captured.update({"host": host, "port": port, "debug": debug})

    monkeypatch.setattr("iac_code.acp.acp_main_http", fake_acp_main_http)

    result = CliRunner().invoke(
        app,
        ["acp", "--transport", "http", "--host", "0.0.0.0", "--port", "9999"],
    )

    assert result.exit_code == 0
    assert captured == {"host": "0.0.0.0", "port": 9999, "debug": False}


def test_acp_command_flushes_telemetry_when_acp_main_raises(monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    shutdown_calls: list[int] = []

    monkeypatch.setattr("iac_code.services.telemetry.bootstrap_telemetry", lambda session_id=None: None)
    monkeypatch.setattr(
        "iac_code.services.telemetry.log_event",
        lambda name, payload=None: events.append((name, dict(payload or {}))),
    )
    monkeypatch.setattr("iac_code.services.telemetry.add_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.services.telemetry.graceful_shutdown", lambda: shutdown_calls.append(1))

    def fake_acp_main(*, debug: bool = False) -> None:
        raise RuntimeError("acp init failed")

    monkeypatch.setattr("iac_code.acp.acp_main", fake_acp_main)

    result = CliRunner().invoke(app, ["acp"])

    assert result.exit_code != 0
    exited = [payload for name, payload in events if name == "iac.session.exited"]
    assert len(exited) == 1
    assert exited[0]["reason"] == "error"
    assert shutdown_calls == [1]
