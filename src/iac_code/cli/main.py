"""CLI entry point for iac-code."""

import asyncio
import os
import sys
import uuid

import typer
from typer.completion import install_callback, show_callback

from iac_code import __release_date__, __version__
from iac_code.config import DEFAULT_MODEL, load_saved_model
from iac_code.i18n import _, setup_i18n
from iac_code.services.qwenpaw_source import QwenPawError as _QwenPawError
from iac_code.utils.log import setup_logging

# Initialize i18n. Thanks to `gettext.bindtextdomain` inside setup_i18n(),
# this works regardless of where it's called relative to `import typer` / click.
setup_i18n()

app = typer.Typer(
    name="iac-code",
    help=_("AI-powered infrastructure orchestration tool"),
    invoke_without_command=True,
    # Disable Typer's built-in completion options; we declare translatable ones below.
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    model: str = typer.Option("", "--model", "-m", help=_("LLM model to use")),
    prompt: str = typer.Option("", "--prompt", "-p", help=_("Non-interactive mode: run a single prompt and exit")),
    output_format: str = typer.Option("text", "--output-format", help=_("Output format: text, json, stream-json")),
    max_turns: int = typer.Option(100, "--max-turns", help=_("Maximum agent turns in headless mode")),
    debug: bool = typer.Option(False, "--debug", "-d", help=_("Enable debug logging")),
    version: bool = typer.Option(False, "--version", "-v", "-V", is_eager=True, help=_("Show version and exit")),
    resume: str = typer.Option("", "--resume", "-r", help=_("Resume a session by ID")),
    continue_session: bool = typer.Option(False, "--continue", "-c", help=_("Resume the most recent session")),
    install_completion: bool = typer.Option(
        None,
        "--install-completion",
        callback=install_callback,
        expose_value=False,
        is_eager=True,
        help=_("Install completion for the current shell."),
    ),
    show_completion: bool = typer.Option(
        None,
        "--show-completion",
        callback=show_callback,
        expose_value=False,
        is_eager=True,
        help=_("Show completion for the current shell, to copy it or customize the installation."),
    ),
) -> None:
    """IaC Code - AI-powered infrastructure orchestration"""
    if version:
        if __release_date__:
            print(f"iac-code v{__version__} ({__release_date__})")
        else:
            print(f"iac-code v{__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return

    if resume and continue_session:
        typer.echo(_("Error: --resume and --continue cannot be used together."), err=True)
        raise typer.Exit(1)

    # Priority: CLI parameter > saved config > default
    if not model:
        try:
            model = load_saved_model() or DEFAULT_MODEL
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

    if prompt:
        # Read from stdin if prompt is "-"
        if prompt == "-":
            prompt = sys.stdin.read().strip()

        # Headless mode: generate session_id for logging only
        session_id = str(uuid.uuid4())
        setup_logging(session_id=session_id, debug=debug)

        from iac_code.services.telemetry import add_metric, bootstrap_telemetry, graceful_shutdown, log_event
        from iac_code.services.telemetry.names import Events, Metrics

        bootstrap_telemetry(session_id=session_id)
        log_event(
            Events.SESSION_STARTED,
            {
                "headless": True,
                "output_format": output_format or "text",
            },
        )
        add_metric(Metrics.SESSION_COUNT, 1, {})

        def _telemetry_excepthook(exc_type, exc_value, traceback_obj):
            try:
                log_event(
                    Events.EXCEPTION_UNCAUGHT,
                    {
                        "error_name": exc_type.__name__,
                        "location": "cli",
                    },
                )
                graceful_shutdown()
            finally:
                sys.__excepthook__(exc_type, exc_value, traceback_obj)

        sys.excepthook = _telemetry_excepthook

        def _async_excepthook(loop, context):
            exc = context.get("exception")
            try:
                log_event(
                    Events.EXCEPTION_UNHANDLED,
                    {
                        "error_name": type(exc).__name__ if exc else "Unknown",
                        "location": "asyncio",
                    },
                )
            except Exception:
                pass
            loop.default_exception_handler(context)

        async def _run_with_handler(coro):
            loop = asyncio.get_event_loop()
            loop.set_exception_handler(_async_excepthook)
            return await coro

        from iac_code.cli.headless import HeadlessRunner
        from iac_code.cli.output_formats import OutputFormat

        fmt = OutputFormat(output_format)
        try:
            runner = HeadlessRunner(model=model, output_format=fmt, max_turns=max_turns)
            exit_code = asyncio.run(_run_with_handler(runner.run(prompt)))
        except _QwenPawError as exc:
            typer.echo(str(exc), err=True)
            raise SystemExit(1)
        raise SystemExit(exit_code)

    else:
        # Interactive REPL mode
        from iac_code.ui.repl import InlineREPL

        # Check if stdin has piped input (not a TTY)
        initial_prompt = None
        if not sys.stdin.isatty():
            piped = sys.stdin.read().strip()
            if piped:
                initial_prompt = piped
            # Replace fd 0 itself with /dev/tty so ALL code (including
            # low-level termios/os.read on fd 0) sees a real terminal.
            tty_fd = os.open("/dev/tty", os.O_RDWR)
            os.dup2(tty_fd, 0)
            os.close(tty_fd)
            sys.stdin = os.fdopen(0, "r", closefd=False)

        resume_arg: str | bool | None = True if continue_session else (resume or None)

        try:
            repl = InlineREPL(model=model, resume_session_id=resume_arg)
        except (ValueError, _QwenPawError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

        setup_logging(session_id=repl.session_id, debug=debug)

        from iac_code.services.telemetry import add_metric, bootstrap_telemetry, graceful_shutdown, log_event
        from iac_code.services.telemetry.names import Events, Metrics

        bootstrap_telemetry(session_id=repl.session_id)
        log_event(
            Events.SESSION_STARTED,
            {
                "headless": False,
                "output_format": "text",
            },
        )
        add_metric(Metrics.SESSION_COUNT, 1, {})

        def _telemetry_excepthook(exc_type, exc_value, traceback_obj):
            try:
                log_event(
                    Events.EXCEPTION_UNCAUGHT,
                    {
                        "error_name": exc_type.__name__,
                        "location": "cli",
                    },
                )
                graceful_shutdown()
            finally:
                sys.__excepthook__(exc_type, exc_value, traceback_obj)

        sys.excepthook = _telemetry_excepthook

        def _async_excepthook(loop, context):
            exc = context.get("exception")
            try:
                log_event(
                    Events.EXCEPTION_UNHANDLED,
                    {
                        "error_name": type(exc).__name__ if exc else "Unknown",
                        "location": "asyncio",
                    },
                )
            except Exception:
                pass
            loop.default_exception_handler(context)

        async def _run_with_handler(coro):
            loop = asyncio.get_event_loop()
            loop.set_exception_handler(_async_excepthook)
            return await coro

        import signal as _signal

        def _on_sigterm(signum, frame):
            sys.exit(0)

        try:
            _signal.signal(_signal.SIGTERM, _on_sigterm)
        except OSError:
            pass

        asyncio.run(_run_with_handler(repl.run(initial_prompt=initial_prompt)))


@app.command(help=_("Run iac-code as an ACP server."))
def acp(
    transport: str = typer.Option("stdio", help=_("Transport type: stdio or http")),
    port: int = typer.Option(8765, help=_("HTTP server port")),
    host: str = typer.Option("127.0.0.1", help=_("HTTP server host")),
    debug: bool = typer.Option(False, "--debug", "-d", help=_("Enable debug logging")),
) -> None:
    """Run iac-code as an ACP server."""
    if transport == "http":
        from iac_code.acp import acp_main_http

        acp_main_http(host=host, port=port, debug=debug)
    else:
        from iac_code.acp import acp_main

        acp_main(debug=debug)


if __name__ == "__main__":
    app()
