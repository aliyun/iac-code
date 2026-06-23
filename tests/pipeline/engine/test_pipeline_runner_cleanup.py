from __future__ import annotations

import logging

import pytest
import yaml

from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupResource, ObservedResource
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner, PipelineStatePersistenceError
from iac_code.pipeline.engine.session import PipelineSession
from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec
from iac_code.types.stream_events import ResourceObservedEvent


def _runner(tmp_path) -> PipelineRunner:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.session = PipelineSession(tmp_path / "session" / "pipeline")
    runner.context = PipelineContext({})
    runner._loaded = LoadedPipeline(
        name="test",
        steps=[],
        context_dependencies={},
        max_rollbacks=3,
        skills={},
    )
    return runner


def test_runner_persists_resource_observed_returned_by_step_hook(tmp_path) -> None:
    runner = _runner(tmp_path)

    def on_resource_observed(ctx, event, *, ledger, step_id, attempt_id):
        assert ctx is runner.context
        assert isinstance(ledger, CleanupLedger)
        return ObservedResource(
            provider=event.provider,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            resource_name=event.resource_name,
            region_id=event.region_id,
            source_step_id=step_id,
            source_attempt_id=attempt_id,
            observed_action=event.action,
            observed_at=1.0,
        )

    step = StepSpec(
        step_id="deploying",
        conclusion_field="deployment",
        forward=None,
        prompt_file="deploying.md",
    )
    step.on_resource_observed = on_resource_observed

    runner._handle_resource_observed(
        step,
        ResourceObservedEvent(
            provider="ros",
            resource_type="stack",
            resource_id="stack-123",
            resource_name="demo",
            region_id="cn-hangzhou",
            action="CreateStack",
        ),
        attempt_id="att_0001",
    )

    [observed] = runner.cleanup_ledger().observed_resources()
    assert observed.resource_id == "stack-123"
    assert observed.source_step_id == "deploying"
    assert observed.source_attempt_id == "att_0001"


def test_runner_raises_cleanup_observed_write_failure(tmp_path, monkeypatch, caplog) -> None:
    runner = _runner(tmp_path)

    def on_resource_observed(ctx, event, *, ledger, step_id, attempt_id):
        return ObservedResource(
            provider=event.provider,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            resource_name=event.resource_name,
            region_id=event.region_id,
            source_step_id=step_id,
            source_attempt_id=attempt_id,
            observed_action=event.action,
            observed_at=1.0,
        )

    def fail_record_observed(self, observed):
        raise OSError("cleanup disk full")

    step = StepSpec(
        step_id="deploying",
        conclusion_field="deployment",
        forward=None,
        prompt_file="deploying.md",
    )
    step.on_resource_observed = on_resource_observed
    monkeypatch.setattr(CleanupLedger, "record_observed", fail_record_observed)
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.pipeline_runner")

    with pytest.raises(PipelineStatePersistenceError) as exc_info:
        runner._handle_resource_observed(
            step,
            ResourceObservedEvent(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                resource_name="demo",
                region_id="cn-hangzhou",
                action="CreateStack",
            ),
            attempt_id="att_0001",
        )

    assert exc_info.value.step_id == "deploying"
    assert "Failed to persist observed cleanup resource" in caplog.text
    assert "step_id=deploying" in caplog.text
    assert "cleanup disk full" in caplog.text


def test_runner_marks_cleanup_required_from_rollback_hook(tmp_path) -> None:
    runner = _runner(tmp_path)
    observed = ObservedResource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        resource_name="demo",
        region_id="cn-hangzhou",
        source_step_id="deploying",
        source_attempt_id="att_0001",
        observed_action="CreateStack",
    )
    runner.cleanup_ledger().record_observed(observed)

    def on_rollback_cleanup_required(ctx, *, ledger, from_step, from_attempt_id, to_step, reason):
        assert ctx is runner.context
        assert from_step == "deploying"
        assert from_attempt_id == "att_0001"
        assert to_step == "confirm_and_select"
        assert reason == "invalid selection"
        return [CleanupResource.from_observed(ledger.observed_resources()[0], reason=reason)]

    step = StepSpec(
        step_id="deploying",
        conclusion_field="deployment",
        forward=None,
        prompt_file="deploying.md",
    )
    step.on_rollback_cleanup_required = on_rollback_cleanup_required

    runner._mark_rollback_cleanup_required(
        step,
        "confirm_and_select",
        "invalid selection",
        from_attempt_id="att_0001",
    )

    [pending] = runner.cleanup_ledger().pending_resources()
    assert pending.resource_id == "stack-123"
    assert pending.cleanup_reason == "invalid selection"
    data = yaml.safe_load((runner.session.session_dir / "cleanup.yaml").read_text(encoding="utf-8"))
    assert [entry["type"] for entry in data["history"]] == ["cleanup_required"]
