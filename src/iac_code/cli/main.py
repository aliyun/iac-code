"""CLI entry point for iac-code."""

import asyncio
import json
import os
import shlex
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from typer._completion_classes import completion_init
from typer.completion import install_callback, show_callback

from iac_code import __release_date__, __version__
from iac_code.config import DEFAULT_MODEL, load_saved_model
from iac_code.i18n import _, setup_i18n
from iac_code.services.qwenpaw_source import QwenPawError as _QwenPawError
from iac_code.utils.log import setup_logging
from iac_code.utils.signals import reraise_default

completion_init()

# Initialize i18n. Thanks to `gettext.bindtextdomain` inside setup_i18n(),
# this works regardless of where it's called relative to `import typer` / click.
setup_i18n()

# Import subcommands with Typer parameter help after i18n setup, because Typer
# captures custom option help text when the function signature is defined.
from iac_code.cli.update import update as _update_command  # noqa: E402

app = typer.Typer(
    name="iac-code",
    help=_("AI-powered infrastructure orchestration tool"),
    invoke_without_command=True,
    # Disable Typer's built-in completion options; we declare translatable ones below.
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

a2a_client_app = typer.Typer(
    help=_("Use iac-code as an A2A client."),
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(a2a_client_app, name="a2a-client")


def _a2a_client_missing_dependencies_message() -> str:
    return _("A2A client dependencies are missing. Install with: pip install 'iac-code[a2a]'")


def _a2a_server_missing_dependencies_message() -> str:
    return _("A2A server dependencies are missing. Install with: pip install 'iac-code[a2a]'")


# `install-git-bash` is a Windows-only helper that installs Git for Windows
# via the npmmirror mirror. We register it conditionally so it does not
# show up in --help on non-Windows platforms (where it could not work).
if sys.platform == "win32":
    from iac_code.cli.install_git_bash import install_git_bash as _install_git_bash

    app.command(
        name="install-git-bash",
        help=_("Install Git for Windows via the npmmirror mirror (Windows only)."),
    )(_install_git_bash)

app.command(name="update", help=_("Update iac-code to the latest version."))(_update_command)


@a2a_client_app.callback()
def a2a_client(
    ctx: typer.Context,
    config: str = typer.Option("", "--config", help=_("YAML config file containing A2A client options")),
) -> None:
    """Use iac-code as an A2A client."""
    try:
        import iac_code.a2a.client  # noqa: F401
    except ImportError:
        typer.echo(
            _a2a_client_missing_dependencies_message(),
            err=True,
        )
        raise typer.Exit(1)
    try:
        ctx.obj = {"a2a_client_config": _load_a2a_config(config) if config else {}}
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    model: str = typer.Option("", "--model", "-m", help=_("LLM model to use")),
    prompt: str = typer.Option("", "--prompt", "-p", help=_("Non-interactive mode: run a single prompt and exit")),
    output_format: str = typer.Option("text", "--output-format", help=_("Output format: text, json, stream-json")),
    max_turns: int = typer.Option(100, "--max-turns", help=_("Maximum agent turns in headless mode")),
    debug: bool = typer.Option(False, "--debug", "-d", help=_("Enable debug logging")),
    verbose: bool = typer.Option(False, "--verbose", help=_("Show headless progress on stderr")),
    version: bool = typer.Option(False, "--version", "-v", "-V", is_eager=True, help=_("Show version and exit")),
    resume: str = typer.Option("", "--resume", "-r", help=_("Resume a session by ID or name")),
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
    allowed_tools: str = typer.Option(
        "",
        "--allowed-tools",
        help=_("Comma-separated tool permission patterns to allow, e.g. 'bash(git *),write_file'"),
    ),
    disallowed_tools: str = typer.Option(
        "",
        "--disallowed-tools",
        help=_("Comma-separated tool permission patterns to deny"),
    ),
    permission_mode: str = typer.Option(
        "",
        "--permission-mode",
        help=_("Permission mode: default, accept_edits, bypass_permissions, dont_ask"),
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

    # Enable ANSI/VT escape sequences on Windows consoles.
    from iac_code.utils.console import enable_virtual_terminal

    enable_virtual_terminal()

    # On Windows, verify Git Bash is available before proceeding.
    if sys.platform == "win32":
        from iac_code.utils.platform import GitBashNotFoundError, PlatformInfo

        try:
            PlatformInfo.detect()
        except GitBashNotFoundError as e:
            from rich.console import Console

            Console(stderr=True).print(f"[red]✗[/red] {e}", soft_wrap=True)
            raise SystemExit(1)

    if resume and continue_session:
        typer.echo(_("Error: --resume and --continue cannot be used together."), err=True)
        raise typer.Exit(1)

    fmt = None
    if prompt:
        from iac_code.cli.output_formats import OutputFormat

        normalized_output_format = (output_format or "text").strip().lower()
        try:
            fmt = OutputFormat(normalized_output_format)
        except ValueError as exc:
            valid = ", ".join(item.value for item in OutputFormat)
            typer.echo(_("Invalid --output-format '{}'. Valid values: {}").format(output_format, valid), err=True)
            raise typer.Exit(1) from exc

        from iac_code.pipeline.config import RunMode, get_run_mode

        if get_run_mode() == RunMode.PIPELINE:
            typer.echo(
                _("Pipeline mode requires the interactive REPL. Remove --prompt or set IAC_CODE_MODE=normal."),
                err=True,
            )
            raise typer.Exit(1)

    # Priority: CLI parameter > saved config > default
    if not model:
        try:
            model = load_saved_model() or DEFAULT_MODEL
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

    if prompt:
        assert fmt is not None

        # Read from stdin if prompt is "-"
        if prompt == "-":
            prompt = sys.stdin.read().strip()

        if permission_mode:
            from iac_code.services.permissions.loader import parse_cli_permission_mode

            try:
                parse_cli_permission_mode(permission_mode)
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc

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
                "output_format": fmt.value,
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
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(_async_excepthook)
            return await coro

        from iac_code.cli.headless import HeadlessRunner

        cli_allowed = [s.strip() for s in allowed_tools.split(",") if s.strip()] if allowed_tools else None
        cli_disallowed = [s.strip() for s in disallowed_tools.split(",") if s.strip()] if disallowed_tools else None
        try:
            runner = HeadlessRunner(
                model=model,
                output_format=fmt,
                max_turns=max_turns,
                cli_allowed_tools=cli_allowed,
                cli_disallowed_tools=cli_disallowed,
                cli_permission_mode=permission_mode or None,
                verbose=verbose,
            )
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
            # Replace fd 0 itself with the real console so ALL code (including
            # low-level termios/os.read on fd 0) sees a real terminal.
            if sys.platform == "win32":
                tty_fd = os.open("CONIN$", os.O_RDWR)
            else:
                tty_fd = os.open("/dev/tty", os.O_RDWR)
            os.dup2(tty_fd, 0)
            os.close(tty_fd)
            sys.stdin = os.fdopen(0, "r", closefd=False)

        resume_arg: str | bool | None = True if continue_session else (resume or None)

        cli_allowed = [s.strip() for s in allowed_tools.split(",") if s.strip()] if allowed_tools else None
        cli_disallowed = [s.strip() for s in disallowed_tools.split(",") if s.strip()] if disallowed_tools else None
        cli_perm_mode = permission_mode or None

        try:
            repl = InlineREPL(
                model=model,
                resume_session_id=resume_arg,
                cli_allowed_tools=cli_allowed,
                cli_disallowed_tools=cli_disallowed,
                cli_permission_mode=cli_perm_mode,
            )
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
            loop = asyncio.get_running_loop()
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
    import atexit
    import signal as _signal_mod
    import time

    from iac_code.services.telemetry import add_metric, bootstrap_telemetry, graceful_shutdown, log_event
    from iac_code.services.telemetry.names import Events, Metrics

    telemetry_session_id = f"acp-server-{uuid.uuid4()}"
    bootstrap_telemetry(session_id=telemetry_session_id)
    log_event(
        Events.SESSION_STARTED,
        {
            "mode": "acp-server",
            "transport": transport,
        },
    )
    add_metric(Metrics.SESSION_COUNT, 1, {})

    started = time.monotonic()
    exit_reason = "normal"
    _finalized = [False]

    def _finalize_telemetry(reason_override: str | None = None) -> None:
        if _finalized[0]:
            return
        _finalized[0] = True
        final_reason = reason_override or exit_reason
        try:
            log_event(
                Events.SESSION_EXITED,
                {
                    "mode": "acp-server",
                    "reason": final_reason,
                    "duration_s": int(time.monotonic() - started),
                },
            )
        finally:
            graceful_shutdown()

    atexit.register(_finalize_telemetry, "atexit")

    def _telemetry_excepthook(exc_type, exc_value, traceback_obj):
        try:
            log_event(
                Events.EXCEPTION_UNCAUGHT,
                {
                    "error_name": exc_type.__name__,
                    "location": "acp",
                },
            )
            _finalize_telemetry(f"exception:{exc_type.__name__}")
        finally:
            sys.__excepthook__(exc_type, exc_value, traceback_obj)

    sys.excepthook = _telemetry_excepthook

    _prev_sigterm = _signal_mod.getsignal(_signal_mod.SIGTERM)
    _prev_sigint = _signal_mod.getsignal(_signal_mod.SIGINT)

    def _telemetry_signal_handler(signum, frame):
        _finalize_telemetry(f"signal:{signum}")
        prev = _prev_sigterm if signum == _signal_mod.SIGTERM else _prev_sigint
        if callable(prev):
            prev(signum, frame)  # ty: ignore[call-top-callable]
            return
        reraise_default(signum)

    _signal_mod.signal(_signal_mod.SIGTERM, _telemetry_signal_handler)
    _signal_mod.signal(_signal_mod.SIGINT, _telemetry_signal_handler)

    try:
        if transport == "http":
            from iac_code.acp import acp_main_http

            acp_main_http(host=host, port=port, debug=debug)
        else:
            from iac_code.acp import acp_main

            acp_main(debug=debug)
    except Exception:
        exit_reason = "error"
        raise
    finally:
        _finalize_telemetry()


def _load_a2a_config(path: str) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise typer.BadParameter("A2A config file must contain a YAML mapping.")
    return _normalize_a2a_config_mapping(data)


def _normalize_a2a_config_mapping(data: dict[Any, Any]) -> dict[str, Any]:
    return {str(key).replace("-", "_"): _normalize_a2a_config_value(value) for key, value in data.items()}


def _normalize_a2a_config_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _normalize_a2a_config_mapping(value)
    if isinstance(value, list):
        return [_normalize_a2a_config_value(item) for item in value]
    return value


def _a2a_config_value(ctx: typer.Context, config: dict[str, Any], name: str, current: Any) -> Any:
    if name not in config:
        return current
    source = getattr(ctx, "get_parameter_source", lambda _name: None)(name)
    if source is not None and getattr(source, "name", "") != "DEFAULT":
        return current
    return config[name]


def _a2a_client_config(ctx: typer.Context) -> dict[str, Any]:
    current: typer.Context | None = ctx
    while current is not None:
        obj = getattr(current, "obj", None)
        if isinstance(obj, dict) and isinstance(obj.get("a2a_client_config"), dict):
            return obj["a2a_client_config"]
        current = getattr(current, "parent", None)
    return {}


def _a2a_client_auth_options(
    ctx: typer.Context,
    config: dict[str, Any],
    *,
    token: str,
    basic_username: str,
    basic_password: str,
    api_key: str,
    api_key_header: str,
) -> dict[str, str]:
    return {
        "token": _a2a_config_value(ctx, config, "token", token),
        "basic_username": _a2a_config_value(ctx, config, "basic_username", basic_username),
        "basic_password": _a2a_config_value(ctx, config, "basic_password", basic_password),
        "api_key": _a2a_config_value(ctx, config, "api_key", api_key),
        "api_key_header": _a2a_config_value(ctx, config, "api_key_header", api_key_header),
    }


def _a2a_client_card_verification_options(
    ctx: typer.Context,
    config: dict[str, Any],
    *,
    verify_card_secret: str,
    verify_card_jwks_url: str,
    require_card_signature: bool,
) -> dict[str, Any]:
    return {
        "verify_card_secret": _a2a_config_value(ctx, config, "verify_card_secret", verify_card_secret),
        "verify_card_jwks_url": _a2a_config_value(ctx, config, "verify_card_jwks_url", verify_card_jwks_url),
        "require_card_signature": _a2a_config_value(
            ctx,
            config,
            "require_card_signature",
            require_card_signature,
        ),
    }


def _require_a2a_client_value(value: str, *, option_name: str, config_name: str | None = None) -> str:
    if value:
        return value
    config_name = config_name or option_name.removeprefix("--")
    raise ValueError(f"{config_name} is required. Provide {option_name} or {config_name} in --config.")


def _a2a_client_route_specs(ctx: typer.Context, config: dict[str, Any], route: list[str]) -> list[str]:
    source = getattr(ctx, "get_parameter_source", lambda _name: None)("route")
    if source is not None and getattr(source, "name", "") != "DEFAULT":
        return route
    route_config = config.get("routes", config.get("route", route))
    if isinstance(route_config, str):
        return [route_config]
    if not isinstance(route_config, list):
        return route
    return [_format_a2a_route_config(item) for item in route_config]


def _format_a2a_route_config(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        raise ValueError("A2A client routes config entries must be strings or mappings.")
    name = str(item.get("name", ""))
    url = str(item.get("url", ""))
    if not name or not url:
        raise ValueError("A2A client route config entries require name and url.")
    parts = [f"{name}={url}"]
    for key in ("skills", "tags"):
        value = item.get(key)
        if value:
            if isinstance(value, str):
                parts.append(f"{key}={value}")
            elif isinstance(value, list):
                parts.append(f"{key}={','.join(str(entry) for entry in value)}")
            else:
                raise ValueError(f"A2A client route {key} must be a string or list.")
    return ";".join(parts)


@app.command(help=_("Run iac-code as an A2A 1.0 server."))
def a2a(
    ctx: typer.Context,
    config_path: str = typer.Option("", "--config", help=_("YAML config file for A2A server options")),
    host: str = typer.Option("127.0.0.1", help=_("HTTP server host")),
    port: int = typer.Option(
        41242,
        help=_("HTTP server port. 41242 is the iac-code default inspired by Gemini CLI, not a registered A2A port."),
    ),
    transport: str = typer.Option(
        "http",
        "--transport",
        help=_("A2A transport: http, stdio, unix, websocket, grpc, grpc-jsonrpc, or redis-streams"),
    ),
    debug: bool = typer.Option(False, "--debug", "-d", help=_("Enable debug logging")),
    thinking_exposure: list[str] | None = typer.Option(
        None,
        "--thinking-exposure",
        help=_("Expose A2A thinking signal types; repeat for multiple. Values: raw-thinking, tool-trace."),
    ),
) -> None:
    """Run iac-code as an A2A 1.0 server."""
    try:
        config = _load_a2a_config(config_path) if config_path else {}
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    host = _a2a_config_value(ctx, config, "host", host)
    port = _a2a_config_value(ctx, config, "port", port)
    transport = _a2a_config_value(ctx, config, "transport", transport)
    socket_path = config.get("socket_path", "")
    ws_path = config.get("ws_path", "/a2a")
    grpc_host = config.get("grpc_host", "")
    grpc_port = config.get("grpc_port")
    redis_url = config.get("redis_url", "")
    request_stream = config.get("request_stream", "iac-code:a2a:requests")
    response_stream = config.get("response_stream", "iac-code:a2a:responses")
    consumer_group = config.get("consumer_group", "iac-code")
    token = config.get("token", "")
    basic_username = config.get("basic_username", "")
    basic_password = config.get("basic_password", "")
    api_key = config.get("api_key", "")
    api_key_header = config.get("api_key_header", "")
    persistence_dir = config.get("persistence_dir", "")
    artifact_dir = config.get("artifact_dir", "")
    signing_secret = config.get("signing_secret", "")
    push_notifications = config.get("push_notifications", False)
    push_queue = config.get("push_queue", "local-file")
    push_redis_url = config.get("push_redis_url", "")
    push_stream = config.get("push_stream", "iac-code:a2a:push")
    push_retry_key = config.get("push_retry_key", "iac-code:a2a:push:retry")
    push_dead_stream = config.get("push_dead_stream", "iac-code:a2a:push:dead")
    push_consumer_group = config.get("push_consumer_group", "iac-code-push")
    push_consumer_name = config.get("push_consumer_name", "")
    push_lease_timeout_ms = config.get("push_lease_timeout_ms", 300000)
    auto_approve_permissions = config.get("auto_approve_permissions", False)
    thinking_exposure = _a2a_config_value(ctx, config, "thinking_exposure", thinking_exposure)
    model = load_saved_model() or DEFAULT_MODEL
    setup_logging(session_id="a2a-server", debug=debug)
    try:
        from iac_code.a2a.app import (
            resolve_api_key,
            resolve_api_key_header,
            resolve_basic_credentials,
            resolve_token,
            run_server,
        )
    except ImportError as exc:
        typer.echo(
            _a2a_server_missing_dependencies_message(),
            err=True,
        )
        raise typer.Exit(1) from exc

    import atexit
    import signal as _signal_mod
    import time

    from iac_code.services.telemetry import add_metric, bootstrap_telemetry, graceful_shutdown, log_event
    from iac_code.services.telemetry.names import Events, Metrics

    telemetry_session_id = f"a2a-server-{uuid.uuid4()}"
    bootstrap_telemetry(session_id=telemetry_session_id)
    log_event(
        Events.SESSION_STARTED,
        {
            "mode": "a2a-server",
            "transport": transport,
        },
    )
    add_metric(Metrics.SESSION_COUNT, 1, {})

    started = time.monotonic()
    exit_reason = "normal"
    _finalized = [False]

    def _finalize_telemetry(reason_override: str | None = None) -> None:
        if _finalized[0]:
            return
        _finalized[0] = True
        final_reason = reason_override or exit_reason
        try:
            log_event(
                Events.SESSION_EXITED,
                {
                    "mode": "a2a-server",
                    "reason": final_reason,
                    "duration_s": int(time.monotonic() - started),
                },
            )
        finally:
            graceful_shutdown()

    # atexit fires on normal interpreter exit, including after uvicorn returns
    # from a graceful shutdown — covers the path where the finally below also
    # ran (idempotent via the flag).
    atexit.register(_finalize_telemetry, "atexit")

    def _telemetry_excepthook(exc_type, exc_value, traceback_obj):
        try:
            log_event(
                Events.EXCEPTION_UNCAUGHT,
                {
                    "error_name": exc_type.__name__,
                    "location": "a2a",
                },
            )
            _finalize_telemetry(f"exception:{exc_type.__name__}")
        finally:
            sys.__excepthook__(exc_type, exc_value, traceback_obj)

    sys.excepthook = _telemetry_excepthook

    # Install our own SIGTERM/SIGINT handlers BEFORE uvicorn does its own. In
    # the common path uvicorn replaces these and triggers graceful shutdown
    # itself, after which the finally below runs. These handlers only fire if
    # a signal arrives before uvicorn installs its own (or if uvicorn never
    # got a chance to) — the ARMS sandbox SIGTERM-then-SIGKILL pattern.
    _prev_sigterm = _signal_mod.getsignal(_signal_mod.SIGTERM)
    _prev_sigint = _signal_mod.getsignal(_signal_mod.SIGINT)

    def _telemetry_signal_handler(signum, frame):
        _finalize_telemetry(f"signal:{signum}")
        prev = _prev_sigterm if signum == _signal_mod.SIGTERM else _prev_sigint
        if callable(prev):
            prev(signum, frame)  # ty: ignore[call-top-callable]
            return
        reraise_default(signum)

    _signal_mod.signal(_signal_mod.SIGTERM, _telemetry_signal_handler)
    _signal_mod.signal(_signal_mod.SIGINT, _telemetry_signal_handler)

    try:
        if transport == "unix" and not socket_path:
            raise RuntimeError("socket-path is required in --config for --transport unix.")
        if transport == "redis-streams" and not redis_url:
            raise RuntimeError("redis-url is required in --config for --transport redis-streams.")
        if push_queue == "redis-streams" and not push_redis_url:
            raise RuntimeError("push-redis-url is required in --config for push-queue: redis-streams.")
        resolved_basic = resolve_basic_credentials(basic_username or None, basic_password or None)
        run_server(
            host=host,
            port=port,
            token=resolve_token(token or None),
            model=model,
            basic_username=resolved_basic[0] if resolved_basic else None,
            basic_password=resolved_basic[1] if resolved_basic else None,
            api_key=resolve_api_key(api_key or None),
            api_key_header=resolve_api_key_header(api_key_header or None),
            persistence_dir=persistence_dir or None,
            artifact_dir=artifact_dir or None,
            signing_secret=signing_secret or None,
            push_notifications=push_notifications,
            push_queue=push_queue,
            push_redis_url=push_redis_url or None,
            push_stream=push_stream,
            push_retry_key=push_retry_key,
            push_dead_stream=push_dead_stream,
            push_consumer_group=push_consumer_group,
            push_consumer_name=push_consumer_name or None,
            push_lease_timeout_ms=push_lease_timeout_ms,
            transport=transport,
            socket_path=socket_path or None,
            ws_path=ws_path,
            grpc_host=grpc_host or None,
            grpc_port=grpc_port,
            redis_url=redis_url or None,
            request_stream=request_stream,
            response_stream=response_stream,
            consumer_group=consumer_group,
            auto_approve_permissions=auto_approve_permissions,
            thinking_exposure=thinking_exposure,
        )
    except (RuntimeError, ValueError) as exc:
        exit_reason = "error"
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        _finalize_telemetry()


@a2a_client_app.command(name="call", help=_("Send a prompt to an A2A JSON-RPC endpoint."))
def a2a_call(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    route: list[str] = typer.Option([], "--route", help=_("Route spec: name=url;skills=skill1,skill2;tags=tag1,tag2")),
    route_name: str = typer.Option("", "--route-name", help=_("Named A2A route to call")),
    prompt: str = typer.Option(..., "--prompt", "-p", help=_("Prompt to send")),
    cwd: str = typer.Option(".", "--cwd", help=_("Working directory metadata to send with the request")),
    context_id: str = typer.Option("", "--context-id", help=_("A2A context ID to continue")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
    verify_card_secret: str = typer.Option(
        "",
        "--verify-card-secret",
        "--signing-secret",
        help=_("Secret used to verify the A2A Agent Card"),
    ),
    verify_card_jwks_url: str = typer.Option(
        "",
        "--verify-card-jwks-url",
        help=_("Remote JWKS URL used to verify the A2A Agent Card"),
    ),
    require_card_signature: bool = typer.Option(
        False,
        "--require-card-signature",
        "--require-signature",
        help=_("Require a valid A2A Agent Card signature"),
    ),
    timeout: float = typer.Option(30.0, "--timeout", help=_("A2A call timeout in seconds")),
    stream: bool = typer.Option(False, "--stream", help=_("Use A2A streaming message delivery")),
) -> None:
    """Send a prompt to an A2A JSON-RPC endpoint."""
    try:
        config = _a2a_client_config(ctx)
        url = _a2a_config_value(ctx, config, "url", url)
        route = _a2a_client_route_specs(ctx, config, route)
        route_name = _a2a_config_value(ctx, config, "route_name", route_name)
        cwd = _a2a_config_value(ctx, config, "cwd", cwd)
        if cwd in ("", "."):
            cwd = str(Path.cwd())
        context_id = _a2a_config_value(ctx, config, "context_id", context_id)
        timeout = _a2a_config_value(ctx, config, "timeout", timeout)
        stream = _a2a_config_value(ctx, config, "stream", stream)
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        card_options = _a2a_client_card_verification_options(
            ctx,
            config,
            verify_card_secret=verify_card_secret,
            verify_card_jwks_url=verify_card_jwks_url,
            require_card_signature=require_card_signature,
        )
        if not url:
            if not route and not route_name:
                raise ValueError(
                    "url is required. Provide --url or url in --config, or configure --route/--route-name."
                )
            from iac_code.a2a.router import A2ARouter

            selected = A2ARouter([_parse_a2a_route_spec(value) for value in route]).resolve(
                name=route_name or None,
                prompt=prompt,
            )
            url = selected.url
        output = asyncio.run(
            _run_a2a_call(
                url=url,
                prompt=prompt,
                cwd=cwd,
                context_id=context_id or None,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                verify_card_secret=card_options["verify_card_secret"] or None,
                verify_card_jwks_url=card_options["verify_card_jwks_url"] or None,
                require_card_signature=card_options["require_card_signature"],
                timeout_seconds=timeout,
                stream=stream,
                stream_callback=typer.echo if stream else None,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if output:
        typer.echo(output)


@a2a_client_app.command(name="discover", help=_("Discover an A2A Agent Card."))
def a2a_discover(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A agent base URL")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
    verify_card_secret: str = typer.Option(
        "",
        "--verify-card-secret",
        "--signing-secret",
        help=_("Secret used to verify the A2A Agent Card"),
    ),
    verify_card_jwks_url: str = typer.Option(
        "",
        "--verify-card-jwks-url",
        help=_("Remote JWKS URL used to verify the A2A Agent Card"),
    ),
    require_card_signature: bool = typer.Option(
        False,
        "--require-card-signature",
        "--require-signature",
        help=_("Require a valid A2A Agent Card signature"),
    ),
) -> None:
    """Discover an A2A Agent Card."""
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        card_options = _a2a_client_card_verification_options(
            ctx,
            config,
            verify_card_secret=verify_card_secret,
            verify_card_jwks_url=verify_card_jwks_url,
            require_card_signature=require_card_signature,
        )
        output = asyncio.run(
            _run_a2a_discover(
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                verify_card_secret=card_options["verify_card_secret"] or None,
                verify_card_jwks_url=card_options["verify_card_jwks_url"] or None,
                require_card_signature=card_options["require_card_signature"],
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="task-get", help=_("Get an A2A task."))
def a2a_task_get(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    task_id: str = typer.Option("", "--task-id", help=_("A2A task ID")),
    history_length: int | None = typer.Option(None, "--history-length", help=_("Maximum task history items to return")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        task_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "task_id", task_id),
            option_name="--task-id",
            config_name="task-id",
        )
        history_length = _a2a_config_value(ctx, config, "history_length", history_length)
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        output = asyncio.run(
            _run_a2a_client_json(
                "get_task",
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                task_id=task_id,
                history_length=history_length,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="task-list", help=_("List A2A tasks."))
def a2a_task_list(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    context_id: str = typer.Option("", "--context-id", help=_("Filter by A2A context ID")),
    status: str = typer.Option("", "--status", help=_("Filter by A2A task state")),
    page_size: int | None = typer.Option(None, "--page-size", help=_("Maximum tasks to return")),
    page_token: str = typer.Option("", "--page-token", help=_("Pagination token")),
    include_artifacts: bool = typer.Option(False, "--include-artifacts", help=_("Include task artifacts")),
    output: str = typer.Option("table", "--output", help=_("Output format: table or json")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        context_id = _a2a_config_value(ctx, config, "context_id", context_id)
        status = _a2a_config_value(ctx, config, "status", status)
        page_size = _a2a_config_value(ctx, config, "page_size", page_size)
        page_token = _a2a_config_value(ctx, config, "page_token", page_token)
        include_artifacts = _a2a_config_value(ctx, config, "include_artifacts", include_artifacts)
        output = _a2a_config_value(ctx, config, "output", output)
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        rendered = asyncio.run(
            _run_a2a_task_list(
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                context_id=context_id or None,
                status=status or None,
                page_size=page_size,
                page_token=page_token or None,
                include_artifacts=include_artifacts or None,
                output=output,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(rendered)


@a2a_client_app.command(name="task-cancel", help=_("Cancel an A2A task."))
def a2a_task_cancel(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    task_id: str = typer.Option("", "--task-id", help=_("A2A task ID")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        task_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "task_id", task_id),
            option_name="--task-id",
            config_name="task-id",
        )
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        output = asyncio.run(
            _run_a2a_client_json(
                "cancel_task",
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                task_id=task_id,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="task-subscribe", help=_("Subscribe to an A2A task event stream."))
def a2a_task_subscribe(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    task_id: str = typer.Option("", "--task-id", help=_("A2A task ID")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        task_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "task_id", task_id),
            option_name="--task-id",
            config_name="task-id",
        )
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        output = asyncio.run(
            _run_a2a_task_subscribe(
                url=url,
                task_id=task_id,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="push-config-create", help=_("Create an A2A task push notification config."))
def a2a_push_config_create(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    task_id: str = typer.Option("", "--task-id", help=_("A2A task ID")),
    config_id: str = typer.Option("", "--config-id", help=_("Push config ID")),
    callback_url: str = typer.Option("", "--callback-url", help=_("Push callback URL")),
    notification_token: str = typer.Option("", "--notification-token", help=_("Notification verification token")),
    auth_scheme: str = typer.Option("", "--auth-scheme", help=_("Callback authentication scheme")),
    auth_credentials: str = typer.Option("", "--auth-credentials", help=_("Callback authentication credentials")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        task_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "task_id", task_id),
            option_name="--task-id",
            config_name="task-id",
        )
        config_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "config_id", config_id),
            option_name="--config-id",
            config_name="config-id",
        )
        callback_url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "callback_url", callback_url),
            option_name="--callback-url",
            config_name="callback-url",
        )
        notification_token = _a2a_config_value(ctx, config, "notification_token", notification_token)
        auth_scheme = _a2a_config_value(ctx, config, "auth_scheme", auth_scheme)
        auth_credentials = _a2a_config_value(ctx, config, "auth_credentials", auth_credentials)
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    authentication = None
    if auth_scheme or auth_credentials:
        authentication = {"scheme": auth_scheme, "credentials": auth_credentials}
    try:
        output = asyncio.run(
            _run_a2a_client_json(
                "create_push_notification_config",
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                task_id=task_id,
                config_id=config_id,
                callback_url_=callback_url,
                token_=notification_token or None,
                authentication=authentication,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="push-config-get", help=_("Get an A2A task push notification config."))
def a2a_push_config_get(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    task_id: str = typer.Option("", "--task-id", help=_("A2A task ID")),
    config_id: str = typer.Option("", "--config-id", help=_("Push config ID")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        task_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "task_id", task_id),
            option_name="--task-id",
            config_name="task-id",
        )
        config_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "config_id", config_id),
            option_name="--config-id",
            config_name="config-id",
        )
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        output = asyncio.run(
            _run_a2a_client_json(
                "get_push_notification_config",
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                task_id=task_id,
                config_id=config_id,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="push-config-list", help=_("List A2A task push notification configs."))
def a2a_push_config_list(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    task_id: str = typer.Option("", "--task-id", help=_("A2A task ID")),
    page_size: int | None = typer.Option(None, "--page-size", help=_("Maximum configs to return")),
    page_token: str = typer.Option("", "--page-token", help=_("Pagination token")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        task_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "task_id", task_id),
            option_name="--task-id",
            config_name="task-id",
        )
        page_size = _a2a_config_value(ctx, config, "page_size", page_size)
        page_token = _a2a_config_value(ctx, config, "page_token", page_token)
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        output = asyncio.run(
            _run_a2a_client_json(
                "list_push_notification_configs",
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                task_id=task_id,
                page_size=page_size,
                page_token=page_token or None,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="push-config-delete", help=_("Delete an A2A task push notification config."))
def a2a_push_config_delete(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    task_id: str = typer.Option("", "--task-id", help=_("A2A task ID")),
    config_id: str = typer.Option("", "--config-id", help=_("Push config ID")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        task_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "task_id", task_id),
            option_name="--task-id",
            config_name="task-id",
        )
        config_id = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "config_id", config_id),
            option_name="--config-id",
            config_name="config-id",
        )
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        output = asyncio.run(
            _run_a2a_client_json(
                "delete_push_notification_config",
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
                task_id=task_id,
                config_id=config_id,
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="extended-card", help=_("Get an authenticated extended A2A Agent Card."))
def a2a_extended_card(
    ctx: typer.Context,
    url: str = typer.Option("", "--url", help=_("A2A JSON-RPC endpoint URL")),
    token: str = typer.Option("", "--token", help=_("Bearer token for A2A HTTP requests")),
    basic_username: str = typer.Option("", "--basic-username", help=_("Basic auth username for A2A HTTP requests")),
    basic_password: str = typer.Option("", "--basic-password", help=_("Basic auth password for A2A HTTP requests")),
    api_key: str = typer.Option("", "--api-key", help=_("API key for A2A HTTP requests")),
    api_key_header: str = typer.Option("X-API-Key", "--api-key-header", help=_("HTTP header name for A2A API key")),
) -> None:
    try:
        config = _a2a_client_config(ctx)
        url = _require_a2a_client_value(
            _a2a_config_value(ctx, config, "url", url),
            option_name="--url",
            config_name="url",
        )
        auth_options = _a2a_client_auth_options(
            ctx,
            config,
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        )
        output = asyncio.run(
            _run_a2a_client_json(
                "get_extended_agent_card",
                url=url,
                token=auth_options["token"] or None,
                basic_username=auth_options["basic_username"] or None,
                basic_password=auth_options["basic_password"] or None,
                api_key=auth_options["api_key"] or None,
                api_key_header=auth_options["api_key_header"],
            )
        )
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@a2a_client_app.command(name="route-preview", help=_("Preview A2A route resolution."))
def a2a_route_preview(
    route: list[str] = typer.Option(
        [],
        "--route",
        help=_("Route spec: name=url;skills=skill1,skill2;tags=tag1,tag2"),
    ),
    name: str = typer.Option("", "--name", help=_("Route name to resolve")),
    skill: str = typer.Option("", "--skill", help=_("Skill ID to resolve")),
    prompt: str = typer.Option("", "--prompt", help=_("Prompt text used for tag/name route matching")),
    route_state_dir: str = typer.Option(
        "",
        "--route-state-dir",
        "--persistence-dir",
        help=_("Directory for persisted A2A routes"),
    ),
    save_routes: bool = typer.Option(False, "--save-routes", help=_("Save the provided routes as a route snapshot")),
) -> None:
    """Preview A2A route resolution."""
    try:
        routes = [_parse_a2a_route_spec(value) for value in route]
        if not routes:
            raise ValueError("At least one --route is required.")
        if save_routes or route_state_dir:
            if not route_state_dir:
                raise ValueError("--route-state-dir is required with --save-routes.")
            _save_a2a_route_snapshots(route_state_dir, routes)

        from iac_code.a2a.router import A2ARouter

        resolved = A2ARouter(routes).resolve(name=name or None, skill=skill or None, prompt=prompt or None)
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(asdict(resolved), ensure_ascii=False, indent=2, sort_keys=True))


def _build_a2a_auth_config(
    *,
    token: str | None,
    basic_username: str | None,
    basic_password: str | None,
    api_key: str | None,
    api_key_header: str,
):
    try:
        from iac_code.a2a.transport import A2AAuthConfig
    except ImportError as exc:
        typer.echo(
            _a2a_client_missing_dependencies_message(),
            err=True,
        )
        raise typer.Exit(1) from exc

    return A2AAuthConfig(
        bearer_token=token,
        api_key=api_key,
        api_key_header=api_key_header or "X-API-Key",
        basic_username=basic_username,
        basic_password=basic_password,
    )


async def _run_a2a_call(
    *,
    url: str,
    prompt: str,
    cwd: str,
    context_id: str | None,
    token: str | None,
    basic_username: str | None,
    basic_password: str | None,
    api_key: str | None,
    api_key_header: str,
    verify_card_secret: str | None,
    verify_card_jwks_url: str | None,
    require_card_signature: bool,
    timeout_seconds: float = 30.0,
    stream: bool = False,
    stream_callback: Callable[[str], None] | None = None,
) -> str:
    from iac_code.a2a.client import A2AClient

    client = A2AClient(
        auth=_build_a2a_auth_config(
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        ),
        verification_secret=verify_card_secret,
        verification_jwks_url=verify_card_jwks_url,
        require_card_signature=require_card_signature,
        timeout_seconds=timeout_seconds,
    )
    try:
        card = await client.discover(url)
        endpoint_url = client.select_endpoint_url(card, fallback_url=url)
        if stream:
            lines = []
            async for event in client.stream_message(endpoint_url, prompt, cwd=str(Path(cwd)), context_id=context_id):
                line = _format_a2a_stream_event(event)
                if stream_callback is not None:
                    stream_callback(line)
                else:
                    lines.append(line)
            return "\n".join(lines)
        response = await client.send_message(endpoint_url, prompt, cwd=str(Path(cwd)), context_id=context_id)
        return response.text or json.dumps(response.payload, ensure_ascii=False, indent=2, sort_keys=True)
    finally:
        await client.aclose()


async def _run_a2a_discover(
    *,
    url: str,
    token: str | None,
    basic_username: str | None,
    basic_password: str | None,
    api_key: str | None,
    api_key_header: str,
    verify_card_secret: str | None,
    verify_card_jwks_url: str | None,
    require_card_signature: bool,
) -> str:
    from iac_code.a2a.client import A2AClient

    client = A2AClient(
        auth=_build_a2a_auth_config(
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        ),
        verification_secret=verify_card_secret,
        verification_jwks_url=verify_card_jwks_url,
        require_card_signature=require_card_signature,
    )
    try:
        card = await client.discover(url)
        return json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True)
    finally:
        await client.aclose()


async def _run_a2a_client_json(
    method_name: str,
    *,
    url: str,
    token: str | None,
    basic_username: str | None,
    basic_password: str | None,
    api_key: str | None,
    api_key_header: str,
    **kwargs: Any,
) -> str:
    from iac_code.a2a.client import A2AClient

    client = A2AClient(
        auth=_build_a2a_auth_config(
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        ),
    )
    try:
        method_kwargs = dict(kwargs)
        notification_token = method_kwargs.pop("token_", None)
        if notification_token is not None:
            method_kwargs["token"] = notification_token
        callback_url = method_kwargs.pop("callback_url_", None)
        if callback_url is not None:
            method_kwargs["url"] = callback_url
        method = getattr(client, method_name)
        if method_name == "create_push_notification_config":
            response = await method(endpoint_url=url, **method_kwargs)
        else:
            response = await method(url, **method_kwargs)
        return json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True)
    finally:
        await client.aclose()


async def _run_a2a_task_list(
    *,
    url: str,
    token: str | None,
    basic_username: str | None,
    basic_password: str | None,
    api_key: str | None,
    api_key_header: str,
    context_id: str | None,
    status: str | None,
    page_size: int | None,
    page_token: str | None,
    include_artifacts: bool | None,
    output: str,
) -> str:
    if output not in {"table", "json"}:
        raise ValueError("--output must be table or json.")

    from iac_code.a2a.client import A2AClient

    client = A2AClient(
        auth=_build_a2a_auth_config(
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        ),
    )
    try:
        response = await client.list_tasks(
            url,
            context_id=context_id,
            status=status,
            page_size=page_size,
            page_token=page_token,
            include_artifacts=include_artifacts,
        )
        if output == "json":
            return json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True)
        return _format_a2a_task_list(
            response,
            url=url,
            context_id=context_id,
            status=status,
            page_size=page_size,
            include_artifacts=bool(include_artifacts),
        )
    finally:
        await client.aclose()


async def _run_a2a_task_subscribe(
    *,
    url: str,
    task_id: str,
    token: str | None,
    basic_username: str | None,
    basic_password: str | None,
    api_key: str | None,
    api_key_header: str,
) -> str:
    from iac_code.a2a.client import A2AClient

    client = A2AClient(
        auth=_build_a2a_auth_config(
            token=token,
            basic_username=basic_username,
            basic_password=basic_password,
            api_key=api_key,
            api_key_header=api_key_header,
        ),
    )
    try:
        events = [event async for event in client.subscribe_task(url, task_id)]
        return "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events)
    finally:
        await client.aclose()


def _format_a2a_stream_event(event: dict[str, Any]) -> str:
    text = _extract_a2a_text(event)
    if text:
        return text
    return json.dumps(event, ensure_ascii=False, sort_keys=True)


def _extract_a2a_text(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    text = result.get("text")
    if isinstance(text, str) and text:
        return text
    status = result.get("status")
    if isinstance(status, dict):
        message_text = _extract_a2a_message_text(status.get("message"))
        if message_text:
            return message_text
    message_text = _extract_a2a_message_text(result.get("message"))
    if message_text:
        return message_text
    return ""


def _extract_a2a_message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts = [str(part["text"]) for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return " ".join(texts)


def _format_a2a_task_list(
    response: dict[str, Any],
    *,
    url: str,
    context_id: str | None,
    status: str | None,
    page_size: int | None,
    include_artifacts: bool,
) -> str:
    result = response.get("result")
    if not isinstance(result, dict):
        return json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True)

    raw_tasks = result.get("tasks")
    tasks = raw_tasks if isinstance(raw_tasks, list) else []
    if not tasks:
        return "No A2A tasks found."

    rows = [["ID", "Status", "Context", "Updated", "Message"]]
    for item in tasks:
        task = item if isinstance(item, dict) else {}
        raw_status = task.get("status")
        status_obj = raw_status if isinstance(raw_status, dict) else {}
        rows.append(
            [
                _clip(str(task.get("id") or ""), 28),
                _friendly_task_state(str(status_obj.get("state") or "")),
                _clip(str(task.get("contextId") or task.get("context_id") or ""), 22),
                _clip(str(status_obj.get("timestamp") or ""), 20),
                _clip(_extract_a2a_message_text(status_obj.get("message")), 56),
            ]
        )

    table = _render_table(rows)
    summary = _format_a2a_task_list_summary(result, len(tasks))
    next_token = result.get("nextPageToken") or result.get("next_page_token")
    lines = [table, summary]
    if isinstance(next_token, str) and next_token:
        lines.append(
            "Next page: "
            + _format_a2a_task_list_next_command(
                url=url,
                context_id=context_id,
                status=status,
                page_size=page_size,
                page_token=next_token,
                include_artifacts=include_artifacts,
            )
        )
    return "\n".join(lines)


def _format_a2a_task_list_summary(result: dict[str, Any], shown: int) -> str:
    total = result.get("totalSize") or result.get("total_size")
    if isinstance(total, int):
        return f"Showing {shown} of {total} tasks."
    return f"Showing {shown} tasks."


def _format_a2a_task_list_next_command(
    *,
    url: str,
    context_id: str | None,
    status: str | None,
    page_size: int | None,
    page_token: str,
    include_artifacts: bool,
) -> str:
    parts = ["iac-code", "a2a-client", "task-list", "--url", url]
    if context_id:
        parts.extend(["--context-id", context_id])
    if status:
        parts.extend(["--status", status])
    if page_size is not None:
        parts.extend(["--page-size", str(page_size)])
    if include_artifacts:
        parts.append("--include-artifacts")
    parts.extend(["--page-token", page_token])
    return " ".join(shlex.quote(part) for part in parts)


def _render_table(rows: list[list[str]]) -> str:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    rendered = []
    for row_index, row in enumerate(rows):
        rendered.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())
        if row_index == 0:
            rendered.append("  ".join("-" * width for width in widths).rstrip())
    return "\n".join(rendered)


def _friendly_task_state(value: str) -> str:
    if value.startswith("TASK_STATE_"):
        return value.removeprefix("TASK_STATE_").lower().replace("_", "-")
    return value.lower().replace("_", "-")


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _parse_a2a_route_spec(value: str):
    try:
        from iac_code.a2a.router import A2ARoute
    except ImportError as exc:
        typer.echo(
            _a2a_client_missing_dependencies_message(),
            err=True,
        )
        raise typer.Exit(1) from exc

    parts = [part.strip() for part in value.split(";") if part.strip()]
    if not parts or "=" not in parts[0]:
        raise ValueError("A2A route must start with name=url.")
    name, url = (part.strip() for part in parts[0].split("=", 1))
    if not name or not url:
        raise ValueError("A2A route name and URL are required.")

    skills: list[str] = []
    tags: list[str] = []
    legacy_parts: list[str] = []
    for part in parts[1:]:
        key, separator, raw = part.partition("=")
        if not separator:
            legacy_parts.append(part)
            continue
        values = [item.strip() for item in raw.split(",") if item.strip()]
        if key == "skills":
            skills = values
        elif key == "tags":
            tags = values
        else:
            raise ValueError(f"Unknown A2A route segment {key!r}. Expected skills or tags.")
    if legacy_parts and not skills:
        skills = [legacy_parts[0]]
    if len(legacy_parts) > 1 and not tags:
        tags = [item.strip() for item in legacy_parts[1].split(",") if item.strip()]
    return A2ARoute(name=name, url=url, skills=skills, tags=tags)


def _save_a2a_route_snapshots(persistence_dir: str, routes: list[Any]) -> None:
    try:
        from iac_code.a2a.persistence import A2APersistenceStore, A2ARouteSnapshot
    except ImportError as exc:
        typer.echo(
            _a2a_client_missing_dependencies_message(),
            err=True,
        )
        raise typer.Exit(1) from exc

    A2APersistenceStore(persistence_dir).save_routes(
        [
            A2ARouteSnapshot(name=route.name, url=route.url, skills=list(route.skills), tags=list(route.tags))
            for route in routes
        ]
    )


if __name__ == "__main__":
    app()
