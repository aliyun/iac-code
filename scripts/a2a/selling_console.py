"""Local web console for A2A selling pipelines.

The bundled web UI currently sends text input only; use the A2A debugger for
image-part request coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import json
import os
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

a2a_debugger = importlib.import_module("scripts.a2a.debugger")

WEB_ROOT = Path(__file__).resolve().with_name("selling_console_web")
STATIC_CONTENT_TYPES = {
    "/styles.css": "text/css; charset=utf-8",
    "/app.js": "application/javascript; charset=utf-8",
}
TEMPLATE_PLACEHOLDERS = (
    "__DEFAULTS_JSON__",
    "__DEFAULT_SERVER_URL_ATTR__",
    "__DEFAULT_CWD_ATTR__",
    "__STATIC_ASSET_VERSION__",
)


@dataclass(frozen=True)
class SellingConsoleConfig:
    host: str
    port: int
    default_server_url: str
    default_cwd: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local A2A selling pipeline console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=41980)
    parser.add_argument("--default-server-url", default="http://127.0.0.1:41299")
    parser.add_argument("--default-cwd", default=os.getcwd())
    return parser.parse_args(argv)


def _html_attribute_value(value: str) -> str:
    escaped = html.escape(value, quote=True)
    for placeholder in TEMPLATE_PLACEHOLDERS:
        escaped = escaped.replace(placeholder, placeholder.replace("_", "&#95;"))
    return escaped


def _json_for_template(value: object) -> str:
    json_value = a2a_debugger._json_for_script(value)
    for placeholder in TEMPLATE_PLACEHOLDERS:
        json_value = json_value.replace(placeholder, placeholder.replace("_", "\\u005f"))
    return json_value


def _static_asset_version() -> str:
    digest = hashlib.sha256()
    for asset_name in ("styles.css", "app.js"):
        digest.update(asset_name.encode("utf-8"))
        digest.update((WEB_ROOT / asset_name).read_bytes())
    return digest.hexdigest()[:12]


def render_index_html(config: SellingConsoleConfig) -> str:
    defaults_json = _json_for_template(
        {
            "serverUrl": config.default_server_url,
            "cwd": config.default_cwd,
        }
    )
    return (
        (WEB_ROOT / "index.html")
        .read_text(encoding="utf-8")
        .replace("__DEFAULT_SERVER_URL_ATTR__", _html_attribute_value(config.default_server_url))
        .replace("__DEFAULT_CWD_ATTR__", _html_attribute_value(config.default_cwd))
        .replace("__DEFAULTS_JSON__", defaults_json)
        .replace("__STATIC_ASSET_VERSION__", _static_asset_version())
    )


def _send_text(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    raw_body = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw_body)))
    handler.end_headers()
    handler.wfile.write(raw_body)


def _send_static(handler: BaseHTTPRequestHandler, path: str) -> bool:
    if path not in STATIC_CONTENT_TYPES:
        return False
    candidate = (WEB_ROOT / path.lstrip("/")).resolve()
    if not candidate.is_file() or WEB_ROOT.resolve() not in candidate.parents:
        return False
    _send_text(handler, 200, candidate.read_text(encoding="utf-8"), STATIC_CONTENT_TYPES[path])
    return True


def _proxy_error_body(exc: BaseException) -> dict[str, object]:
    if isinstance(exc, HTTPError):
        raw = exc.read()
        data, text = a2a_debugger._decode_json_text(raw)
        return a2a_debugger._proxy_error(
            a2a_debugger.ProxyResult(
                status_code=exc.code,
                data=data,
                text=text,
                headers=dict(exc.headers.items()),
                error=f"HTTP {exc.code}",
            )
        )
    return a2a_debugger._proxy_error(
        a2a_debugger.ProxyResult(status_code=0, data=None, text="", headers={}, error=str(exc))
    )


def _write_sse_error_event(handler: BaseHTTPRequestHandler, message: str) -> None:
    body = f"data: {json.dumps({'ok': False, 'error': message}, ensure_ascii=False)}\n\n".encode("utf-8")
    try:
        handler.wfile.write(body)
        handler.wfile.flush()
    except OSError:
        return


def create_server(config: SellingConsoleConfig) -> ThreadingHTTPServer:
    class SellingConsoleHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = sys.platform != "win32"

    class SellingConsoleHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    _send_text(self, 200, render_index_html(config), "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/health":
                    status, body = a2a_debugger._health_response(
                        a2a_debugger._query_params(self.path).get("serverUrl", "")
                    )
                    a2a_debugger._send_json(self, status, body)
                    return
                if parsed.path == "/api/pipeline/state":
                    status, body = a2a_debugger._pipeline_state_response(a2a_debugger._query_params(self.path))
                    a2a_debugger._send_json(self, status, body)
                    return
                if parsed.path == "/api/task/get":
                    status, body = a2a_debugger._task_get_response(a2a_debugger._query_params(self.path))
                    a2a_debugger._send_json(self, status, body)
                    return
                if _send_static(self, parsed.path):
                    return
            except ValueError as exc:
                a2a_debugger._send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                a2a_debugger._send_json(self, 502, _proxy_error_body(exc))
                return
            a2a_debugger._send_json(self, 404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/message/stream":
                    body = a2a_debugger._read_json_body(self)
                    server_url, payload = a2a_debugger._message_stream_body(body)
                    try:
                        with a2a_debugger._open_sse_stream(server_url, payload) as response:
                            headers = getattr(response, "headers", {})
                            content_type = ""
                            if hasattr(headers, "get"):
                                content_type = str(headers.get("Content-Type", "")).lower()
                            if content_type and "text/event-stream" not in content_type:
                                raw = response.read()
                                data, _text = a2a_debugger._decode_json_text(raw)
                                message = a2a_debugger._jsonrpc_error_message(data)
                                if message:
                                    a2a_debugger._send_sse_event(
                                        self,
                                        200,
                                        {
                                            "type": "error",
                                            "error": message,
                                            "statusCode": response.status,
                                            "body": data,
                                        },
                                    )
                                    return
                                a2a_debugger._send_sse_error(self, 502, "Target server returned a non-SSE response")
                                return
                            self.send_response(response.status)
                            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                            self.end_headers()
                            response_iter = iter(response)
                            while True:
                                try:
                                    line = next(response_iter)
                                except StopIteration:
                                    break
                                except (TimeoutError, URLError, OSError) as exc:
                                    _write_sse_error_event(self, str(exc))
                                    return
                                try:
                                    self.wfile.write(line)
                                    self.wfile.flush()
                                except OSError as exc:
                                    if a2a_debugger._is_client_disconnect_error(exc):
                                        return
                                    return
                    except HTTPError as exc:
                        a2a_debugger._send_sse_error(self, 502, f"HTTP {exc.code}")
                    except (TimeoutError, URLError, OSError) as exc:
                        a2a_debugger._send_sse_error(self, 502, str(exc))
                    return
                if parsed.path == "/api/task/cancel":
                    body = a2a_debugger._read_json_body(self)
                    status, response_body = a2a_debugger._task_cancel_response(body)
                    a2a_debugger._send_json(self, status, response_body)
                    return
            except ValueError as exc:
                a2a_debugger._send_json(self, 400, {"ok": False, "error": str(exc)})
                return
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                a2a_debugger._send_json(self, 502, _proxy_error_body(exc))
                return
            a2a_debugger._send_json(self, 404, {"ok": False, "error": "Not found"})

    return SellingConsoleHTTPServer((config.host, config.port), SellingConsoleHandler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = SellingConsoleConfig(
        host=args.host,
        port=args.port,
        default_server_url=args.default_server_url,
        default_cwd=args.default_cwd,
    )
    server = create_server(config)
    host, port = server.server_address
    print(f"Selling pipeline console listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
