from __future__ import annotations

import asyncio

import pytest

from iac_code.desktop.loop_runner import run


def test_loop_runner_uses_selected_factory_and_closes_loop() -> None:
    selected: list[asyncio.AbstractEventLoop] = []

    def factory() -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        selected.append(loop)
        return loop

    async def identify_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert run(identify_loop(), loop_factory=factory) is selected[0]
    assert selected[0].is_closed()


@pytest.mark.asyncio
async def test_loop_runner_rejects_nested_use() -> None:
    coro = asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="existing event loop"):
            run(coro)
    finally:
        coro.close()
