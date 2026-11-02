"""Delay only the selected input publication in the A2A E2E server process."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from functools import wraps
from pathlib import Path
from typing import Any

from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor


class InputPublicationBackupDelay:
    """Own the test window without delaying unrelated permission backups."""

    def __init__(self) -> None:
        self.control = Path(os.environ.get("IAC_CODE_E2E_BACKUP_DELAY_CONTROL", "."))
        try:
            self.delay_seconds = float(os.environ.get("IAC_CODE_E2E_BACKUP_DELAY_SECONDS", "0"))
        except ValueError:
            self.delay_seconds = 0.0

    @staticmethod
    def marker(control: Path, name: str) -> Path:
        return control.with_name(f"{control.name}.{name}.json")

    @staticmethod
    def write_marker(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def normalize_kind(kind: Any) -> Any:
        return "candidate_selection" if kind == "candidate_select" else kind

    def claim(self, envelope: dict[str, Any]) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
        if envelope.get("eventType") != "input_required":
            return None
        data = envelope.get("data")
        step = envelope.get("step")
        if not isinstance(data, dict):
            return None
        step_id = data.get("stepId") or (step.get("id") if isinstance(step, dict) else envelope.get("stepId"))
        kind = self.normalize_kind(data.get("kind"))
        controls = (
            sorted(
                path.with_name(path.name.removesuffix(".arm.json"))
                for path in self.control.glob("backup-delay-*.arm.json")
            )
            if self.control.is_dir()
            else [self.control]
        )
        for control in controls:
            arm_path = self.marker(control, "arm")
            if not arm_path.is_file():
                continue
            arm = json.loads(arm_path.read_text(encoding="utf-8"))
            if arm.get("stepId") != step_id or kind not in [self.normalize_kind(item) for item in arm.get("kinds", [])]:
                continue
            identity = {
                "eventType": "input_required",
                "stepId": step_id,
                "kind": kind,
                "eventId": envelope.get("eventId"),
                "sequence": envelope.get("sequence"),
            }
            try:
                descriptor = os.open(self.marker(control, "claimed"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as claim:
                json.dump(identity, claim)
            started = {
                **identity,
                "startedAt": time.time(),
                "startedMonotonic": time.monotonic(),
                "delaySeconds": self.delay_seconds,
            }
            self.write_marker(self.marker(control, "started"), started)
            return control, arm, started
        return None

    async def wait_for_dispatch(self, control: Path, arm: dict[str, Any], started: dict[str, Any]) -> None:
        while (remaining := self.delay_seconds - (time.monotonic() - started["startedMonotonic"])) > 0:
            await asyncio.sleep(remaining)
        if arm.get("awaitRequestDispatch") is not True:
            return
        wait_seconds = float(arm.get("dispatchWaitSeconds", 180.0))
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("invalid backup fixture dispatch wait")
        deadline = started["startedMonotonic"] + min(180.0, max(self.delay_seconds, wait_seconds))
        while not self.marker(control, "dispatched").is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("backup fixture request dispatch handshake exceeded deadline")
            await asyncio.sleep(0.02)

    def finish(self, control: Path, started: dict[str, Any], *, succeeded: bool) -> None:
        finished_monotonic = time.monotonic()
        self.write_marker(
            self.marker(control, "finished"),
            {
                **started,
                "finishedAt": time.time(),
                "finishedMonotonic": finished_monotonic,
                "elapsedSeconds": finished_monotonic - started["startedMonotonic"],
                "succeeded": succeeded,
            },
        )

    def install(self) -> None:
        if not os.environ.get("IAC_CODE_E2E_BACKUP_DELAY_CONTROL") or not (
            math.isfinite(self.delay_seconds) and self.delay_seconds > 0
        ):
            return
        original = IacCodeA2APipelineExecutor._backup_pipeline_publication
        if getattr(original, "__e2e_backup_delay__", False):
            return

        @wraps(original)
        async def delayed_backup(executor: Any, envelope: dict[str, Any], **kwargs: Any) -> Any:
            window = self.claim(envelope)
            if window is None:
                return await original(executor, envelope, **kwargs)
            control, arm, started = window
            succeeded = False
            try:
                await self.wait_for_dispatch(control, arm, started)
                result = await original(executor, envelope, **kwargs)
                succeeded = True
                return result
            finally:
                self.finish(control, started, succeeded=succeeded)

        delayed_backup.__e2e_backup_delay__ = True
        IacCodeA2APipelineExecutor._backup_pipeline_publication = delayed_backup


InputPublicationBackupDelay().install()
