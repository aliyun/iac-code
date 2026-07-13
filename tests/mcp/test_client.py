from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from collections.abc import Mapping
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anyio
import httpx
import pytest

import iac_code.mcp.client as client_module
import iac_code.mcp.oauth as oauth_module
from iac_code.mcp.client import MCPClientAdapter
from iac_code.mcp.errors import MCPConnectionError, MCPNeedsAuthError
from iac_code.mcp.oauth import OAuthMetadata, oauth_storage_key
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import (
    MCPConfigScope,
    MCPConnectionState,
    MCPServerConfig,
    MCPTransport,
    normalize_initialize_metadata,
)


def test_connection_error_sanitizes_raw_exception_text() -> None:
    error = client_module._connection_error(
        "remote",
        RuntimeError("failed https://user:pass@example.com/mcp Authorization: Bearer sk-live-secret"),
    )

    assert isinstance(error, MCPConnectionError)
    message = str(error)
    assert "user:pass" not in message
    assert "sk-live-secret" not in message
    assert "Authorization: Bearer" not in message
    assert "https://[REDACTED]@example.com/mcp" in message


def test_normalize_initialize_metadata_accepts_protocol_version_snake_case_alias() -> None:
    metadata = normalize_initialize_metadata("remote", {"protocol_version": "2025-06-18"})

    assert metadata.protocol_version == "2025-06-18"


@pytest.mark.asyncio
async def test_stdio_adapter_initializes_sdk_session_with_roots(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        seen["stdio_params"] = params
        seen["errlog"] = errlog
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            seen["read_stream"] = read_stream
            seen["write_stream"] = write_stream
            seen["list_roots_callback"] = list_roots_callback
            self.closed = False

        async def __aenter__(self):
            seen["session"] = self
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            seen["initialized"] = True

        async def list_tools(self):
            return [{"name": "plan"}]

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping(
            "local",
            {
                "command": "uvx",
                "args": ["server"],
                "env": {"API_KEY": "fake"},
            },
        ),
        roots=[tmp_path / "repo"],
    )

    await adapter.connect()

    assert seen["stdio_params"].command == "uvx"
    assert seen["stdio_params"].args == ["server"]
    assert seen["stdio_params"].env["API_KEY"] == "fake"
    assert "PATH" in seen["stdio_params"].env
    assert seen["errlog"] is not None
    assert seen["initialized"] is True
    assert await adapter.list_tools() == [{"name": "plan"}]
    roots = await seen["list_roots_callback"](None)
    assert len(roots.roots) == 1
    assert str(roots.roots[0].uri).startswith("file://")

    await adapter.close()
    assert seen["session"].closed is True


@pytest.mark.asyncio
async def test_stdio_adapter_normalizes_initialize_instruction_capability_version_metadata(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeInitializeResult:
        def model_dump(self, **kwargs):
            return {
                "instructions": (
                    "Prefer ROS templates from this MCP server.\nAuthorization: Bearer sk-live-secret\n" + ("x" * 5000)
                ),
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {},
                },
                "serverInfo": {
                    "name": "aliyun-ros-mcp",
                    "version": "1.2.3",
                },
                "protocolVersion": "2025-06-18",
            }

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return FakeInitializeResult()

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("configured-name", {"command": "uvx"}))

    await adapter.connect()
    try:
        metadata = adapter.metadata

        assert metadata is not None
        assert metadata.state is MCPConnectionState.CONNECTED
        assert metadata.server_name == "configured-name"
        assert metadata.capabilities == {"tools": {"listChanged": True}, "resources": {}}
        assert metadata.server_info == {"name": "aliyun-ros-mcp", "version": "1.2.3"}
        assert metadata.protocol_version == "2025-06-18"
        assert metadata.instructions is not None
        assert metadata.instructions.startswith("Prefer ROS templates")
        assert metadata.instructions.endswith("[truncated]")
        assert len(metadata.instructions) <= 4000
        assert "sk-live-secret" not in metadata.instructions
        assert "Authorization: Bearer" not in metadata.instructions
        assert metadata.config_signature is not None
        assert metadata.config_signature.startswith("stdio:")
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_stdio_adapter_keeps_empty_instruction_capability_version_metadata_empty(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return {
                "instructions": "   ",
                "capabilities": {"tools": "", "resources": {"description": " "}},
                "serverInfo": {"name": "", "version": " "},
                "protocolVersion": "   ",
            }

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("empty", {"command": "uvx"}))

    await adapter.connect()
    try:
        metadata = adapter.metadata

        assert metadata is not None
        assert metadata.instructions is None
        assert metadata.capabilities == {"tools": "", "resources": {"description": ""}}
        assert metadata.server_info == {"name": "", "version": ""}
        assert metadata.protocol_version is None
        assert "Unknown error" not in repr(metadata)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_stdio_adapter_does_not_inherit_secret_process_environment(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setenv("IAC_CODE_API_KEY", "real-secret")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "cloud-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://user:pass@proxy.example:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/cacert.pem")

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        seen["stdio_params"] = params
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping(
            "local",
            {
                "command": "uvx",
                "env": {"API_TOKEN": "explicit-token"},
            },
        ),
    )

    await adapter.connect()

    env = seen["stdio_params"].env
    assert env["PATH"] == "/usr/bin"
    assert env["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert env["SSL_CERT_FILE"] == "/tmp/cacert.pem"
    assert env["API_TOKEN"] == "explicit-token"
    assert "HTTP_PROXY" not in env
    assert "IAC_CODE_API_KEY" not in env
    assert "ALIBABA_CLOUD_ACCESS_KEY_SECRET" not in env

    await adapter.close()


@pytest.mark.asyncio
async def test_remote_headers_helper_merges_dynamic_headers_with_safe_execution(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("YUQUE_MCP_TOKEN", "real-token")
    monkeypatch.setenv("IAC_CODE_API_KEY", "real-secret")
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    _write_headers_helper(
        source_dir,
        """
import json
import os
import sys

payload = {
    "X-Org": "platform",
    "X-Cwd": os.getcwd(),
    "X-Args": ",".join(sys.argv[1:]),
    "X-Stdin": sys.stdin.read(),
    "X-Path-Seen": "yes" if os.environ.get("PATH") else "no",
    "X-Secret-Seen": os.environ.get("YUQUE_MCP_TOKEN", ""),
    "X-App-Secret-Seen": os.environ.get("IAC_CODE_API_KEY", ""),
}
print(json.dumps(payload))
""",
    )
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"X-Org": "static", "X-Static": "kept"},
                "headersHelper": "{} ./scripts/mcp_headers.py 'space value' '&&' '|' ';'".format(sys.executable),
            },
        ),
        source_dir=str(source_dir),
    )

    headers = await MCPClientAdapter(config)._remote_headers()

    assert headers == {
        "X-Org": "platform",
        "X-Static": "kept",
        "X-Cwd": str(source_dir),
        "X-Args": "space value,&&,|,;",
        "X-Stdin": "",
        "X-Path-Seen": "yes",
        "X-Secret-Seen": "",
        "X-App-Secret-Seen": "",
    }


