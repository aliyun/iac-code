"""Tests for /debug command."""

from unittest.mock import MagicMock

import pytest
from loguru import logger

from iac_code.commands.debug import debug_command


@pytest.fixture(autouse=True)
def _isolate_log_state(tmp_path, monkeypatch):
    """Route log paths to tmp and reset loguru state between tests."""
    monkeypatch.setattr("iac_code.utils.log.get_config_dir", lambda: tmp_path)
    monkeypatch.delenv("DEBUG", raising=False)
    logger.remove()
    # Reset module-level state so tests don't leak across each other
    import iac_code.utils.log as log_mod

    log_mod._startup_handler_id = None
    log_mod._runtime_debug_handler_ids = []
    log_mod._debug_enabled = False
    log_mod._current_log_file = None
    yield
    logger.remove()


def _make_context(session_id: str | None = "abc123"):
    context = MagicMock()
    context.repl = MagicMock()
    context.repl._session_id = session_id
    return context


class TestDebugCommand:
    @pytest.mark.asyncio
    async def test_no_args_when_off_shows_disabled_status(self):
        """`/debug` with debug off → reports it's disabled."""
        result = await debug_command(context=_make_context(), args=[])
        assert "off" in result.lower() or "disabled" in result.lower()

    @pytest.mark.asyncio
    async def test_no_args_when_on_shows_enabled_status_and_path(self):
        """`/debug` with debug already on → reports enabled + log path."""
        from iac_code.utils.log import enable_debug_at_runtime

        enable_debug_at_runtime("abc123")
        result = await debug_command(context=_make_context(), args=[])
        assert "on" in result.lower() or "enabled" in result.lower()
        assert "abc123.log" in result

    @pytest.mark.asyncio
    async def test_on_enables_and_returns_path(self):
        """`/debug on` enables debug and returns log path."""
        from iac_code.utils.log import is_debug_enabled

        result = await debug_command(context=_make_context(), args=["on"])
        assert is_debug_enabled() is True
        assert "abc123.log" in result

    @pytest.mark.asyncio
    async def test_off_disables(self):
        """`/debug off` disables debug logging."""
        from iac_code.utils.log import enable_debug_at_runtime, is_debug_enabled

        enable_debug_at_runtime("abc123")
        assert is_debug_enabled() is True

        result = await debug_command(context=_make_context(), args=["off"])
        assert is_debug_enabled() is False
        assert "off" in result.lower() or "disabled" in result.lower()

    @pytest.mark.asyncio
    async def test_invalid_arg_returns_usage_hint(self):
        """Unknown arg → usage hint, no state change."""
        from iac_code.utils.log import is_debug_enabled

        result = await debug_command(context=_make_context(), args=["banana"])
        assert "[on|off]" in result or "on|off" in result
        assert is_debug_enabled() is False

    @pytest.mark.asyncio
    async def test_off_when_already_off_is_noop(self):
        """`/debug off` when already off returns disabled status without error."""
        from iac_code.utils.log import is_debug_enabled

        result = await debug_command(context=_make_context(), args=["off"])
        assert is_debug_enabled() is False
        assert isinstance(result, str) and len(result) > 0


class TestDebugExtra:
    @pytest.mark.asyncio
    async def test_debug_no_context(self):
        result = await debug_command()
        assert "context" in result.lower()

    @pytest.mark.asyncio
    async def test_debug_no_session_id(self):
        context = _make_context(session_id=None)
        result = await debug_command(context=context)
        assert "session" in result.lower()
