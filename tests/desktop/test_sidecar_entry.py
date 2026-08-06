from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from iac_code.desktop.control import encode_control_message


def _read_exact(carrier: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = carrier.recv(size)
        if not chunk:
            raise EOFError("sidecar control carrier closed")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _read_message(carrier: socket.socket) -> dict[str, object]:
    size = struct.unpack(">I", _read_exact(carrier, 4))[0]
    message = json.loads(_read_exact(carrier, size).decode("utf-8"))
    assert isinstance(message, dict)
    return message


def test_startup_failure_before_control_connection_is_captured(capsys: pytest.CaptureFixture[str]) -> None:
    from iac_code.desktop.__main__ import _send_startup_failure

    _send_startup_failure(None, None, "initialization_failed", RuntimeError("preload failed"))

    assert capsys.readouterr().err == (
        "Desktop sidecar startup failed before control connection: RuntimeError: preload failed\n"
    )


def test_desktop_startup_applies_persisted_debug_and_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.desktop.__main__ import _initialize_runtime_services
    from iac_code.services import telemetry
    from iac_code.utils import log
    from iac_code.web import server, settings

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(settings, "developer_settings", lambda: {"debug": True})
    monkeypatch.setattr(
        log,
        "setup_logging",
        lambda **kwargs: calls.append(("logging", kwargs)),
    )
    monkeypatch.setattr(
        server,
        "apply_persisted_telemetry_content_settings",
        lambda: calls.append(("telemetry-settings",)),
    )
    monkeypatch.setattr(
        telemetry,
        "bootstrap_telemetry",
        lambda **kwargs: calls.append(("telemetry", kwargs)),
    )

    _initialize_runtime_services(9, stdout=False)

    assert calls == [
        ("logging", {"session_id": "desktop-9", "stdout": False, "debug": True}),
        ("telemetry-settings",),
        ("telemetry", {"session_id": "desktop-9"}),
    ]


@pytest.mark.skipif(os.name == "nt", reason="the Windows sidecar uses a named-pipe carrier")
@pytest.mark.parametrize("with_recovery_journal", [False, True])
def test_sidecar_entry_serves_health_and_stops_over_control(
    tmp_path: Path,
    with_recovery_journal: bool,
) -> None:
    project = tmp_path / "project"
    host_state = tmp_path / "host-state"
    install_lock = tmp_path / "install-lock"
    runtime = tmp_path / "runtime"
    logs = host_state / "logs"
    config = tmp_path / "config"
    for path in (project, host_state, install_lock, runtime, logs, config):
        path.mkdir(parents=True, exist_ok=True)
    if with_recovery_journal:
        (install_lock / ("a" * 64 + ".transaction.json")).write_text("not-json", encoding="utf-8")
    host, child = socket.socketpair()
    host.settimeout(20)
    command = [
        sys.executable,
        "-m",
        "iac_code.desktop",
        "--requested-port",
        "0",
        "--desktop-install-id",
        "iac-code-development",
        "--host-state-dir",
        str(host_state),
        "--desktop-install-lock-dir",
        str(install_lock),
        "--runtime-dir",
        str(runtime),
        "--default-project-cwd",
        str(project),
        "--distribution-channel",
        "development",
        "--update-mode",
        "external",
        "--sidecar-generation",
        "7",
        "--control-fd",
        str(child.fileno()),
        "--host-capture-path",
        str(logs / "host-capture.log"),
    ]
    process = subprocess.Popen(
        command,
        pass_fds=(child.fileno(),),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "IAC_CODE_CONFIG_DIR": str(config),
            "IAC_CODE_LOG_DIR": str(logs),
        },
    )
    child.close()
    try:
        ready = _read_message(host)
        if with_recovery_journal:
            assert ready == {
                "type": "startup-recovery-begin",
                "sidecarGeneration": 7,
                "timeoutSeconds": 360.0,
            }
            ready = _read_message(host)
        assert ready["type"] == "ready"
        assert ready["sidecarGeneration"] == 7
        port = int(ready["port"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            assert response.status == 200
            assert json.load(response) == {"service": "iac-code-web", "status": "ok"}

        host.sendall(encode_control_message({"type": "shutdown", "force": False}))
        assert _read_message(host) == {"type": "stopped", "sidecarGeneration": 7}
        assert process.wait(timeout=10) == 0
    finally:
        host.close()
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
        assert b"Traceback" not in stdout + stderr