@pytest.mark.asyncio
async def test_remote_headers_helper_dynamic_headers_override_static_case_insensitively(tmp_path: Path) -> None:
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    _write_headers_helper(
        source_dir,
        """
import json

print(json.dumps({"authorization": "Bearer dynamic", "X-Org": "platform"}))
""",
    )
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer static", "X-Org": "static", "X-Static": "kept"},
                "headersHelper": "{} ./scripts/mcp_headers.py".format(sys.executable),
            },
        ),
        source_dir=str(source_dir),
    )

    headers = await MCPClientAdapter(config)._remote_headers()

    assert headers == {"authorization": "Bearer dynamic", "X-Org": "platform", "X-Static": "kept"}


@pytest.mark.asyncio
async def test_remote_headers_helper_uses_workspace_root_when_config_has_no_source_dir(tmp_path: Path) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    _write_headers_helper(
        workspace_root,
        """
import json
import os

print(json.dumps({"X-Cwd": os.getcwd()}))
""",
    )
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "sse",
            "url": "https://example.com/sse",
            "headersHelper": "{} ./scripts/mcp_headers.py".format(sys.executable),
        },
    )

    headers = await MCPClientAdapter(config, roots=[workspace_root])._remote_headers()

    assert headers == {"X-Cwd": str(workspace_root)}


@pytest.mark.asyncio
async def test_remote_headers_helper_runtime_secret_is_not_persisted_or_exposed(tmp_path: Path) -> None:
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    _write_headers_helper(
        source_dir,
        """
import json

print(json.dumps({"Authorization": "Bearer ${YUQUE_MCP_TOKEN}"}))
""",
    )
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "{} ./scripts/mcp_headers.py".format(sys.executable),
            },
        ),
        source_dir=str(source_dir),
    )

    headers = await MCPClientAdapter(config)._remote_headers()

    assert headers == {"Authorization": "Bearer ${YUQUE_MCP_TOKEN}"}
    assert "YUQUE_MCP_TOKEN" not in json.dumps(config.raw)
    assert "YUQUE_MCP_TOKEN" not in config.headers
    assert "YUQUE_MCP_TOKEN" not in config.content_signature()


@pytest.mark.asyncio
async def test_remote_headers_helper_env_reference_is_not_expanded_into_subprocess_argv(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MCP_TOKEN", "runtime-secret")
    monkeypatch.setenv("IAC_CODE_API_KEY", "app-secret")
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs

        class FakeProcess:
            returncode = 0

            async def wait(self) -> int:
                return 0

        return FakeProcess()

    async def fake_collect_output(process: Any) -> tuple[bytes, bytes]:
        return b'{"X-Token":"ok"}', b""

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(client_module, "_collect_headers_helper_output", fake_collect_output)
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headersHelper": "python ./headers.py --token ${MCP_TOKEN}",
        },
    )

    headers = await MCPClientAdapter(config, roots=[tmp_path])._remote_headers()

    assert headers == {"X-Token": "ok"}
    assert "runtime-secret" not in repr(captured["argv"])
    assert captured["argv"] == ["python", "./headers.py", "--token", "${MCP_TOKEN}"]
    assert captured["kwargs"]["env"]["MCP_TOKEN"] == "runtime-secret"
    assert "IAC_CODE_API_KEY" not in captured["kwargs"]["env"]


def test_headers_helper_windows_command_line_parser_handles_quoted_paths_and_arguments() -> None:
    assert client_module._split_headers_helper_command(
        'python "C:\\Program Files\\helper.py" --flag "space value"',
        platform="nt",
    ) == ["python", "C:\\Program Files\\helper.py", "--flag", "space value"]
    assert client_module._split_headers_helper_command(
        "python ./scripts/mcp_headers.py 'space value' '&&' '|' ';'",
        platform="nt",
    ) == ["python", "./scripts/mcp_headers.py", "space value", "&&", "|", ";"]
    assert client_module._split_headers_helper_command('cmd /c "echo hello"', platform="nt") == [
        "cmd",
        "/c",
        "echo hello",
    ]


@pytest.mark.asyncio
async def test_remote_headers_helper_times_out(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(client_module, "_HEADERS_HELPER_TIMEOUT_SECONDS", 0.05, raising=False)
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    _write_headers_helper(
        source_dir,
        """
import time

time.sleep(10)
""",
    )
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "{} ./scripts/mcp_headers.py".format(sys.executable),
            },
        ),
        source_dir=str(source_dir),
    )

    with pytest.raises(MCPConnectionError, match="timed out"):
        await MCPClientAdapter(config)._remote_headers()


@pytest.mark.asyncio
async def test_remote_headers_helper_cleans_up_process_on_cancellation(tmp_path: Path) -> None:
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    pid_file = source_dir / "helper.pid"
    _write_headers_helper(
        source_dir,
        """
import os
import sys
import time

pid_file = sys.argv[1]
with open(pid_file, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
    handle.flush()
time.sleep(10)
""",
    )
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "{} ./scripts/mcp_headers.py {}".format(sys.executable, pid_file),
            },
        ),
        source_dir=str(source_dir),
    )

    task = asyncio.create_task(MCPClientAdapter(config)._remote_headers())
    for _attempt in range(50):
        if pid_file.exists():
            break
        await asyncio.sleep(0.02)
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _attempt in range(50):
        if not _process_is_alive(pid):
            break
        await asyncio.sleep(0.02)
    assert not _process_is_alive(pid)


@pytest.mark.asyncio
async def test_remote_headers_helper_rejects_oversized_stdout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(client_module, "_HEADERS_HELPER_STDOUT_MAX_BYTES", 64, raising=False)
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    _write_headers_helper(
        source_dir,
        """
import sys

sys.stdout.write(" " * 128)
sys.stdout.write('{"X-Org": "platform"}')
sys.stdout.flush()
""",
    )
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "{} ./scripts/mcp_headers.py".format(sys.executable),
            },
        ),
        source_dir=str(source_dir),
    )

    with pytest.raises(MCPConnectionError, match="output too large"):
        await MCPClientAdapter(config)._remote_headers()


