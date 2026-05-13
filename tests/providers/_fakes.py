"""Fake SDK clients for provider tests.

Models the surface of `anthropic.AsyncAnthropic` and `openai.AsyncOpenAI`
that our provider code actually touches. Events are plain SimpleNamespace
instances — construction is cheap and the attribute shapes mirror what
the real SDKs return at runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterable


def ns(**kwargs: Any) -> SimpleNamespace:
    """Shorthand for building SDK-shaped event objects."""
    return SimpleNamespace(**kwargs)


# ------------------------------------------------------------------
# Anthropic fake
# ------------------------------------------------------------------


class _FakeAnthropicStream:
    def __init__(self, events: Iterable[Any], final_message: Any) -> None:
        self._events = list(events)
        self._final_message = final_message

    async def __aenter__(self) -> "_FakeAnthropicStream":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def __aiter__(self) -> "_FakeAnthropicStream":
        self._iter = iter(self._events)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def get_final_message(self) -> Any:
        return self._final_message


class _FakeAnthropicMessages:
    def __init__(
        self,
        stream_events: Iterable[Any] | None = None,
        stream_final: Any = None,
        create_response: Any = None,
    ) -> None:
        self._stream_events = list(stream_events or [])
        self._stream_final = stream_final
        self._create_response = create_response
        self.stream_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeAnthropicStream:
        self.stream_calls.append(kwargs)
        return _FakeAnthropicStream(self._stream_events, self._stream_final)

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return self._create_response


class FakeAnthropicClient:
    """Drop-in replacement for `anthropic.AsyncAnthropic` in provider tests."""

    def __init__(
        self,
        stream_events: Iterable[Any] | None = None,
        stream_final: Any = None,
        create_response: Any = None,
    ) -> None:
        self.messages = _FakeAnthropicMessages(
            stream_events=stream_events,
            stream_final=stream_final,
            create_response=create_response,
        )


# ------------------------------------------------------------------
# OpenAI fake
# ------------------------------------------------------------------


class _FakeOpenAIStreamResponse:
    def __init__(self, chunks: Iterable[Any]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> "_FakeOpenAIStreamResponse":
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeOpenAIChatCompletions:
    def __init__(
        self,
        stream_chunks: Iterable[Any] | None = None,
        create_response: Any = None,
        raise_on_create: Exception | None = None,
    ) -> None:
        self._stream_chunks = list(stream_chunks) if stream_chunks is not None else None
        self._create_response = create_response
        self._raise_on_create = raise_on_create
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raise_on_create is not None:
            raise self._raise_on_create
        if kwargs.get("stream"):
            return _FakeOpenAIStreamResponse(self._stream_chunks or [])
        return self._create_response


class FakeOpenAIClient:
    """Drop-in replacement for `openai.AsyncOpenAI` in provider tests."""

    def __init__(
        self,
        stream_chunks: Iterable[Any] | None = None,
        create_response: Any = None,
        raise_on_create: Exception | None = None,
        base_url: str = "https://fake.openai.local",
    ) -> None:
        self.chat = ns(
            completions=_FakeOpenAIChatCompletions(
                stream_chunks=stream_chunks,
                create_response=create_response,
                raise_on_create=raise_on_create,
            )
        )
        self.base_url = base_url
