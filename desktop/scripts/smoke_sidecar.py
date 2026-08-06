#!/usr/bin/env python3
"""Exercise the frozen sidecar through its real platform control carrier."""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[2]


def windows_control_pipe_name(pid: int, unique: object) -> str:
    """Return a smoke pipe name accepted by the sidecar control boundary."""
    return r"\\.\pipe\iac-code-desktop-smoke-{}-{}".format(pid, unique)


def _read_exact(carrier: socket.socket | BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = carrier.recv(size) if isinstance(carrier, socket.socket) else carrier.read(size)
        if not chunk:
            raise EOFError("frozen sidecar closed its control carrier")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _read_message(carrier: socket.socket | BinaryIO) -> dict[str, object]:
    size = struct.unpack(">I", _read_exact(carrier, 4))[0]
    message = json.loads(_read_exact(carrier, size))
    if not isinstance(message, dict):
        raise RuntimeError("frozen sidecar returned a non-object control message")
    return message


def _send_message(carrier: socket.socket | BinaryIO, message: dict[str, object]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    frame = struct.pack(">I", len(payload)) + payload
    if isinstance(carrier, socket.socket):
        carrier.sendall(frame)
    else:
        carrier.write(frame)
        carrier.flush()


def _windows_control_pipe() -> tuple[str, Callable[[], BinaryIO]]:
    import ctypes
    import msvcrt
    import uuid

    pipe_name = windows_control_pipe_name(os.getpid(), uuid.uuid4())
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
    handle = kernel32.CreateNamedPipeW(pipe_name, 3, 8, 1, 1024 * 1024, 1024 * 1024, 30_000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    class _Connector:
        def connect(self) -> BinaryIO:
            connected = kernel32.ConnectNamedPipe(ctypes.c_void_p(handle), None)
            if not connected and ctypes.get_last_error() != 535:  # ERROR_PIPE_CONNECTED
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                raise ctypes.WinError(ctypes.get_last_error())
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            return os.fdopen(descriptor, "r+b", buffering=0)

    return pipe_name, _Connector().connect


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "executable",
        type=Path,
        nargs="?",
        default=ROOT
        / "desktop/dist/sidecar/iac-code-sidecar"
        / ("iac-code-sidecar.exe" if os.name == "nt" else "iac-code-sidecar"),
    )
    parser.add_argument(
        "--expect-managed-infraguard",
        action="store_true",
        help="require the Desktop prerequisite endpoint to detect the managed InfraGuard binary",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    executable = args.executable.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="iac-code-frozen-smoke-") as temporary:
        root = Path(temporary)
        paths = {name: root / name for name in ("project", "host-state", "install-lock", "runtime", "config")}
        for path in paths.values():
            path.mkdir()
        logs = paths["host-state"] / "logs"
        logs.mkdir()
        if os.name == "nt":
            pipe_name, connect_pipe = _windows_control_pipe()
            control_arguments = ["--control-pipe", pipe_name]
        else:
            host, child = socket.socketpair()
            host.settimeout(30)
            control_arguments = ["--control-fd", str(child.fileno())]
        arguments = [
                str(executable),
                "--requested-port",
                "0",
                "--desktop-install-id",
                "iac-code-frozen-smoke",
                "--host-state-dir",
                str(paths["host-state"]),
                "--desktop-install-lock-dir",
                str(paths["install-lock"]),
                "--runtime-dir",
                str(paths["runtime"]),
                "--default-project-cwd",
                str(paths["project"]),
                "--distribution-channel",
                "development",
                "--update-mode",
                "external",
                "--sidecar-generation",
                "1",
                *control_arguments,
                "--host-capture-path",
                str(logs / "host-capture.log"),
                "--config-dir",
                str(paths["config"]),
                "--log-dir",
                str(logs),
            ]
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"pass_fds": (child.fileno(),)}),
        )
        if os.name == "nt":
            host = connect_pipe()
        else:
            child.close()
        try:
            ready = _read_message(host)
            if ready.get("type") != "ready":
                raise RuntimeError("frozen sidecar did not become ready: {!r}".format(ready))
            port = int(ready["port"])
            with urllib.request.urlopen("http://127.0.0.1:{}/health".format(port), timeout=5) as response:
                if response.status != 200 or json.load(response).get("status") != "ok":
                    raise RuntimeError("frozen sidecar health check failed")
            if args.expect_managed_infraguard:
                prerequisite_url = "http://127.0.0.1:{}/api/settings/pipeline-review-step/prerequisite".format(port)
                with urllib.request.urlopen(prerequisite_url, timeout=35) as response:
                    prerequisite = json.load(response)
                if prerequisite.get("status") != "available" or prerequisite.get("satisfied") is not True:
                    raise RuntimeError("frozen sidecar did not detect managed InfraGuard: {!r}".format(prerequisite))
            _send_message(host, {"type": "shutdown", "force": False})
            stopped = _read_message(host)
            if stopped.get("type") != "stopped":
                raise RuntimeError("frozen sidecar did not acknowledge shutdown")
            if process.wait(timeout=10) != 0:
                raise RuntimeError("frozen sidecar returned a non-zero status")
        finally:
            host.close()
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=5)
            if b"Traceback" in stdout + stderr:
                raise RuntimeError((stdout + stderr).decode("utf-8", errors="replace"))
        terraform = root / "terraform"
        terraform.mkdir()
        (terraform / "main.tf").write_text('resource "alicloud_vpc" "main" {}\n', encoding="utf-8")
        converted = root / "template.json"
        subprocess.run(
            [str(executable.with_name("iac-code-tf2ros")), str(terraform), str(converted)],
            check=True,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        template = json.loads(converted.read_text(encoding="utf-8"))
        if template.get("Workspace", {}).get("main.tf") != 'resource "alicloud_vpc" "main" {}\n':
            raise RuntimeError("frozen tf2ros helper produced an invalid workspace")
    print("Frozen sidecar smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