@pytest.mark.asyncio
async def test_remote_headers_helper_rejects_oversized_stderr(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(client_module, "_HEADERS_HELPER_STDERR_MAX_BYTES", 64, raising=False)
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    _write_headers_helper(
        source_dir,
        """
import json
import sys

sys.stderr.write("Authorization: Bearer runtime-secret\\n")
sys.stderr.write("x" * 128)
sys.stderr.flush()
print(json.dumps({"X-Org": "platform"}))
""",
    )
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "{} ./scripts/mcp_headers.py".format(sys.executable),
            },
        ),
        source_dir=str(source_dir),
    )

    with pytest.raises(MCPConnectionError) as raised:
        await MCPClientAdapter(config)._remote_headers()

    message = str(raised.value)
    assert "output too large" in message
    assert "runtime-secret" not in message
    assert "Authorization: Bearer" not in message


@pytest.mark.asyncio
async def test_remote_headers_helper_stderr_is_redacted_in_diagnostics(tmp_path: Path) -> None:
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    _write_headers_helper(
        source_dir,
        """
import sys

sys.stderr.write("Authorization: Bearer runtime-secret\\n")
sys.stderr.write("URL https://user:password@example.com/mcp\\n")
print("{not-json")
""",
    )
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "{} ./scripts/mcp_headers.py".format(sys.executable),
            },
        ),
        source_dir=str(source_dir),
    )

    with pytest.raises(MCPConnectionError) as raised:
        await MCPClientAdapter(config)._remote_headers()

    message = str(raised.value)
    assert "runtime-secret" not in message
    assert "Authorization: Bearer" not in message
    assert "user:password" not in message
    assert "https://[redacted]@example.com/mcp" in message
    assert "[redacted]" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('print("[1, 2]")', "JSON object"),
        ("print('{\"X-Org\": 1}')", "string header"),
    ],
)
async def test_remote_headers_helper_rejects_invalid_json_shapes(tmp_path: Path, body: str, match: str) -> None:
    source_dir = tmp_path / "project"
    source_dir.mkdir()
    _write_headers_helper(source_dir, body)
    config = replace(
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "{} ./scripts/mcp_headers.py".format(sys.executable),
            },
        ),
        source_dir=str(source_dir),
    )

    with pytest.raises(MCPConnectionError, match=match):
        await MCPClientAdapter(config)._remote_headers()


@pytest.mark.asyncio
async def test_timed_out_session_operation_does_not_stop_worker(monkeypatch) -> None:
    release = threading.Event()

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def list_resources(self):
            await asyncio.to_thread(release.wait, 2)
            return [{"uri": "resource://slow"}]

        async def list_tools(self):
            return [{"name": "plan"}]

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))
    await adapter.connect()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.list_resources(), timeout=0.01)
        release.set()
        await asyncio.sleep(0.05)

        assert await adapter.list_tools() == [{"name": "plan"}]
    finally:
        await adapter.close()


def test_stdio_adapter_operations_survive_event_loop_boundary(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            self.closed = False

        async def __aenter__(self):
            seen["session"] = self
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            return None

        async def list_tools(self):
            return [{"name": "plan"}]

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))

    asyncio.run(adapter.connect())
    try:
        assert asyncio.run(asyncio.wait_for(adapter.list_tools(), timeout=1.0)) == [{"name": "plan"}]
    finally:
        asyncio.run(adapter.close())

    assert seen["session"].closed is True


@pytest.mark.asyncio
async def test_websocket_adapter_initializes_sdk_session_with_url(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_websocket_client(url):
        seen["url"] = url
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            seen["read_stream"] = read_stream
            seen["write_stream"] = write_stream
            seen["list_roots_callback"] = list_roots_callback
            self.closed = False

        async def __aenter__(self):
            seen["session"] = self
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            seen["initialized"] = True

        async def list_tools(self):
            return [{"name": "plan"}]

    import mcp.client.session as session_module
    import mcp.client.websocket as websocket_module

    monkeypatch.setattr(websocket_module, "websocket_client", fake_websocket_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("remote-ws", {"type": "ws", "url": "wss://example.com/mcp"})
    )

    await adapter.connect()

    assert seen["url"] == "wss://example.com/mcp"
    assert seen["initialized"] is True
    assert await adapter.list_tools() == [{"name": "plan"}]

    await adapter.close()
    assert seen["session"].closed is True


@pytest.mark.asyncio
async def test_adapter_forwards_list_changed_notifications(monkeypatch) -> None:
    changed: list[str] = []
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            self.original_notifications = 0

        async def __aenter__(self):
            seen["session"] = self
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def _received_notification(self, notification):
            self.original_notifications += 1

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        list_changed_callback=lambda capability: changed.append(capability),
    )
    await adapter.connect()

    await seen["session"]._received_notification(
        SimpleNamespace(root=SimpleNamespace(method="notifications/tools/list_changed"))
    )

    assert seen["session"].original_notifications == 1
    assert changed == ["tools"]


@pytest.mark.asyncio
async def test_adapter_forwards_request_elicitation_to_callback(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            seen["elicitation_callback"] = elicitation_callback

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module
    from mcp import types

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    requests: list[dict[str, Any]] = []

    async def elicitation_callback(params: Mapping[str, Any]) -> Mapping[str, Any]:
        requests.append(dict(params))
        return {"action": "accept", "content": {"region": "cn-hangzhou"}}

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        elicitation_callback=elicitation_callback,
    )
    await adapter.connect()
    try:
        result = await seen["elicitation_callback"](
            None,
            types.ElicitRequestFormParams(
                message="Choose region",
                requestedSchema={"type": "object"},
            ),
        )
    finally:
        await adapter.close()

    assert result.action == "accept"
    assert result.content == {"region": "cn-hangzhou"}
    assert requests == [{"mode": "form", "message": "Choose region", "requestedSchema": {"type": "object"}}]


