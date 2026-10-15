"""Deterministic process E2E matrix for A2A pause, resume, and termination."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from iac_code.services.session_backup import SessionBackupService
from iac_code.services.session_storage import SessionStorage
from scripts.a2a.e2e.execution_control import run_execution_control_scenarios as runner

# A scenario includes process startup, control round trips (including real
# disconnect-deadline checks), and durable completion. These phases share the
# overall watchdog; keep the per-step scenario wait budget at 20 seconds.
# In particular, Windows pipeline setup can consume most of the old 25s budget
# before the resumed tool is released. Leave time for real backup/stream drain.
WAIT_TIMEOUT = 20
OVERALL_TIMEOUT = 3 * WAIT_TIMEOUT
PROCESS_TIMEOUT = OVERALL_TIMEOUT + 10  # Allow the runner to clean up and write audits.
TEST_TIMEOUT = PROCESS_TIMEOUT + 10

CASES = (
    ("warm-resume-pausing", "normal"),
    ("warm-resume-pausing", "pipeline"),
    ("warm-resume-paused", "normal"),
    ("warm-resume-paused", "pipeline"),
    ("disconnect-timeout-after-operation-id", "normal"),
    ("disconnect-timeout-after-operation-id", "pipeline"),
    ("disconnect-timeout-inflight-sync-call", "pipeline"),
    ("disconnect-timeout-backup-blocked", "pipeline"),
    ("natural-completion-while-pausing", "normal"),
    ("slow-termination-storage", "normal"),
    ("terminate-during-bootstrap", "normal"),
    ("terminate-during-bootstrap", "pipeline"),
    ("terminate-during-bootstrap-disconnected", "normal"),
    ("terminate-during-bootstrap-disconnected", "pipeline"),
    ("disconnect-timeout-during-turn-backup", "normal"),
    ("legacy-cancel-idle", "pipeline"),
    ("slow-rollover-storage", "normal"),
    ("stack-instances-timeout-after-operation-id", "normal"),
    ("stack-instances-terminate-inflight", "normal"),
    ("recovery-during-normal-rollover", "normal"),
)


class _SharedPublication:
    def __init__(self, root: Path) -> None:
        self.root = root / "shared-backup"
        self.session = self.root / "projects" / "fixture-project" / "fixture-session"
        self.session.mkdir(parents=True)
        self.commit_id = "fixture-target-commit"
        (self.session / "payload.json").write_text(
            json.dumps({"commit": self.commit_id, "result": "complete fixture payload"}), encoding="utf-8"
        )

    def write_state(self, *, commit_id: str | None = None, session_id: str = "fixture-session") -> None:
        state = {
            "schema_version": 2,
            "session_id": session_id,
            "generation": 2,
            "parent_generation": 1,
            "commit_id": commit_id or self.commit_id,
            "status": "succeeded",
            "reason": "input_required",
            "updated_at": "2026-10-15T00:00:00Z",
            "writer_id": "fixture-writer",
            "publication_proofs": {},
        }
        (self.session / ".backup-state.json").write_text(json.dumps(state), encoding="utf-8")

    def write_marker(self) -> None:
        (self.session / ".backup-publication.json").write_text(
            json.dumps({"version": 1, "sessionId": "fixture-session", "generation": 2, "commitId": self.commit_id}),
            encoding="utf-8",
        )

    @staticmethod
    def scenario(root: Path) -> runner._Scenario:
        return runner._Scenario(
            run_dir=root,
            server=SimpleNamespace(assert_running=lambda: None),
            scenario="disconnect-timeout-after-operation-id",
            mode="normal",
            timeout=0.05,
        )


@pytest.mark.parametrize(
    "publication", ["marker_only", "old_state", "wrong_session", "failed_state", "state_with_marker"]
)
def test_shared_publication_wait_rejects_uncommitted_capture(tmp_path: Path, publication: str) -> None:
    shared = _SharedPublication(tmp_path)
    if publication == "old_state":
        shared.write_state(commit_id="fixture-old-commit")
    elif publication == "wrong_session":
        shared.write_state(session_id="another-session")
    elif publication in {"failed_state", "state_with_marker"}:
        shared.write_state()
        if publication == "failed_state":
            state_path = shared.session / ".backup-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update(
                status="failed",
                attempt_commit_id="another-attempt",
                attempt_publication_proofs={},
                error="injected failure",
                attempt=1,
                retry_count=0,
                exhausted=True,
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
    if publication in {"marker_only", "state_with_marker"}:
        shared.write_marker()

    with pytest.raises(TimeoutError, match="staged snapshot publication"):
        shared.scenario(tmp_path)._wait_shared_publication(shared.commit_id)


def test_shared_publication_wait_returns_complete_matching_capture(tmp_path: Path) -> None:
    shared = _SharedPublication(tmp_path)
    shared.write_state()

    joined = shared.scenario(tmp_path)._wait_shared_publication(shared.commit_id)

    assert "complete fixture payload" in joined
    assert shared.commit_id in joined


def test_shared_publication_wait_locks_the_payload_read_window(tmp_path: Path, monkeypatch) -> None:
    shared = _SharedPublication(tmp_path)
    shared.write_state()
    scenario = shared.scenario(tmp_path)
    read_payload = scenario._shared_backup_text
    reading = threading.Event()
    publishing = threading.Event()
    acquired = threading.Event()

    def publish_next_capture() -> None:
        reading.wait()
        publishing.set()
        service = SessionBackupService(SessionStorage(projects_dir=tmp_path / "config" / "projects"))
        with service._shared_session_lock(
            shared.root, project="fixture-project", session_id="fixture-session"
        ):
            acquired.set()
            shared.write_marker()

    def read_while_publishing(*args) -> str:
        reading.set()
        assert publishing.wait(1), "publisher did not attempt its shared lock"
        assert not acquired.wait(0.05), "publisher changed the capture during the payload read"
        return read_payload(*args)

    monkeypatch.setattr(scenario, "_shared_backup_text", read_while_publishing)
    publisher = threading.Thread(target=publish_next_capture)
    publisher.start()
    try:
        joined = scenario._wait_shared_publication(shared.commit_id)
        assert "complete fixture payload" in joined
    finally:
        reading.set()
        publisher.join(2)
    assert not publisher.is_alive()
    assert acquired.is_set()


def test_legacy_cancel_waits_for_drain_before_validating_idle_exit(monkeypatch, tmp_path: Path) -> None:
    """A response delayed by backup publication must not use the 2s probe budget."""
    final = {"result": {"status": {"state": "TASK_STATE_CANCELED"}}}
    response = Mock()
    response.status = 200
    response.read.return_value = json.dumps(final).encode("utf-8")
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)

    def delayed_cancel(request, *, timeout):
        assert json.loads(request.data)["method"] == "CancelTask"
        if timeout < 3.0:
            raise TimeoutError("cancellation backup is still being published")
        return response

    monkeypatch.setattr(runner, "urlopen", delayed_cancel)
    (tmp_path / "server.log").write_text("", encoding="utf-8")
    (tmp_path / "fixture-lifecycle.jsonl").write_text('{"event":"idle.shutdown"}\n', encoding="utf-8")
    server = SimpleNamespace(url="http://fixture.invalid", wait_for_idle_exit=Mock())
    scenario = runner._Scenario(
        run_dir=tmp_path, server=server, scenario="legacy-cancel-idle", mode="pipeline", timeout=WAIT_TIMEOUT
    )
    scenario.task_id = "task-fixture"
    scenario._wait_log_event = Mock()
    initial = SimpleNamespace(snapshot=lambda: "LEGACY_CANCEL_FIRST_TOKEN", join=Mock())

    scenario._legacy_cancel_idle(initial)

    initial.join.assert_called_once_with(WAIT_TIMEOUT)
    scenario._wait_log_event.assert_called_once_with(tmp_path / "provider-lifecycle.jsonl", "provider.closed")
    server.wait_for_idle_exit.assert_called_once_with()
    assert json.loads((tmp_path / "task-final.json").read_text(encoding="utf-8")) == final


@pytest.mark.integration
@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize(("scenario", "mode"), CASES, ids=["{}-{}".format(*case) for case in CASES])
def test_execution_control_scenario(tmp_path: Path, scenario: str, mode: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    run_dir = tmp_path / "{}-{}".format(scenario, mode)
    runner = repo_root / "scripts" / "a2a" / "e2e" / "execution_control" / "run_execution_control_scenarios.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(run_dir),
            "--scenario",
            scenario,
            "--mode",
            mode,
            "--timeout",
            str(WAIT_TIMEOUT),
            "--overall-timeout",
            str(OVERALL_TIMEOUT),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=PROCESS_TIMEOUT,
        check=False,
    )
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
    assert completed.returncode == 0, "scenario failed: {}\nstdout:\n{}\nstderr:\n{}\nserver:\n{}\nrunner:\n{}".format(
        summary,
        completed.stdout,
        completed.stderr,
        (run_dir / "server.log").read_text(encoding="utf-8") if (run_dir / "server.log").exists() else "",
        (run_dir / "runner-error.txt").read_text(encoding="utf-8") if (run_dir / "runner-error.txt").exists() else "",
    )
    assert summary is not None and summary["status"] == "passed"
    assert 0 < summary["elapsedSeconds"] < PROCESS_TIMEOUT
    assert summary["server"]["returnCode"] is not None
    assert summary["server"]["forcedKill"] is False
    for artifact in (
        "agent-card.json",
        "backup-audit.json",
        "control-requests.json",
        "execution-state-timeline.json",
        "initial.events.jsonl",
        "provider-lifecycle.jsonl",
        "requests.jsonl",
        "server-lifecycle.json",
        "server.log",
        "stream-summaries.json",
        "tool-lifecycle.jsonl",
    ):
        assert (run_dir / artifact).exists(), artifact
    for stream in summary["streams"]:
        assert (run_dir / "{}.events.jsonl".format(stream["name"])).exists()
    provider_calls = [
        json.loads(line)
        for line in (run_dir / "provider-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
        if '"provider.called"' in line
    ]
    if scenario.startswith("terminate-during-bootstrap"):
        assert not provider_calls
    else:
        assert provider_calls
    assert all("messageCount" in call and "messageSummary" in call for call in provider_calls)
