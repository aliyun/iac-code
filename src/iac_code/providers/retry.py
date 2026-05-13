"""Retry strategy with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


class RetryableError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class NonRetryableError(Exception):
    pass


@dataclass
class RetryConfig:
    max_retries: int = 5
    base_delay: float = 0.5
    max_delay: float = 32.0
    jitter_factor: float = 0.25

    def calculate_delay(self, attempt: int) -> float:
        base = min(self.base_delay * math.pow(2, attempt), self.max_delay)
        jitter = random.random() * self.jitter_factor * base
        return base + jitter


OnRetryCallback = Callable[[int, Exception, float], Awaitable[None]]


async def with_retry(
    operation: Callable[[], Awaitable[Any]],
    config: RetryConfig,
    on_retry: OnRetryCallback | None = None,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(config.max_retries + 1):
        try:
            return await operation()
        except NonRetryableError:
            raise
        except RetryableError as e:
            last_error = e
            if attempt >= config.max_retries:
                raise
            delay = config.calculate_delay(attempt)
            if on_retry:
                await on_retry(attempt + 1, e, delay)
            await asyncio.sleep(delay)
    assert last_error is not None  # pragma: no cover
    raise last_error  # pragma: no cover