@pytest.mark.asyncio
async def test_adapter_retries_tool_call_after_url_elicitation(monkeypatch) -> None:
    attempts = 0
    events: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            nonlocal attempts
            attempts += 1
            events.append(("call_tool", attempts))
            if attempts == 1:
                raise _url_elicitation_error()
            return {"content": [{"type": "text", "text": name}], "arguments": arguments or {}}

        async def send_notification(self, notification):
            events.append(("notification", notification))

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    elicitations: list[dict[str, Any]] = []

    async def elicitation_callback(params: Mapping[str, Any]) -> Mapping[str, Any]:
        elicitations.append(dict(params))
        return {"action": "accept"}

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        elicitation_callback=elicitation_callback,
    )
    await adapter.connect()
    try:
        result = await adapter.call_tool("authorize", {"stack": "demo"})
    finally:
        await adapter.close()

    assert result == {"content": [{"type": "text", "text": "authorize"}], "arguments": {"stack": "demo"}}
    assert attempts == 2
    assert events[0] == ("call_tool", 1)
    assert events[2] == ("call_tool", 2)
    notification = events[1][1]
    assert notification.method == "notifications/elicitation/complete"
    assert notification.params.elicitationId == "auth-1"
    assert elicitations == [
        {
            "mode": "url",
            "message": "Authorize access",
            "url": "https://auth.example/authorize",
            "elicitationId": "auth-1",
        }
    ]


@pytest.mark.asyncio
async def test_adapter_does_not_retry_or_complete_cancelled_url_elicitation(monkeypatch) -> None:
    attempts = 0
    notifications: list[Any] = []

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            nonlocal attempts
            attempts += 1
            raise _url_elicitation_error()

        async def send_notification(self, notification):
            notifications.append(notification)

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    def cancel_elicitation(params: Mapping[str, Any]) -> Mapping[str, Any]:
        _ = params
        return {"action": "cancel"}

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        elicitation_callback=cancel_elicitation,
    )
    await adapter.connect()
    try:
        with pytest.raises(MCPConnectionError):
            await adapter.call_tool("authorize", {})
    finally:
        await adapter.close()

    assert attempts == 1
    assert notifications == []


@pytest.mark.asyncio
async def test_adapter_bounds_url_elicitation_retry_attempts(monkeypatch) -> None:
    attempts = 0
    notifications: list[Any] = []

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            nonlocal attempts
            attempts += 1
            raise _url_elicitation_error("auth-{}".format(attempts))

        async def send_notification(self, notification):
            notifications.append(notification)

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    def accept_elicitation(params: Mapping[str, Any]) -> Mapping[str, Any]:
        _ = params
        return {"action": "accept"}

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        elicitation_callback=accept_elicitation,
    )
    await adapter.connect()
    try:
        with pytest.raises(MCPConnectionError):
            await adapter.call_tool("authorize", {})
    finally:
        await adapter.close()

    assert attempts == 4
    assert [item.params.elicitationId for item in notifications] == ["auth-1", "auth-2", "auth-3"]


@pytest.mark.asyncio
async def test_adapter_does_not_retry_non_url_elicitation_mcp_errors(monkeypatch) -> None:
    attempts = 0

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            nonlocal attempts
            attempts += 1
            raise _generic_mcp_error()

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    elicitations: list[dict[str, Any]] = []

    def record_elicitation(params: Mapping[str, Any]) -> Mapping[str, Any]:
        elicitations.append(dict(params))
        return {"action": "accept"}

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        elicitation_callback=record_elicitation,
    )
    await adapter.connect()
    try:
        with pytest.raises(MCPConnectionError):
            await adapter.call_tool("broken", {})
    finally:
        await adapter.close()

    assert attempts == 1
    assert elicitations == []


@pytest.mark.asyncio
async def test_http_adapter_marks_session_terminated_mcp_error_as_session_expiry(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        yield object(), object(), lambda: "session-1"

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            raise _session_terminated_mcp_error()

    import mcp.client.session as session_module
    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example/mcp"}))
    await adapter.connect()
    try:
        with pytest.raises(MCPConnectionError) as raised:
            await adapter.call_tool("search", {})
    finally:
        await adapter.close()

    assert getattr(raised.value, "mcp_session_expired", False) is True
    assert "session expired" in str(raised.value)


@pytest.mark.asyncio
async def test_http_adapter_does_not_mark_generic_mcp_error_as_session_expiry(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        yield object(), object(), lambda: "session-1"

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            raise _generic_mcp_error()

    import mcp.client.session as session_module
    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example/mcp"}))
    await adapter.connect()
    try:
        with pytest.raises(MCPConnectionError) as raised:
            await adapter.call_tool("broken", {})
    finally:
        await adapter.close()

    assert getattr(raised.value, "mcp_session_expired", False) is False
    assert "ordinary MCP failure" in str(raised.value)


@pytest.mark.asyncio
async def test_adapter_close_cancels_pending_url_elicitation(monkeypatch) -> None:
    entered = threading.Event()
    blocker: asyncio.Event | None = None

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            raise _url_elicitation_error()

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    async def elicitation_callback(params: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal blocker
        _ = params
        blocker = asyncio.Event()
        entered.set()
        await blocker.wait()
        return {"action": "accept"}

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        elicitation_callback=elicitation_callback,
    )
    await adapter.connect()
    call_task = asyncio.create_task(adapter.call_tool("authorize", {}))
    assert await asyncio.to_thread(entered.wait, 1)

    try:
        await asyncio.wait_for(adapter.close(), timeout=1)
        thread = adapter._worker_thread
        assert thread is None or not thread.is_alive()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call_task, timeout=1)
    finally:
        loop = adapter._loop
        if blocker is not None and loop is not None:
            loop.call_soon_threadsafe(blocker.set)
        if not call_task.done():
            call_task.cancel()
        with suppress(asyncio.CancelledError, MCPConnectionError):
            await call_task
        await adapter.close()


@pytest.mark.asyncio
async def test_adapter_close_stops_original_worker_thread_during_blocking_url_elicitation(monkeypatch) -> None:
    entered_input = threading.Event()
    release_input = threading.Event()

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            raise _url_elicitation_error()

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    async def elicitation_callback(params: Mapping[str, Any]) -> Mapping[str, Any]:
        _ = params

        def wait_for_user_input() -> None:
            entered_input.set()
            release_input.wait(5)

        await asyncio.get_running_loop().run_in_executor(None, wait_for_user_input)
        return {"action": "accept"}

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        elicitation_callback=elicitation_callback,
    )
    await adapter.connect()
    original_thread = adapter._worker_thread
    assert original_thread is not None

    call_task = asyncio.create_task(adapter.call_tool("authorize", {}))
    assert await asyncio.to_thread(entered_input.wait, 1)

    try:
        await asyncio.wait_for(adapter.close(), timeout=1)
        assert not original_thread.is_alive()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call_task, timeout=1)
    finally:
        release_input.set()
        if not call_task.done():
            call_task.cancel()
        with suppress(asyncio.CancelledError, MCPConnectionError):
            await call_task
        await adapter.close()


