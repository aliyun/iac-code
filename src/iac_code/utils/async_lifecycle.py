"""Cancellation-safe helpers for owned asynchronous lifecycle tasks."""

from __future__ import annotations

import asyncio
from typing import TypeVar, cast

T = TypeVar("T")


async def await_task_to_completion(task: asyncio.Task[T]) -> T:
    """Wait for an owned task to finish before propagating caller cancellation."""

    async def capture_outcome() -> tuple[BaseException | None, T | None]:
        try:
            return None, await task
        except BaseException as exc:
            return exc, None

    waiter = asyncio.create_task(capture_outcome())
    interruption: BaseException | None = None

    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except BaseException as exc:
            if interruption is None:
                interruption = exc

    completion_error, result = waiter.result()
    if interruption is not None:
        if completion_error is not None:
            raise interruption from completion_error
        raise interruption
    if completion_error is not None:
        raise completion_error
    return cast(T, result)
