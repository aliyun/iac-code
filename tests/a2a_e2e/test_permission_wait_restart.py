from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.timeout(90)
@pytest.mark.parametrize(
    ("mode", "decision", "expected_executions"),
    [
        ("normal", "allow_once", 1),
        ("normal", "deny", 0),
        ("pipeline", "allow_once", 1),
        ("pipeline", "deny", 0),
    ],
)
def test_permission_wait_response_recovers_after_real_a2a_process_restart(
    tmp_path: Path,
    mode: str,
    decision: str,
    expected_executions: int,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "run_permission_wait_restart.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(tmp_path / "{}-{}".format(mode, decision)),
            "--decision",
            decision,
            "--mode",
            mode,
            "--timeout",
            "20",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=80,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["mode"] == mode
    assert result["decision"] == decision
    assert result["checkpointPhase"] == "RESOLVED"
    assert result["toolExecutions"] == expected_executions
    assert result["duplicateAcknowledged"] is True
    assert result["conflictRejected"] is True
    assert result["taskId"]
    assert result["contextId"]
    if mode == "normal":
        assert result["assistantFinalPublished"] is True
        assert result["terminalInputRequiredPublished"] is True
    else:
        assert result["pipelineCoordinatesPreserved"] is True
        assert result["pipelineJournalOrdered"] is True
        assert result["pipelineRollbackAbsent"] is True
        assert result["parentStreamEndedAtPermissionBoundary"] is True


@pytest.mark.integration
@pytest.mark.timeout(90)
def test_pipeline_permission_after_candidate_selection_recovers_on_new_response_stream(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "permission_wait" / "run_permission_wait_restart.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--run-dir",
            str(tmp_path / "pipeline-candidate-first"),
            "--decision",
            "allow_once",
            "--mode",
            "pipeline",
            "--candidate-first",
            "--timeout",
            "20",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=80,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["candidateSelectionBeforePermission"] is True
    assert result["checkpointPhase"] == "RESOLVED"
    assert result["toolExecutions"] == 1
    assert result["pipelineJournalOrdered"] is True
