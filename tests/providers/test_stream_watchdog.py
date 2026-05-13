import asyncio

import pytest

from iac_code.providers.stream_watchdog import StreamIdleTimeoutError, StreamWatchdog


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
