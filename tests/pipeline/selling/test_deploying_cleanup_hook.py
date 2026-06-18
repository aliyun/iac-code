from __future__ import annotations

from iac_code.pipeline.engine.cleanup import CleanupLedger
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.selling.hooks import deploying
from iac_code.types.stream_events import ResourceObservedEvent


def test_deploying_hook_returns_ros_create_stack_observation_without_persisting(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ctx = PipelineContext({})
    event = ResourceObservedEvent(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        resource_name="demo",
        region_id="cn-hangzhou",
        action="CreateStack",
        tool_name="ros_stack",
        tool_use_id="toolu-create",
        metadata={"params": {"TemplateBody": "secret template", "Password": "secret"}},
    )

    observed = deploying.on_resource_observed(ctx, event, ledger=ledger, step_id="deploying", attempt_id="att_0001")

    assert observed is not None
    assert observed.provider == "ros"
    assert observed.resource_type == "stack"
    assert observed.resource_id == "stack-123"
    assert observed.source_step_id == "deploying"
    assert observed.source_attempt_id == "att_0001"
    assert observed.metadata == {"tool_name": "ros_stack", "tool_use_id": "toolu-create"}
    assert ledger.observed_resources() == []


def test_deploying_hook_ignores_non_create_stack_observations(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ctx = PipelineContext({})

    observed = deploying.on_resource_observed(
        ctx,
        ResourceObservedEvent(
            provider="ros",
            resource_type="stack",
            resource_id="stack-123",
            action="UpdateStack",
        ),
        ledger=ledger,
        step_id="deploying",
        attempt_id="att_0001",
    )

    assert observed is None
    assert ledger.observed_resources() == []


def test_deploying_hook_marks_only_deploying_rollback_resources(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ctx = PipelineContext({})
    observed = deploying.on_resource_observed(
        ctx,
        ResourceObservedEvent(
            provider="ros",
            resource_type="stack",
            resource_id="stack-123",
            resource_name="demo",
            region_id="cn-hangzhou",
            action="CreateStack",
        ),
        ledger=ledger,
        step_id="deploying",
        attempt_id="att_0001",
    )
    assert observed is not None
    ledger.record_observed(observed)

    cleanup = deploying.on_rollback_cleanup_required(
        ctx,
        ledger=ledger,
        from_step="deploying",
        from_attempt_id="att_0001",
        to_step="confirm_and_select",
        reason="invalid selection",
    )

    assert len(cleanup) == 1
    assert cleanup[0].resource_id == "stack-123"
    assert cleanup[0].cleanup_reason == "invalid selection"
    assert ledger.pending_resources() == []


def test_deploying_hook_ignores_other_step_rollbacks(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ctx = PipelineContext({})
    observed = deploying.on_resource_observed(
        ctx,
        ResourceObservedEvent(
            provider="ros",
            resource_type="stack",
            resource_id="stack-123",
            action="CreateStack",
        ),
        ledger=ledger,
        step_id="deploying",
        attempt_id="att_0001",
    )
    assert observed is not None
    ledger.record_observed(observed)

    cleanup = deploying.on_rollback_cleanup_required(
        ctx,
        ledger=ledger,
        from_step="confirm_and_select",
        from_attempt_id="att_0001",
        to_step="architecture_planning",
        reason="retry",
    )

    assert cleanup == []
    assert ledger.pending_resources() == []


def test_deploying_hook_marks_only_current_attempt_resources(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ctx = PipelineContext({})
    first = deploying.on_resource_observed(
        ctx,
        ResourceObservedEvent(
            provider="ros",
            resource_type="stack",
            resource_id="stack-old",
            region_id="cn-hangzhou",
            action="CreateStack",
        ),
        ledger=ledger,
        step_id="deploying",
        attempt_id="att_0001",
    )
    second = deploying.on_resource_observed(
        ctx,
        ResourceObservedEvent(
            provider="ros",
            resource_type="stack",
            resource_id="stack-current",
            region_id="cn-hangzhou",
            action="CreateStack",
        ),
        ledger=ledger,
        step_id="deploying",
        attempt_id="att_0002",
    )
    assert first is not None
    assert second is not None
    ledger.record_observed(first)
    ledger.record_observed(second)

    cleanup = deploying.on_rollback_cleanup_required(
        ctx,
        ledger=ledger,
        from_step="deploying",
        from_attempt_id="att_0002",
        to_step="confirm_and_select",
        reason="retry current attempt",
    )

    assert [resource.resource_id for resource in cleanup] == ["stack-current"]


def test_deploying_cleanup_ledger_does_not_persist_observed_secret_metadata(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ctx = PipelineContext({})
    observed = deploying.on_resource_observed(
        ctx,
        ResourceObservedEvent(
            provider="ros",
            resource_type="stack",
            resource_id="stack-secret",
            resource_name="demo",
            region_id="cn-hangzhou",
            action="CreateStack",
            tool_name="ros_stack",
            tool_use_id="toolu-create",
            metadata={"params": {"TemplateBody": "secret template body", "DbPassword": "super-secret"}},
        ),
        ledger=ledger,
        step_id="deploying",
        attempt_id="att_0001",
    )
    assert observed is not None

    ledger.record_observed(observed)
    cleanup = deploying.on_rollback_cleanup_required(
        ctx,
        ledger=ledger,
        from_step="deploying",
        from_attempt_id="att_0001",
        to_step="confirm_and_select",
        reason="rollback",
    )
    ledger.mark_cleanup_required(cleanup, source_step_id="deploying", reason="rollback")

    text = ledger.path.read_text(encoding="utf-8")
    assert "secret template body" not in text
    assert "super-secret" not in text
    assert "TemplateBody" not in text
    assert "DbPassword" not in text
