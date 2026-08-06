"""Desktop-local asyncio runner that honors Uvicorn's selected loop factory."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")
LoopFactory = Callable[[], asyncio.AbstractEventLoop]


def run(coro: Coroutine[Any, Any, T], *, loop_factory: LoopFactory | None = None) -> T:
    """Run *coro* with *loop_factory* on every supported Python version."""
    if asyncio._get_running_loop() is not None:  # type: ignore[attr-defined]
        raise RuntimeError("desktop loop runner cannot run inside an existing event loop")
    if "loop_factory" in inspect.signature(asyncio.run).parameters:
        return asyncio.run(coro, loop_factory=loop_factory)  # type: ignore[call-overload]

    loop = loop_factory() if loop_factory is not None else asyncio.new_event_loop()
    if not isinstance(loop, asyncio.AbstractEventLoop):
        coro.close()
        raise TypeError("loop_factory must return an event loop")
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(contextvars.copy_context().run(loop.create_task, coro))
        loop.run_until_complete(loop.shutdown_asyncgens())
        shutdown_executor = getattr(loop, "shutdown_default_executor", None)
        if shutdown_executor is not None:
            loop.run_until_complete(shutdown_executor())
        return result
    finally:
        asyncio.set_event_loop(None)
        loop.close()
