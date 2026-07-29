from __future__ import annotations

import json

from scripts.a2a.e2e.run_contract_scenarios import (
    SCENARIOS,
    _audit_a2a_attribution,
    _audit_pipeline_provider_attribution,
    _load_public_payloads,
    parse_args,
)


def test_contract_runner_exposes_the_three_required_a2a_scenarios() -> None:
    args = parse_args([])

    assert args.scenario is None
    assert SCENARIOS == {
        "e3a-recovery": "fault-after-snapshot",
        "e3b-success": "contract-graceful-success",
        "e3b-cancel": "contract-graceful-cancel",
    }


def test_load_public_payloads_reads_stream_task_and_cancel_artifacts(tmp_path) -> None:
    (tmp_path / "stream.events.jsonl").write_text(
        json.dumps({"result": {"status": "working"}}) + "\n" + json.dumps({"result": {"status": "completed"}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "result.task-get.json").write_text(json.dumps({"result": {"id": "task-1"}}), encoding="utf-8")
    (tmp_path / "result.cancel-response.json").write_text(
        json.dumps({"result": {"status": "canceled"}}), encoding="utf-8"
    )

    payloads = _load_public_payloads(tmp_path)

    assert len(payloads) == 4
    assert {payload["result"].get("status") for payload in payloads if "status" in payload["result"]} == {
        "working",
        "completed",
        "canceled",
    }


def test_a2a_attribution_requires_matching_task_context_and_pipeline_run() -> None:
    records = [
        {
            "kind": "log",
            "name": "iac.pipeline.step.completed",
            "attributes": {"task_id": "task-1", "context_id": "ctx-1", "pipeline_run_id": "ctx-1"},
        }
    ]

    assert _audit_a2a_attribution(records, task_id="task-1", context_id="ctx-1")["passed"] is True
    assert _audit_a2a_attribution(records, task_id="task-other", context_id="ctx-1")["passed"] is False


def test_pipeline_provider_attribution_ignores_control_spans_but_checks_pipeline_spans() -> None:
    control_span = {"kind": "span", "name": "chat fixture", "span_id": "control", "attributes": {}}
    pipeline_span = {
        "kind": "span",
        "name": "chat fixture",
        "span_id": "pipeline",
        "attributes": {
            "iac_code.mode": "pipeline",
            "pipeline_name": "selling",
            "pipeline_run_id": "ctx-1",
        },
    }

    assert _audit_pipeline_provider_attribution([control_span, pipeline_span], context_id="ctx-1")["passed"] is True
    assert (
        _audit_pipeline_provider_attribution([control_span, pipeline_span], context_id="ctx-other")["passed"] is False
    )
