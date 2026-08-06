"""Pipeline prerequisite resolution helpers."""

from __future__ import annotations

import hashlib
import http.client
import inspect
import json
import os
import platform
import queue
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from iac_code.desktop.external_env import guarded_command, popen_external, run_external, spawn_env, spawn_env_kwargs
from iac_code.i18n import _

CommandExists = Callable[[str], object]
RunCommand = Callable[..., "CommandResult"]
ChooseInstaller = Callable[[str, list["InstallerSpec"]], str | None]
ProgressHandler = Callable[["PrerequisiteProgress"], None]


class DesktopDownloadTransaction(Protocol):
    def begin(
        self,
        installed_path: Path,
        *,
        installer_id: str,
        expected_sha256: str,
        platform_name: str,
    ) -> Path: ...

    def transition(self, phase: str) -> None: ...

    def cancel_before_replace(self) -> None: ...

    def complete(self) -> None: ...

_MAX_FAILURE_MESSAGE_CHARS = 1200
_MAX_FAILURE_MESSAGE_LINES = 14
_DOWNLOAD_CHUNK_SIZE = 256 * 1024
_DOWNLOAD_PROGRESS_INTERVAL = 1024 * 1024
_DEFAULT_PROBE_TIMEOUT_SECONDS = 30.0


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class PrerequisiteProgress:
    name: str
    installer_id: str | None
    phase: str
    status: str
    message: str
    installer_display_key: str | None = None
    installer_display_name: str | None = None
    command: list[str] = field(default_factory=list)
    stream: str | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None


@dataclass
class InstallerSpec:
    id: str
    platforms: list[str]
    display_key: str | None = None
    display_name: str | None = None
    requires_commands: list[str] = field(default_factory=list)
    commands: list[list[Any]] = field(default_factory=list)
    path_hints: list[dict[str, Any]] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    download: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None


@dataclass
class PrerequisiteDecision:
    name: str
    command: str
    status: str
    required_flags: list[str]
    resolved_path: str | None = None
    installer_id: str | None = None
    message: str = ""


@dataclass
class PrerequisiteResolution:
    feature_flags: dict[str, bool]
    decisions: dict[str, PrerequisiteDecision]
    env_overrides: dict[str, str] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return asdict(self)


def inspect_prerequisites(
    raw_prerequisites: Mapping[str, Mapping[str, Any]],
    *,
    feature_flags: Mapping[str, bool],
    platform_system: str | None = None,
    platform_machine: str | None = None,
    command_exists: CommandExists | None = None,
    run_command: RunCommand | None = None,
) -> PrerequisiteResolution:
    checker = command_exists or _default_command_exists
    runner = run_command or _default_run_command
    current_platform = _normalize_platform(platform_system or platform.system())
    current_architecture = _normalize_architecture(platform_machine or platform.machine())
    resolved_flags = dict(feature_flags)
    decisions: dict[str, PrerequisiteDecision] = {}
    env_overrides: dict[str, str] = {}

    for name, raw_prerequisite in raw_prerequisites.items():
        command = str(raw_prerequisite.get("command", name))
        required_flags = list(raw_prerequisite.get("required_by_flags") or [])

        if not _has_enabled_required_flag(required_flags, resolved_flags):
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="skipped",
                required_flags=required_flags,
            )
            continue

        exists, resolved_path = _check_command(command, checker)
        if exists:
            version_ok, version_message = _check_prerequisite_version(
                raw_prerequisite,
                name=name,
                command=command,
                resolved_path=resolved_path,
                run_command=runner,
                env_overrides=env_overrides,
                installer_id=None,
                progress_handler=None,
            )
            if not version_ok:
                decisions[name] = _non_interactive_missing_decision(
                    raw_prerequisite,
                    resolved_flags,
                    name=name,
                    command=command,
                    required_flags=required_flags,
                    resolved_path=resolved_path,
                    message=version_message,
                )
                continue
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="available",
                required_flags=required_flags,
                resolved_path=resolved_path,
            )
            continue

        # Not on PATH: resolve infraguard the same way use-time lookup does — check the
        # installer's install_dir (e.g. ~/bin) and version-check it. This is read-only
        # (no download/mkdir) and keeps detection consistent with prepare_prerequisites,
        # so a binary present in install_dir is reported available without touching PATH.
        available_installers = _available_installers(raw_prerequisite, current_platform, current_architecture, checker)
        resolved_path, resolved_installer_id, _hint_version_message = _resolve_path_hint_from_installers(
            raw_prerequisite,
            name,
            command,
            available_installers,
            runner,
            env_overrides,
            current_platform,
            None,
        )
        if resolved_path is not None:
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="available",
                required_flags=required_flags,
                resolved_path=resolved_path,
                installer_id=resolved_installer_id,
            )
            continue

        if _on_missing_action(raw_prerequisite, "non_interactive") == "disable_feature":
            _disable_flags(resolved_flags, required_flags)
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="disabled_feature",
                required_flags=required_flags,
            )
            continue

        decisions[name] = PrerequisiteDecision(
            name=name,
            command=command,
            status="missing",
            required_flags=required_flags,
        )

    return PrerequisiteResolution(feature_flags=resolved_flags, decisions=decisions, env_overrides=env_overrides)


