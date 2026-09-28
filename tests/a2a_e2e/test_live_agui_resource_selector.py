from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.a2a.e2e.resource_selector.run_live_agui_resource_selector import (
    SCENARIOS,
    _interrupts,
    _run_payload,
    _selector_interrupt,
)

# The global offline-test fixture replaces HOME and clears IAC_CODE_CONFIG_DIR.
# An explicitly opted-in live subprocess must retain the user's actual credential chain.
_REAL_HOME = os.environ.get("HOME")
_REAL_USERPROFILE = os.environ.get("USERPROFILE")
_REAL_CONFIG_DIR = os.environ.get("IAC_CODE_CONFIG_DIR")


def test_agui_live_matrix_covers_both_surfaces_and_all_answers() -> None:
    assert set(SCENARIOS) == {
        "normal-selected",
        "normal-canceled",
        "normal-direct-input",
        "pipeline-selected",
        "pipeline-canceled",
        "pipeline-direct-input",
    }


def test_live_payload_uses_actual_selling_pipeline_and_same_resume_coordinates(tmp_path: Path) -> None:
    arguments = {"thread_id": "thread-1", "invocation_id": "invocation-1", "cwd": tmp_path}
    first = _run_payload(**arguments, run_mode="pipeline", prompt="Choose a VPC")
    resumed = _run_payload(
        **arguments,
        run_mode="pipeline",
        resume=[{"interruptId": "resource-1", "status": "cancelled", "payload": {"optionsEmpty": False}}],
    )
    assert first["forwardedProps"]["iacCode"]["pipelineName"] == "selling_solution_first"
    assert resumed["forwardedProps"]["iacCode"]["pipelineName"] == "selling_solution_first"
    assert resumed["threadId"] == first["threadId"]
    assert resumed["forwardedProps"]["iacCode"]["rosInvocationId"] == "invocation-1"
    assert resumed["messages"] == []


def test_agui_live_selector_interrupt_reads_public_metadata() -> None:
    selector = {
        "id": "resource-1",
        "metadata": {"kind": "cloud_resource_selection", "selector": {"id": "vpc.vpc"}},
    }
    events = [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [selector]}}]
    assert _interrupts(events) == [selector]
    assert _selector_interrupt(_interrupts(events)) == selector


@pytest.mark.integration
@pytest.mark.resource_selector_live
@pytest.mark.timeout(1200)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_real_agui_http_sse_resource_selector_matrix(tmp_path: Path, scenario: str) -> None:
    if os.environ.get("IAC_CODE_AGUI_RESOURCE_SELECTOR_LIVE_E2E", "").lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("explicit real AG-UI resource-selector E2E is disabled")
    env = os.environ.copy()
    if _REAL_HOME is not None:
        env["HOME"] = _REAL_HOME
    if _REAL_USERPROFILE is not None:
        env["USERPROFILE"] = _REAL_USERPROFILE
    if _REAL_CONFIG_DIR is None:
        env.pop("IAC_CODE_CONFIG_DIR", None)
    else:
        env["IAC_CODE_CONFIG_DIR"] = _REAL_CONFIG_DIR
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts/a2a/e2e/resource_selector/run_live_agui_resource_selector.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--allow-real-cloud",
            "--scenario",
            scenario,
            "--run-dir",
            str(tmp_path / scenario),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["scenario"] == scenario
    assert result["selectorId"] == "vpc.vpc"
    assert result["candidateCount"] > 0
    assert result["interruptIssued"] is True
    assert result["toolCompleted"] is True
    assert result["continuationObserved"] is True
    assert result["usedRealLlm"] is True
    assert result["usedRealCloudQuery"] is True
    assert result["usedAguiHttpSse"] is True
