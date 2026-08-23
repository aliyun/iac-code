import asyncio

import pytest

from iac_code.providers.stream_watchdog import (
    DEFAULT_THINKING_PHASE_TIMEOUT,
    StreamIdleTimeoutError,
    StreamWatchdog,
    ThinkingPhaseTimeoutError,
    ThinkingPhaseWatchdog,
)


@pytest.mark.asyncio
class TestStreamWatchdog:
    async def test_no_timeout_when_active(self):
        watchdog = StreamWatchdog(idle_timeout=0.5)
        watchdog.start()
        for _ in range(3):
            await asyncio.sleep(0.1)
            watchdog.ping()
        watchdog.stop()

    async def test_timeout_when_idle(self):
        """ping() raises StreamIdleTimeoutError when idle exceeds timeout."""
        watchdog = StreamWatchdog(idle_timeout=0.1)
        watchdog.start()
        await asyncio.sleep(0.3)
        with pytest.raises(StreamIdleTimeoutError):
            watchdog.ping()
        watchdog.stop()

    async def test_ping_resets_timer(self):
        """Frequent pings within timeout don't raise."""
        watchdog = StreamWatchdog(idle_timeout=0.2)
        watchdog.start()
        await asyncio.sleep(0.15)
        watchdog.ping()  # resets timer
        await asyncio.sleep(0.15)
        watchdog.ping()  # still within timeout since last ping
        watchdog.stop()

    async def test_stop_prevents_timeout(self):
        """Stopped watchdog doesn't raise on ping."""
        watchdog = StreamWatchdog(idle_timeout=0.1)
        watchdog.start()
        watchdog.stop()
        await asyncio.sleep(0.2)
        watchdog.ping()  # no raise — watchdog is stopped

    async def test_context_manager(self):
        async with StreamWatchdog(idle_timeout=0.5) as wd:
            wd.ping()


@pytest.mark.asyncio
class TestThinkingPhaseWatchdog:
    async def test_default_timeout_matches_module_constant(self):
        assert ThinkingPhaseWatchdog().phase_timeout == DEFAULT_THINKING_PHASE_TIMEOUT

    async def test_continuous_thinking_trips_the_phase_budget(self):
        """长尾场景:thinking delta 持续到达,空闲看门狗不会响,阶段看门狗必须响。"""
        watchdog = ThinkingPhaseWatchdog(phase_timeout=0.1)
        watchdog.on_thinking()  # 阶段开始
        await asyncio.sleep(0.2)
        with pytest.raises(ThinkingPhaseTimeoutError) as excinfo:
            watchdog.on_thinking()
        assert excinfo.value.phase_timeout == 0.1
        assert excinfo.value.elapsed > 0.1

    async def test_visible_output_ends_the_phase(self):
        """产出可见输出后重新计时,不会把两段思考累加成一段。"""
        watchdog = ThinkingPhaseWatchdog(phase_timeout=0.15)
        watchdog.on_thinking()
        await asyncio.sleep(0.1)
        watchdog.on_output()
        watchdog.on_thinking()  # 新阶段
        await asyncio.sleep(0.1)
        watchdog.on_thinking()  # 单阶段仍在预算内 → 不抛

    async def test_thinking_within_budget_does_not_raise(self):
        watchdog = ThinkingPhaseWatchdog(phase_timeout=1.0)
        for _ in range(3):
            await asyncio.sleep(0.05)
            watchdog.on_thinking()

    async def test_elapsed_is_zero_outside_a_phase(self):
        watchdog = ThinkingPhaseWatchdog(phase_timeout=1.0)
        assert watchdog.elapsed() == 0.0
        watchdog.on_thinking()
        await asyncio.sleep(0.05)
        assert watchdog.elapsed() > 0
        watchdog.on_output()
        assert watchdog.elapsed() == 0.0
