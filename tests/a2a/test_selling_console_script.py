from __future__ import annotations

import html as html_lib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "selling_console.py"
SCRIPTS_README_PATH = Path(__file__).resolve().parents[2] / "scripts" / "README.md"
NODE_RELATIVE_PATH = Path(".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
RECOVERABLE_JSONRPC_ERROR = {
    "jsonrpc": "2.0",
    "id": "1",
    "error": {
        "code": -32602,
        "message": "Pipeline already running.",
        "data": {
            "recoverableTaskId": "task-owner",
            "contextId": "ctx-1",
            "sidecarStatus": "running",
        },
    },
}


def bundled_node_candidates() -> list[Path]:
    override = os.environ.get("IAC_CODE_TEST_NODE")
    if override:
        return [Path(override).expanduser()]
    candidates = [Path.home() / NODE_RELATIVE_PATH]
    home_env = os.environ.get("HOME")
    if home_env:
        candidates.append(Path(home_env).expanduser() / NODE_RELATIVE_PATH)
    candidates.extend(parent / NODE_RELATIVE_PATH for parent in SCRIPT_PATH.parents)
    return candidates


def node_command() -> list[str]:
    node = shutil.which("node")
    if node:
        return [node]
    for fallback in bundled_node_candidates():
        if fallback.exists():
            return [str(fallback)]
    pytest.skip("node is not installed")


def test_scripts_readme_mentions_selling_console() -> None:
    readme = SCRIPTS_README_PATH.read_text(encoding="utf-8")

    assert "a2a/selling_console.py" in readme
    assert "Selling pipeline console" in readme


def load_module():
    spec = importlib.util.spec_from_file_location("selling_console", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class JsonTargetHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body: dict[str, Any] = {"ok": True}
    response_headers: dict[str, str] = {"Content-Type": "application/json"}
    requests: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        self._record_request()
        self._send_response()

    def do_POST(self) -> None:
        self._record_request()
        self._send_response()

    def _record_request(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": raw_body.decode("utf-8") if raw_body else "",
            }
        )

    def _send_response(self) -> None:
        body = json.dumps(self.__class__.response_body).encode("utf-8")
        self.send_response(self.__class__.response_status)
        for name, value in self.__class__.response_headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SseTargetHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_POST(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        self.__class__.requests.append(
            {
                "headers": dict(self.headers.items()),
                "body": raw_body.decode("utf-8"),
            }
        )
        body = (
            b'data: {"jsonrpc":"2.0","result":{"id":"task-1","contextId":"ctx-1",'
            b'"status":{"state":"TASK_STATE_WORKING"}}}\n\n'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def serve_handler(handler_cls: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def start_console(console, *, default_cwd: str = "/workspace/demo"):
    config = console.SellingConsoleConfig(
        host="127.0.0.1",
        port=0,
        default_server_url="http://127.0.0.1:41299",
        default_cwd=default_cwd,
    )
    server = console.create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    class RunningServer:
        url = f"http://{host}:{port}"

        def close(self) -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    return RunningServer()


def test_create_server_disables_address_reuse_on_windows(monkeypatch) -> None:
    console = load_module()
    monkeypatch.setattr(console.sys, "platform", "win32")
    config = console.SellingConsoleConfig(
        host="127.0.0.1",
        port=0,
        default_server_url="http://127.0.0.1:41299",
        default_cwd="/workspace/demo",
    )

    server = console.create_server(config)
    try:
        assert server.allow_reuse_address is False
    finally:
        server.server_close()


def get_text(url: str) -> tuple[int, str, str]:
    with urlopen(url, timeout=5) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")


def get_json(url: str) -> tuple[int, dict[str, Any]]:
    with urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def get_json_error(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            raw_body = exc.read()
        finally:
            exc.close()
        return exc.code, json.loads(raw_body.decode("utf-8"))


def post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def post_json_error(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            raw_body = exc.read()
        finally:
            exc.close()
        return exc.code, json.loads(raw_body.decode("utf-8"))


def post_raw(url: str, body: dict[str, Any]) -> tuple[int, str, str]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return response.status, response.read().decode("utf-8"), response.headers.get("Content-Type", "")


def post_raw_response(url: str, body: dict[str, Any]) -> tuple[int, str, str]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8"), response.headers.get("Content-Type", "")
    except HTTPError as exc:
        try:
            raw_body = exc.read()
        finally:
            exc.close()
        return exc.code, raw_body.decode("utf-8"), exc.headers.get("Content-Type", "")
    except RemoteDisconnected as exc:
        return 0, str(exc), ""


def raw_http_request(url: str, request_text: str) -> tuple[int, str]:
    parsed = urlparse(url)
    assert parsed.hostname is not None
    assert parsed.port is not None
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(request_text.encode("ascii"))
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)

    response = b"".join(chunks).decode("utf-8", errors="replace")
    status_line = response.splitlines()[0] if response else ""
    status_parts = status_line.split()
    status = int(status_parts[1]) if len(status_parts) >= 2 and status_parts[0].startswith("HTTP/") else 0
    _, _, body = response.partition("\r\n\r\n")
    return status, body


def test_pipeline_state_route_requires_context_or_task() -> None:
    console = load_module()
    running = start_console(console)
    try:
        query = urlencode({"serverUrl": "http://127.0.0.1:41299"})
        with pytest.raises(HTTPError) as exc_info:
            get_json(f"{running.url}/api/pipeline/state?{query}")
    finally:
        running.close()

    assert exc_info.value.code == 400
    try:
        response_body = json.loads(exc_info.value.read().decode("utf-8"))
    finally:
        exc_info.value.close()
    assert response_body == {"ok": False, "error": "contextId or taskId is required"}


def test_pipeline_state_route_proxies_query_parameters() -> None:
    console = load_module()
    JsonTargetHandler.requests = []
    JsonTargetHandler.response_body = {"snapshot": {"status": "working"}}

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            query = urlencode({"serverUrl": target, "contextId": "ctx-1", "taskId": "task-1", "afterSequence": "7"})
            status, body = get_json(f"{running.url}/api/pipeline/state?{query}")
        finally:
            running.close()

    assert status == 200
    assert body == {"snapshot": {"status": "working"}}
    assert JsonTargetHandler.requests[0]["path"] == (
        "/iac-code/pipeline/state?contextId=ctx-1&taskId=task-1&afterSequence=7"
    )


def test_task_get_route_sends_get_task_jsonrpc() -> None:
    console = load_module()
    JsonTargetHandler.requests = []
    JsonTargetHandler.response_body = {"jsonrpc": "2.0", "result": {"id": "task-1"}}

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            query = urlencode({"serverUrl": target, "taskId": "task-1", "historyLength": "2"})
            status, body = get_json(f"{running.url}/api/task/get?{query}")
        finally:
            running.close()

    assert status == 200
    assert body == {"jsonrpc": "2.0", "result": {"id": "task-1"}}
    payload = json.loads(JsonTargetHandler.requests[0]["body"])
    assert payload["method"] == "GetTask"
    assert payload["params"] == {"id": "task-1", "historyLength": 2}


def test_task_cancel_route_sends_cancel_task_jsonrpc() -> None:
    console = load_module()
    JsonTargetHandler.requests = []
    JsonTargetHandler.response_body = {"jsonrpc": "2.0", "result": {"id": "task-1"}}

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            status, body = post_json(f"{running.url}/api/task/cancel", {"serverUrl": target, "taskId": "task-1"})
        finally:
            running.close()

    assert status == 200
    assert body == {"jsonrpc": "2.0", "result": {"id": "task-1"}}
    payload = json.loads(JsonTargetHandler.requests[0]["body"])
    assert payload["method"] == "CancelTask"
    assert payload["params"] == {"id": "task-1"}


def test_message_stream_route_forwards_sse_and_cwd_metadata() -> None:
    console = load_module()
    SseTargetHandler.requests = []

    with serve_handler(SseTargetHandler) as target:
        running = start_console(console)
        try:
            status, text, content_type = post_raw(
                f"{running.url}/api/message/stream",
                {
                    "serverUrl": target,
                    "cwd": "/workspace/demo",
                    "iacCodeModel": " kimi-k2.7-code ",
                    "prompt": "部署一个静态网站",
                },
            )
        finally:
            running.close()

    assert status == 200
    assert "event-stream" in content_type
    assert "TASK_STATE_WORKING" in text
    payload = json.loads(SseTargetHandler.requests[0]["body"])
    assert payload["method"] == "SendStreamingMessage"
    assert payload["params"]["message"]["metadata"] == {
        "iac_code": {"cwd": "/workspace/demo", "iac_code_model": "kimi-k2.7-code"}
    }


def test_message_stream_route_surfaces_recoverable_task_id_from_jsonrpc_error() -> None:
    console = load_module()
    JsonTargetHandler.response_status = 200
    JsonTargetHandler.response_body = RECOVERABLE_JSONRPC_ERROR
    JsonTargetHandler.response_headers = {"Content-Type": "application/json"}
    JsonTargetHandler.requests = []

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            status, text, content_type = post_raw(
                f"{running.url}/api/message/stream",
                {"serverUrl": target, "cwd": "/workspace/demo", "prompt": "部署一个静态网站"},
            )
        finally:
            running.close()

    assert status == 200
    assert "event-stream" in content_type
    assert "data: " in text
    assert "Pipeline already running." in text
    assert "task-owner" in text


def test_selling_console_web_extracts_delivery_task_aliases() -> None:
    console = load_module()
    app_js = (console.WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert '"deliveryTaskId"' in app_js
    assert '"deliveryContextId"' in app_js


def test_message_stream_route_keeps_read_errors_in_sse_body(monkeypatch: pytest.MonkeyPatch) -> None:
    console = load_module()

    class TimedOutSseStream:
        status = 200

        def __init__(self) -> None:
            self._sent_first_event = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            if self._sent_first_event:
                raise TimeoutError("upstream timed out")
            self._sent_first_event = True
            return b'data: {"ok": true, "event": "first"}\n\n'

    def open_sse_stream(server_url: str, payload: dict[str, Any]) -> TimedOutSseStream:
        assert server_url == "http://127.0.0.1:41299"
        assert payload["method"] == "SendStreamingMessage"
        return TimedOutSseStream()

    monkeypatch.setattr(console.a2a_debugger, "_open_sse_stream", open_sse_stream)

    running = start_console(console)
    try:
        status, text, content_type = post_raw(
            f"{running.url}/api/message/stream",
            {
                "serverUrl": "http://127.0.0.1:41299",
                "cwd": "/workspace/demo",
                "prompt": "部署一个静态网站",
            },
        )
    finally:
        running.close()

    assert status == 200
    assert "event-stream" in content_type
    assert 'data: {"ok": true, "event": "first"}' in text
    assert '"ok": false' in text
    assert "upstream timed out" in text
    assert "HTTP/" not in text
    assert "Content-Type:" not in text


def test_message_stream_route_reports_upstream_reset_before_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    console = load_module()

    def open_sse_stream(server_url: str, payload: dict[str, Any]):
        assert server_url == "http://127.0.0.1:41299"
        assert payload["method"] == "SendStreamingMessage"
        raise ConnectionResetError("upstream reset before headers")

    monkeypatch.setattr(console.a2a_debugger, "_open_sse_stream", open_sse_stream)

    running = start_console(console)
    try:
        status, text, content_type = post_raw_response(
            f"{running.url}/api/message/stream",
            {
                "serverUrl": "http://127.0.0.1:41299",
                "cwd": "/workspace/demo",
                "prompt": "部署一个静态网站",
            },
        )
    finally:
        running.close()

    assert status == 502
    assert "event-stream" in content_type
    assert '"ok": false' in text
    assert "upstream reset before headers" in text


def test_message_stream_route_keeps_upstream_reset_during_stream_in_sse_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = load_module()

    class ResettingSseStream:
        status = 200

        def __init__(self) -> None:
            self._sent_first_event = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            if self._sent_first_event:
                raise ConnectionResetError("upstream reset during stream")
            self._sent_first_event = True
            return b'data: {"ok": true, "event": "first"}\n\n'

    def open_sse_stream(server_url: str, payload: dict[str, Any]) -> ResettingSseStream:
        assert server_url == "http://127.0.0.1:41299"
        assert payload["method"] == "SendStreamingMessage"
        return ResettingSseStream()

    monkeypatch.setattr(console.a2a_debugger, "_open_sse_stream", open_sse_stream)

    running = start_console(console)
    try:
        status, text, content_type = post_raw(
            f"{running.url}/api/message/stream",
            {
                "serverUrl": "http://127.0.0.1:41299",
                "cwd": "/workspace/demo",
                "prompt": "部署一个静态网站",
            },
        )
    finally:
        running.close()

    assert status == 200
    assert "event-stream" in content_type
    assert 'data: {"ok": true, "event": "first"}' in text
    assert '"ok": false' in text
    assert "upstream reset during stream" in text
    assert "HTTP/" not in text
    assert "Content-Type:" not in text


def test_message_stream_route_does_not_rewrite_headers_after_client_write_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = load_module()
    send_sse_error_calls: list[tuple[int, str]] = []
    write_attempts: list[bytes] = []

    class OneLineSseStream:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def __iter__(self):
            yield b'data: {"ok": true, "event": "first"}\n\n'

    class BodyWriteFailingWriter:
        def __init__(self, wrapped) -> None:
            self._wrapped = wrapped

        def write(self, data: bytes) -> int:
            write_attempts.append(data)
            raise OSError("disk full-ish write failure")

        def __getattr__(self, name: str):
            return getattr(self._wrapped, name)

    def open_sse_stream(server_url: str, payload: dict[str, Any]) -> OneLineSseStream:
        assert server_url == "http://127.0.0.1:41299"
        assert payload["method"] == "SendStreamingMessage"
        return OneLineSseStream()

    original_end_headers = console.BaseHTTPRequestHandler.end_headers

    def end_headers(handler) -> None:
        original_end_headers(handler)
        if handler.path == "/api/message/stream":
            handler.wfile = BodyWriteFailingWriter(handler.wfile)

    def send_sse_error(handler, status: int, message: str) -> None:
        send_sse_error_calls.append((status, message))

    monkeypatch.setattr(console.a2a_debugger, "_open_sse_stream", open_sse_stream)
    monkeypatch.setattr(console.BaseHTTPRequestHandler, "end_headers", end_headers)
    monkeypatch.setattr(console.a2a_debugger, "_send_sse_error", send_sse_error)

    running = start_console(console)
    try:
        status, _text, content_type = post_raw(
            f"{running.url}/api/message/stream",
            {
                "serverUrl": "http://127.0.0.1:41299",
                "cwd": "/workspace/demo",
                "prompt": "部署一个静态网站",
            },
        )
    finally:
        running.close()

    assert status == 200
    assert "event-stream" in content_type
    assert write_attempts == [b'data: {"ok": true, "event": "first"}\n\n']
    assert send_sse_error_calls == []


def test_message_stream_route_does_not_rewrite_headers_when_sse_error_event_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = load_module()
    send_json_calls: list[tuple[int, Any]] = []
    send_sse_error_calls: list[tuple[int, str]] = []
    write_attempts: list[bytes] = []

    class TimedOutSseStream:
        status = 200

        def __init__(self) -> None:
            self._sent_first_event = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            if self._sent_first_event:
                raise TimeoutError("upstream timed out")
            self._sent_first_event = True
            return b'data: {"ok": true, "event": "first"}\n\n'

    class ErrorEventWriteFailingWriter:
        def __init__(self, wrapped) -> None:
            self._wrapped = wrapped

        def write(self, data: bytes) -> int:
            write_attempts.append(data)
            if len(write_attempts) == 2:
                raise OSError("cannot write error event")
            return self._wrapped.write(data)

        def __getattr__(self, name: str):
            return getattr(self._wrapped, name)

    def open_sse_stream(server_url: str, payload: dict[str, Any]) -> TimedOutSseStream:
        assert server_url == "http://127.0.0.1:41299"
        assert payload["method"] == "SendStreamingMessage"
        return TimedOutSseStream()

    original_end_headers = console.BaseHTTPRequestHandler.end_headers

    def end_headers(handler) -> None:
        original_end_headers(handler)
        if handler.path == "/api/message/stream":
            handler.wfile = ErrorEventWriteFailingWriter(handler.wfile)

    def send_json(handler, status: int, value: Any) -> None:
        send_json_calls.append((status, value))

    def send_sse_error(handler, status: int, message: str) -> None:
        send_sse_error_calls.append((status, message))

    monkeypatch.setattr(console.a2a_debugger, "_open_sse_stream", open_sse_stream)
    monkeypatch.setattr(console.BaseHTTPRequestHandler, "end_headers", end_headers)
    monkeypatch.setattr(console.a2a_debugger, "_send_json", send_json)
    monkeypatch.setattr(console.a2a_debugger, "_send_sse_error", send_sse_error)

    running = start_console(console)
    try:
        status, text, content_type = post_raw(
            f"{running.url}/api/message/stream",
            {
                "serverUrl": "http://127.0.0.1:41299",
                "cwd": "/workspace/demo",
                "prompt": "部署一个静态网站",
            },
        )
    finally:
        running.close()

    assert status == 200
    assert "event-stream" in content_type
    assert text == 'data: {"ok": true, "event": "first"}\n\n'
    assert [attempt.startswith(b"data: ") for attempt in write_attempts] == [True, True]
    assert send_json_calls == []
    assert send_sse_error_calls == []


def test_implemented_post_route_rejects_malformed_json() -> None:
    console = load_module()
    running = start_console(console)
    try:
        target = urlparse(running.url)
        status, body = raw_http_request(
            running.url,
            "\r\n".join(
                [
                    "POST /api/task/cancel HTTP/1.1",
                    f"Host: {target.hostname}:{target.port}",
                    "Content-Type: application/json",
                    "Content-Length: 1",
                    "Connection: close",
                    "",
                    "{",
                ]
            ),
        )
    finally:
        running.close()

    assert status == 400
    response_body = json.loads(body)
    assert response_body["ok"] is False
    assert response_body["error"] == "Request body must be valid JSON"


def test_unimplemented_post_route_ignores_malformed_content_length() -> None:
    console = load_module()
    running = start_console(console)
    try:
        target = urlparse(running.url)
        status, body = raw_http_request(
            running.url,
            "\r\n".join(
                [
                    "POST /api/not-found HTTP/1.1",
                    f"Host: {target.hostname}:{target.port}",
                    "Content-Type: application/json",
                    "Content-Length: nope",
                    "Connection: close",
                    "",
                    "",
                ]
            ),
        )
    finally:
        running.close()

    assert status == 404
    assert json.loads(body)["error"] == "Not found"


def test_parse_args_defaults_to_loopback_and_current_directory(monkeypatch, tmp_path: Path) -> None:
    console = load_module()
    monkeypatch.chdir(tmp_path)

    args = console.parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 41980
    assert args.default_server_url == "http://127.0.0.1:41299"
    assert args.default_cwd == str(tmp_path)


def test_script_help_exits_successfully() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Run a local A2A selling pipeline console." in result.stdout


def test_index_route_serves_selling_console_html(tmp_path: Path) -> None:
    console = load_module()
    running = start_console(console, default_cwd=str(tmp_path))
    try:
        status, content_type, html = get_text(running.url)
    finally:
        running.close()

    assert status == 200
    assert "text/html" in content_type
    assert "阿里云" in html
    assert "您的购买方案" in html
    assert "window.SELLING_CONSOLE_DEFAULTS" in html
    assert "http://127.0.0.1:41299" in html
    assert str(tmp_path) in html


def test_index_html_escapes_defaults_json_for_script_context() -> None:
    console = load_module()
    html = console.render_index_html(
        console.SellingConsoleConfig(
            host="127.0.0.1",
            port=41980,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="</script><script>alert(1)</script>",
        )
    )

    defaults_start = html.index("window.SELLING_CONSOLE_DEFAULTS = ")
    script_end = html.index("</script>", defaults_start)
    defaults_assignment = html[defaults_start:script_end]

    assert "</script><script>" not in defaults_assignment
    assert "<\\/script><script>alert(1)<\\/script>" in defaults_assignment


def test_index_html_escapes_visible_default_input_values() -> None:
    console = load_module()
    default_server_url = "http://example.test/<api>?x='single'&y=\"q\""
    default_cwd = "/tmp/<demo> & 'single' \"quoted\""

    html = console.render_index_html(
        console.SellingConsoleConfig(
            host="127.0.0.1",
            port=41980,
            default_server_url=default_server_url,
            default_cwd=default_cwd,
        )
    )

    server_marker = 'id="server-url"'
    server_tag_start = html.rindex("<input", 0, html.index(server_marker))
    server_tag = html[server_tag_start : html.index(">", server_tag_start)]
    cwd_marker = 'id="cwd"'
    cwd_tag_start = html.rindex("<input", 0, html.index(cwd_marker))
    cwd_tag = html[cwd_tag_start : html.index(">", cwd_tag_start)]

    assert 'value="http://example.test/&lt;api&gt;?x=&#x27;single&#x27;&amp;y=&quot;q&quot;"' in server_tag
    assert "<api>" not in server_tag
    assert "x='single'" not in server_tag
    assert 'y="q"' not in server_tag
    assert 'value="/tmp/&lt;demo&gt; &amp; &#x27;single&#x27; &quot;quoted&quot;"' in cwd_tag
    assert "<demo>" not in cwd_tag
    assert "& 'single'" not in cwd_tag
    assert '"quoted"' not in cwd_tag

    defaults_start = html.index("window.SELLING_CONSOLE_DEFAULTS = ") + len("window.SELLING_CONSOLE_DEFAULTS = ")
    defaults_end = html.index(";", defaults_start)
    assert json.loads(html[defaults_start:defaults_end]) == {
        "serverUrl": default_server_url,
        "cwd": default_cwd,
    }


def test_index_html_does_not_reprocess_template_placeholders_inside_defaults() -> None:
    console = load_module()
    default_server_url = "__DEFAULT_CWD_ATTR__"
    default_cwd = "__DEFAULT_SERVER_URL_ATTR__"

    html = console.render_index_html(
        console.SellingConsoleConfig(
            host="127.0.0.1",
            port=41980,
            default_server_url=default_server_url,
            default_cwd=default_cwd,
        )
    )

    defaults_start = html.index("window.SELLING_CONSOLE_DEFAULTS = ") + len("window.SELLING_CONSOLE_DEFAULTS = ")
    defaults_end = html.index(";", defaults_start)
    assert json.loads(html[defaults_start:defaults_end]) == {
        "serverUrl": default_server_url,
        "cwd": default_cwd,
    }
    server_marker = 'id="server-url"'
    server_tag_start = html.rindex("<input", 0, html.index(server_marker))
    server_tag = html[server_tag_start : html.index(">", server_tag_start)]
    cwd_marker = 'id="cwd"'
    cwd_tag_start = html.rindex("<input", 0, html.index(cwd_marker))
    cwd_tag = html[cwd_tag_start : html.index(">", cwd_tag_start)]

    assert 'value="&#95;&#95;DEFAULT&#95;CWD&#95;ATTR&#95;&#95;"' in server_tag
    assert 'value="&#95;&#95;DEFAULT&#95;SERVER&#95;URL&#95;ATTR&#95;&#95;"' in cwd_tag
    assert 'value="__DEFAULT_CWD_ATTR__"' in html_lib.unescape(server_tag)
    assert 'value="__DEFAULT_SERVER_URL_ATTR__"' in html_lib.unescape(cwd_tag)
    assert "__DEFAULTS_JSON__" not in html
    assert "__DEFAULT_SERVER_URL_ATTR__" not in html
    assert "__DEFAULT_CWD_ATTR__" not in html


def test_index_html_contains_screenshot_layout_regions() -> None:
    console = load_module()

    html = console.render_index_html(
        console.SellingConsoleConfig(
            host="127.0.0.1",
            port=41980,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        'class="topbar"',
        'id="workflow-panel"',
        'id="status-pill"',
        'id="status-alert"',
        'id="step-list"',
        'aria-label="Pipeline 实时步骤"',
        'id="composer-progress"',
        'aria-label="Pipeline 总进度"',
        'id="plans-grid"',
        'id="composer-input"',
        'id="send-button"',
        'id="deep-think-button"',
        'id="debug-drawer"',
        'id="server-url"',
        'id="cwd"',
        'id="iac-code-model"',
        'id="health-button"',
        'id="fetch-state-button"',
        'id="cancel-button"',
        'aria-label="帮助"',
        'aria-label="刷新"',
        'aria-label="反馈"',
        'aria-label="设置"',
        "您的购买方案",
        "内容由 AI 生成，方案与价格仅供参考",
    ]:
        assert expected in html
    assert '<article class="step-card current">' not in html


def test_index_html_uses_cache_busted_static_assets() -> None:
    console = load_module()

    html = console.render_index_html(
        console.SellingConsoleConfig(
            host="127.0.0.1",
            port=41980,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    assert 'href="/styles.css?v=' in html
    assert 'src="/app.js?v=' in html
    assert "selling-console-20260618" not in html
    assert "__STATIC_ASSET_VERSION__" not in html


def test_styles_define_console_layout_tokens() -> None:
    css = (SCRIPT_PATH.parent / "selling_console_web" / "styles.css").read_text(encoding="utf-8")

    for expected in [
        "--aliyun-orange",
        ".console-shell",
        ".workflow-panel",
        ".plan-card",
        ".price",
        ".utility-rail",
        "@media (max-width: 980px)",
        "overflow-x: hidden",
        "minmax(0, 1fr)",
        ":focus-visible",
        ":focus-within",
        "overflow-wrap: anywhere",
    ]:
        assert expected in css


def test_frontend_javascript_is_syntax_valid() -> None:
    app_js = SCRIPT_PATH.parent / "selling_console_web" / "app.js"

    result = subprocess.run([*node_command(), "--check", str(app_js)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_static_asset_route_serves_css_and_js() -> None:
    console = load_module()
    running = start_console(console)
    try:
        css_status, css_type, css = get_text(f"{running.url}/styles.css")
        js_status, js_type, js = get_text(f"{running.url}/app.js")
        css_query_status, css_query_type, css_query = get_text(f"{running.url}/styles.css?v=test")
        js_query_status, js_query_type, js_query = get_text(f"{running.url}/app.js?v=test")
    finally:
        running.close()

    assert css_status == 200
    assert "text/css" in css_type
    assert ".topbar" in css
    assert js_status == 200
    assert "javascript" in js_type
    assert "SellingConsoleReducers" in js
    assert css_query_status == 200
    assert "text/css" in css_query_type
    assert css_query == css
    assert js_query_status == 200
    assert "javascript" in js_query_type
    assert js_query == js


def test_static_asset_route_rejects_path_traversal() -> None:
    console = load_module()
    running = start_console(console)
    try:
        status, response_body = get_json_error(f"{running.url}/../debugger.py")
    finally:
        running.close()

    assert status == 404
    assert response_body["error"] == "Not found"


def test_health_route_proxies_a2a_health_and_agent_card() -> None:
    console = load_module()
    JsonTargetHandler.requests = []
    JsonTargetHandler.response_body = {"status": "ok"}

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            query = urlencode({"serverUrl": target})
            status, body = get_json(f"{running.url}/api/health?{query}")
        finally:
            running.close()

    assert status == 200
    assert body["ok"] is True
    assert [request["path"] for request in JsonTargetHandler.requests] == ["/health", "/.well-known/agent-card.json"]
