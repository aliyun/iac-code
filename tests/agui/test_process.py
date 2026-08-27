from __future__ import annotations

import asyncio
import contextlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from iac_code.agui.process import LocalA2AProcess
from iac_code.agui.server import _local_a2a_url, run_server


class _Process:
    def poll(self) -> None:
        return None


class _HealthResponse:
    status_code = 200


def test_managed_a2a_child_inherits_execution_environment_without_interpreting_it(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    def popen(command, *, env):
        captured["command"] = command
        captured["env"] = env
        return _Process()

    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_A2A_EXTREME_PERFORMANCE", "true")
    monkeypatch.setattr("iac_code.agui.process.subprocess.Popen", popen)
    monkeypatch.setattr("iac_code.agui.process.httpx.get", lambda *_args, **_kwargs: _HealthResponse())

    process = LocalA2AProcess()
    process.start()

    command = captured["command"]
    environment = captured["env"]
    assert command[3] == "a2a"
    assert environment["IACCODE_A2A_ALLOWED_CWDS"] == str(tmp_path)
    assert environment["IAC_CODE_A2A_SAFE_MODE"] == "true"
    assert environment["IAC_CODE_MODE"] == "pipeline"
    assert environment["IAC_CODE_A2A_EXTREME_PERFORMANCE"] == "true"
    assert environment["IACCODE_A2A_HTTP_TOKEN"] == process.token


def test_explicit_a2a_url_must_be_loopback() -> None:
    assert _local_a2a_url("http://127.0.0.1:41242") == "http://127.0.0.1:41242/"
    with pytest.raises(ValueError, match="loopback"):
        _local_a2a_url("https://a2a.example.com")


def test_idle_shutdown_requests_uvicorn_exit_and_runs_lifespan_cleanup(monkeypatch) -> None:
    servers = []
    endpoint_closed: list[bool] = []

    class Config:
        def __init__(self, app, **_kwargs) -> None:
            self.app = app

    class Server:
        def __init__(self, config) -> None:
            self.config = config
            self.should_exit = False
            servers.append(self)

        def run(self) -> None:
            async def serve() -> None:
                async with self.config.app.router.lifespan_context(self.config.app):
                    await asyncio.sleep(0.15)

            asyncio.run(serve())

    @contextlib.contextmanager
    def endpoint(**_kwargs):
        try:
            yield "http://127.0.0.1:41242/", None
        finally:
            endpoint_closed.append(True)

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=Config, Server=Server))
    monkeypatch.setattr("iac_code.agui.server._a2a_endpoint", endpoint)

    run_server(idle_shutdown=0.01)

    assert len(servers) == 1
    assert servers[0].should_exit is True
    assert endpoint_closed == [True]