def prepare_prerequisites(
    raw_prerequisites: Mapping[str, Mapping[str, Any]],
    *,
    feature_flags: Mapping[str, bool],
    surface: str,
    platform_system: str | None = None,
    platform_machine: str | None = None,
    command_exists: CommandExists | None = None,
    run_command: RunCommand | None = None,
    choose_installer: ChooseInstaller | None = None,
    progress_handler: ProgressHandler | None = None,
    desktop_transaction: DesktopDownloadTransaction | None = None,
    force_repair: bool = False,
    desktop_cancel_event: threading.Event | None = None,
) -> PrerequisiteResolution:
    if force_repair and desktop_transaction is None:
        raise ValueError("force_repair is available only for a Desktop managed transaction")
    checker = command_exists or _default_command_exists
    runner = run_command or _default_run_command
    chooser = choose_installer or _default_choose_installer
    current_platform = _normalize_platform(platform_system or platform.system())
    current_architecture = _normalize_architecture(platform_machine or platform.machine())
    resolved_flags = dict(feature_flags)
    decisions: dict[str, PrerequisiteDecision] = {}
    env_overrides: dict[str, str] = {}

    for name, raw_prerequisite in raw_prerequisites.items():
        command = str(raw_prerequisite.get("command", name))
        required_flags = list(raw_prerequisite.get("required_by_flags") or [])

        if not _has_enabled_required_flag(required_flags, resolved_flags):
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="skipped",
                required_flags=required_flags,
            )
            continue

        version_failure_message = ""
        exists, resolved_path = _check_command(command, checker)
        if exists and not force_repair:
            version_ok, version_message = _check_prerequisite_version(
                raw_prerequisite,
                name=name,
                command=command,
                resolved_path=resolved_path,
                run_command=runner,
                env_overrides=env_overrides,
                installer_id=None,
                progress_handler=progress_handler,
            )
            if version_ok:
                decisions[name] = PrerequisiteDecision(
                    name=name,
                    command=command,
                    status="available",
                    required_flags=required_flags,
                    resolved_path=resolved_path,
                )
                continue
            version_failure_message = version_message

        available_installers = _available_installers(raw_prerequisite, current_platform, current_architecture, checker)
        resolved_path, resolved_installer_id, hint_version_message = (None, None, "")
        if not force_repair:
            resolved_path, resolved_installer_id, hint_version_message = _resolve_path_hint_from_installers(
                raw_prerequisite,
                name,
                command,
                available_installers,
                runner,
                env_overrides,
                current_platform,
                progress_handler,
            )
        if hint_version_message and not version_failure_message:
            version_failure_message = hint_version_message
        if resolved_path is not None:
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="available",
                required_flags=required_flags,
                resolved_path=resolved_path,
                installer_id=resolved_installer_id,
            )
            continue

        if _on_missing_action(raw_prerequisite, str(surface)) != "prompt_install":
            decisions[name] = _non_interactive_missing_decision(
                raw_prerequisite,
                resolved_flags,
                name=name,
                command=command,
                required_flags=required_flags,
                message=version_failure_message,
            )
            continue

        selected_installer_id = chooser(name, available_installers)
        selected_installer = _find_installer(available_installers, selected_installer_id)
        if selected_installer is None:
            _disable_flags(resolved_flags, required_flags)
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="declined_or_unavailable",
                required_flags=required_flags,
                message=version_failure_message,
            )
            continue

        install_result, installed_path = _run_installer(
            name,
            command,
            selected_installer,
            runner,
            env_overrides,
            current_platform,
            current_architecture,
            progress_handler,
            desktop_transaction,
            desktop_cancel_event,
        )
        if install_result is not None:
            _disable_flags(resolved_flags, required_flags)
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="install_failed",
                required_flags=required_flags,
                installer_id=selected_installer.id,
                message=_failure_message(install_result),
            )
            continue

        if installed_path is not None:
            exists = True
            resolved_path = installed_path
        else:
            exists, resolved_path = _check_command(command, checker)
        if not exists:
            resolved_path, version_failure_message = _resolve_path_hints(
                raw_prerequisite,
                name,
                command,
                selected_installer.id,
                selected_installer.display_key,
                selected_installer.display_name,
                selected_installer.path_hints,
                runner,
                env_overrides,
                current_platform,
                progress_handler,
            )
            exists = resolved_path is not None

        if not exists:
            _disable_flags(resolved_flags, required_flags)
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="missing_after_install",
                required_flags=required_flags,
                installer_id=selected_installer.id,
                message=version_failure_message,
            )
            continue

        version_ok, version_message = _check_prerequisite_version(
            raw_prerequisite,
            name=name,
            command=command,
            resolved_path=resolved_path,
            run_command=runner,
            env_overrides=env_overrides,
            installer_id=selected_installer.id,
            installer_display_key=selected_installer.display_key,
            installer_display_name=selected_installer.display_name,
            progress_handler=progress_handler,
        )
        if not version_ok:
            _disable_flags(resolved_flags, required_flags)
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="install_failed",
                required_flags=required_flags,
                resolved_path=resolved_path,
                installer_id=selected_installer.id,
                message=version_message,
            )
            continue

        if desktop_transaction is not None:
            desktop_transaction.transition("validated_pending_post_install")

        post_install_result = _run_post_install(
            raw_prerequisite,
            name,
            selected_installer.id,
            selected_installer.display_key,
            selected_installer.display_name,
            runner,
            env_overrides,
            progress_handler,
            # Windows resolves a bare executable before applying the child
            # environment's updated PATH. Use the freshly installed Desktop
            # binary directly for post-install commands in this process.
            resolved_command_path=(
                resolved_path if desktop_transaction is not None and current_platform == "windows" else None
            ),
        )
        if post_install_result is not None:
            _disable_flags(resolved_flags, required_flags)
            decisions[name] = PrerequisiteDecision(
                name=name,
                command=command,
                status="post_install_failed",
                required_flags=required_flags,
                resolved_path=resolved_path,
                installer_id=selected_installer.id,
                message=_failure_message(post_install_result),
            )
            continue

        if desktop_transaction is not None:
            desktop_transaction.complete()

        decisions[name] = PrerequisiteDecision(
            name=name,
            command=command,
            status="available",
            required_flags=required_flags,
            resolved_path=resolved_path,
            installer_id=selected_installer.id,
        )

    return PrerequisiteResolution(feature_flags=resolved_flags, decisions=decisions, env_overrides=env_overrides)


def _default_command_exists(command: str) -> str | None:
    # The Desktop runtime owns the InfraGuard binary installed from Settings.
    # Prefer that canonical path over PATH: the frozen sidecar directory and a
    # user's shell PATH may contain an older launcher/shim, and selecting it
    # would make Settings and fresh Pipeline runs disagree with the installer.
    if command.lower() in {"infraguard", "infraguard.exe"} and os.environ.get("IAC_CODE_DESKTOP_RUNTIME") == "1":
        raw_managed_path = os.environ.get("IAC_CODE_DESKTOP_INFRAGUARD_PATH", "").strip()
        if raw_managed_path:
            managed_path = _expanded_path(raw_managed_path)
            if _is_executable(managed_path):
                return str(managed_path)
    return shutil.which(command)


