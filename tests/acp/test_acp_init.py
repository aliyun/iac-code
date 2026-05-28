"""Tests for iac_code.acp.__init__ (acp_main and acp_main_http)."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAcpMain:
    """Tests for acp_main() stdio entry point."""

    def test_normal_agent_completion(self):
        """Agent task completes normally without shutdown signal."""
        mock_server = MagicMock()
        mock_server.shutdown = AsyncMock()
        mock_server.metrics.snapshot.return_value = {"requests": 0}

        async def fake_run_agent(*args, **kwargs):
            return None

        with (
            patch("iac_code.acp.server.ACPServer", return_value=mock_server),
            patch("acp.run_agent", side_effect=fake_run_agent),
            patch("iac_code.utils.log.setup_logging") as mock_setup,
        ):
            loop = asyncio.new_event_loop()

            def patched_run(coro):
                try:
                    loop.run_until_complete(coro)
                finally:
                    loop.close()

            with (
                patch("asyncio.run", side_effect=patched_run),
                patch.object(loop, "add_signal_handler"),
            ):
                from iac_code.acp import acp_main

                acp_main()

            mock_setup.assert_called_once_with(session_id="acp", debug=False)
            mock_server.shutdown.assert_awaited_once()

    def test_shutdown_signal_cancels_agent(self):
        """Shutdown signal triggers graceful cancellation of agent task."""
        mock_server = MagicMock()
        mock_server.shutdown = AsyncMock()
        mock_server.metrics.snapshot.return_value = {}

        signal_handlers: dict = {}

        async def slow_agent(*args, **kwargs):
            await asyncio.sleep(5)

        def capture_signal_handler(sig, handler):
            signal_handlers[sig] = handler

        with (
            patch("iac_code.acp.server.ACPServer", return_value=mock_server),
            patch("acp.run_agent", side_effect=slow_agent),
            patch("iac_code.utils.log.setup_logging"),
        ):
            loop = asyncio.new_event_loop()

            async def run_with_signal(coro):
                task = asyncio.ensure_future(coro)
                await asyncio.sleep(0.01)
                # Fire the SIGINT handler
                if signal.SIGINT in signal_handlers:
                    signal_handlers[signal.SIGINT]()
                await task

            def patched_run(coro):
                try:
                    loop.run_until_complete(run_with_signal(coro))
                finally:
                    loop.close()

            with (
                patch("asyncio.run", side_effect=patched_run),
                patch.object(loop, "add_signal_handler", side_effect=capture_signal_handler),
            ):
                from iac_code.acp import acp_main

                acp_main()

            mock_server.shutdown.assert_awaited_once()


class TestAcpMainHttp:
    """Tests for acp_main_http() HTTP+SSE entry point."""

    def test_missing_uvicorn_raises_system_exit(self):
        """When uvicorn is not installed, SystemExit is raised with helpful message."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "uvicorn":
                raise ImportError("No module named 'uvicorn'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(SystemExit, match="HTTP transport requires extra dependencies"):
                from iac_code.acp import acp_main_http

                acp_main_http()

    @patch("iac_code.utils.log.setup_logging")
    @patch("uvicorn.run")
    @patch("iac_code.acp.http_sse.create_app")
    def test_normal_startup_with_defaults(self, mock_create_app, mock_uvicorn_run, mock_setup):
        """HTTP server starts with default host and port."""
        mock_app = MagicMock()
        mock_create_app.return_value = mock_app

        from iac_code.acp import acp_main_http

        acp_main_http()

        mock_create_app.assert_called_once()
        mock_uvicorn_run.assert_called_once_with(
            mock_app,
            host="127.0.0.1",
            port=8765,
            log_level="info",
        )
        mock_setup.assert_called_once_with(session_id="acp", debug=False)

    @patch("iac_code.utils.log.setup_logging")
    @patch("uvicorn.run")
    @patch("iac_code.acp.http_sse.create_app")
    def test_custom_host_and_port(self, mock_create_app, mock_uvicorn_run, mock_setup):
        """HTTP server respects custom host and port parameters."""
        mock_app = MagicMock()
        mock_create_app.return_value = mock_app

        from iac_code.acp import acp_main_http

        acp_main_http(host="0.0.0.0", port=9000)

        mock_uvicorn_run.assert_called_once_with(
            mock_app,
            host="0.0.0.0",
            port=9000,
            log_level="info",
        )
        mock_setup.assert_called_once_with(session_id="acp", debug=False)

    @patch("iac_code.utils.log.setup_logging")
    @patch("uvicorn.run")
    @patch("iac_code.acp.http_sse.create_app")
    def test_debug_flag_enables_debug_logging(self, mock_create_app, mock_uvicorn_run, mock_setup):
        """Passing debug=True propagates to setup_logging *and* uvicorn log level.

        The original implementation accepted ``debug`` but never used it for
        uvicorn, so ``--debug`` was silently a no-op for the HTTP transport.
        After the fix it must lower uvicorn's own log_level too.
        """
        import logging as _logging

        mock_app = MagicMock()
        mock_create_app.return_value = mock_app

        from iac_code.acp import acp_main_http

        acp_main_http(debug=True)

        mock_setup.assert_called_once_with(session_id="acp", debug=True)
        mock_uvicorn_run.assert_called_once_with(
            mock_app,
            host="127.0.0.1",
            port=8765,
            log_level="debug",
        )
        # The stdlib root + iac_code.acp loggers must be lowered to DEBUG so
        # ACP modules (which use stdlib logging, not loguru) actually emit.
        assert _logging.getLogger("iac_code.acp").isEnabledFor(_logging.DEBUG)
