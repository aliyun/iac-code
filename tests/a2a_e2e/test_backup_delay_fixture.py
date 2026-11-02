"""Offline behavior checks for the live-case backup delay hook."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO_ROOT / "scripts/a2a/e2e/fixtures/backup-delay-sitecustomize/sitecustomize.py"
_DELAY_SECONDS = 0.05
# Startup/import, window coordination and process drain share the outer watchdog.
_WAIT_TIMEOUT = 20
_PROCESS_TIMEOUT = 3 * _WAIT_TIMEOUT
pytestmark = pytest.mark.timeout(_PROCESS_TIMEOUT + 20)


class _EarlyWakeClock:
    def __init__(self, start: float) -> None:
        self.now = start
        self.first_sleep = True

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds * 0.94 if self.first_sleep else max(seconds, 0.000001)
        self.first_sleep = False


class _OfflinePublicationBoundary:
    async def backup(self, envelope: dict[str, Any], **kwargs: Any) -> None:
        from iac_code.services.session_backup import SessionBackupService

        # Keep the old hook's real service entry reachable, with cloud backup disabled.
        await asyncio.to_thread(
            SessionBackupService().backup_session,
            "",
            "fixture-session",
            reason=kwargs["reason"],
            critical=False,
        )
        if envelope.get("failBackup"):
            raise RuntimeError("injected publication backup failure")


class _BackupDelayProbe:
    def __init__(self, control: Path, fixture_path: Path) -> None:
        from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
        from iac_code.services.session_backup import BackupReason

        self.control = control
        self.reason = BackupReason.INPUT_REQUIRED
        self.executor = object.__new__(IacCodeA2APipelineExecutor)
        IacCodeA2APipelineExecutor._backup_pipeline_publication = _OfflinePublicationBoundary.backup
        # Import after replacing the external boundary, never through startup PYTHONPATH.
        spec = importlib.util.spec_from_file_location("backup_delay_fixture_probe", fixture_path)
        assert spec is not None and spec.loader is not None
        self.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fixture)

    def marker(self, name: str) -> Path:
        return self.control.with_name(f"{self.control.name}.{name}.json")

    def arm(self, **overrides: Any) -> None:
        self.marker("arm").write_text(
            json.dumps({"stepId": "confirm_and_select", "kinds": ["candidate_select"], **overrides}),
            encoding="utf-8",
        )

    @staticmethod
    def envelope(**overrides: Any) -> dict[str, Any]:
        return {
            "eventType": "input_required",
            "stepId": "confirm_and_select",
            "eventId": "evt-target",
            "sequence": 42,
            "status": "input-required",
            "data": {
                "kind": "candidate_selection",
                "stepId": "confirm_and_select",
                "question": "private-question-text",
                "options": [{"label": "private-option-text", "payload": "private-payload-text"}],
            },
            **overrides,
        }

    def publish(self, envelope: dict[str, Any]) -> None:
        asyncio.run(self.executor._backup_pipeline_publication(envelope, reason=self.reason))

    def wait_for_marker(self, name: str) -> dict[str, Any]:
        deadline = time.monotonic() + _WAIT_TIMEOUT
        while not self.marker(name).is_file():
            assert time.monotonic() < deadline, f"{name} marker was never published"
            time.sleep(0.005)
        return json.loads(self.marker(name).read_text(encoding="utf-8"))

    def non_target_events(self) -> None:
        self.arm()
        for envelope in [
            self.envelope(eventType="permission_requested"),
            self.envelope(eventType="permission_resolved"),
            self.envelope(eventType="status", status="working"),
            self.envelope(stepId="another_step", data={"kind": "candidate_selection", "stepId": "another_step"}),
            self.envelope(data={"kind": "ask_user_question", "stepId": "confirm_and_select"}),
        ]:
            self.publish(envelope)
            assert not self.marker("started").exists(), f"non-target event consumed arm: {envelope}"

        self.publish(self.envelope())
        started = self.wait_for_marker("started")
        assert started["eventType"] == "input_required"
        assert started["stepId"] == "confirm_and_select"
        assert started["kind"] == "candidate_selection"
        assert started["eventId"] == "evt-target"
        assert started["sequence"] == 42
        finished = self.wait_for_marker("finished")
        assert finished["elapsedSeconds"] >= _DELAY_SECONDS
        assert finished["succeeded"] is True
        for marker in ("started", "finished", "claimed"):
            path = self.marker(marker)
            if path.exists():
                text = path.read_text(encoding="utf-8")
                assert "private-" not in text
                assert '"question"' not in text and '"options"' not in text and '"payload"' not in text

        self.publish(self.envelope(eventId="evt-second", sequence=43))
        assert self.wait_for_marker("started") == started, "one arm delayed a second publication"

    def dispatch_handshake(self) -> None:
        self.arm(awaitRequestDispatch=True, dispatchWaitSeconds=5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            publication = pool.submit(self.publish, self.envelope())
            started = self.wait_for_marker("started")
            try:
                time.sleep(_DELAY_SECONDS * 2)
                assert not publication.done(), "publication finished before request dispatch"
                assert not self.marker("finished").exists()
            finally:
                dispatched_at = time.monotonic()
                self.marker("dispatched").write_text(
                    json.dumps({"requestStartedMonotonic": dispatched_at}), encoding="utf-8"
                )
            publication.result()
        finished = self.wait_for_marker("finished")
        assert started["startedMonotonic"] < dispatched_at <= finished["finishedMonotonic"]
        assert finished["elapsedSeconds"] >= _DELAY_SECONDS
        assert finished["succeeded"] is True

    def backup_failure(self) -> None:
        self.arm()
        with pytest.raises(RuntimeError, match="injected publication backup failure"):
            self.publish(self.envelope(failBackup=True))
        finished = self.wait_for_marker("finished")
        assert finished["elapsedSeconds"] >= _DELAY_SECONDS
        assert finished["succeeded"] is False

    def missing_dispatch(self) -> None:
        self.arm(awaitRequestDispatch=True, dispatchWaitSeconds=0.15)
        with pytest.raises(TimeoutError, match="dispatch"):
            self.publish(self.envelope())
        finished = self.wait_for_marker("finished")
        assert _DELAY_SECONDS <= finished["elapsedSeconds"] < 5
        assert finished["succeeded"] is False

    def early_wakeup(self) -> None:
        for start in (0.0, 1667.171):
            clock = _EarlyWakeClock(start)
            self.fixture.time = clock
            self.fixture.asyncio = SimpleNamespace(sleep=clock.sleep)
            delay = self.fixture.InputPublicationBackupDelay()
            asyncio.run(delay.wait_for_dispatch(self.control, {}, {"startedMonotonic": start}))
            assert clock.monotonic() - start >= _DELAY_SECONDS, "early timer wake shortened the required backup window"

    def competing_publication(self) -> None:
        self.fixture.InputPublicationBackupDelay.write_marker(self.marker(f"ready-{os.getpid()}"), {"pid": os.getpid()})
        self.wait_for_marker("go")
        self.publish(self.envelope(eventId=f"evt-{os.getpid()}", sequence=os.getpid()))
        self.marker(f"completed-{os.getpid()}").write_text("{}", encoding="utf-8")


class _BackupDelayProcess:
    def __init__(self, tmp_path: Path, *, through_launcher: bool = False) -> None:
        self.tmp_path = tmp_path
        self.through_launcher = through_launcher
        self.control = tmp_path / "backup-delay"
        self.env = os.environ.copy()
        self.env.update(
            IAC_CODE_CONFIG_DIR=str(self.tmp_path / "config"),
            IAC_CODE_CONFIG_BACKUP_DIR="",
            IAC_CODE_E2E_BACKUP_DELAY_CONTROL=str(self.control),
            IAC_CODE_E2E_BACKUP_DELAY_SECONDS=str(_DELAY_SECONDS),
        )
        self.env["PYTHONPATH"] = str(_REPO_ROOT / "src")

    def start(self, scenario: str) -> subprocess.Popen[str]:
        command = [sys.executable, str(Path(__file__).resolve()), scenario, str(self.control), str(_FIXTURE)]
        if self.through_launcher:
            command = [
                sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))", *command,
            ]
        return subprocess.Popen(
            command,
            cwd=self.tmp_path,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def run(self, scenario: str) -> None:
        process = self.start(scenario)
        try:
            stdout, stderr = process.communicate(timeout=_PROCESS_TIMEOUT)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        assert process.returncode == 0, stdout + stderr


def test_non_target_events_do_not_consume_matching_input_delay(tmp_path: Path) -> None:
    _BackupDelayProcess(tmp_path).run("non_target_events")


def test_matching_publication_stays_in_backup_until_request_dispatch(tmp_path: Path) -> None:
    _BackupDelayProcess(tmp_path).run("dispatch_handshake")


def test_publication_backup_error_records_failed_delay(tmp_path: Path) -> None:
    _BackupDelayProcess(tmp_path).run("backup_failure")


def test_missing_request_dispatch_times_out_with_failed_marker(tmp_path: Path) -> None:
    _BackupDelayProcess(tmp_path).run("missing_dispatch")


def test_early_timer_wake_does_not_shorten_backup_delay(tmp_path: Path) -> None:
    _BackupDelayProcess(tmp_path).run("early_wakeup")


@pytest.mark.parametrize("through_launcher", [False, True], ids=["direct", "launcher"])
def test_two_processes_claim_only_one_shared_delay_window(tmp_path: Path, through_launcher: bool) -> None:
    harness = _BackupDelayProcess(tmp_path, through_launcher=through_launcher)
    control_root = tmp_path / "controls"
    control_root.mkdir()
    harness.control = control_root / "backup-delay-shared"
    harness.env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] = str(control_root)
    control = harness.control
    arm = control.with_name(f"{control.name}.arm.json")
    arm.write_text(
        json.dumps({
            "stepId": "confirm_and_select", "kinds": ["candidate_selection"],
            "awaitRequestDispatch": True, "dispatchWaitSeconds": 20,
        }),
        encoding="utf-8",
    )
    processes = [harness.start("competing_publication") for _ in range(2)]
    process_deadline = time.monotonic() + _PROCESS_TIMEOUT
    started_path = control.with_name(f"{control.name}.started.json")
    finished_path = control.with_name(f"{control.name}.finished.json")
    outcomes = []
    try:
        deadline = time.monotonic() + _WAIT_TIMEOUT
        while len(ready_paths := list(control.parent.glob(f"{control.name}.ready-*.json"))) < len(processes):
            for process in processes:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail(f"child exited before both probes were ready: {stdout}{stderr}")
            assert time.monotonic() < deadline, f"both child processes did not become ready: {ready_paths}"
            time.sleep(0.005)
        assert len(ready_paths) == len(processes)
        # A Windows venv Popen PID belongs to its redirector, not necessarily the probe.
        child_pids = [json.loads(path.read_text(encoding="utf-8"))["pid"] for path in ready_paths]
        assert len(set(child_pids)) == len(processes)
        if through_launcher:
            assert set(child_pids).isdisjoint(process.pid for process in processes)
        completed_paths = [control.with_name(f"{control.name}.completed-{pid}.json") for pid in child_pids]
        go = control.with_name(f"{control.name}.go.json")
        go_temporary = go.with_suffix(".tmp")
        go_temporary.write_text("{}", encoding="utf-8")
        go_temporary.replace(go)
        deadline = time.monotonic() + _WAIT_TIMEOUT
        while not started_path.exists() or not any(path.exists() for path in completed_paths):
            assert time.monotonic() < deadline, "both processes claimed the arm; neither bypassed the delay"
            time.sleep(0.005)
        completed = [path.exists() for path in completed_paths]
        assert completed.count(True) == 1, "one process must remain delayed until request dispatch"
        assert not finished_path.exists()
        started_text = started_path.read_text(encoding="utf-8")
        winner_pid = child_pids[completed.index(False)]
        assert json.loads(started_text)["eventId"] == f"evt-{winner_pid}"
    finally:
        dispatched_at = time.monotonic()
        control.with_name(f"{control.name}.dispatched.json").write_text(
            json.dumps({"requestStartedMonotonic": dispatched_at}), encoding="utf-8"
        )
        try:
            for process in processes:
                outcomes.append(process.communicate(timeout=max(0, process_deadline - time.monotonic())))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
            for process in processes:
                process.communicate()
        for process, (stdout, stderr) in zip(processes, outcomes):
            assert process.returncode == 0, stdout + stderr
    assert all(path.exists() for path in completed_paths)
    assert started_path.read_text(encoding="utf-8") == started_text, "losing process replaced the started identity"
    finished = json.loads(finished_path.read_text(encoding="utf-8"))
    assert finished["eventId"] == f"evt-{winner_pid}"
    assert finished["finishedMonotonic"] >= dispatched_at
    assert finished["succeeded"] is True


if __name__ == "__main__":
    probe = _BackupDelayProbe(Path(sys.argv[2]), Path(sys.argv[3]))
    getattr(probe, sys.argv[1])()
