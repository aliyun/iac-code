"""Deterministic process E2E matrix for A2A pause, resume, and termination."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.mark.integration
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
            "20",
            "--overall-timeout",
            "25",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=35,
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
    assert 0 < summary["elapsedSeconds"] < 35
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