@pytest.mark.asyncio
async def test_adapter_call_cancellation_cancels_pending_url_elicitation(monkeypatch) -> None:
    entered = threading.Event()
    blocker: asyncio.Event | None = None

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None, elicitation_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, **kwargs):
            raise _url_elicitation_error()

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    async def elicitation_callback(params: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal blocker
        _ = params
        blocker = asyncio.Event()
        entered.set()
        await blocker.wait()
        return {"action": "accept"}

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        elicitation_callback=elicitation_callback,
    )
    await adapter.connect()
    call_task = asyncio.create_task(adapter.call_tool("authorize", {}))
    assert await asyncio.to_thread(entered.wait, 1)

    call_task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call_task, timeout=1)
        await asyncio.wait_for(adapter.close(), timeout=1)
        thread = adapter._worker_thread
        assert thread is None or not thread.is_alive()
    finally:
        loop = adapter._loop
        if blocker is not None and loop is not None:
            loop.call_soon_threadsafe(blocker.set)
        with suppress(asyncio.CancelledError, MCPConnectionError):
            await call_task
        await adapter.close()


@pytest.mark.asyncio
async def test_stdio_adapter_captures_bounded_stderr_on_connect_failure(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        assert errlog is not None
        errlog.write("debug line\n")
        try:
            yield object(), object()
        finally:
            errlog.write("fatal line\n")

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            raise RuntimeError("init failed")

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))

    with pytest.raises(MCPConnectionError, match="fatal line"):
        await adapter.connect()

    assert adapter.metadata is not None
    assert adapter.metadata.stderr_tail is not None
    assert "fatal line" in adapter.metadata.stderr_tail


@pytest.mark.asyncio
async def test_stdio_adapter_redacts_stderr_on_connect_failure(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        assert errlog is not None
        errlog.write("Authorization: Bearer runtime-secret\n")
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            raise RuntimeError("init failed")

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))

    with pytest.raises(MCPConnectionError) as raised:
        await adapter.connect()

    message = str(raised.value)
    assert "runtime-secret" not in message
    assert "Authorization: Bearer" not in message
    assert "[redacted]" in message


@pytest.mark.asyncio
async def test_stdio_adapter_close_cleans_up_worker_after_connect_timeout(monkeypatch) -> None:
    initialized = threading.Event()
    closed = threading.Event()

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            closed.set()

        async def initialize(self):
            initialized.set()
            await asyncio.Event().wait()

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adapter.connect(), timeout=0.1)

    assert initialized.wait(timeout=1)
    await adapter.close()
    assert closed.wait(timeout=1)


@pytest.mark.asyncio
async def test_stdio_adapter_close_after_connect_timeout_runs_stdio_cleanup_and_captures_stderr(monkeypatch) -> None:
    initialized = threading.Event()
    stdio_closed = threading.Event()

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        assert errlog is not None
        errlog.write("server starting\n")
        try:
            yield object(), object()
        finally:
            errlog.write("shutdown Authorization: Bearer runtime-secret\n")
            stdio_closed.set()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            initialized.set()
            await asyncio.Event().wait()

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adapter.connect(), timeout=0.1)

    assert initialized.wait(timeout=1)
    await asyncio.wait_for(adapter.close(), timeout=1)

    assert stdio_closed.wait(timeout=1)
    assert adapter.stderr_tail is not None
    assert "server starting" in adapter.stderr_tail
    assert "shutdown" in adapter.stderr_tail
    assert "runtime-secret" not in adapter.stderr_tail
    assert "Authorization: Bearer" not in adapter.stderr_tail
    assert "[redacted]" in adapter.stderr_tail
    assert adapter._worker_thread is None


@pytest.mark.asyncio
async def test_stdio_adapter_close_uses_bounded_wait_when_sdk_cleanup_blocks(monkeypatch) -> None:
    entered_cleanup = threading.Event()
    release_cleanup = threading.Event()

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        try:
            yield object(), object()
        finally:
            entered_cleanup.set()
            await asyncio.to_thread(release_cleanup.wait, 5)

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))
    adapter._close_timeout_seconds = 0.05

    await adapter.connect()
    original_thread = adapter._worker_thread
    assert original_thread is not None

    await asyncio.wait_for(adapter.close(), timeout=0.5)
    assert entered_cleanup.wait(timeout=1)
    assert original_thread.is_alive()
    assert adapter._worker_thread is original_thread
    with pytest.raises(MCPConnectionError, match="closing"):
        await asyncio.wait_for(adapter.connect(), timeout=0.5)
    with pytest.raises(MCPConnectionError, match="closing"):
        await asyncio.wait_for(adapter.list_tools(), timeout=0.5)

    release_cleanup.set()
    for _attempt in range(50):
        if not original_thread.is_alive():
            break
        await asyncio.sleep(0.02)
    assert not original_thread.is_alive()
    assert adapter._worker_thread is None


