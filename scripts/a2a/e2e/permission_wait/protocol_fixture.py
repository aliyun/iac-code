"""Strict local model endpoints for the real provider permission-batch E2Es."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOOL_IDS = ["tool-1", "tool-2", "tool-3"]


def validate_canceled_request(protocol: str, payload: dict[str, Any]) -> None:
    """Validate the wire contract independently of the internal Message objects."""
    if protocol == "chat":
        messages = payload["messages"]
        assert messages[-1]["role"] == "user" and messages[-1]["content"] == "new prompt"
        calls = [call for message in messages for call in message.get("tool_calls", [])]
        results = [message for message in messages if message["role"] == "tool"]
        assert [call["id"] for call in calls] == TOOL_IDS
        assert all(call["function"]["name"] == "write_test" for call in calls)
        assert [result["tool_call_id"] for result in results] == TOOL_IDS
        assert all("canceled before execution" in result["content"] for result in results)
        assert messages[-4:-1] == results
    elif protocol == "responses":
        items = payload["input"]
        calls = [item for item in items if item.get("type") == "function_call"]
        results = [item for item in items if item.get("type") == "function_call_output"]
        assert [call["call_id"] for call in calls] == TOOL_IDS
        assert all(call["name"] == "write_test" for call in calls)
        assert [result["call_id"] for result in results] == TOOL_IDS
        assert all("canceled before execution" in result["output"] for result in results)
        assert items[-1]["role"] == "user"
        assert items[-1]["content"] in ("new prompt", [{"type": "input_text", "text": "new prompt"}])
        assert items[-4:-1] == results
    else:
        assert protocol == "anthropic"
        messages = payload["messages"]
        calls = [
            block
            for message in messages
            if isinstance(message["content"], list)
            for block in message["content"]
            if block["type"] == "tool_use"
        ]
        assert [call["id"] for call in calls] == TOOL_IDS
        assert all(call["name"] == "write_test" for call in calls)
        # Anthropic may coalesce consecutive user messages. Both shapes must
        # retain the tool results before the new user text.
        user_blocks = []
        for message in messages[messages.index(next(m for m in messages if m.get("role") == "assistant")) + 1 :]:
            assert message["role"] == "user"
            content = message["content"]
            user_blocks.extend(content if isinstance(content, list) else [{"type": "text", "text": content}])
        results = [block for block in user_blocks if block["type"] == "tool_result"]
        assert [result["tool_use_id"] for result in results] == TOOL_IDS
        assert all(
            result["is_error"] is True and "canceled before execution" in result["content"] for result in results
        )
        assert user_blocks[-1]["type"] == "text" and user_blocks[-1]["text"] == "new prompt"


class ModelServer:
    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.fixture = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:{}".format(self.httpd.server_port) + ("" if self.protocol == "anthropic" else "/v1")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        fixture = self.server.fixture
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            expected_path = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "anthropic": "/v1/messages"}
            assert self.path == expected_path[fixture.protocol], self.path
            assert payload["stream"] is True
            if fixture.requests:
                validate_canceled_request(fixture.protocol, payload)
            fixture.requests.append(payload)
        except (AssertionError, KeyError, TypeError, StopIteration) as exc:
            fixture.errors.append(repr(exc))
            self.send_error(400, "invalid fixture model request")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        first = len(fixture.requests) == 1
        if fixture.protocol == "chat":
            if first:
                for index, tool_id in enumerate(TOOL_IDS):
                    self.event(
                        {
                            "id": "msg",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": "fixture",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": index,
                                                "id": tool_id,
                                                "type": "function",
                                                "function": {
                                                    "name": "write_test",
                                                    "arguments": json.dumps({"value": tool_id}),
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
            else:
                self.event(
                    {
                        "id": "msg",
                        "choices": [{"index": 0, "delta": {"content": "handled new prompt"}, "finish_reason": None}],
                    }
                )
            self.event(
                {
                    "id": "msg",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if first else "stop"}],
                }
            )
            self.wfile.write(b"data: [DONE]\n\n")
        elif fixture.protocol == "responses":
            output = (
                [
                    {
                        "type": "function_call",
                        "id": "fc_" + tool_id,
                        "call_id": tool_id,
                        "name": "write_test",
                        "arguments": json.dumps({"value": tool_id}),
                        "status": "completed",
                    }
                    for tool_id in TOOL_IDS
                ]
                if first
                else [
                    {
                        "type": "message",
                        "id": "msg",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "handled new prompt", "annotations": []}],
                    }
                ]
            )
            self.event(
                {
                    "type": "response.completed",
                    "sequence_number": 0,
                    "response": {
                        "id": "resp",
                        "object": "response",
                        "status": "completed",
                        "output": output,
                        "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
                    },
                },
                "response.completed",
            )
        else:
            self.event(
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
            if first:
                for index, tool_id in enumerate(TOOL_IDS):
                    self.event(
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {"type": "tool_use", "id": tool_id, "name": "write_test", "input": {}},
                        },
                        "content_block_start",
                    )
                    self.event(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": json.dumps({"value": tool_id})},
                        },
                        "content_block_delta",
                    )
                    self.event({"type": "content_block_stop", "index": index}, "content_block_stop")
            else:
                self.event(
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    "content_block_start",
                )
                self.event(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "handled new prompt"},
                    },
                    "content_block_delta",
                )
                self.event({"type": "content_block_stop", "index": 0}, "content_block_stop")
            self.event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use" if first else "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
                "message_delta",
            )
            self.event({"type": "message_stop"}, "message_stop")
        self.wfile.flush()

    def event(self, value, name=None):
        if name:
            self.wfile.write(("event: " + name + "\n").encode())
        self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())

    def log_message(self, *_args):
        pass
