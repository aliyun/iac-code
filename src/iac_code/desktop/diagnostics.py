"""Read-only diagnostics for the Desktop settings panel."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from iac_code.config import _resolve_config_dir
from iac_code.desktop.external_env import run_external, spawn_env
from iac_code.desktop.runtime import DesktopRuntimeConfig
from iac_code.services.qwenpaw_source import _resolve_secret_dir
from iac_code.utils.platform import GitBashNotFoundError, PlatformInfo

_TOOLS = ("git", "terraform", "node", "npm", "npx", "infraguard")


def _tool_status(name: str, deadline: float | None = None) -> dict[str, Any]:
    resolved = shutil.which(name)
    if not resolved:
        return {"status": "unavailable", "path": None, "version": None}
    version = None
    timeout = 3.0
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "timeout", "path": str(Path(resolved).resolve()), "version": None}
        timeout = min(timeout, remaining)
    try:
        result = run_external(
            [resolved, "--version"],
            capture_output=True,
            check=False,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            encoding="utf-8",
            errors="replace",
            env=spawn_env(),
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
        output = (result.stdout or result.stderr).strip().splitlines()
        if output:
            version = output[0][:300]
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "path": str(Path(resolved).resolve()), "version": None}
    except OSError:
        pass
    return {"status": "available", "path": str(Path(resolved).resolve()), "version": version}


def _qwenpaw_location() -> dict[str, str | None]:
    secret_dir = _resolve_secret_dir()
    if secret_dir is None:
        return {"path": None, "source": None}
    resolved = secret_dir.resolve()
    environment_path = os.environ.get("QWENPAW_SECRET_DIR") or os.environ.get("COPAW_SECRET_DIR")
    if environment_path and Path(environment_path).expanduser().resolve() == resolved:
        source = "env"
    elif resolved.parent == Path.home():
        source = "home"
    else:
        source = "process-cwd"
    return {"path": str(resolved), "source": source}


def collect_desktop_diagnostics(
    config: DesktopRuntimeConfig,
    current_project_cwd: Path | None = None,
    *,
    tool_progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Collect bounded, non-secret Desktop runtime and tool information."""
    raw_deadline = os.environ.get("IAC_CODE_DESKTOP_PROBE_DEADLINE")
    try:
        deadline = float(raw_deadline) if raw_deadline else None
    except ValueError:
        deadline = None
    try:
        detected = (
            PlatformInfo.detect(desktop_deadline_monotonic=deadline)
            if deadline is not None
            else PlatformInfo.detect()
        )
        platform_payload: dict[str, Any] = {
            "os": detected.os_kind,
            "architecture": platform.machine(),
            "shell": detected.shell_path,
            "shellStatus": "available",
        }
    except GitBashNotFoundError as exc:
        platform_payload = {
            "os": "Windows",
            "architecture": platform.machine(),
            "shell": None,
            "shellStatus": "unavailable",
            "shellGuidance": str(exc),
        }
    except TimeoutError:
        platform_payload = {
            "os": platform.system(),
            "architecture": platform.machine(),
            "shell": None,
            "shellStatus": "timeout",
        }

    context = config.install_context
    tools: dict[str, dict[str, Any]] = {}
    for name in _TOOLS:
        tools[name] = _tool_status(name, deadline)
        if tool_progress is not None:
            tool_progress(name, tools[name])
    return {
        "runtime": "desktop",
        "distributionChannel": config.distribution_channel,
        "updateMode": config.update_mode,
        "paths": {
            "config": str(_resolve_config_dir()),
            "project": str(current_project_cwd or config.default_project_cwd),
            "runtime": str(context.runtime_dir),
            "hostState": str(context.host_state_dir),
            "installLocks": str(context.install_lock_dir),
            "hostCapture": str(context.host_capture_path) if context.host_capture_path else None,
            "pythonLog": str(context.python_log_path) if context.python_log_path else None,
        },
        "platform": platform_payload,
        "tools": tools,
        "qwenpaw": _qwenpaw_location(),
        "degradedPrerequisites": list(context.degraded_prerequisites),
    }