def _default_run_command(
    command: list[str],
    env: Mapping[str, str] | None = None,
    on_output: Callable[[str, str], None] | None = None,
    timeout_seconds: float | None = None,
    desktop_cancel_event: threading.Event | None = None,
) -> CommandResult:
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    elif os.environ.get("IAC_CODE_DESKTOP_PROBE_CONTAINER") != "1":
        popen_kwargs["start_new_session"] = True
    try:
        process = popen_external(
            guarded_command(command, kind="prerequisite"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=spawn_env(env),
            **popen_kwargs,
        )
    except OSError as exc:
        return CommandResult(command=command, returncode=127, stdout="", stderr=str(exc))

    output_parts: list[str] = []
    assert process.stdout is not None
    stdout = process.stdout
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def read_stdout() -> None:
        try:
            for line in stdout:
                output_queue.put(("line", line))
        finally:
            output_queue.put(("done", None))

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None and timeout_seconds > 0 else None
    reader_done = False
    try:
        while True:
            if desktop_cancel_event is not None and desktop_cancel_event.is_set():
                _terminate_process(process)
                _drain_command_output(output_queue, output_parts, on_output)
                return CommandResult(
                    command=command,
                    returncode=130,
                    stdout="".join(output_parts),
                    stderr="Desktop prerequisite installation canceled",
                )
            if deadline is None:
                wait_timeout = 0.1
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process(process)
                    _drain_command_output(output_queue, output_parts, on_output)
                    return CommandResult(
                        command=command,
                        returncode=124,
                        stdout="".join(output_parts),
                        stderr=_("command timed out after {seconds} seconds").format(
                            seconds=_format_timeout_seconds(timeout_seconds or 0)
                        ),
                    )
                wait_timeout = min(0.1, remaining)

            try:
                kind, payload = output_queue.get(timeout=wait_timeout)
            except queue.Empty:
                kind, payload = "", None

            if kind == "line" and payload is not None:
                output_parts.append(payload)
                if on_output is not None:
                    on_output("stdout", payload.rstrip("\n"))
            elif kind == "done":
                reader_done = True

            returncode = process.poll()
            if returncode is not None and (reader_done or output_queue.empty()):
                _drain_command_output(output_queue, output_parts, on_output)
                return CommandResult(
                    command=command,
                    returncode=returncode,
                    stdout="".join(output_parts),
                    stderr="",
                )
    except KeyboardInterrupt:
        _terminate_process(process)
        raise


def _drain_command_output(
    output_queue: queue.Queue[tuple[str, str | None]],
    output_parts: list[str],
    on_output: Callable[[str, str], None] | None,
) -> None:
    while True:
        try:
            kind, payload = output_queue.get_nowait()
        except queue.Empty:
            return
        if kind != "line" or payload is None:
            continue
        output_parts.append(payload)
        if on_output is not None:
            on_output("stdout", payload.rstrip("\n"))


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        _terminate_windows_process_tree(process)
    else:
        _terminate_posix_process_group(process)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            _terminate_windows_process_tree(process, force=True)
        else:
            _terminate_posix_process_group(process, force=True)
        process.wait()


def _terminate_posix_process_group(process: subprocess.Popen[str], *, force: bool = False) -> None:
    from iac_code.desktop.external_env import is_guardian_process

    if is_guardian_process(process):
        if force:
            process.kill()
        else:
            process.terminate()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        if force:
            process.kill()
        else:
            process.terminate()


def _terminate_windows_process_tree(process: subprocess.Popen[str], *, force: bool = False) -> None:
    command = ["taskkill", "/T", "/PID", str(process.pid)]
    if force:
        command.insert(1, "/F")
    try:
        run_external(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        if force:
            process.kill()
        else:
            process.terminate()


def _default_choose_installer(_name: str, _installers: list[InstallerSpec]) -> str | None:
    return None


def _check_command(command: str, command_exists: CommandExists) -> tuple[bool, str | None]:
    result = command_exists(command)
    if isinstance(result, bool):
        return result, command if result else None
    if isinstance(result, (str, os.PathLike)):
        return True, os.fspath(result)
    if result:
        return True, command
    return False, None


def _has_enabled_required_flag(required_flags: list[str], feature_flags: Mapping[str, bool]) -> bool:
    return any(feature_flags.get(flag, False) for flag in required_flags)


def _disable_flags(feature_flags: dict[str, bool], required_flags: list[str]) -> None:
    for flag in required_flags:
        feature_flags[flag] = False


def _on_missing_action(raw_prerequisite: Mapping[str, Any], surface: str) -> str | None:
    on_missing = raw_prerequisite.get("on_missing") or {}
    if isinstance(on_missing, Mapping):
        action = on_missing.get(surface)
        return str(action) if action is not None else None
    return None


def _non_interactive_missing_decision(
    raw_prerequisite: Mapping[str, Any],
    feature_flags: dict[str, bool],
    *,
    name: str,
    command: str,
    required_flags: list[str],
    resolved_path: str | None = None,
    message: str = "",
) -> PrerequisiteDecision:
    if _on_missing_action(raw_prerequisite, "non_interactive") == "disable_feature":
        _disable_flags(feature_flags, required_flags)
        return PrerequisiteDecision(
            name=name,
            command=command,
            status="disabled_feature",
            required_flags=required_flags,
            resolved_path=resolved_path,
            message=message,
        )
    return PrerequisiteDecision(
        name=name,
        command=command,
        status="missing",
        required_flags=required_flags,
        resolved_path=resolved_path,
        message=message,
    )


def _normalize_platform(platform_system: str) -> str:
    normalized = platform_system.lower()
    if normalized == "darwin":
        return "darwin"
    if normalized.startswith("linux"):
        return "linux"
    if normalized.startswith(("win", "cygwin", "msys")):
        return "windows"
    return normalized


def _normalize_architecture(machine: str) -> str:
    normalized = machine.lower().replace("_", "-")
    if normalized in {"x86-64", "x64"}:
        return "amd64"
    if normalized in {"amd64", "x86_64"}:
        return "amd64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return normalized


def _available_installers(
    raw_prerequisite: Mapping[str, Any],
    current_platform: str,
    current_architecture: str,
    command_exists: CommandExists,
) -> list[InstallerSpec]:
    installers = [_installer_from_raw(raw_installer) for raw_installer in raw_prerequisite.get("installers") or []]
    return [
        installer
        for installer in installers
        if _installer_matches_platform(installer, current_platform, current_architecture)
        and all(_check_command(required_command, command_exists)[0] for required_command in installer.requires_commands)
    ]


def _installer_matches_platform(installer: InstallerSpec, current_platform: str, current_architecture: str) -> bool:
    if current_platform not in installer.platforms:
        return False
    if installer.download:
        asset = _select_download_asset(installer.download, current_platform, current_architecture)
        if asset is None or not _download_urls(asset) or not _download_sha256(asset):
            return False
    return True


def _installer_from_raw(raw_installer: Mapping[str, Any]) -> InstallerSpec:
    return InstallerSpec(
        id=str(raw_installer["id"]),
        platforms=list(raw_installer.get("platforms") or []),
        display_key=str(raw_installer["display_key"]) if raw_installer.get("display_key") else None,
        display_name=str(raw_installer["display_name"]) if raw_installer.get("display_name") else None,
        requires_commands=list(raw_installer.get("requires_commands") or []),
        commands=[list(command) for command in raw_installer.get("commands") or []],
        path_hints=[dict(path_hint) for path_hint in raw_installer.get("path_hints") or []],
        env={str(key): str(value) for key, value in (raw_installer.get("env") or {}).items()},
        download=dict(raw_installer.get("download") or {}),
        timeout_seconds=_timeout_seconds(raw_installer.get("timeout_seconds")),
    )


def _find_installer(installers: list[InstallerSpec], installer_id: str | None) -> InstallerSpec | None:
    if installer_id is None:
        return None
    return next((installer for installer in installers if installer.id == installer_id), None)


def _resolve_path_hint_from_installers(
    raw_prerequisite: Mapping[str, Any],
    name: str,
    command: str,
    installers: list[InstallerSpec],
    run_command: RunCommand,
    env_overrides: dict[str, str],
    current_platform: str,
    progress_handler: ProgressHandler | None,
) -> tuple[str | None, str | None, str]:
    version_failure_message = ""
    for installer in installers:
        resolved_path, install_dir = _resolve_download_installed_path(
            installer,
            command,
            current_platform,
        )
        if resolved_path is not None:
            version_ok, version_message = _check_prerequisite_version(
                raw_prerequisite,
                name=name,
                command=command,
                resolved_path=resolved_path,
                run_command=run_command,
                env_overrides=env_overrides,
                installer_id=installer.id,
                installer_display_key=installer.display_key,
                installer_display_name=installer.display_name,
                progress_handler=progress_handler,
            )
            if version_ok:
                if install_dir is not None:
                    _prepend_path(env_overrides, install_dir)
                return resolved_path, installer.id, ""
            if version_message:
                version_failure_message = version_message

        resolved_path, version_message = _resolve_path_hints(
            raw_prerequisite,
            name,
            command,
            installer.id,
            installer.display_key,
            installer.display_name,
            installer.path_hints,
            run_command,
            env_overrides,
            current_platform,
            progress_handler,
        )
        if resolved_path is not None:
            return resolved_path, installer.id, ""
        if version_message:
            version_failure_message = version_message
    return None, None, version_failure_message


def _resolve_download_installed_path(
    installer: InstallerSpec,
    command: str,
    current_platform: str,
) -> tuple[str | None, str | None]:
    if not installer.download:
        return None, None
    install_dir = _expanded_path(str(installer.download.get("install_dir") or "~/bin"))
    installed_name = str(installer.download.get("installed_name") or command)
    installed_path = install_dir / _installed_executable_name(installed_name, current_platform)
    if not _is_executable(installed_path):
        return None, None
    return str(installed_path), str(install_dir)


def _run_installer(
    name: str,
    command_name: str,
    installer: InstallerSpec,
    run_command: RunCommand,
    env_overrides: dict[str, str],
    current_platform: str,
    current_architecture: str,
    progress_handler: ProgressHandler | None,
    desktop_transaction: DesktopDownloadTransaction | None,
    desktop_cancel_event: threading.Event | None,
) -> tuple[CommandResult | None, str | None]:
    installer_env = dict(env_overrides)
    installer_env.update(_expand_env_mapping(installer.env))
    command_result = _run_commands(
        installer.commands,
        run_command,
        installer_env,
        name=name,
        installer_id=installer.id,
        installer_display_key=installer.display_key,
        installer_display_name=installer.display_name,
        phase="install",
        progress_handler=progress_handler,
        timeout_seconds=installer.timeout_seconds,
    )
    if command_result is not None:
        return command_result, None

    if installer.download:
        return _download_configured_asset(
            name,
            command_name,
            installer,
            env_overrides,
            current_platform,
            current_architecture,
            progress_handler,
            desktop_transaction,
            desktop_cancel_event,
        )
    return None, None


def _run_commands(
    commands: list[list[Any]],
    run_command: RunCommand,
    env_overrides: Mapping[str, str],
    *,
    name: str,
    installer_id: str | None,
    phase: str,
    progress_handler: ProgressHandler | None,
    installer_display_key: str | None = None,
    installer_display_name: str | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult | None:
    for raw_command in commands:
        command = _resolve_command(raw_command)
        _emit_progress(
            progress_handler,
            name=name,
            installer_id=installer_id,
            installer_display_key=installer_display_key,
            installer_display_name=installer_display_name,
            phase=phase,
            status="started",
            message=_("Running {command}").format(command=_format_command(command)),
            command=command,
        )
        result = _call_run_command(
            run_command,
            command,
            env_overrides,
            name=name,
            installer_id=installer_id,
            installer_display_key=installer_display_key,
            installer_display_name=installer_display_name,
            phase=phase,
            progress_handler=progress_handler,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            _emit_progress(
                progress_handler,
                name=name,
                installer_id=installer_id,
                installer_display_key=installer_display_key,
                installer_display_name=installer_display_name,
                phase=phase,
                status="failed",
                message=_failure_message(result),
                command=command,
            )
            return result
        _emit_progress(
            progress_handler,
            name=name,
            installer_id=installer_id,
            installer_display_key=installer_display_key,
            installer_display_name=installer_display_name,
            phase=phase,
            status="succeeded",
            message=_("Finished {command}").format(command=_format_command(command)),
            command=command,
        )
    return None


def _resolve_command(raw_command: list[Any]) -> list[str]:
    return [_resolve_command_part(part) for part in raw_command]


def _resolve_command_part(part: Any) -> str:
    if not isinstance(part, Mapping):
        return str(part)

    env_name = part.get("env")
    if isinstance(env_name, str):
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value

    if part.get("kind") == "go-install-latest-github-tag":
        return _resolve_latest_github_tag_go_install_target(part)

    default_value = part.get("default")
    if default_value is not None:
        return str(default_value)
    return str(part)


def _resolve_latest_github_tag_go_install_target(config: Mapping[str, Any]) -> str:
    module = str(config.get("module") or "").strip()
    if not module:
        return str(config)

    fallback_ref = str(config.get("fallback_ref") or "").strip()
    try:
        latest_ref = _latest_github_tag_commit_ref(config)
    except Exception:
        latest_ref = ""

    ref = latest_ref or fallback_ref
    if not ref:
        return module
    return f"{module}@{ref}"


def _latest_github_tag_commit_ref(config: Mapping[str, Any]) -> str:
    repo = str(config.get("repo") or "").strip()
    tag_prefix = str(config.get("tag_prefix") or "").strip()
    if not repo or not tag_prefix:
        return ""
    timeout = float(config.get("timeout_seconds") or 10)
    git_ref = _latest_git_ls_remote_tag_commit_ref(repo, tag_prefix, timeout)
    if git_ref:
        return git_ref
    refs_url = "https://api.github.com/repos/{repo}/git/matching-refs/tags/{tag_prefix}".format(
        repo=repo,
        tag_prefix=urllib.parse.quote(tag_prefix, safe="/"),
    )
    refs = _read_json_url(refs_url, timeout=timeout)
    if not isinstance(refs, list):
        return ""

    best_ref: Mapping[str, Any] | None = None
    best_version: list[int] = []
    full_prefix = f"refs/tags/{tag_prefix}"
    for raw_ref in refs:
        if not isinstance(raw_ref, Mapping):
            continue
        ref_name = str(raw_ref.get("ref") or "")
        if not ref_name.startswith(full_prefix):
            continue
        version = ref_name[len(full_prefix) :]
        version_parts = _version_parts(version)
        if best_ref is None or version_parts > best_version:
            best_ref = raw_ref
            best_version = version_parts

    if best_ref is None:
        return ""
    raw_object = best_ref.get("object")
    if not isinstance(raw_object, Mapping):
        return ""
    return _github_ref_object_commit_sha(repo, raw_object, timeout)


def _github_ref_object_commit_sha(repo: str, raw_object: Mapping[str, Any], timeout: float) -> str:
    object_type = str(raw_object.get("type") or "")
    sha = str(raw_object.get("sha") or "")
    if not sha:
        return ""
    if object_type == "commit":
        return sha
    if object_type != "tag":
        return ""
    tag_url = "https://api.github.com/repos/{repo}/git/tags/{sha}".format(repo=repo, sha=sha)
    tag = _read_json_url(tag_url, timeout=timeout)
    if not isinstance(tag, Mapping):
        return ""
    tag_object = tag.get("object")
    if not isinstance(tag_object, Mapping):
        return ""
    if str(tag_object.get("type") or "") != "commit":
        return ""
    return str(tag_object.get("sha") or "")


def _latest_git_ls_remote_tag_commit_ref(repo: str, tag_prefix: str, timeout: float) -> str:
    try:
        completed = run_external(
            guarded_command(
                [
                "git",
                "ls-remote",
                "--tags",
                f"https://github.com/{repo}.git",
                f"refs/tags/{tag_prefix}*",
                ],
                kind="prerequisite",
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            **spawn_env_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""

    tags: dict[str, dict[str, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        peeled = ref.endswith("^{}")
        if peeled:
            ref = ref[:-3]
        prefix = f"refs/tags/{tag_prefix}"
        if not ref.startswith(prefix):
            continue
        version = ref[len(prefix) :]
        entry = tags.setdefault(version, {})
        entry["commit" if peeled else "tag"] = sha

    if not tags:
        return ""
    latest_version = max(tags, key=_version_parts)
    latest = tags[latest_version]
    return latest.get("commit") or latest.get("tag") or ""


def _read_json_url(url: str, *, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _run_post_install(
    raw_prerequisite: Mapping[str, Any],
    name: str,
    installer_id: str | None,
    installer_display_key: str | None,
    installer_display_name: str | None,
    run_command: RunCommand,
    env_overrides: Mapping[str, str],
    progress_handler: ProgressHandler | None,
    *,
    resolved_command_path: str | None = None,
) -> CommandResult | None:
    post_install = raw_prerequisite.get("post_install") or {}
    if not isinstance(post_install, Mapping):
        return None
    commands = [list(command) for command in post_install.get("commands") or []]
    if resolved_command_path:
        for command in commands:
            if command and command[0] == name:
                command[0] = resolved_command_path
    timeout_seconds = _timeout_seconds(post_install.get("timeout_seconds"))
    return _run_commands(
        commands,
        run_command,
        env_overrides,
        name=name,
        installer_id=installer_id,
        installer_display_key=installer_display_key,
        installer_display_name=installer_display_name,
        phase="post_install",
        progress_handler=progress_handler,
        timeout_seconds=timeout_seconds,
    )


def _resolve_path_hints(
    raw_prerequisite: Mapping[str, Any],
    name: str,
    command: str,
    installer_id: str | None,
    installer_display_key: str | None,
    installer_display_name: str | None,
    path_hints: list[dict[str, Any]],
    run_command: RunCommand,
    env_overrides: dict[str, str],
    current_platform: str,
    progress_handler: ProgressHandler | None,
) -> tuple[str | None, str]:
    version_failure_message = ""
    for path_hint in path_hints:
        if path_hint.get("kind") != "command_output":
            continue
        hint_command = path_hint.get("command")
        if not isinstance(hint_command, list):
            continue
        result = _call_run_command(
            run_command,
            [str(part) for part in hint_command],
            env_overrides,
            name=name,
            installer_id=installer_id,
            installer_display_key=installer_display_key,
            installer_display_name=installer_display_name,
            phase="path_hint",
            progress_handler=progress_handler,
            timeout_seconds=_probe_timeout_seconds(path_hint.get("timeout_seconds")),
        )
        if result.returncode != 0:
            continue
        output_lines = result.stdout.strip().splitlines()
        if not output_lines:
            continue
        directory = Path(output_lines[0])
        append = path_hint.get("append")
        if append:
            directory = directory / str(append)
        for executable in _executable_candidates(directory, command, current_platform):
            if _is_executable(executable):
                version_ok, version_message = _check_prerequisite_version(
                    raw_prerequisite,
                    name=name,
                    command=command,
                    resolved_path=str(executable),
                    run_command=run_command,
                    env_overrides=env_overrides,
                    installer_id=installer_id,
                    progress_handler=progress_handler,
                )
                if not version_ok:
                    if version_message:
                        version_failure_message = version_message
                    continue
                _prepend_path(env_overrides, str(directory))
                return str(executable), ""
    return None, version_failure_message


def _executable_candidates(directory: Path, command: str, current_platform: str) -> list[Path]:
    candidates = [directory / command]
    if current_platform != "windows":
        return candidates

    suffixes = _windows_executable_suffixes()
    command_lower = command.lower()
    if any(command_lower.endswith(suffix) for suffix in suffixes):
        return candidates
    for suffix in suffixes:
        candidates.append(directory / f"{command}{suffix}")
    return candidates


def _windows_executable_suffixes() -> list[str]:
    extensions = [".exe", ".bat", ".cmd", ".com"]
    raw_extensions = os.environ.get("PATHEXT")
    if raw_extensions:
        extensions.extend(extension.strip().lower() for extension in raw_extensions.split(";") if extension.strip())
    return list(dict.fromkeys(extension if extension.startswith(".") else f".{extension}" for extension in extensions))


def _check_prerequisite_version(
    raw_prerequisite: Mapping[str, Any],
    *,
    name: str,
    command: str,
    resolved_path: str | None,
    run_command: RunCommand,
    env_overrides: Mapping[str, str],
    installer_id: str | None,
    progress_handler: ProgressHandler | None,
    installer_display_key: str | None = None,
    installer_display_name: str | None = None,
) -> tuple[bool, str]:
    version_check = raw_prerequisite.get("version_check")
    if not isinstance(version_check, Mapping):
        return True, ""

    minimum = str(version_check.get("minimum") or "").strip()
    if not minimum:
        return True, ""

    version_command = _version_check_command(version_check, command, resolved_path)
    result = _call_run_command(
        run_command,
        version_command,
        env_overrides,
        name=name,
        installer_id=installer_id,
        installer_display_key=installer_display_key,
        installer_display_name=installer_display_name,
        phase="version_check",
        progress_handler=progress_handler,
        timeout_seconds=_probe_timeout_seconds(version_check.get("timeout_seconds")),
    )
    if result.returncode != 0:
        return False, _("Version check failed for {name}: {reason}").format(
            name=name,
            reason=_failure_message(result),
        )

    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    version = _extract_version(output, version_check)
    if version is None:
        return False, _("Could not determine {name} version from output.").format(name=name)

    if _compare_versions(version, minimum) < 0:
        return False, _("{name} version {version} is lower than required {minimum}.").format(
            minimum=minimum,
            name=name,
            version=version,
        )
    return True, ""


def _version_check_command(
    version_check: Mapping[str, Any],
    command: str,
    resolved_path: str | None,
) -> list[str]:
    raw_command = version_check.get("command")
    if not isinstance(raw_command, list) or not raw_command:
        raw_command = [command, "version"]
    version_command = [str(part) for part in raw_command]
    if resolved_path and version_command and version_command[0] == command:
        version_command[0] = resolved_path
    return version_command


def _extract_version(output: str, version_check: Mapping[str, Any]) -> str | None:
    pattern = str(version_check.get("pattern") or r"(?P<version>\d+(?:\.\d+){1,3})")
    match = re.search(pattern, output)
    if match is None:
        return None
    group_dict = match.groupdict()
    if "version" in group_dict:
        return group_dict["version"]
    if match.lastindex:
        return match.group(1)
    return match.group(0)


def _compare_versions(actual: str, minimum: str) -> int:
    actual_parts = _version_parts(actual)
    minimum_parts = _version_parts(minimum)
    width = max(len(actual_parts), len(minimum_parts))
    actual_parts.extend([0] * (width - len(actual_parts)))
    minimum_parts.extend([0] * (width - len(minimum_parts)))
    if actual_parts < minimum_parts:
        return -1
    if actual_parts > minimum_parts:
        return 1
    return 0


def _version_parts(version: str) -> list[int]:
    match = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    if match is None:
        return [0]
    return [int(part) for part in match.group(1).split(".")]


def _download_configured_asset(
    name: str,
    command_name: str,
    installer: InstallerSpec,
    env_overrides: dict[str, str],
    current_platform: str,
    current_architecture: str,
    progress_handler: ProgressHandler | None,
    desktop_transaction: DesktopDownloadTransaction | None,
    desktop_cancel_event: threading.Event | None,
) -> tuple[CommandResult | None, str | None]:
    asset = _select_download_asset(installer.download, current_platform, current_architecture)
    pseudo_command = ["download", installer.id]
    if asset is None:
        return (
            CommandResult(
                command=pseudo_command,
                returncode=1,
                stdout="",
                stderr=_(
                    "No download asset configured for platform {platform}/{architecture} in installer {installer}."
                ).format(
                    architecture=current_architecture,
                    installer=installer.id,
                    platform=current_platform,
                ),
            ),
            None,
        )

    urls = _download_urls(asset)
    if not urls:
        return (
            CommandResult(
                command=pseudo_command,
                returncode=1,
                stdout="",
                stderr=_("No usable download URL configured for installer {installer}.").format(
                    installer=installer.id,
                ),
            ),
            None,
        )

    install_dir = _expanded_path(str(installer.download.get("install_dir") or "~/bin"))
    installed_name = str(installer.download.get("installed_name") or command_name)
    installed_path = install_dir / _installed_executable_name(installed_name, current_platform)
    filename = str(asset.get("filename") or installed_path.name)
    expected_sha256 = _download_sha256(asset)
    timeout_seconds = _download_timeout_seconds(installer.download, asset)
    if not expected_sha256:
        return (
            CommandResult(
                command=pseudo_command,
                returncode=1,
                stdout="",
                stderr=_("download asset {filename} is missing sha256").format(filename=filename),
            ),
            None,
        )
    failures: list[str] = []
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (
            CommandResult(
                command=pseudo_command,
                returncode=1,
                stdout="",
                stderr=_("failed to create install directory {path}: {error}").format(
                    error=exc,
                    path=install_dir,
                ),
            ),
            None,
        )

    for url in urls:
        tmp_path = (
            desktop_transaction.begin(
                installed_path,
                installer_id=installer.id,
                expected_sha256=expected_sha256,
                platform_name=current_platform,
            )
            if desktop_transaction is not None
            else installed_path.with_name(f".{installed_path.name}.download")
        )
        try:
            result = _download_url(
                url,
                tmp_path,
                expected_sha256,
                progress_handler,
                name=name,
                installer_id=installer.id,
                installer_display_key=installer.display_key,
                installer_display_name=installer.display_name,
                filename=filename,
                timeout_seconds=timeout_seconds,
                desktop_cancel_event=desktop_cancel_event,
            )
        except KeyboardInterrupt:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            if desktop_transaction is not None:
                desktop_transaction.cancel_before_replace()
            raise
        if result is not None:
            failures.append(_failure_message(result))
            try:
                tmp_path.unlink()
            except OSError:
                pass
            continue

        if desktop_cancel_event is not None and desktop_cancel_event.is_set():
            try:
                tmp_path.unlink()
            except OSError:
                pass
            if desktop_transaction is not None:
                desktop_transaction.cancel_before_replace()
            return (
                CommandResult(
                    command=pseudo_command,
                    returncode=130,
                    stdout="",
                    stderr="Desktop prerequisite installation canceled",
                ),
                None,
            )

        try:
            if desktop_transaction is not None:
                desktop_transaction.transition("replace_pending")
            if desktop_transaction is not None and current_platform != "windows":
                tmp_path.chmod(tmp_path.stat().st_mode | 0o755)
            if desktop_transaction is not None:
                # ``os.fsync`` maps to ``FlushFileBuffers`` on Windows, which
                # rejects a read-only handle.  Recovery already uses ``r+b``
                # for the same durability barrier; the normal install path
                # must do so as well or every managed Windows download stops
                # in ``replace_pending`` before the atomic replace.
                with tmp_path.open("r+b") as downloaded:
                    os.fsync(downloaded.fileno())
            tmp_path.replace(installed_path)
            if desktop_transaction is None and current_platform != "windows":
                installed_path.chmod(installed_path.stat().st_mode | 0o755)
            if desktop_transaction is not None:
                desktop_transaction.transition("replaced_pending_validation")
        except OSError as exc:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return (
                CommandResult(
                    command=pseudo_command,
                    returncode=1,
                    stdout="",
                    stderr=_("failed to install {filename} to {path}: {error}").format(
                        error=exc,
                        filename=filename,
                        path=installed_path,
                    ),
                ),
                None,
            )
        _prepend_path(env_overrides, str(install_dir))
        return None, str(installed_path)

    if desktop_transaction is not None:
        desktop_transaction.cancel_before_replace()

    return (
        CommandResult(
            command=pseudo_command,
            returncode=1,
            stdout="",
            stderr="\n".join(failures),
        ),
        None,
    )


def _download_url(
    url: str,
    target_path: Path,
    expected_sha256: str,
    progress_handler: ProgressHandler | None,
    *,
    name: str,
    installer_id: str | None,
    filename: str,
    timeout_seconds: float | None,
    desktop_cancel_event: threading.Event | None = None,
    installer_display_key: str | None = None,
    installer_display_name: str | None = None,
) -> CommandResult | None:
    command = ["download", filename]
    _emit_progress(
        progress_handler,
        name=name,
        installer_id=installer_id,
        installer_display_key=installer_display_key,
        installer_display_name=installer_display_name,
        phase="download",
        status="started",
        message=_("Downloading {filename}").format(filename=filename),
        command=command,
    )
    try:
        digest = hashlib.sha256()
        downloaded = 0
        last_reported = 0
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response, target_path.open("wb") as output:
            total = _content_length(response)
            _emit_download_progress(
                progress_handler,
                name=name,
                installer_id=installer_id,
                installer_display_key=installer_display_key,
                installer_display_name=installer_display_name,
                command=command,
                filename=filename,
                downloaded=downloaded,
                total=total,
            )
            while True:
                if desktop_cancel_event is not None and desktop_cancel_event.is_set():
                    return CommandResult(
                        command=command,
                        returncode=130,
                        stdout="",
                        stderr="Desktop prerequisite installation canceled",
                    )
                chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if downloaded - last_reported >= _DOWNLOAD_PROGRESS_INTERVAL:
                    last_reported = downloaded
                    _emit_download_progress(
                        progress_handler,
                        name=name,
                        installer_id=installer_id,
                        installer_display_key=installer_display_key,
                        installer_display_name=installer_display_name,
                        command=command,
                        filename=filename,
                        downloaded=downloaded,
                        total=total,
                    )
        _emit_download_progress(
            progress_handler,
            name=name,
            installer_id=installer_id,
            installer_display_key=installer_display_key,
            installer_display_name=installer_display_name,
            command=command,
            filename=filename,
            downloaded=downloaded,
            total=total,
        )
        if total > 0 and downloaded != total:
            return CommandResult(
                command=command,
                returncode=1,
                stdout="",
                stderr=_("incomplete download for {filename}: expected {expected}, got {actual}").format(
                    actual=_format_bytes(downloaded),
                    expected=_format_bytes(total),
                    filename=filename,
                ),
            )
        actual_sha256 = digest.hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            return CommandResult(
                command=command,
                returncode=1,
                stdout="",
                stderr=_("sha256 mismatch for {filename}: expected {expected}, got {actual}").format(
                    actual=actual_sha256,
                    expected=expected_sha256,
                    filename=filename,
                ),
            )
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, http.client.HTTPException) as exc:
        return CommandResult(
            command=command,
            returncode=1,
            stdout="",
            stderr=_download_error_message(exc, url),
        )

    _emit_progress(
        progress_handler,
        name=name,
        installer_id=installer_id,
        installer_display_key=installer_display_key,
        installer_display_name=installer_display_name,
        phase="download",
        status="succeeded",
        message=_("Downloaded {filename}").format(filename=filename),
        command=command,
    )
    return None


def _emit_download_progress(
    progress_handler: ProgressHandler | None,
    *,
    name: str,
    installer_id: str | None,
    command: list[str],
    filename: str,
    downloaded: int,
    total: int,
    installer_display_key: str | None = None,
    installer_display_name: str | None = None,
) -> None:
    if total > 0:
        message = _("Downloading {filename}: {percent} ({downloaded} / {total})").format(
            downloaded=_format_bytes(downloaded),
            filename=filename,
            percent=_format_download_percent(downloaded, total),
            total=_format_bytes(total),
        )
        total_bytes = total
    else:
        message = _("Downloading {filename}: {downloaded} downloaded").format(
            downloaded=_format_bytes(downloaded),
            filename=filename,
        )
        total_bytes = None
    _emit_progress(
        progress_handler,
        name=name,
        installer_id=installer_id,
        installer_display_key=installer_display_key,
        installer_display_name=installer_display_name,
        phase="download",
        status="output",
        message=message,
        command=command,
        downloaded_bytes=downloaded,
        total_bytes=total_bytes,
    )


def _content_length(response: Any) -> int:
    raw_length = response.headers.get("Content-Length") if getattr(response, "headers", None) is not None else None
    try:
        return int(raw_length or 0)
    except (TypeError, ValueError):
        return 0


def _select_download_asset(
    download: Mapping[str, Any],
    current_platform: str,
    current_architecture: str,
) -> Mapping[str, Any] | None:
    assets = download.get("assets") or []
    for raw_asset in assets:
        if not isinstance(raw_asset, Mapping):
            continue
        platforms = [str(value) for value in raw_asset.get("platforms") or []]
        architectures = [_normalize_architecture(str(value)) for value in raw_asset.get("architectures") or []]
        if platforms and current_platform not in platforms:
            continue
        if architectures and current_architecture not in architectures:
            continue
        return raw_asset
    return None


def _download_urls(asset: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for raw_url in asset.get("urls") or []:
        url: str | None = None
        if isinstance(raw_url, Mapping):
            env_name = raw_url.get("env")
            if env_name:
                url = os.environ.get(str(env_name))
            else:
                configured_url = raw_url.get("url")
                url = str(configured_url) if configured_url else None
        elif raw_url:
            url = str(raw_url)
        if not url:
            continue
        urls.append(os.path.expandvars(os.path.expanduser(url)))
    return urls


def _download_timeout_seconds(download: Mapping[str, Any], asset: Mapping[str, Any]) -> float | None:
    raw_value = asset.get("timeout_seconds")
    if raw_value is None:
        raw_value = download.get("timeout_seconds")
    return _timeout_seconds(raw_value)


def _timeout_seconds(raw_value: Any) -> float | None:
    if raw_value is None or raw_value == "":
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _probe_timeout_seconds(raw_value: Any) -> float:
    return _timeout_seconds(raw_value) or _DEFAULT_PROBE_TIMEOUT_SECONDS


def _download_error_message(exc: BaseException, _url: str) -> str:
    return _("download failed: {error_type}").format(error_type=type(exc).__name__)


def _download_sha256(asset: Mapping[str, Any]) -> str:
    raw_sha256 = asset.get("sha256")
    value: str | None
    if isinstance(raw_sha256, Mapping):
        env_name = raw_sha256.get("env")
        value = os.environ.get(str(env_name)) if env_name else None
    elif raw_sha256:
        value = str(raw_sha256)
    else:
        value = None
    return os.path.expandvars(os.path.expanduser(value or "")).strip().lower()


def _installed_executable_name(command: str, current_platform: str) -> str:
    if current_platform == "windows" and not command.lower().endswith(".exe"):
        return f"{command}.exe"
    return command


def _expanded_path(raw_path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw_path)))


def _expand_env_mapping(raw_env: Mapping[str, str]) -> dict[str, str]:
    return {key: os.path.expandvars(os.path.expanduser(value)) for key, value in raw_env.items()}


def _is_executable(path: Path) -> bool:
    return path.is_file() and (os.access(path, os.X_OK) or os.name == "nt")


def _prepend_path(env_overrides: dict[str, str], directory: str) -> None:
    existing_path = env_overrides.get("PATH", os.environ.get("PATH", ""))
    segments = [segment for segment in existing_path.split(os.pathsep) if segment]
    if directory not in segments:
        segments.insert(0, directory)
    env_overrides["PATH"] = os.pathsep.join(segments)


def _call_run_command(
    run_command: RunCommand,
    command: list[str],
    env_overrides: Mapping[str, str],
    *,
    name: str,
    installer_id: str | None,
    phase: str,
    progress_handler: ProgressHandler | None,
    installer_display_key: str | None = None,
    installer_display_name: str | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    env = _environment_with_overrides(env_overrides) if env_overrides else None
    on_output: Callable[[str, str], None] | None = None
    if progress_handler is not None:

        def on_output(stream: str, text: str) -> None:
            if not text:
                return
            _emit_progress(
                progress_handler,
                name=name,
                installer_id=installer_id,
                installer_display_key=installer_display_key,
                installer_display_name=installer_display_name,
                phase=phase,
                status="output",
                message=text,
                command=command,
                stream=stream,
            )

    kwargs: dict[str, Any] = {}
    if _run_command_accepts_keyword_on_output(run_command):
        kwargs["on_output"] = on_output
    if timeout_seconds is not None and _run_command_accepts_keyword(run_command, "timeout_seconds"):
        kwargs["timeout_seconds"] = timeout_seconds

    if kwargs:
        try:
            return run_command(command, env, **kwargs)
        except OSError as exc:
            return CommandResult(command=command, returncode=127, stdout="", stderr=str(exc))
    if _run_command_accepts_positional_on_output(run_command):
        try:
            return run_command(command, env, on_output)
        except OSError as exc:
            return CommandResult(command=command, returncode=127, stdout="", stderr=str(exc))
    try:
        return run_command(command, env)
    except OSError as exc:
        return CommandResult(command=command, returncode=127, stdout="", stderr=str(exc))


def _run_command_accepts_keyword(run_command: RunCommand, keyword: str) -> bool:
    try:
        signature = inspect.signature(run_command)
    except (TypeError, ValueError):
        return True
    return keyword in signature.parameters or any(
        parameter.kind == parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )


def _run_command_accepts_keyword_on_output(run_command: RunCommand) -> bool:
    return _run_command_accepts_keyword(run_command, "on_output")


def _run_command_accepts_positional_on_output(run_command: RunCommand) -> bool:
    try:
        signature = inspect.signature(run_command)
    except (TypeError, ValueError):
        return True
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {
            parameter.POSITIONAL_ONLY,
            parameter.POSITIONAL_OR_KEYWORD,
        }
    ]
    return len(positional) >= 3 or any(
        parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values()
    )


def _emit_progress(
    progress_handler: ProgressHandler | None,
    *,
    name: str,
    installer_id: str | None,
    phase: str,
    status: str,
    message: str,
    installer_display_key: str | None = None,
    installer_display_name: str | None = None,
    command: list[str] | None = None,
    stream: str | None = None,
    downloaded_bytes: int | None = None,
    total_bytes: int | None = None,
) -> None:
    if progress_handler is None:
        return
    try:
        progress_handler(
            PrerequisiteProgress(
                name=name,
                installer_id=installer_id,
                phase=phase,
                status=status,
                message=message,
                installer_display_key=installer_display_key,
                installer_display_name=installer_display_name,
                command=list(command or []),
                stream=stream,
                downloaded_bytes=downloaded_bytes,
                total_bytes=total_bytes,
            )
        )
    except Exception:
        return


def _failure_message(result: CommandResult) -> str:
    body_parts = [part.strip() for part in (result.stderr, result.stdout) if part and part.strip()]
    body = "\n".join(body_parts) if body_parts else _("exit code {returncode}").format(returncode=result.returncode)
    lines = [line.rstrip() for line in body.splitlines() if line.strip()]
    if len(lines) > _MAX_FAILURE_MESSAGE_LINES:
        lines = ["..."] + lines[-_MAX_FAILURE_MESSAGE_LINES:]
    message = _("{command} exited with {returncode}: {body}").format(
        command=_format_command(result.command),
        returncode=result.returncode,
        body="\n".join(lines),
    )
    if len(message) > _MAX_FAILURE_MESSAGE_CHARS:
        message = "..." + message[-(_MAX_FAILURE_MESSAGE_CHARS - 3) :]
    return message


def _format_command(command: list[str]) -> str:
    return shlex.join(str(part) for part in command)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(value)} B"


def _format_timeout_seconds(seconds: float) -> str:
    value = float(seconds)
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_download_percent(downloaded: int, total: int) -> str:
    if total <= 0:
        return ""
    percent = max(0.0, min(100.0, downloaded / total * 100))
    if 0 < percent < 1:
        return "<1%"
    return f"{percent:.0f}%"


def _environment_with_overrides(env_overrides: Mapping[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(env_overrides)
    return env
