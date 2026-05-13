from unittest.mock import AsyncMock

import pytest

from iac_code.providers.retry import NonRetryableError, RetryableError, RetryConfig, with_retry


class TestRetryConfig:
    def test_default_config(self):
        config = RetryConfig()
        assert config.max_retries == 5
        assert config.base_delay == 0.5
        assert config.max_delay == 32.0

    def test_exponential_backoff(self):
        config = RetryConfig(base_delay=0.5, max_delay=32.0, jitter_factor=0.0)
        assert config.calculate_delay(0) == 0.5
        assert config.calculate_delay(1) == 1.0
        assert config.calculate_delay(2) == 2.0
        assert config.calculate_delay(10) == 32.0

    def test_jitter(self):
        config = RetryConfig(base_delay=1.0, jitter_factor=0.25)
        delay = config.calculate_delay(0)
        assert 1.0 <= delay <= 1.25


@pytest.mark.asyncio
class TestWithRetry:
    async def test_success_no_retry(self):
        op = AsyncMock(return_value="ok")
        result = await with_retry(op, RetryConfig())
        assert result == "ok"
        assert op.call_count == 1

    async def test_retry_on_retryable(self):
        op = AsyncMock(side_effect=[RetryableError("fail"), "ok"])
        result = await with_retry(op, RetryConfig(max_retries=3, base_delay=0.01))
        assert result == "ok"
        assert op.call_count == 2

    async def test_no_retry_on_non_retryable(self):
        op = AsyncMock(side_effect=NonRetryableError("fatal"))
        with pytest.raises(NonRetryableError):
            await with_retry(op, RetryConfig())
        assert op.call_count == 1

    async def test_exhausted_retries(self):
        op = AsyncMock(side_effect=RetryableError("always"))
        with pytest.raises(RetryableError):
            await with_retry(op, RetryConfig(max_retries=2, base_delay=0.01))
        assert op.call_count == 3

    async def test_on_retry_callback(self):
        attempts = []
        op = AsyncMock(side_effect=[RetryableError("x"), "ok"])

        async def on_retry(attempt, error, delay):
            attempts.append(attempt)

        await with_retry(op, RetryConfig(max_retries=3, base_delay=0.01), on_retry=on_retry)
        assert attempts == [1]
