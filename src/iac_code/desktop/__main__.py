"""Frozen Python sidecar entry point used by the Tauri Desktop host."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from iac_code.desktop import DESKTOP_PROTOCOL_VERSION
from iac_code.desktop.control import (
    ControlProtocolError,
    DesktopControlDispatcher,
    FramedControl,
    install_control_dispatcher,
)
from iac_code.desktop.ports import DesktopPortError, bind_loopback_listener
from iac_code.desktop.runtime import DesktopInstallContext

_HOST_CAPTURE_MAX_BYTES = 5 * 1024 * 1024
_HOST_CAPTURE_BACKUPS = 3
_FORCE_EXIT_SECONDS = 9.0
_DESKTOP_CLOSING_PUBLISH_SECONDS = 0.1
_DESKTOP_CLOSING_FLUSH_SECONDS = 0.05


def _arm_force_exit_watchdog() -> None:
    def terminate() -> None:
        import time

        time.sleep(_FORCE_EXIT_SECONDS)
        os._exit(1)

    threading.Thread(target=terminate, name="iac-code-desktop-exit-watchdog", daemon=True).start()


def _absolute_directory(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise argparse.ArgumentTypeError("must be an existing directory")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iac-code-sidecar")
    parser.add_argument("--requested-port", type=int, required=True)
    parser.add_argument("--desktop-install-id", required=True)
    parser.add_argument("--host-state-dir", type=_absolute_directory, required=True)
    parser.add_argument("--desktop-install-lock-dir", type=_absolute_directory, required=True)
    parser.add_argument("--runtime-dir", type=_absolute_directory, required=True)
    parser.add_argument("--default-project-cwd", type=_absolute_directory, required=True)
    parser.add_argument(
        "--distribution-channel",
        choices=("macos", "windows", "appimage", "deb", "development"),
        required=True,
    )
    parser.add_argument("--update-mode", choices=("tauri", "external"), required=True)
    parser.add_argument("--sidecar-generation", type=int, required=True)
    control = parser.add_mutually_exclusive_group(required=True)
    control.add_argument("--control-fd", type=int)
    control.add_argument("--control-pipe")
    parser.add_argument("--host-capture-path", type=Path)
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--tiktoken-cache-dir", type=Path)
    parser.add_argument("--gui-path")
    return parser


def _open_windows_host_capture(path: Path | None) -> Any | None:
    if os.name != "nt" or path is None:
        return None
    path = path.resolve()
    if path.exists() and path.stat().st_size >= _HOST_CAPTURE_MAX_BYTES:
        for index in range(_HOST_CAPTURE_BACKUPS, 0, -1):
            source = path if index == 1 else Path("{}.{}".format(path, index - 1))
            destination = Path("{}.{}".format(path, index))
            if source.exists():
                destination.unlink(missing_ok=True)
                source.replace(destination)
    capture = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = capture
    sys.stderr = capture
    return capture


def _send_startup_failure(
    control: FramedControl | None,
    args: argparse.Namespace | None,
    code: str,
    error: BaseException,
) -> None:
    if control is None:
        print(
            "Desktop sidecar startup failed before control connection: {}: {}".format(
                type(error).__name__, str(error)[:1000]
            ),
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        control.write(
            {
                "type": "startup-failed",
                "code": code,
                "requestedPort": getattr(args, "requested_port", None),
                "sidecarGeneration": getattr(args, "sidecar_generation", None),
                "message": str(error)[:1000],
            }
        )
    except (OSError, ControlProtocolError):
        pass


async def _publish_desktop_closing(controller: Any, *, force: bool) -> None:
    """Best-effort SSE notice with a strict, Host-independent deadline."""
    publications = [
        session.events.publish("desktop-closing", {"force": force})
        for session in controller.manager.loaded_sessions()
    ]
    if publications:
        try:
            await asyncio.wait_for(
                asyncio.gather(*publications, return_exceptions=True),
                timeout=_DESKTOP_CLOSING_PUBLISH_SECONDS,
            )
        except (asyncio.TimeoutError, TimeoutError):
            pass
    # Give the existing StreamingResponse tasks one bounded scheduling window
    # to hand the already-published frame to Uvicorn. No client ACK is awaited.
    await asyncio.sleep(_DESKTOP_CLOSING_FLUSH_SECONDS)


async def _dispatch_control(dispatcher: DesktopControlDispatcher, app: Any, server: Any) -> None:
    controller = app.state.desktop_controller
    while True:
        message = await dispatcher.queue.get()
        if message is None:
            controller.commit_shutdown(force=True)
            _arm_force_exit_watchdog()
            server.force_exit = True
            server.should_exit = True
            return
        message_type = message["type"]
        if message_type == "prepare-close":
            dispatcher.control.write(controller.prepare_close())
        elif message_type == "close-status":
            dispatcher.control.write(controller.close_state())
        elif message_type == "resume":
            dispatcher.control.write(controller.resume())
        elif message_type == "set-default-project":
            try:
                project = controller.set_default_project(str(message.get("path") or ""))
            except (OSError, ValueError) as exc:
                dispatcher.control.write(
                    {
                        "type": "default-project-set",
                        "path": message.get("path"),
                        "pickerOperationId": message.get("pickerOperationId"),
                        "sourceGeneration": message.get("sourceGeneration"),
                        "error": str(exc),
                    }
                )
            else:
                dispatcher.control.write(
                    {
                        "type": "default-project-set",
                        "path": str(project),
                        "pickerOperationId": message.get("pickerOperationId"),
                        "sourceGeneration": message.get("sourceGeneration"),
                    }
                )
        elif message_type == "shutdown":
            force = bool(message.get("force"))
            controller.commit_shutdown(force=force)
            if force:
                _arm_force_exit_watchdog()
            await _publish_desktop_closing(controller, force=force)
            server.force_exit = force
            server.should_exit = True
            return


async def _serve(args: argparse.Namespace, control: FramedControl, listener: Any) -> None:
    import uvicorn

    from iac_code.desktop.external_env import initialize_windows_creation_coordinator
    from iac_code.i18n import resolve_ui_language, set_language
    from iac_code.services.telemetry import bootstrap_telemetry, graceful_shutdown
    from iac_code.utils.log import current_log_file, setup_logging
    from iac_code.web.app import create_app
    from iac_code.web.server import protect_loopback_app
    from iac_code.web.settings import get_ui_language

    initialize_windows_creation_coordinator()
    await asyncio.to_thread(
        setup_logging,
        session_id="desktop-{}".format(args.sidecar_generation),
        stdout=sys.stdout is not None,
    )
    await asyncio.to_thread(bootstrap_telemetry, session_id="desktop-{}".format(args.sidecar_generation))
    dispatcher = DesktopControlDispatcher(control, asyncio.get_running_loop(), args.sidecar_generation)
    install_control_dispatcher(dispatcher)
    dispatcher.start()
    ui_language = await asyncio.to_thread(get_ui_language)
    set_language(resolve_ui_language(ui_language))
    install_context = DesktopInstallContext(
        install_id=args.desktop_install_id,
        runtime_dir=args.runtime_dir,
        host_state_dir=args.host_state_dir,
        install_lock_dir=args.desktop_install_lock_dir,
        sidecar_generation=args.sidecar_generation,
        host_capture_path=args.host_capture_path,
        python_log_path=current_log_file(),
    )
    from iac_code.desktop.download_journal import DESKTOP_RECOVERY_TIMEOUT_SECONDS, recover_install_transactions

    if any(args.desktop_install_lock_dir.glob("*.transaction.json")):
        control.write(
            {
                "type": "startup-recovery-begin",
                "sidecarGeneration": args.sidecar_generation,
                "timeoutSeconds": DESKTOP_RECOVERY_TIMEOUT_SECONDS,
            }
        )

    install_context = replace(
        install_context,
        degraded_prerequisites=await asyncio.to_thread(recover_install_transactions, install_context),
    )
    app = await asyncio.to_thread(
        create_app,
        desktop_runtime=True,
        default_project_cwd=args.default_project_cwd,
        distribution_channel=args.distribution_channel,
        update_mode=args.update_mode,
        desktop_install_context=install_context,
    )
    config = uvicorn.Config(
        protect_loopback_app(app),
        log_config=None,
        lifespan="auto",
        timeout_graceful_shutdown=3,
    )
    config.load()
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve(sockets=[listener.socket]), name="desktop-uvicorn")
    try:
        while not server.started:
            if serve_task.done():
                await serve_task
                raise RuntimeError("Desktop server stopped before readiness")
            await asyncio.sleep(0.01)
        control.write(
            {
                "type": "ready",
                "port": listener.port,
                "pid": os.getpid(),
                "protocolVersion": DESKTOP_PROTOCOL_VERSION,
                "sidecarGeneration": args.sidecar_generation,
                "degradedPrerequisites": list(install_context.degraded_prerequisites),
            }
        )
        await _dispatch_control(dispatcher, app, server)
        await serve_task
        control.write({"type": "stopped", "sidecarGeneration": args.sidecar_generation})
    finally:
        install_control_dispatcher(None)
        server.should_exit = True
        if not serve_task.done():
            await serve_task
        graceful_shutdown()


def main(argv: list[str] | None = None) -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv[:1] == ["--desktop-probe-worker"]:
        if len(effective_argv) != 3:
            return 2
        from iac_code.desktop.external_env import initialize_windows_native_preload
        from iac_code.desktop.probe_worker import worker_main

        initialize_windows_native_preload()
        return worker_main(effective_argv[1], effective_argv[2])
    control: FramedControl | None = None
    args: argparse.Namespace | None = None
    listener = None
    try:
        args = _parser().parse_args(effective_argv)
        _open_windows_host_capture(args.host_capture_path)
        if args.sidecar_generation <= 0:
            raise ValueError("sidecar generation must be positive")
        if args.config_dir is not None:
            os.environ["IAC_CODE_CONFIG_DIR"] = str(args.config_dir)
        if args.log_dir is not None:
            os.environ["IAC_CODE_LOG_DIR"] = str(args.log_dir)
        if args.tiktoken_cache_dir is not None:
            os.environ["TIKTOKEN_CACHE_DIR"] = str(args.tiktoken_cache_dir)
        if args.gui_path:
            os.environ["PATH"] = args.gui_path
        os.environ["IAC_CODE_DESKTOP_RUNTIME"] = "1"
        os.environ["IAC_CODE_DESKTOP_INSTALL_LOCK_DIR"] = str(args.desktop_install_lock_dir)
        installed_name = "infraguard.exe" if os.name == "nt" else "infraguard"
        os.environ["IAC_CODE_DESKTOP_INFRAGUARD_PATH"] = str((Path.home() / "bin" / installed_name).resolve())
        os.environ["PYTHONUTF8"] = "1"
        from iac_code.desktop.external_env import initialize_windows_native_preload

        initialize_windows_native_preload()
        os.chdir(args.runtime_dir)
        if args.control_fd is not None:
            control = FramedControl.from_fd(args.control_fd)
        else:
            control = FramedControl.from_named_pipe(args.control_pipe)
        listener = bind_loopback_listener(args.requested_port)

        import uvicorn

        from iac_code.desktop.loop_runner import run

        loop_probe = uvicorn.Config("unused:app")
        loop_factory = loop_probe.get_loop_factory()
        run(_serve(args, control, listener), loop_factory=loop_factory)
        return 0
    except DesktopPortError as exc:
        _send_startup_failure(control, args, exc.code, exc)
        return 2
    except BaseException as exc:
        _send_startup_failure(control, args, "initialization_failed", exc)
        return 1
    finally:
        install_control_dispatcher(None)
        if listener is not None:
            listener.socket.close()
        if control is not None:
            control.close()


if __name__ == "__main__":
    raise SystemExit(main())
