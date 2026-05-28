from __future__ import annotations

import contextlib
import logging

logger = logging.getLogger(__name__)


def acp_main(*, debug: bool = False) -> None:
    """Run iac-code as an ACP stdio server."""
    import asyncio
    import signal

    import acp

    from iac_code.acp.server import ACPServer
    from iac_code.utils.log import setup_logging

    # Configure logging *before* the event loop starts so startup-time
    # messages obey the ``--debug`` flag too.
    setup_logging(session_id="acp", debug=debug)
    _apply_stdlib_log_level(debug)

    async def _run() -> None:
        server = ACPServer()
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            logger.info("Received shutdown signal, initiating graceful shutdown...")
            shutdown_event.set()

        from iac_code.utils.signals import install_signal_handler

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            install_signal_handler(loop, sig, _signal_handler)

        agent_task = asyncio.create_task(acp.run_agent(server, use_unstable_protocol=True))
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        try:
            # Wait for either the agent to finish or a shutdown signal
            done, pending = await asyncio.wait(
                {agent_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if shutdown_event.is_set() and not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except asyncio.CancelledError:
                    pass
        finally:
            if not shutdown_task.done():
                shutdown_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await shutdown_task
            await server.shutdown()
            logger.info("ACP stdio server shut down. Metrics: %s", server.metrics.snapshot())

    asyncio.run(_run())


def acp_main_http(*, host: str = "127.0.0.1", port: int = 8765, debug: bool = False) -> None:
    """Start ACP server with HTTP+SSE transport."""
    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "HTTP transport requires extra dependencies. Install with: pip install iac-code[http]"
        ) from None

    from iac_code.utils.log import setup_logging

    setup_logging(session_id="acp", debug=debug)
    _apply_stdlib_log_level(debug)

    from iac_code.acp.http_sse import create_app

    app = create_app()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="debug" if debug else "info",
    )


def _apply_stdlib_log_level(debug: bool) -> None:
    """Lower the stdlib root + iac_code.acp logger to DEBUG when requested.

    ``setup_logging`` configures loguru, but the ACP modules use the stdlib
    ``logging`` module. Without this the ``--debug`` flag would have no
    visible effect on ACP-emitted logs.
    """
    level = logging.DEBUG if debug else logging.INFO
    logging.getLogger().setLevel(level)
    logging.getLogger("iac_code.acp").setLevel(level)
