from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from iac_code.pipeline.engine.loader import load_pipeline_dir
from scripts.a2a.e2e.resource_selector.run_live_resource_selector import (
    SCENARIOS,
    _iac_code_values,
    _resource_selection_inputs,
    _selection_response,
)


def _live_enabled() -> bool:
    return os.environ.get("IAC_CODE_A2A_RESOURCE_SELECTOR_LIVE_E2E", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _live_scenarios() -> list[str]:
    raw = os.environ.get(
        "IAC_CODE_A2A_RESOURCE_SELECTOR_LIVE_SCENARIOS",
        ",".join(SCENARIOS),
    )
    return [item.strip() for item in raw.split(",") if item.strip()]


# tests/conftest.py's autouse ``_isolate_iac_home`` fixture repoints ``HOME`` at an
# empty per-test directory so ordinary tests can never touch the developer's real
# configuration.  The live runner is started as a subprocess and inherits that
# environment, so its default ``--source-config-dir`` of ``~/.iac-code`` would
# resolve to the empty directory and the readiness gate would report missing LLM
# and cloud credentials.  Capture the real home here, while the module is being
# imported and before any test fixture has run.
_REAL_HOME = Path(os.path.expanduser("~"))


def test_resource_selection_event_extraction_deduplicates_input_projections(tmp_path: Path) -> None:
    pending = {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "resource-" + "a" * 32,
        "toolUseId": "tool-1",
        "selector": {
            "id": "vpc.vpc",
            "associationPropertyMetadata": {"RegionId": "cn-hangzhou"},
            "source": None,
        },
    }
    event_path = tmp_path / "events.jsonl"
    event_path.write_text(
        json.dumps({"result": {"metadata": {"iac_code": {"input": pending, "inputRequired": pending}}}}) + "\n",
        encoding="utf-8",
    )

    assert _resource_selection_inputs(event_path) == [pending]


def test_selected_response_echoes_authoritative_correlation_fields() -> None:
    pending = {
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "resource-" + "a" * 32,
        "toolUseId": "tool-1",
        "selector": {"id": "vpc.vpc", "source": None},
    }

    response = _selection_response(pending, status="selected", value="vpc-test", label="test-vpc")

    assert response == {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "status": "selected",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "resource-" + "a" * 32,
        "toolUseId": "tool-1",
        "selectorId": "vpc.vpc",
        "value": "vpc-test",
        "label": "test-vpc",
    }

    canceled = _selection_response(pending, status="canceled", options_empty=False)
    assert canceled == {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "status": "canceled",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "resource-" + "a" * 32,
        "toolUseId": "tool-1",
        "optionsEmpty": False,
    }


def test_iac_code_value_extraction_reads_nested_transport_metadata(tmp_path: Path) -> None:
    event_path = tmp_path / "events.jsonl"
    event_path.write_text(
        json.dumps({"result": {"metadata": {"iac_code": {"inputReceived": {"duplicate": True}}}}}) + "\n",
        encoding="utf-8",
    )

    assert _iac_code_values(event_path, "inputReceived") == [{"duplicate": True}]


def test_live_pipeline_fixtures_load_with_production_pipeline_loader() -> None:
    root = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "a2a"
        / "e2e"
        / "resource_selector"
        / "live_pipelines"
    )

    assert {path.name for path in root.iterdir() if path.is_dir()} == {
        "immediate_handoff",
        "resource_selector_stage",
    }
    for pipeline_dir in root.iterdir():
        if pipeline_dir.is_dir():
            loaded = load_pipeline_dir(pipeline_dir)
            assert len(loaded.steps) == 1
            assert loaded.on_complete is not None


@pytest.mark.integration
@pytest.mark.resource_selector_live
@pytest.mark.skipif(not _live_enabled(), reason="explicit live A2A resource-selector E2E is disabled")
@pytest.mark.timeout(1200)
@pytest.mark.parametrize("scenario", _live_scenarios())
def test_real_llm_and_cloud_resource_selector_a2a_flow(tmp_path: Path, scenario: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "a2a" / "e2e" / "resource_selector" / "run_live_resource_selector.py"
    command = [
        sys.executable,
        str(runner),
        "--allow-real-cloud",
        "--scenario",
        scenario,
        "--run-dir",
        str(tmp_path / scenario),
    ]
    source_config = os.environ.get("IAC_CODE_A2A_RESOURCE_SELECTOR_LIVE_CONFIG_DIR") or str(_REAL_HOME / ".iac-code")
    command.extend(("--source-config-dir", source_config))
    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1180,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["scenario"] == scenario
    assert result["selectorId"] == "vpc.vpc"
    if scenario == "empty-list-canceled-next-turn":
        assert result["candidateCount"] == 0
    else:
        assert result["candidateCount"] > 0
    if scenario == "sequential-vpc-vswitch":
        assert result["secondarySelectorId"] == "vpc.vswitch"
        assert result["secondaryCandidateCount"] > 0
    if scenario == "duplicate-and-conflict":
        assert result["duplicateAcknowledged"] is True
        assert result["conflictRejected"] is True
    if scenario in {"pipeline-handoff-normal", "pipeline-stage-selection"}:
        assert result["pipelineHandoffVerified"] is True
    assert result["usedRealLlm"] is True
    assert result["usedRealCloudQuery"] is True
    assert result["nextTurnCompleted"] is True