@pytest.mark.asyncio
async def test_stdio_adapter_close_fallback_terminates_process_when_sdk_cleanup_raises(monkeypatch) -> None:
    created: dict[str, Any] = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin, self._stdin_reader = anyio.create_memory_object_stream[bytes](0)
            self._stdout_sender, self.stdout = anyio.create_memory_object_stream[bytes](0)
            self.terminated = False
            self.returncode: int | None = None

        async def __aenter__(self) -> "FakeProcess":
            return self

        async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
            await self.stdin.aclose()
            await self._stdin_reader.aclose()
            await self._stdout_sender.aclose()
            await self.stdout.aclose()

        async def wait(self) -> int:
            raise RuntimeError("stdio cleanup failed before process termination")

        async def mark_terminated(self) -> None:
            self.terminated = True
            self.returncode = -9
            await self._stdout_sender.aclose()
            await self.stdout.aclose()

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.terminated = True
            self.returncode = -9

    class ExplodingWaitProcess:
        def __init__(self, process: FakeProcess) -> None:
            self._iac_code_fallback_process = process

        @property
        def stdin(self) -> Any:
            return self._iac_code_fallback_process.stdin

        @property
        def stdout(self) -> Any:
            return self._iac_code_fallback_process.stdout

        @property
        def returncode(self) -> int | None:
            return self._iac_code_fallback_process.returncode

        async def __aenter__(self) -> "ExplodingWaitProcess":
            return self

        async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
            return None

        async def wait(self) -> int:
            raise RuntimeError("stdio cleanup failed before process termination")

        def terminate(self) -> None:
            self._iac_code_fallback_process.terminate()

        def kill(self) -> None:
            self._iac_code_fallback_process.kill()

    async def fake_create_process(command, args, env=None, errlog=None, cwd=None):
        _ = command, args, env, errlog, cwd
        process = FakeProcess()
        created["process"] = process
        return ExplodingWaitProcess(process)

    async def fake_terminate_process_tree(process: Any) -> None:
        await process.mark_terminated()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "_create_platform_compatible_process", fake_create_process)
    monkeypatch.setattr(stdio_module, "_terminate_process_tree", fake_terminate_process_tree)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": sys.executable}))

    try:
        await adapter.connect()
        process = created["process"]
        assert process.terminated is False

        await asyncio.wait_for(adapter.close(), timeout=1)

        assert process.terminated is True
        assert process.returncode == -9
    finally:
        process = created.get("process")
        if process is not None:
            await process.mark_terminated()


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "fork") or not hasattr(os, "getpgid"),
    reason="POSIX process-group semantics require os.fork and os.getpgid",
)
@pytest.mark.asyncio
async def test_stdio_adapter_close_fallback_terminates_owned_process_group_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}
    info_path = tmp_path / "stdio-processes.txt"
    script = tmp_path / "forking_stdio_server.py"
    script.write_text(
        """
import os
import pathlib
import signal
import sys
import time

info_path = pathlib.Path(sys.argv[1])
child_pid = os.fork()
if child_pid == 0:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with info_path.open("a", encoding="utf-8") as handle:
        handle.write(f"child {os.getpid()} {os.getpgid(0)}\\n")
        handle.flush()
    while True:
        time.sleep(60)

with info_path.open("a", encoding="utf-8") as handle:
    handle.write(f"parent {os.getpid()} {os.getpgid(0)} {child_pid}\\n")
    handle.flush()
while True:
    time.sleep(60)
""".lstrip(),
        encoding="utf-8",
    )

    class ExplodingWaitProcess:
        def __init__(self, process: Any) -> None:
            self._iac_code_fallback_process = process

        @property
        def stdin(self) -> Any:
            return self._iac_code_fallback_process.stdin

        @property
        def stdout(self) -> Any:
            return self._iac_code_fallback_process.stdout

        @property
        def pid(self) -> int:
            return self._iac_code_fallback_process.pid

        @property
        def returncode(self) -> int | None:
            return self._iac_code_fallback_process.returncode

        async def __aenter__(self) -> "ExplodingWaitProcess":
            return self

        async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
            return None

        async def wait(self) -> int:
            raise RuntimeError("stdio cleanup failed before process termination")

        def terminate(self) -> None:
            self._iac_code_fallback_process.terminate()

        def kill(self) -> None:
            self._iac_code_fallback_process.kill()

    async def fake_create_process(command, args, env=None, errlog=None, cwd=None):
        _ = env, errlog, cwd
        process = await anyio.open_process([command, *args], start_new_session=True)
        created["process"] = process
        return ExplodingWaitProcess(process)

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "_create_platform_compatible_process", fake_create_process)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    unrelated = await anyio.open_process(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": sys.executable, "args": [str(script), str(info_path)]})
    )
    parent_pid: int | None = None
    child_pid: int | None = None

    try:
        await adapter.connect()
        process = created["process"]
        parent_pid, parent_pgid, child_pid, child_pgid = await _read_stdio_process_tree(info_path)

        assert process.pid == parent_pid
        assert parent_pgid == child_pgid
        assert os.getpgid(unrelated.pid) != parent_pgid
        assert _process_is_alive(parent_pid)
        assert _process_is_alive(child_pid)
        assert _process_is_alive(unrelated.pid)

        await asyncio.wait_for(adapter.close(), timeout=3)

        assert await _wait_for_process_exit(parent_pid)
        assert await _wait_for_process_exit(child_pid)
        assert _process_is_alive(unrelated.pid)
    finally:
        for pid in (child_pid, parent_pid):
            if pid is not None and _process_is_alive(pid):
                with suppress(ProcessLookupError):
                    os.kill(pid, 9)
        process = created.get("process")
        if process is not None and _process_is_alive(process.pid):
            process.kill()
            with suppress(Exception):
                await process.wait()
        if _process_is_alive(unrelated.pid):
            unrelated.kill()
            with suppress(Exception):
                await unrelated.wait()


@pytest.mark.asyncio
async def test_http_401_www_authenticate_without_oauth_raises_needs_auth(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
        },
    )

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        seen["url"] = url
        seen["auth"] = auth
        request = httpx.Request("GET", url)
        response = httpx.Response(
            401,
            request=request,
            headers={"WWW-Authenticate": 'Bearer realm="mcp", error="invalid_token"'},
        )
        raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)
        yield object(), object(), None

    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)

    adapter = MCPClientAdapter(config)

    with pytest.raises(MCPNeedsAuthError) as raised:
        await adapter.connect()

    assert seen["url"] == "https://mcp.example.com/mcp"
    assert seen["auth"] is None
    assert getattr(raised.value, "auth_error", None) == "invalid_token"


@pytest.mark.asyncio
async def test_http_401_session_expired_without_auth_challenge_reconnects(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
        },
    )

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        request = httpx.Request("GET", url)
        response = httpx.Response(
            401,
            request=request,
            text="MCP session expired: mcp-session-id is no longer valid",
        )
        raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)
        yield object(), object(), None

    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)

    adapter = MCPClientAdapter(config)

    with pytest.raises(MCPConnectionError) as raised:
        await adapter.connect()

    assert getattr(raised.value, "mcp_session_expired", False) is True
    assert "session expired" in str(raised.value)


@pytest.mark.asyncio
async def test_http_403_generic_session_text_without_expiry_marker_requires_auth(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
        },
    )

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request, text="session lacks required scope")
        raise httpx.HTTPStatusError("403 Forbidden", request=request, response=response)
        yield object(), object(), None

    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)

    adapter = MCPClientAdapter(config)

    with pytest.raises(MCPNeedsAuthError) as raised:
        await adapter.connect()

    assert getattr(raised.value, "mcp_session_expired", False) is False


@pytest.mark.asyncio
async def test_http_403_insufficient_scope_auth_challenge_reports_required_scopes(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        request = httpx.Request("GET", url)
        response = httpx.Response(
            403,
            request=request,
            headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="mcp write:stack"'},
        )
        raise httpx.HTTPStatusError("403 Forbidden", request=request, response=response)
        yield object(), object(), None

    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)

    adapter = MCPClientAdapter(config)

    with pytest.raises(MCPNeedsAuthError) as raised:
        await adapter.connect()

    assert getattr(raised.value, "auth_error", None) == "insufficient_scope"
    assert getattr(raised.value, "required_scopes", ()) == ("mcp", "write:stack")
    assert "write:stack" in str(raised.value)


