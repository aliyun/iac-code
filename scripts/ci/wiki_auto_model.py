"""Local model endpoint with explicit gates for Wiki process contracts."""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        fixture = self.server.fixture
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        helper = any("Wiki keyword extraction" in str(m.get("content")) for m in payload.get("messages", []))
        with fixture.changed:
            fixture.requests.append({"kind": "helper" if helper else "main", "payload": payload})
            scripts = fixture.helper_scripts if helper else fixture.main_scripts
            script = scripts.popleft() if scripts else {}
            fixture.changed.notify_all()
        if helper:
            keywords = list(script.get("keywords", fixture.keywords))
            fixture.helper_started.set()
            if not script.get("gate", fixture.release_helper).wait(30):
                fixture.errors.append("keyword gate was not released")
                self.send_error(500)
                return
            text = script.get("text", json.dumps({"keywords": keywords}))
            with fixture.lock:
                fixture.order.append("helper_done")
        else:
            fixture.main_started.set()
            with fixture.lock:
                fixture.order.append("main_started")
            if not script.get("gate", fixture.release_main).wait(30):
                fixture.errors.append("main gate was not released")
                self.send_error(500)
                return
            text = script.get("text", "main answer")
        calls = script.get("calls", [])
        tool_calls = [
            {
                "index": index,
                "id": f"call-{len(fixture.requests)}-{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for index, (name, arguments) in enumerate(calls)
        ]
        response = {
            "id": "wiki-fixture",
            "model": "gpt-4o-mini",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        }
        try:
            if script.get("status", 200) != 200:
                self.send_error(script["status"])
                return
            if not payload.get("stream"):
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            delta = {"role": "assistant", "content": text}
            if tool_calls:
                delta["tool_calls"] = tool_calls
            for delta, finish in ((delta, None), ({}, "tool_calls" if tool_calls else "stop")):
                chunk = {
                    "id": "wiki-fixture",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # Explicit cancellation may close the real provider's request.
        finally:
            with fixture.changed:
                fixture.completed.append("helper" if helper else "main")
                fixture.changed.notify_all()


class WikiModel:
    def __init__(self, keywords=None):
        self.keywords = ["B70"] if keywords is None else keywords
        self.requests = []
        self.order = []
        self.errors = []
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.completed = []
        self.helper_scripts = deque()
        self.main_scripts = deque()
        self.helper_started = threading.Event()
        self.main_started = threading.Event()
        self.release_helper = threading.Event()
        self.release_helper.set()
        self.release_main = threading.Event()
        self.release_main.set()

    def wait_requests(self, *, helper=0, main=0):
        with self.changed:
            return self.changed.wait_for(
                lambda: (
                    sum(r["kind"] == "helper" for r in self.requests) >= helper
                    and sum(r["kind"] == "main" for r in self.requests) >= main
                ),
                timeout=15,
            )

    def wait_completed(self, *, helper=0, main=0):
        with self.changed:
            return self.changed.wait_for(
                lambda: self.completed.count("helper") >= helper and self.completed.count("main") >= main,
                timeout=15,
            )

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.fixture = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        return self

    def __exit__(self, *args):
        self.release_helper.set()
        self.release_main.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
