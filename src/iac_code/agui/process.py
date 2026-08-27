"""Lifecycle management for the local A2A execution-kernel subprocess."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx

from iac_code.i18n import _


@dataclass
class LocalA2AProcess:
    host: str = "127.0.0.1"
    startup_timeout: float = 20.0

    def __post_init__(self) -> None:
        self.port = _available_port(self.host)
        self.token = secrets.token_urlsafe(32)
        self.url = f"http://{self.host}:{self.port}/"
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError(_("The local A2A process is already started."))
        env = os.environ.copy()
        env["IACCODE_A2A_HTTP_TOKEN"] = self.token
        agui_roots = env.get("IAC_CODE_AGUI_ALLOWED_CWDS")
        if agui_roots and not env.get("IACCODE_A2A_ALLOWED_CWDS"):
            env["IACCODE_A2A_ALLOWED_CWDS"] = agui_roots
        command = [
            sys.executable,
            "-c",
            "from iac_code.cli.main import app; app()",
            "a2a",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--thinking-exposure",
            "all",
        ]
        self._process = subprocess.Popen(command, env=env)
        self._wait_until_ready()

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _wait_until_ready(self) -> None:
        assert self._process is not None
        deadline = time.monotonic() + self.startup_timeout
        headers = {"Authorization": f"Bearer {self.token}"}
        while time.monotonic() < deadline:
            return_code = self._process.poll()
            if return_code is not None:
                self._process = None
                raise RuntimeError(_("The local A2A process exited during startup (exit code {}).").format(return_code))
            try:
                response = httpx.get(self.url + "health", headers=headers, timeout=0.5)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        self.close()
        raise RuntimeError(_("The local A2A process did not become ready in time."))

    def __enter__(self) -> LocalA2AProcess:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _available_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