@pytest.mark.asyncio
async def test_http_invalid_client_auth_challenge_clears_registered_client_state(monkeypatch) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
            "oauth": {},
        },
    )
    storage = FakeSecretStorage()
    storage.set_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER), "access-token")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope=MCPConfigScope.USER), "refresh-token")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope=MCPConfigScope.USER), "9999999999")
    storage.set_secret(oauth_storage_key(config, "client_id", scope=MCPConfigScope.USER), "registered-client")
    storage.set_secret(oauth_storage_key(config, "client_secret", scope=MCPConfigScope.USER), "registered-secret")
    storage.set_secret(oauth_storage_key(config, "client_auth_method", scope=MCPConfigScope.USER), "client_secret_post")

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        request = httpx.Request("GET", url)
        response = httpx.Response(
            401,
            request=request,
            headers={"WWW-Authenticate": 'Bearer error="invalid_client"'},
        )
        raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)
        yield object(), object(), None

    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)

    adapter = MCPClientAdapter(config, scope=MCPConfigScope.USER, secret_storage=cast(MCPSecretStorage, storage))

    with pytest.raises(MCPNeedsAuthError):
        await adapter.connect()

    for kind in ("access_token", "refresh_token", "expires_at", "client_id", "client_secret", "client_auth_method"):
        assert storage.get_secret(oauth_storage_key(config, kind, scope=MCPConfigScope.USER)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [MCPTransport.HTTP, MCPTransport.SSE])
async def test_remote_transport_auth_provider_uses_valid_cached_token_without_discovery(
    monkeypatch,
    transport,
) -> None:
    seen: dict[str, Any] = {}
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": transport.value,
            "url": "https://mcp.example.com/path/mcp",
            "headers": {"X-Org": "iac"},
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = FakeSecretStorage()
    storage.set_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER), "cached-token")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope=MCPConfigScope.USER), "9999999999")

    def discover_oauth_metadata(config: MCPServerConfig) -> OAuthMetadata:
        raise AssertionError("metadata discovery should be deferred for valid cached tokens")

    monkeypatch.setattr(oauth_module, "discover_oauth_metadata", discover_oauth_metadata)

    async def exercise_auth_provider(auth: Any) -> None:
        assert auth is not None
        request = httpx.Request("GET", config.url or "")
        flow = auth.async_auth_flow(request)
        authed_request = await flow.__anext__()
        seen["authorization"] = authed_request.headers.get("Authorization")
        try:
            await flow.asend(httpx.Response(200, request=authed_request, json={}))
        except StopAsyncIteration:
            pass

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["auth"] = auth
        await exercise_auth_provider(auth)
        yield object(), object(), None

    @asynccontextmanager
    async def fake_sse_client(url, headers=None, auth=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["auth"] = auth
        await exercise_auth_provider(auth)
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.sse as sse_module
    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(sse_module, "sse_client", fake_sse_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(config, scope=MCPConfigScope.USER, secret_storage=cast(MCPSecretStorage, storage))
    await adapter.connect()

    assert seen["url"] == "https://mcp.example.com/path/mcp"
    assert seen["headers"] == {"X-Org": "iac"}
    assert seen["auth"] is not None
    assert seen["authorization"] == "Bearer cached-token"

    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [MCPTransport.HTTP, MCPTransport.SSE])
async def test_remote_transport_client_metadata_uses_valid_cached_token_without_discovery(
    monkeypatch,
    transport,
) -> None:
    seen: dict[str, Any] = {}
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": transport.value,
            "url": "https://mcp.example.com/path/mcp",
            "headers": {"X-Org": "iac"},
            "oauth": {
                "clientId": "client-id",
                "clientMetadataUrl": "https://metadata.example.com/client.json",
            },
        },
    )
    storage = FakeSecretStorage()
    storage.set_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER), "cached-token")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope=MCPConfigScope.USER), "9999999999")

    def discover_oauth_metadata(config: MCPServerConfig) -> OAuthMetadata:
        raise AssertionError("metadata discovery should be deferred for valid cached tokens")

    monkeypatch.setattr(oauth_module, "discover_oauth_metadata", discover_oauth_metadata)

    async def exercise_auth_provider(auth: Any) -> None:
        assert auth is not None
        request = httpx.Request("GET", config.url or "")
        flow = auth.async_auth_flow(request)
        authed_request = await flow.__anext__()
        seen["authorization"] = authed_request.headers.get("Authorization")
        try:
            await flow.asend(httpx.Response(200, request=authed_request, json={}))
        except StopAsyncIteration:
            pass

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["auth"] = auth
        await exercise_auth_provider(auth)
        yield object(), object(), None

    @asynccontextmanager
    async def fake_sse_client(url, headers=None, auth=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["auth"] = auth
        await exercise_auth_provider(auth)
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.sse as sse_module
    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(sse_module, "sse_client", fake_sse_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(config, scope=MCPConfigScope.USER, secret_storage=cast(MCPSecretStorage, storage))
    await adapter.connect()

    assert seen["url"] == "https://mcp.example.com/path/mcp"
    assert seen["headers"] == {"X-Org": "iac"}
    assert seen["auth"] is not None
    assert seen["authorization"] == "Bearer cached-token"

    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [MCPTransport.HTTP, MCPTransport.SSE])
async def test_remote_transport_auth_provider_refreshes_expired_token_with_resource(monkeypatch, transport) -> None:
    seen: dict[str, Any] = {}
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": transport.value,
            "url": "https://mcp.example.com/path/mcp",
            "headers": {"X-Org": "iac"},
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = FakeSecretStorage()
    storage.set_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER), "expired-token")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope=MCPConfigScope.USER), "refresh-token")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope=MCPConfigScope.USER), "100")

    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config: OAuthMetadata(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
            resource="https://mcp.example.com",
        ),
    )

    refresh_calls: list[tuple[str, dict[str, str], dict[str, str] | None]] = []

    def post_token(url: str, data: dict[str, str], *, headers: dict[str, str] | None = None) -> dict[str, object]:
        refresh_calls.append((url, dict(data), headers))
        return {"access_token": "refreshed-token", "expires_in": 3600, "token_type": "Bearer"}

    monkeypatch.setattr(oauth_module, "_post_token", post_token)

    async def exercise_auth_provider(auth: Any) -> None:
        assert auth is not None
        assert hasattr(auth, "async_auth_flow")
        request = httpx.Request("GET", config.url or "")
        flow = auth.async_auth_flow(request)
        authed_request = await flow.__anext__()
        seen["request_url"] = str(authed_request.url)
        seen["authorization"] = authed_request.headers.get("Authorization")
        try:
            await flow.asend(httpx.Response(200, request=authed_request, json={}))
        except StopAsyncIteration:
            pass

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["auth"] = auth
        await exercise_auth_provider(auth)
        yield object(), object(), None

    @asynccontextmanager
    async def fake_sse_client(url, headers=None, auth=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["auth"] = auth
        await exercise_auth_provider(auth)
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.sse as sse_module
    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(sse_module, "sse_client", fake_sse_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(config, scope=MCPConfigScope.USER, secret_storage=cast(MCPSecretStorage, storage))
    await adapter.connect()

    assert seen["url"] == "https://mcp.example.com/path/mcp"
    assert seen["headers"] == {"X-Org": "iac"}
    assert seen["auth"] is not None
    assert seen["request_url"] == "https://mcp.example.com/path/mcp"
    assert refresh_calls == [
        (
            "https://auth.example/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "resource": "https://mcp.example.com",
            },
            None,
        )
    ]
    assert seen["authorization"] == "Bearer refreshed-token"
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)) == "refreshed-token"

    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [MCPTransport.HTTP, MCPTransport.SSE])
