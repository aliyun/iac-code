"""Offline Responses fixtures shared by provider and agent tests."""

from collections import deque
from typing import Any


def message(text="answer", **extra):
    return {
        "type": "message",
        "id": "msg1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        **extra,
    }


def reasoning(text="thought", **extra):
    return {
        "type": "reasoning",
        "id": "rs1",
        "summary": [{"type": "summary_text", "text": text}],
        "encrypted_content": "opaque-fixture",
        **extra,
    }


def call(call_id="call1", name="lookup", arguments='{"value": 1}', **extra):
    return {
        "type": "function_call",
        "id": "fc_" + call_id,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
        **extra,
    }


def response(output=None, **extra):
    return {
        "id": "resp1",
        "status": "completed",
        "output": [message()] if output is None else output,
        "usage": {"input_tokens": 20, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 7}},
        **extra,
    }


def terminal(data):
    return {"type": "response." + data["status"], "response": data}


class FakeResponseStream:
    def __init__(self, events):
        self.events = iter(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self):
        self.closed = True


class FakeResponsesClient:
    def __init__(self, *results: Any, base_url="https://api.openai.com/v1"):
        self.base_url = base_url
        self.results = deque(results)
        self.responses = self
        self.calls = []
        self.streams = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        if kwargs.get("stream"):
            stream = FakeResponseStream(result)
            self.streams.append(stream)
            return stream
        return result
