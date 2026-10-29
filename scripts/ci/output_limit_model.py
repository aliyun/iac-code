"""Local SSE model for output-limit process E2Es."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class OutputLimitModel:
    def __init__(self, protocol: str, scenario: str) -> None:
        self.protocol = protocol
        self.scenario = scenario
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.fixture = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        base = "http://127.0.0.1:{}".format(self.httpd.server_port)
        return base if self.protocol == "anthropic" else base + "/v1"

    def __enter__(self) -> OutputLimitModel:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        fixture: OutputLimitModel = self.server.fixture
        try:
            expected_path = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "anthropic": "/v1/messages"}
            assert self.path == expected_path[fixture.protocol], self.path
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert payload["stream"] is True
            fixture.requests.append(payload)
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            fixture.errors.append(repr(exc))
            self.send_error(400, "invalid fixture model request")
            return

        turn = len(fixture.requests)
        truncated = fixture.scenario in {"exhausted", "pipeline"} or turn == 1
        tool = fixture.scenario == "tool" and turn == 1
        text = "" if tool or fixture.scenario == "empty" else "first fragment" if turn == 1 else "second fragment"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if fixture.protocol == "chat":
            self._chat(text, tool, truncated)
        elif fixture.protocol == "responses":
            self._responses(text, tool, truncated)
        else:
            self._anthropic(text, tool, truncated)
        self.wfile.flush()

    def _chat(self, text: str, tool: bool, truncated: bool) -> None:
        delta: dict[str, Any] = {}
        if text:
            delta["content"] = text
        if tool:
            delta["tool_calls"] = [
                {
                    "index": 0,
                    "id": "discarded-call",
                    "type": "function",
                    "function": {"name": "write_test", "arguments": '{"value":"discarded"}'},
                }
            ]
        if delta:
            self._event(
                {
                    "id": "msg",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
            )
        self._event(
            {"id": "msg", "choices": [{"index": 0, "delta": {}, "finish_reason": "length" if truncated else "stop"}]}
        )
        self.wfile.write(b"data: [DONE]\n\n")

    def _responses(self, text: str, tool: bool, truncated: bool) -> None:
        output: list[dict[str, Any]] = []
        if tool:
            output.append(
                {
                    "type": "function_call",
                    "id": "fc-discarded",
                    "call_id": "discarded-call",
                    "name": "write_test",
                    "arguments": '{"value":',
                    "status": "in_progress",
                }
            )
            self._event(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc-discarded",
                        "call_id": "discarded-call",
                        "name": "write_test",
                        "arguments": "",
                        "status": "in_progress",
                    },
                },
                "response.output_item.added",
            )
            self._event(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "fc-discarded",
                    "delta": '{"value":',
                },
                "response.function_call_arguments.delta",
            )
            self._event(
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": 0,
                    "item_id": "fc-discarded",
                    "arguments": output[0]["arguments"],
                },
                "response.function_call_arguments.done",
            )
            output[0]["status"] = "incomplete"
            self._event(
                {"type": "response.output_item.done", "output_index": 0, "item": output[0]},
                "response.output_item.done",
            )
        elif text:
            output.append(
                {
                    "type": "message",
                    "id": "msg",
                    "role": "assistant",
                    "status": "incomplete" if truncated else "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
        response = {
            "id": "resp-{}".format(len(self.server.fixture.requests)),
            "object": "response",
            "status": "incomplete" if truncated else "completed",
            "output": output,
            "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        }
        if truncated:
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        kind = "response.incomplete" if truncated else "response.completed"
        self._event({"type": kind, "sequence_number": 0, "response": response}, kind)

    def _anthropic(self, text: str, tool: bool, truncated: bool) -> None:
        self._event(
            {
                "type": "message_start",
                "message": {
                    "id": "msg",
                    "type": "message",
                    "role": "assistant",
                    "model": "fixture",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 20, "output_tokens": 0},
                },
            },
            "message_start",
        )
        if tool:
            self._event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "discarded-call", "name": "write_test", "input": {}},
                },
                "content_block_start",
            )
            self._event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"value":"discarded"}'},
                },
                "content_block_delta",
            )
            self._event({"type": "content_block_stop", "index": 0}, "content_block_stop")
        elif text:
            self._event(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                "content_block_start",
            )
            self._event(
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                "content_block_delta",
            )
            self._event({"type": "content_block_stop", "index": 0}, "content_block_stop")
        self._event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "max_tokens" if truncated else "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
            "message_delta",
        )
        self._event({"type": "message_stop"}, "message_stop")

    def _event(self, value: dict[str, Any], name: str | None = None) -> None:
        if name:
            self.wfile.write(("event: " + name + "\n").encode())
        self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())

    def log_message(self, *_args: object) -> None:
        pass
