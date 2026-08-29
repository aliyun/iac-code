"""Inject a controllable backup delay into the A2A E2E server process."""

from __future__ import annotations

import json
import os
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any

from iac_code.services import session_backup

_DELAY_SECONDS_ENV = "IAC_CODE_E2E_BACKUP_DELAY_SECONDS"
_CONTROL_ENV = "IAC_CODE_E2E_BACKUP_DELAY_CONTROL"
_ARM_WAIT_SECONDS = 5.0
_claim_lock = threading.Lock()
_claimed_controls: set[Path] = set()


def _marker_path(control: Path, marker: str) -> Path:
    return control.with_name(f"{control.name}.{marker}.json")


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _claim_delay(reason: Any) -> tuple[Path, float, float] | None:
    reason_value = getattr(reason, "value", reason)
    if reason_value != session_backup.BackupReason.INPUT_REQUIRED.value:
        return None
    control_value = os.environ.get(_CONTROL_ENV, "")
    try:
        delay_seconds = float(os.environ.get(_DELAY_SECONDS_ENV, ""))
    except ValueError:
        return None
    if not control_value or delay_seconds <= 0:
        return None

    configured_control = Path(control_value)
    deadline = time.monotonic() + _ARM_WAIT_SECONDS
    control: Path | None = None
    while control is None and time.monotonic() < deadline:
        if configured_control.is_dir():
            candidates = sorted(
                arm_path.with_name(arm_path.name.removesuffix(".arm.json"))
                for arm_path in configured_control.glob("backup-delay-*.arm.json")
            )
        else:
            candidates = [configured_control] if _marker_path(configured_control, "arm").is_file() else []
        with _claim_lock:
            control = next((candidate for candidate in candidates if candidate not in _claimed_controls), None)
            if control is not None:
                _claimed_controls.add(control)
        if control is None:
            time.sleep(0.02)
    if control is None:
        return None

    started_at = time.time()
    started_monotonic = time.monotonic()
    _write_marker(
        _marker_path(control, "started"),
        {
            "startedAt": started_at,
            "startedMonotonic": started_monotonic,
            "delaySeconds": delay_seconds,
            "reason": str(reason_value),
        },
    )
    deadline = started_monotonic + delay_seconds
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(remaining)
    return control, started_at, started_monotonic


def _finish_delay(state: tuple[Path, float, float], *, succeeded: bool) -> None:
    control, started_at, started_monotonic = state
    finished_at = time.time()
    finished_monotonic = time.monotonic()
    _write_marker(
        _marker_path(control, "finished"),
        {
            "startedAt": started_at,
            "finishedAt": finished_at,
            "startedMonotonic": started_monotonic,
            "finishedMonotonic": finished_monotonic,
            "elapsedSeconds": finished_monotonic - started_monotonic,
            "succeeded": succeeded,
        },
    )


def _install() -> None:
    original = session_backup.SessionBackupService.backup_session
    original_body = getattr(original, "__wrapped__", None)
    if original_body is None or getattr(original, "__e2e_backup_delay__", False):
        return

    @session_backup._log_backup_session_elapsed
    @wraps(original_body)
    def delayed_backup(self: Any, *args: Any, **kwargs: Any) -> Any:
        delay_state = _claim_delay(kwargs.get("reason"))
        succeeded = False
        try:
            result = original_body(self, *args, **kwargs)
            succeeded = bool(getattr(result, "succeeded", True))
            return result
        finally:
            if delay_state is not None:
                _finish_delay(delay_state, succeeded=succeeded)

    delayed_backup.__e2e_backup_delay__ = True
    session_backup.SessionBackupService.backup_session = delayed_backup


_install()