async def test_remote_transport_cached_token_auth_challenge_propagates_original_response(
    monkeypatch,
    transport,
) -> None:
    seen: dict[str, Any] = {}
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": transport.value,
            "url": "https://mcp.example.com/path/mcp",
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = FakeSecretStorage()
    storage.set_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER), "cached-token")
    storage.set_secret(oauth_storage_key(config, "expires_at", scope=MCPConfigScope.USER), "9999999999")

    def discover_oauth_metadata(config: MCPServerConfig) -> OAuthMetadata:
        raise AssertionError("transport auth must not start interactive discovery from a 403 challenge")

    monkeypatch.setattr(oauth_module, "discover_oauth_metadata", discover_oauth_metadata)

    async def exercise_auth_provider(auth: Any) -> httpx.Request:
        assert auth is not None
        request = httpx.Request("GET", config.url or "")
        flow = auth.async_auth_flow(request)
        authed_request = await flow.__anext__()
        seen["authorization"] = authed_request.headers.get("Authorization")
        response = httpx.Response(
            403,
            request=authed_request,
            headers={
                "WWW-Authenticate": (
                    'Bearer error="insufficient_scope", '
                    'scope="mcp write:stack", '
                    'resource_metadata="https://resource.example/.well-known/oauth-protected-resource/mcp"'
                )
            },
        )
        with pytest.raises(StopAsyncIteration):
            await flow.asend(response)
        return authed_request

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None, auth=None):
        request = await exercise_auth_provider(auth)
        response = httpx.Response(
            403,
            request=request,
            headers={
                "WWW-Authenticate": (
                    'Bearer error="insufficient_scope", '
                    'scope="mcp write:stack", '
                    'resource_metadata="https://resource.example/.well-known/oauth-protected-resource/mcp"'
                )
            },
        )
        raise httpx.HTTPStatusError("403 Forbidden", request=request, response=response)
        yield object(), object(), None

    @asynccontextmanager
    async def fake_sse_client(url, headers=None, auth=None):
        request = await exercise_auth_provider(auth)
        response = httpx.Response(
            403,
            request=request,
            headers={
                "WWW-Authenticate": (
                    'Bearer error="insufficient_scope", '
                    'scope="mcp write:stack", '
                    'resource_metadata="https://resource.example/.well-known/oauth-protected-resource/mcp"'
                )
            },
        )
        raise httpx.HTTPStatusError("403 Forbidden", request=request, response=response)
        yield object(), object()

    import mcp.client.sse as sse_module
    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(sse_module, "sse_client", fake_sse_client)

    adapter = MCPClientAdapter(config, scope=MCPConfigScope.USER, secret_storage=cast(MCPSecretStorage, storage))

    with pytest.raises(MCPNeedsAuthError) as raised:
        await adapter.connect()

    assert seen["authorization"] == "Bearer cached-token"
    assert getattr(raised.value, "auth_error", None) == "insufficient_scope"
    assert getattr(raised.value, "required_scopes", ()) == ("mcp", "write:stack")
    assert getattr(raised.value, "auth_resource_metadata_url", None) == (
        "https://resource.example/.well-known/oauth-protected-resource/mcp"
    )


class FakeSecretStorage:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def set_secret(self, key: str, value: str) -> None:
        self._values[key] = value

    def get_secret(self, key: str) -> str | None:
        return self._values.get(key)

    def delete_secret(self, key: str) -> None:
        self._values.pop(key, None)

    def lock(self, key: str):
        _ = key
        return nullcontext()


def _write_headers_helper(base_dir: Path, body: str) -> Path:
    path = base_dir / "scripts" / "mcp_headers.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.lstrip(), encoding="utf-8")
    return path


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, SystemError):
        return False
    return True


async def _read_stdio_process_tree(path: Path) -> tuple[int, int, int, int]:
    parent: tuple[int, int, int] | None = None
    child: tuple[int, int] | None = None
    for _attempt in range(100):
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 4 and parts[0] == "parent":
                    parent = (int(parts[1]), int(parts[2]), int(parts[3]))
                elif len(parts) == 3 and parts[0] == "child":
                    child = (int(parts[1]), int(parts[2]))
            if parent is not None and child is not None:
                parent_pid, parent_pgid, parent_child_pid = parent
                child_pid, child_pgid = child
                assert parent_child_pid == child_pid
                return parent_pid, parent_pgid, child_pid, child_pgid
        await asyncio.sleep(0.02)
    raise AssertionError("stdio process tree did not report parent and child PIDs")


async def _wait_for_process_exit(pid: int, *, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _process_is_alive(pid):
            return True
        await asyncio.sleep(0.02)
    return not _process_is_alive(pid)


def _url_elicitation_error(elicitation_id: str = "auth-1") -> Exception:
    from mcp import types
    from mcp.shared.exceptions import UrlElicitationRequiredError

    return UrlElicitationRequiredError(
        [
            types.ElicitRequestURLParams(
                message="Authorize access",
                url="https://auth.example/authorize",
                elicitationId=elicitation_id,
            )
        ]
    )


def _generic_mcp_error() -> Exception:
    from mcp import types
    from mcp.shared.exceptions import McpError

    return McpError(types.ErrorData(code=-32000, message="ordinary MCP failure"))


def _session_terminated_mcp_error() -> Exception:
    from mcp import types
    from mcp.shared.exceptions import McpError

    return McpError(types.ErrorData(code=32600, message="Session terminated"))
