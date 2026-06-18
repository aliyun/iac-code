import logging
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from unittest.mock import ANY, MagicMock, patch

import pytest

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.interrupt import InterruptVerdict
from iac_code.pipeline.engine.observability import PipelineObservability
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.session import RestoreResult
from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec, SubPipelineSpec
from iac_code.pipeline.engine.types import RollbackRule, StepResult, StepStatus
from iac_code.services.telemetry.config import ContentCaptureMode
from iac_code.services.telemetry.names import Events, GenAiAttr, GenAiOperationName, GenAiSpanKind, Metrics, Spans


def test_pipeline_started_emits_event_and_span_attrs():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.get_session_id", return_value="iac_sess_sid", create=True),
        patch("iac_code.pipeline.engine.observability.get_user_id", return_value="iac_user_uid", create=True),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.start_span", return_value=nullcontext()) as start_span,
    ):
        with obs.pipeline_run_span(total_steps=5):
            obs.pipeline_started(total_steps=5, step_names=["a", "b"])

    start_span.assert_called_once()
    assert start_span.call_args.args[0] == Spans.PIPELINE_RUN
    assert start_span.call_args.args[1]["pipeline_name"] == "selling"
    assert start_span.call_args.args[1]["session_id"] == "sid"
    assert start_span.call_args.args[1][GenAiAttr.SPAN_KIND] == GenAiSpanKind.ENTRY
    assert start_span.call_args.args[1][GenAiAttr.OPERATION_NAME] == GenAiOperationName.ENTER
    assert start_span.call_args.args[1][GenAiAttr.SESSION_ID] == "iac_sess_sid"
    assert start_span.call_args.args[1][GenAiAttr.USER_ID] == "iac_user_uid"
    assert start_span.call_args.args[1][GenAiAttr.FRAMEWORK] == "iac-code-cli"
    assert start_span.call_args.args[1][GenAiAttr.AGENT_NAME] == "selling"
    log_event.assert_called_once_with(
        Events.PIPELINE_STARTED,
        {
            "pipeline_name": "selling",
            "session_id": "sid",
            "cwd_present": True,
            "total_steps": 5,
            "step_names": ["a", "b"],
        },
    )


def test_pipeline_nested_spans_emit_genai_semantics_and_keep_pipeline_attrs():
    obs = PipelineObservability(pipeline_name="selling", session_id="raw-sid", cwd="/repo")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.get_session_id", return_value="iac_sess_raw-sid", create=True),
        patch("iac_code.pipeline.engine.observability.get_user_id", return_value="iac_user_uid", create=True),
        patch("iac_code.pipeline.engine.observability.start_span", return_value=nullcontext()) as start_span,
    ):
        with obs.step_span(
            step_id="evaluate_candidates",
            step_index=2,
            step_attempt=3,
            total_steps=5,
            step_type="llm",
        ):
            pass
        with obs.sub_pipeline_span(
            parent_step_id="evaluate_candidates",
            sub_pipeline_name="candidate_a",
            candidate_index=1,
        ):
            pass
        with obs.sub_step_span(
            parent_step_id="evaluate_candidates",
            sub_pipeline_name="candidate_a",
            sub_step_id="score_candidate",
            sub_step_index=4,
            total_sub_steps=6,
        ):
            pass

    step_attrs = start_span.call_args_list[0].args[1]
    sub_pipeline_attrs = start_span.call_args_list[1].args[1]
    sub_step_attrs = start_span.call_args_list[2].args[1]

    assert step_attrs["pipeline_name"] == "selling"
    assert step_attrs["session_id"] == "raw-sid"
    assert step_attrs["step_id"] == "evaluate_candidates"
    assert step_attrs["step_attempt"] == 3
    assert step_attrs[GenAiAttr.SPAN_KIND] == GenAiSpanKind.STEP
    assert step_attrs[GenAiAttr.OPERATION_NAME] == GenAiOperationName.REACT
    assert step_attrs[GenAiAttr.REACT_ROUND] == 2

    assert sub_pipeline_attrs["parent_step_id"] == "evaluate_candidates"
    assert sub_pipeline_attrs["sub_pipeline_name"] == "candidate_a"
    assert sub_pipeline_attrs[GenAiAttr.SPAN_KIND] == GenAiSpanKind.CHAIN
    assert sub_pipeline_attrs[GenAiAttr.OPERATION_NAME] == GenAiOperationName.INVOKE_AGENT

    assert sub_step_attrs["sub_step_id"] == "score_candidate"
    assert sub_step_attrs["sub_step_index"] == 4
    assert sub_step_attrs[GenAiAttr.SPAN_KIND] == GenAiSpanKind.STEP
    assert sub_step_attrs[GenAiAttr.OPERATION_NAME] == GenAiOperationName.REACT
    assert sub_step_attrs[GenAiAttr.REACT_ROUND] == 4

    for attrs in (step_attrs, sub_pipeline_attrs, sub_step_attrs):
        assert attrs[GenAiAttr.SESSION_ID] == "iac_sess_raw-sid"
        assert attrs[GenAiAttr.USER_ID] == "iac_user_uid"
        assert attrs[GenAiAttr.FRAMEWORK] == "iac-code-cli"
        assert attrs[GenAiAttr.AGENT_NAME] == "selling"


def test_event_sanitizer_replaces_sensitive_fields_by_default():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(
                candidate_name="ECS + RDS production plan",
                error_summary="failed with token=abc123",
                rollback_reason="user said use another budget",
                selected_value="ECS + RDS production plan",
                prompt="Choose a plan",
                cwd="/repo/customer-a",
                error_type="RuntimeError",
            ),
        )

    attrs = log_event.call_args.args[1]
    assert "candidate_name" not in attrs
    assert "error_summary" not in attrs
    assert "rollback_reason" not in attrs
    assert "selected_value" not in attrs
    assert "prompt" not in attrs
    assert "cwd" not in attrs
    assert attrs["candidate_name_present"] is True
    assert attrs["candidate_name_length_bucket"] == "1-50"
    assert attrs["error_summary_present"] is True
    assert attrs["rollback_reason_present"] is True
    assert attrs["selected_value_present"] is True
    assert attrs["prompt_present"] is True
    assert attrs["cwd_present"] is True
    assert attrs["error_type"] == "RuntimeError"


def test_span_attrs_replace_sensitive_fields_even_when_debug_capture_enabled():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.SPAN_AND_EVENT,
        ),
        patch("iac_code.pipeline.engine.observability.start_span", return_value=nullcontext()) as start_span,
    ):
        with obs.sub_pipeline_span(
            candidate_name="customer-specific candidate",
            error_summary="failed with token=abc123",
            prompt="Choose a plan",
            selected_value="customer-specific candidate",
            sub_pipeline_name="evaluate_candidate",
            candidate_index=0,
        ):
            pass

    attrs = start_span.call_args.args[1]
    assert "candidate_name" not in attrs
    assert "error_summary" not in attrs
    assert "prompt" not in attrs
    assert "selected_value" not in attrs
    assert "cwd" not in attrs
    assert attrs["candidate_name_present"] is True
    assert attrs["candidate_name_length_bucket"] == "1-50"
    assert attrs["error_summary_present"] is True
    assert attrs["prompt_present"] is True
    assert attrs["selected_value_present"] is True
    assert attrs["cwd_present"] is True
    assert attrs["sub_pipeline_name"] == "evaluate_candidate"
    assert attrs["candidate_index"] == 0


def test_event_sanitizer_keeps_sensitive_fields_derived_in_no_content_even_when_debug_enabled():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")
    long_candidate = "x" * 250
    secret_error = "request failed token=abc123 password: hunter2"

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(candidate_name=long_candidate, error_summary=secret_error),
        )

    attrs = log_event.call_args.args[1]
    assert "candidate_name" not in attrs
    assert "error_summary" not in attrs
    assert attrs["candidate_name_present"] is True
    assert attrs["candidate_name_length_bucket"] == "201-1000"
    assert attrs["error_summary_present"] is True


def test_event_sanitizer_keeps_sensitive_fields_derived_in_span_only_mode():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.SPAN_ONLY,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(
                candidate_name="ECS + RDS production plan",
                error_summary="failed with token=abc123",
                selected_value="ECS + RDS production plan",
                prompt="Choose a plan",
            ),
        )

    attrs = log_event.call_args.args[1]
    assert "candidate_name" not in attrs
    assert "error_summary" not in attrs
    assert "selected_value" not in attrs
    assert "prompt" not in attrs
    assert "cwd" not in attrs
    assert attrs["candidate_name_present"] is True
    assert attrs["error_summary_present"] is True
    assert attrs["selected_value_present"] is True
    assert attrs["prompt_present"] is True
    assert attrs["cwd_present"] is True


def test_event_sanitizer_keeps_sensitive_fields_derived_in_span_only_mode_with_debug_enabled():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.SPAN_ONLY,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(
                candidate_name="ECS + RDS production plan",
                prompt="Choose a plan",
            ),
        )

    attrs = log_event.call_args.args[1]
    assert "candidate_name" not in attrs
    assert "prompt" not in attrs
    assert "cwd" not in attrs
    assert attrs["candidate_name_present"] is True
    assert attrs["prompt_present"] is True
    assert attrs["cwd_present"] is True


@pytest.mark.parametrize(
    "mode",
    [
        ContentCaptureMode.EVENT_ONLY,
        ContentCaptureMode.SPAN_AND_EVENT,
    ],
)
def test_event_sanitizer_keeps_redacted_raw_fields_in_event_capture_modes(mode):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")
    long_candidate = "x" * 250
    secret_error = "request failed token=abc123 password: hunter2"

    with (
        patch("iac_code.pipeline.engine.observability.get_content_capture_mode", return_value=mode),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(candidate_name=long_candidate, error_summary=secret_error),
        )

    attrs = log_event.call_args.args[1]
    assert attrs["candidate_name"] == ("x" * 200) + "..."
    assert "abc123" not in attrs["error_summary"]
    assert "hunter2" not in attrs["error_summary"]
    assert "[REDACTED]" in attrs["error_summary"]


def test_event_sanitizer_redacts_public_paths_and_bare_keys_in_event_capture_mode():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.EVENT_ONLY,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(
                error_summary="provider rejected sk-live-secret at /Users/alice/.iac-code/settings.yml",
                rollback_reason="retry with ~/.iac-code/.credentials.yml and /etc/iac-code/settings.yml",
            ),
        )

    attrs = log_event.call_args.args[1]
    rendered = str(attrs)
    assert "sk-live-secret" not in rendered
    assert "/Users/alice" not in rendered
    assert "~/.iac-code" not in rendered
    assert "/etc/iac-code" not in rendered


@pytest.mark.parametrize(
    ("secret_text", "secret_values"),
    [
        (
            'payload {"password": "hunter2", "admin_token": "abc123", "api_key": "sk-test"}',
            ["hunter2", "abc123", "sk-test"],
        ),
        (
            "payload {'password': 'hunter2', 'admin_token': 'abc123'}",
            ["hunter2", "abc123"],
        ),
        ("headers Authorization: Bearer bearer-secret-123", ["bearer-secret-123"]),
        (
            "access key AKIA1234567890ABCDEF and aliyun LTAI1234567890abcdef",
            ["AKIA1234567890ABCDEF", "LTAI1234567890abcdef"],
        ),
        (
            'tokens {"session_token": "session-secret", "customer_api_key": "customer-secret"}',
            ["session-secret", "customer-secret"],
        ),
        ('headers {"Authorization": "Bearer bearer-secret-123"}', ["bearer-secret-123"]),
        ("headers {'Authorization': 'Bearer bearer-secret-123'}", ["bearer-secret-123"]),
        (
            'headers {"Authorization": {"scheme": "Bearer", "credentials": "bearer-secret-123"}}',
            ["bearer-secret-123"],
        ),
        (
            "headers {'Authorization': {'scheme': 'Bearer', 'credentials': 'bearer-secret-123'}}",
            ["bearer-secret-123"],
        ),
        (
            'headers {"Authorization": {"credentials": "bearer-secret-123", "scheme": "Bearer"}}',
            ["bearer-secret-123"],
        ),
        (
            "headers {'Authorization': {'credentials': 'bearer-secret-123', 'scheme': 'Bearer'}}",
            ["bearer-secret-123"],
        ),
        ("headers Authorization=Bearer bearer-secret-123", ["bearer-secret-123"]),
        ('headers Authorization = "Bearer bearer-secret-123"', ["bearer-secret-123"]),
        ('headers authorization={"scheme":"Bearer","credentials":"bearer-secret-123"}', ["bearer-secret-123"]),
        ('headers authorization={"scheme"="Bearer","credentials"="bearer-secret-123"}', ["bearer-secret-123"]),
        ('headers authorization={"credentials"="bearer-secret-123","scheme"="Bearer"}', ["bearer-secret-123"]),
        ("headers authorization={scheme:Bearer,credentials:bearer-secret-123}", ["bearer-secret-123"]),
        ("headers authorization={credentials:bearer-secret-123,scheme:Bearer}", ["bearer-secret-123"]),
        ("api key: sk-test", ["sk-test"]),
        ("access key secret = aks-secret", ["aks-secret"]),
        ("access key id: aks-id", ["aks-id"]),
        ("access token: access-token-secret", ["access-token-secret"]),
        ("secret key = secret-key-value", ["secret-key-value"]),
        ('payload {"api key": "sk-test"}', ["sk-test"]),
        ("payload {'api key': 'sk-test'}", ["sk-test"]),
        ('payload {"access key id": "ak-id"}', ["ak-id"]),
        ('payload {"access key secret": "ak-secret"}', ["ak-secret"]),
        ('payload {"secret key": "secret-value"}', ["secret-value"]),
    ],
)
def test_event_sanitizer_redacts_obvious_secret_formats_in_raw_capture(secret_text, secret_values):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.EVENT_ONLY,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(error_summary=secret_text),
        )

    redacted = log_event.call_args.args[1]["error_summary"]
    for secret_value in secret_values:
        assert secret_value not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.parametrize(
    "mode",
    [
        ContentCaptureMode.NO_CONTENT,
        ContentCaptureMode.EVENT_ONLY,
    ],
)
def test_event_sanitizer_redacts_obvious_secrets_in_non_sensitive_string_attrs(mode):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch("iac_code.pipeline.engine.observability.get_content_capture_mode", return_value=mode),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(reason="token=abc123 password: hunter2"),
        )

    attrs = log_event.call_args.args[1]
    assert "reason" in attrs
    assert "abc123" not in attrs["reason"]
    assert "hunter2" not in attrs["reason"]
    assert "[REDACTED]" in attrs["reason"]


def test_event_failure_logging_redacts_obvious_secrets_in_non_sensitive_string_attrs(caplog):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.observability")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.log_event", side_effect=RuntimeError("boom")),
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(reason="token=abc123 password: hunter2 Authorization=Bearer bearer-secret-123"),
        )

    assert "Pipeline telemetry emission failed" in caplog.text
    assert "abc123" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "bearer-secret-123" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_event_failure_logging_does_not_include_raw_sensitive_attrs(caplog):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.observability")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.log_event", side_effect=RuntimeError("boom")),
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(
                candidate_name="customer-specific candidate",
                error_summary="request failed token=abc123 password: hunter2",
                rollback_reason="user said customer budget changed",
                selected_value="ECS + RDS production plan",
                prompt="Choose the customer deployment plan",
            ),
        )

    assert "Pipeline telemetry emission failed" in caplog.text
    assert "customer-specific candidate" not in caplog.text
    assert "abc123" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "user said customer budget changed" not in caplog.text
    assert "ECS + RDS production plan" not in caplog.text
    assert "Choose the customer deployment plan" not in caplog.text
    assert "/repo/customer-a" not in caplog.text
    assert "candidate_name_present" in caplog.text


def test_event_failure_logging_does_not_include_raw_sensitive_attrs_when_event_capture_enabled(caplog):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.observability")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.EVENT_ONLY,
        ),
        patch("iac_code.pipeline.engine.observability.log_event", side_effect=RuntimeError("boom")),
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(
                candidate_name="customer-specific candidate",
                error_summary="request failed token=abc123 password: hunter2",
                selected_value="ECS + RDS production plan",
                prompt="Choose the customer deployment plan",
            ),
        )

    assert "Pipeline telemetry emission failed" in caplog.text
    assert "customer-specific candidate" not in caplog.text
    assert "ECS + RDS production plan" not in caplog.text
    assert "Choose the customer deployment plan" not in caplog.text
    assert "/repo/customer-a" not in caplog.text
    assert "abc123" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "candidate_name_present" in caplog.text


def test_event_failure_logging_does_not_include_raw_exception_message(caplog):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.observability")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.EVENT_ONLY,
        ),
        patch(
            "iac_code.pipeline.engine.observability.log_event",
            side_effect=RuntimeError("customer-specific candidate hunter2"),
        ),
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(
                candidate_name="customer-specific candidate",
                error_summary="request failed password: hunter2",
                prompt="Choose the customer deployment plan",
            ),
        )

    assert "Pipeline telemetry emission failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "customer-specific candidate" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "Choose the customer deployment plan" not in caplog.text


def test_event_content_capture_check_failure_does_not_include_raw_exception_message(caplog):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.observability")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            side_effect=RuntimeError("customer-specific candidate hunter2"),
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs._event(
            Events.PIPELINE_SELECTION_MADE,
            obs.base_attrs(
                candidate_name="customer-specific candidate",
                error_summary="request failed password: hunter2",
            ),
        )

    assert log_event.call_args.args[1]["candidate_name_present"] is True
    assert "Pipeline telemetry event content-capture check failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "customer-specific candidate" not in caplog.text
    assert "hunter2" not in caplog.text


def test_new_metric_attrs_keep_low_cardinality_fields():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    attrs = obs.metric_attrs(
        ui_mode="candidate_selection",
        input_length_bucket="1-50",
        candidate_count_bucket="2-5",
        all_failed=True,
        candidate_name="ECS plan",
        error_summary="unique customer failure",
    )

    assert attrs == {
        "pipeline_name": "selling",
        "ui_mode": "candidate_selection",
        "input_length_bucket": "1-50",
        "candidate_count_bucket": "2-5",
        "all_failed": True,
    }


def test_a2a_correlation_attrs_are_events_only_not_metric_labels():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    obs.set_correlation(task_id="task-1", context_id="ctx-1", pipeline_run_id="ctx-1")

    assert obs.base_attrs()["task_id"] == "task-1"
    assert obs.base_attrs()["context_id"] == "ctx-1"
    assert obs.base_attrs()["pipeline_run_id"] == "ctx-1"
    assert obs.metric_attrs() == {"pipeline_name": "selling"}


def test_user_input_received_records_event_and_wait_metric():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.add_metric") as add_metric,
    ):
        obs.user_input_received(
            step_id="confirm_and_select",
            step_index=4,
            total_steps=5,
            ui_mode="candidate_selection",
            user_input="Plan A",
            wait_duration_ms=1200.0,
        )

    assert log_event.call_args.args[0] == Events.PIPELINE_USER_INPUT_RECEIVED
    attrs = log_event.call_args.args[1]
    assert attrs["input_length_bucket"] == "1-50"
    assert attrs["wait_duration_ms"] == 1200.0
    add_metric.assert_called_once_with(
        Metrics.PIPELINE_USER_INPUT_WAIT_DURATION,
        1200.0,
        {
            "pipeline_name": "selling",
            "step_id": "confirm_and_select",
            "step_index": 4,
            "total_steps": 5,
            "ui_mode": "candidate_selection",
            "input_length_bucket": "1-50",
        },
    )


def test_user_input_required_records_event_with_sanitized_prompt():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs.user_input_required(
            step_id="confirm_and_select",
            step_index=4,
            total_steps=5,
            step_type="selection_step",
            ui_mode="candidate_selection",
            option_count=3,
            prompt="Choose a plan",
        )

    assert log_event.call_args.args[0] == Events.PIPELINE_USER_INPUT_REQUIRED
    attrs = log_event.call_args.args[1]
    assert attrs["step_id"] == "confirm_and_select"
    assert attrs["step_index"] == 4
    assert attrs["total_steps"] == 5
    assert attrs["step_type"] == "selection_step"
    assert attrs["ui_mode"] == "candidate_selection"
    assert attrs["option_count"] == 3
    assert "prompt" not in attrs
    assert attrs["prompt_present"] is True


def test_selection_made_records_event_with_sanitized_selected_value():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
    ):
        obs.selection_made(
            step_id="confirm_and_select",
            ui_mode="candidate_selection",
            option_count=3,
            selected_index=1,
            selected_value="ECS + RDS production plan",
        )

    assert log_event.call_args.args[0] == Events.PIPELINE_SELECTION_MADE
    attrs = log_event.call_args.args[1]
    assert attrs["step_id"] == "confirm_and_select"
    assert attrs["ui_mode"] == "candidate_selection"
    assert attrs["option_count"] == 3
    assert attrs["selected_index"] == 1
    assert "selected_value" not in attrs
    assert attrs["selected_value_present"] is True


def test_candidates_evaluated_records_event_and_count_metrics():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.add_metric") as add_metric,
    ):
        obs.candidates_evaluated(
            parent_step_id="evaluate_candidates",
            sub_pipeline_name="evaluate_candidate",
            candidate_count=3,
            candidate_success_count=2,
            candidate_failed_count=1,
        )

    assert log_event.call_args.args[0] == Events.PIPELINE_CANDIDATES_EVALUATED
    attrs = log_event.call_args.args[1]
    assert attrs["candidate_count"] == 3
    assert attrs["candidate_success_count"] == 2
    assert attrs["candidate_failed_count"] == 1
    assert attrs["all_failed"] is False
    metric_names = [call.args[0] for call in add_metric.call_args_list]
    assert metric_names == [
        Metrics.PIPELINE_CANDIDATE_COUNT,
        Metrics.PIPELINE_CANDIDATE_SUCCESS_COUNT,
        Metrics.PIPELINE_CANDIDATE_FAILED_COUNT,
    ]


def test_funnel_step_records_event_and_count_metric():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.add_metric") as add_metric,
    ):
        obs.funnel_step(
            step_id="deploying",
            step_index=5,
            step_attempt=2,
            total_steps=5,
            status="completed",
            step_type="agent_step",
            ui_mode=None,
            duration_ms=20.0,
        )

    assert log_event.call_args.args[0] == Events.PIPELINE_FUNNEL_STEP
    add_metric.assert_called_once_with(
        Metrics.PIPELINE_FUNNEL_STEP_COUNT,
        1,
        {
            "pipeline_name": "selling",
            "step_id": "deploying",
            "step_index": 5,
            "step_attempt": 2,
            "total_steps": 5,
            "step_type": "agent_step",
            "status": "completed",
        },
    )


def test_step_completed_records_event_and_duration_metric():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.add_metric") as add_metric,
    ):
        obs.step_completed(step_id="architecture", duration_ms=1250.0, step_index=2, total_steps=5)

    log_event.assert_called_once()
    assert log_event.call_args.args[0] == Events.PIPELINE_STEP_COMPLETED
    assert log_event.call_args.args[1]["duration_ms"] == 1250.0
    add_metric.assert_called_once_with(
        Metrics.PIPELINE_STEP_DURATION,
        1250.0,
        {
            "pipeline_name": "selling",
            "step_id": "architecture",
            "step_index": 2,
            "total_steps": 5,
            "status": "completed",
        },
    )


def test_step_failed_keeps_error_id_on_event_but_not_metric():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.add_metric") as add_metric,
    ):
        obs.step_failed(
            step_id="deploying",
            duration_ms=42.0,
            error_summary="failed with DB_PASSWORD=hunter2",
            error_type="StepFailed",
            error_id="err-abc123",
        )

    event_attrs = log_event.call_args.args[1]
    assert event_attrs["error_id"] == "err-abc123"
    assert event_attrs["error_summary_present"] is True
    metric_attrs = add_metric.call_args.args[2]
    assert "error_id" not in metric_attrs


def test_metrics_use_bounded_attrs_while_events_keep_debug_context():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.EVENT_ONLY,
        ),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.add_metric") as add_metric,
    ):
        obs.sub_pipeline_completed(
            duration_ms=42.0,
            failed=True,
            parent_step_id="eval",
            sub_pipeline_name="evaluate_candidate",
            sub_pipeline_id="evaluate_candidate_abc123",
            candidate_index=7,
            candidate_name="customer-specific candidate",
            error_summary="unique customer path failed",
            error_type="RuntimeError",
        )

    event_attrs = log_event.call_args.args[1]
    assert event_attrs["session_id"] == "sid"
    assert event_attrs["cwd"] == "/repo"
    assert event_attrs["sub_pipeline_id"] == "evaluate_candidate_abc123"
    assert event_attrs["candidate_name"] == "customer-specific candidate"
    assert event_attrs["error_summary"] == "unique customer path failed"

    metric_attrs = add_metric.call_args.args[2]
    assert metric_attrs == {
        "pipeline_name": "selling",
        "status": "failed",
        "parent_step_id": "eval",
        "sub_pipeline_name": "evaluate_candidate",
        "candidate_index": 7,
        "error_type": "RuntimeError",
    }


def test_telemetry_failure_is_swallowed(caplog):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with patch("iac_code.pipeline.engine.observability.log_event", side_effect=RuntimeError("boom")):
        obs.pipeline_started(total_steps=1, step_names=["a"])

    assert "Pipeline telemetry emission failed" in caplog.text


def test_safe_span_falls_back_to_nullcontext():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with patch("iac_code.pipeline.engine.observability.start_span", side_effect=RuntimeError("boom")):
        with obs.step_span(step_id="a", step_index=1, total_steps=2):
            marker = "entered"

    assert marker == "entered"


def test_safe_span_falls_back_when_span_enter_fails(caplog):
    class FailingSpan:
        def __enter__(self):
            raise RuntimeError("boom")

        def __exit__(self, exc_type, exc, traceback):
            return False

    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with patch("iac_code.pipeline.engine.observability.start_span", return_value=FailingSpan()):
        with obs.step_span(step_id="a", step_index=1, total_steps=2):
            marker = "entered"

    assert marker == "entered"
    assert "Pipeline telemetry span failed to start" in caplog.text


def test_span_failure_logging_does_not_include_raw_sensitive_attrs(caplog):
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo/customer-a")

    with patch(
        "iac_code.pipeline.engine.observability.start_span",
        side_effect=RuntimeError("customer-specific candidate token=abc123"),
    ):
        with obs.sub_pipeline_span(
            candidate_name="customer-specific candidate",
            error_summary="failed with token=abc123",
        ):
            marker = "entered"

    assert marker == "entered"
    assert "Pipeline telemetry span failed to start" in caplog.text
    assert "customer-specific candidate" not in caplog.text
    assert "abc123" not in caplog.text
    assert "candidate_name_present" in caplog.text


def test_safe_span_swallows_exit_failure(caplog):
    class FailingExitSpan:
        def __enter__(self):
            return "span"

        def __exit__(self, exc_type, exc, traceback):
            raise RuntimeError("exit boom")

    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with patch("iac_code.pipeline.engine.observability.start_span", return_value=FailingExitSpan()):
        with obs.step_span(step_id="a", step_index=1, total_steps=2):
            marker = "entered"

    assert marker == "entered"
    assert "Pipeline telemetry span failed to close" in caplog.text


def test_safe_span_exit_failure_does_not_replace_business_exception(caplog):
    class FailingExitSpan:
        def __enter__(self):
            return "span"

        def __exit__(self, exc_type, exc, traceback):
            raise RuntimeError("exit boom")

    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")

    with (
        patch("iac_code.pipeline.engine.observability.start_span", return_value=FailingExitSpan()),
        pytest.raises(ValueError, match="business boom"),
    ):
        with obs.step_span(step_id="a", step_index=1, total_steps=2):
            raise ValueError("business boom")

    assert "Pipeline telemetry span failed to close" in caplog.text


def test_duration_ms_uses_monotonic_clock():
    start = 10.0
    with patch("iac_code.pipeline.engine.observability.time.monotonic", return_value=11.25):
        assert PipelineObservability.duration_ms(start) == 1250.0


def test_pipeline_user_aborted_emits_terminal_event_and_metric():
    obs = PipelineObservability(pipeline_name="selling", session_id="sid", cwd="/repo")
    obs.pipeline_started_at = 10.0

    with (
        patch(
            "iac_code.pipeline.engine.observability.get_content_capture_mode",
            return_value=ContentCaptureMode.NO_CONTENT,
        ),
        patch("iac_code.pipeline.engine.observability.time.monotonic", return_value=12.5),
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.add_metric") as add_metric,
    ):
        obs.pipeline_user_aborted(total_steps=5, current_step="deploying", reason="ctrl-c")

    log_event.assert_called_once_with(
        Events.PIPELINE_USER_ABORTED,
        {
            "pipeline_name": "selling",
            "session_id": "sid",
            "cwd_present": True,
            "total_steps": 5,
            "current_step": "deploying",
            "reason": "ctrl-c",
            "duration_ms": 2500.0,
        },
    )
    add_metric.assert_called_once_with(
        Metrics.PIPELINE_COMPLETION_TIME,
        2500.0,
        {"pipeline_name": "selling", "status": "user_aborted"},
    )


class _Storage:
    def __init__(self, root: Path):
        self.root = root
        self.meta_entries = []

    def append_meta(self, cwd, session_id, meta):
        self.meta_entries.append(meta)

    def session_dir(self, cwd, session_id):
        return self.root / session_id


@pytest.fixture
def runner(tmp_path: Path) -> PipelineRunner:
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "prompts" / "b.md").write_text("B", encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        dedent(
            """\
            name: test
            context_dependencies:
              a_out: []
              b_out: [a_out]
              evaluated: [a_out]
            max_rollbacks: 3
            steps:
              - id: a
                conclusion_field: a_out
                forward: b
                prompt: prompts/a.md
              - id: b
                conclusion_field: b_out
                forward: null
                prompt: prompts/b.md
            """
        ),
        encoding="utf-8",
    )
    return PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=_Storage(tmp_path / "sessions"),
        session_id="sid",
        cwd=str(tmp_path),
    )


async def _consume_until_pipeline_event(stream, event_type: PipelineEventType, step_id: str | None = None) -> None:
    try:
        while True:
            event = await stream.__anext__()
            if (
                isinstance(event, PipelineEvent)
                and event.type == event_type
                and (step_id is None or event.step_id == step_id)
            ):
                return
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_runner_emits_parent_lifecycle_telemetry(runner):
    calls = []
    runner._observability.pipeline_started = MagicMock(side_effect=lambda **kw: calls.append(("started", kw)))
    runner._observability.step_started = MagicMock(side_effect=lambda **kw: calls.append(("step_started", kw)))
    runner._observability.step_completed = MagicMock(side_effect=lambda **kw: calls.append(("step_completed", kw)))
    runner._observability.pipeline_completed = MagicMock(side_effect=lambda **kw: calls.append(("completed", kw)))

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    async for _event in runner.run("hello"):
        pass

    assert calls[0][0] == "started"
    runner._observability.pipeline_completed.assert_called_once_with(
        total_steps=2,
        failed=False,
        early_exit=False,
    )
    assert [call[1]["step_id"] for call in calls if call[0] == "step_started"] == ["a", "b"]
    assert [call[1]["step_id"] for call in calls if call[0] == "step_completed"] == ["a", "b"]


@pytest.mark.asyncio
async def test_runner_emits_terminal_telemetry_before_final_step_completed_when_stream_closes(runner):
    runner._observability.pipeline_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    await _consume_until_pipeline_event(runner.run("hello"), PipelineEventType.STEP_COMPLETED, step_id="b")

    runner._observability.pipeline_completed.assert_called_once_with(
        total_steps=2,
        failed=False,
        early_exit=False,
    )


@pytest.mark.asyncio
async def test_runner_emits_failed_terminal_telemetry_before_step_failed_when_stream_closes(runner):
    runner._observability.pipeline_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        yield StepResult(step_id=step.step_id, status=StepStatus.FAILED, error="boom")

    runner._step_executor.execute = fake_execute

    await _consume_until_pipeline_event(runner.run("hello"), PipelineEventType.STEP_FAILED, step_id="a")

    runner._observability.pipeline_completed.assert_called_once_with(
        total_steps=2,
        failed=True,
        early_exit=False,
    )


@pytest.mark.asyncio
async def test_runner_emits_failed_terminal_telemetry_when_step_yields_no_result(runner):
    runner._observability.pipeline_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        if False:
            yield

    runner._step_executor.execute = fake_execute

    async for _event in runner.run("hello"):
        pass

    runner._observability.pipeline_completed.assert_called_once_with(
        total_steps=2,
        failed=True,
        early_exit=False,
    )


@pytest.mark.asyncio
async def test_runner_emits_no_result_terminal_telemetry_before_step_failed_when_stream_closes(runner):
    runner._observability.pipeline_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        if False:
            yield

    runner._step_executor.execute = fake_execute

    await _consume_until_pipeline_event(runner.run("hello"), PipelineEventType.STEP_FAILED, step_id="a")

    runner._observability.pipeline_completed.assert_called_once_with(
        total_steps=2,
        failed=True,
        early_exit=False,
    )


@pytest.mark.asyncio
async def test_runner_emits_early_exit_terminal_telemetry_before_step_completed_when_stream_closes(runner):
    runner.state_machine.current_step.exit_condition = {"field": "done", "value": True}
    runner._observability.pipeline_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"done": True}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    await _consume_until_pipeline_event(runner.run("hello"), PipelineEventType.STEP_COMPLETED, step_id="a")

    runner._observability.pipeline_completed.assert_called_once_with(
        total_steps=2,
        failed=False,
        early_exit=True,
    )


def test_mark_user_aborted_emits_user_aborted_terminal_telemetry(runner):
    runner._observability.pipeline_user_aborted = MagicMock()

    runner.mark_user_aborted("ctrl-c")

    runner._observability.pipeline_user_aborted.assert_called_once_with(
        total_steps=2,
        current_step="a",
        reason="ctrl-c",
    )


@pytest.mark.asyncio
async def test_runner_does_not_emit_terminal_telemetry_when_pause_stream_closes_after_input_required(runner):
    runner.state_machine.current_step.auto_advance = False
    runner._observability.pipeline_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"user_prompt": "choose", "options": ["one"]}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    await _consume_until_pipeline_event(runner.run("hello"), PipelineEventType.USER_INPUT_REQUIRED, step_id="a")

    runner._observability.pipeline_completed.assert_not_called()


@pytest.mark.asyncio
async def test_runner_does_not_emit_terminal_telemetry_for_user_input_required_pause(runner):
    runner.state_machine.current_step.auto_advance = False
    runner._observability.pipeline_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"user_prompt": "choose", "options": ["one"]}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    await _consume_until_pipeline_event(runner.run("hello"), PipelineEventType.USER_INPUT_REQUIRED, step_id="a")

    runner._observability.pipeline_completed.assert_not_called()


@pytest.mark.asyncio
async def test_runner_pause_then_resume_emits_terminal_telemetry_once_at_final_completion(runner):
    runner.state_machine.current_step.auto_advance = False
    runner._observability.pipeline_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"user_prompt": "choose", "options": ["one"]} if step.step_id == "a" else {"value": "b"}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute

    await _consume_until_pipeline_event(runner.run("hello"), PipelineEventType.USER_INPUT_REQUIRED, step_id="a")

    runner._observability.pipeline_completed.assert_not_called()

    async for _event in runner.resume("choice"):
        pass

    runner._observability.pipeline_completed.assert_called_once_with(
        total_steps=2,
        failed=False,
        early_exit=False,
    )


@pytest.mark.asyncio
async def test_runner_emits_user_input_required_and_waiting_funnel_telemetry(runner):
    runner.state_machine.current_step.auto_advance = False
    runner.state_machine.current_step.step_type = "agent_step"
    runner.state_machine.current_step.ui_mode = "candidate_selection"
    runner._observability.user_input_required = MagicMock()
    runner._observability.funnel_step = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"user_prompt": "choose", "options": [{"name": "Plan A"}, {"name": "Plan B"}]}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute
    runner._observability.now = MagicMock(return_value=50.0)
    runner._observability.duration_ms = MagicMock(return_value=0.0)

    await _consume_until_pipeline_event(runner.run("hello"), PipelineEventType.USER_INPUT_REQUIRED, step_id="a")

    runner._observability.user_input_required.assert_called_once_with(
        step_id="a",
        step_index=1,
        step_attempt=1,
        total_steps=2,
        step_type="agent_step",
        ui_mode="candidate_selection",
        option_count=2,
        prompt="choose",
    )
    funnel_statuses = [call.kwargs["status"] for call in runner._observability.funnel_step.call_args_list]
    assert funnel_statuses == ["waiting_input"]
    runner._observability.funnel_step.assert_called_once_with(
        step_id="a",
        step_index=1,
        step_attempt=1,
        total_steps=2,
        status="waiting_input",
        step_type="agent_step",
        ui_mode="candidate_selection",
        duration_ms=0.0,
    )
    assert runner._waiting_input_started_at["a"] == 50.0
    assert runner._waiting_input_options_by_step["a"] == [{"name": "Plan A"}, {"name": "Plan B"}]


@pytest.mark.asyncio
async def test_runner_emits_completed_and_failed_funnel_step_telemetry(runner):
    runner._observability.funnel_step = MagicMock()

    async def fake_success(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_success
    async for _event in runner.run("hello"):
        pass

    completed_statuses = [call.kwargs["status"] for call in runner._observability.funnel_step.call_args_list]
    assert completed_statuses == ["completed", "completed"]

    failed_runner = runner.__class__(
        pipeline_dir=runner._pipeline_dir,
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        session_storage=_Storage(Path(runner._cwd) / "sessions2"),
        session_id="sid2",
        cwd=runner._cwd,
    )
    failed_runner._observability.funnel_step = MagicMock()

    async def fake_failure(step, context, session_id, user_message=None, **kwargs):
        yield StepResult(step_id=step.step_id, status=StepStatus.FAILED, error="boom")

    failed_runner._step_executor.execute = fake_failure
    async for _event in failed_runner.run("hello"):
        pass

    failed_runner._observability.funnel_step.assert_called_once()
    assert failed_runner._observability.funnel_step.call_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_runner_resume_emits_user_input_received_and_selection_telemetry(runner):
    runner.state_machine.current_step.auto_advance = False
    runner.state_machine.current_step.ui_mode = "candidate_selection"
    runner._waiting_input_started_at["a"] = 10.0
    runner._waiting_input_options_by_step["a"] = [{"name": "Plan A"}, {"name": "Plan B"}]
    runner._observability.duration_ms = MagicMock(return_value=2500.0)
    runner._observability.user_input_received = MagicMock()
    runner._observability.selection_made = MagicMock()

    async def fake_continue(user_input=None, **kwargs):
        assert kwargs == {"resume_waiting_step": True}
        if False:
            yield

    runner._continue_from_current = fake_continue

    async for _event in runner.resume("Plan B"):
        pass

    runner._observability.user_input_received.assert_called_once_with(
        step_id="a",
        step_index=1,
        step_attempt=1,
        total_steps=2,
        ui_mode="candidate_selection",
        user_input="Plan B",
        wait_duration_ms=2500.0,
    )
    runner._observability.selection_made.assert_called_once_with(
        step_id="a",
        step_attempt=1,
        ui_mode="candidate_selection",
        option_count=2,
        selected_index=1,
        selected_value="Plan B",
    )
    assert "a" not in runner._waiting_input_started_at
    assert "a" not in runner._waiting_input_options_by_step


@pytest.mark.asyncio
async def test_runner_does_not_emit_selection_made_for_unmatched_or_ambiguous_candidate_selection(runner):
    runner.state_machine.current_step.auto_advance = False
    runner.state_machine.current_step.ui_mode = "candidate_selection"
    runner._waiting_input_started_at["a"] = 10.0
    runner._observability.user_input_received = MagicMock()
    runner._observability.selection_made = MagicMock()

    async def fake_continue(user_input=None, **kwargs):
        assert kwargs == {"resume_waiting_step": True}
        if False:
            yield

    runner._continue_from_current = fake_continue

    runner._waiting_input_options_by_step["a"] = [{"name": "Plan A"}, {"name": "Plan B"}]
    async for _event in runner.resume("Plan C"):
        pass

    runner.state_machine._current_index = 0
    runner.state_machine.current_step.ui_mode = "candidate_selection"
    runner._waiting_input_started_at["a"] = 20.0
    runner._waiting_input_options_by_step["a"] = [{"name": "Plan B"}, {"name": "Plan B"}]
    async for _event in runner.resume("Plan B"):
        pass

    runner._observability.selection_made.assert_not_called()


def test_runner_infers_selected_index_only_for_unique_option_names(runner):
    assert runner._infer_selected_index("Plan B", [{"name": "Plan A"}, {"name": "Plan B"}]) == 1
    assert runner._infer_selected_index("Plan B", [{"name": "Plan B"}, {"name": "Plan B"}]) is None
    assert runner._infer_selected_index("Plan B", [{"summary": "Plan B"}]) is None


@pytest.mark.asyncio
async def test_runner_step_span_closes_before_step_completed_boundary_event(runner):
    order = []

    class RecordingSpan:
        def __enter__(self):
            order.append("span_enter")

        def __exit__(self, exc_type, exc, traceback):
            order.append("span_exit")
            return False

    runner._observability.step_span = MagicMock(return_value=RecordingSpan())

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    runner._step_executor.execute = fake_execute
    stream = runner.run("hello")
    try:
        while True:
            event = await stream.__anext__()
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_COMPLETED:
                order.append("step_completed_event")
                break
    finally:
        await stream.aclose()

    assert order == ["span_enter", "span_exit", "step_completed_event"]


def test_restore_from_sidecar_emits_resumed_telemetry(runner):
    runner._observability.pipeline_resumed = MagicMock()
    runner.session.restore_sync = MagicMock(
        return_value=RestoreResult(
            ok=True,
            status="running",
            state_machine_snapshot=runner.state_machine.to_snapshot(),
            context_snapshot=runner.context.to_snapshot(),
            current_step="a",
        )
    )

    result = runner.restore_from_sidecar_sync()

    assert result.ok is True
    runner._observability.pipeline_resumed.assert_called_once_with(status="running", current_step="a")


def test_restore_from_sidecar_emits_sidecar_failed_for_real_restore_failure(runner):
    runner._observability.sidecar_failed = MagicMock()
    runner.session.restore_sync = MagicMock(
        return_value=RestoreResult(ok=False, status="running", reason="corrupt_meta")
    )

    result = runner.restore_from_sidecar_sync()

    assert result.ok is False
    runner._observability.sidecar_failed.assert_called_once_with(
        operation="restore",
        status="running",
        reason="corrupt_meta",
        error_type="RestoreFailed",
        error_summary="corrupt_meta",
        error_id=ANY,
    )


@pytest.mark.asyncio
async def test_runner_emits_parent_rollback_telemetry(runner):
    runner._observability.rollback = MagicMock()
    runner.state_machine.advance()
    runner.state_machine.current_step.rollback_rules.append(RollbackRule(target_step="a", condition="revise"))

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"value": step.step_id}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(
            step_id=step.step_id,
            status=StepStatus.COMPLETED,
            conclusion=conclusion,
            rollback_request=("a", "revise"),
        )

    runner._step_executor.execute = fake_execute

    stream = runner._continue_from_current()
    try:
        async for event in stream:
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.ROLLBACK_TRIGGERED:
                break
    finally:
        await stream.aclose()

    runner._observability.rollback.assert_called_once_with(
        from_step="b",
        to_step="a",
        rollback_reason="revise",
        rollback_scope="parent",
        stale_fields=["b_out"],
    )


@pytest.mark.asyncio
async def test_runner_distinguishes_step_attempts_after_parent_rollback(runner):
    runner._observability.step_started = MagicMock()
    runner._observability.step_span = MagicMock(return_value=nullcontext())
    runner._observability.step_completed = MagicMock()
    runner._observability.funnel_step = MagicMock()
    runner._observability.rollback = MagicMock()
    runner.state_machine._steps["b"].rollback_rules.append(RollbackRule(target_step="a", condition="revise"))
    seen: dict[str, int] = {"a": 0, "b": 0}

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        seen[step.step_id] += 1
        conclusion = {"value": f"{step.step_id}-{seen[step.step_id]}"}
        context.set_conclusion(step.conclusion_field, conclusion)
        rollback_request = ("a", "revise") if step.step_id == "b" and seen["b"] == 1 else None
        yield StepResult(
            step_id=step.step_id,
            status=StepStatus.COMPLETED,
            conclusion=conclusion,
            rollback_request=rollback_request,
        )

    runner._step_executor.execute = fake_execute

    async for _event in runner.run("hello"):
        pass

    assert [call.kwargs["step_id"] for call in runner._observability.step_started.call_args_list] == [
        "a",
        "b",
        "a",
        "b",
    ]
    assert [call.kwargs["step_attempt"] for call in runner._observability.step_started.call_args_list] == [
        1,
        1,
        2,
        2,
    ]
    assert [call.kwargs["step_attempt"] for call in runner._observability.step_completed.call_args_list] == [
        1,
        1,
        2,
        2,
    ]
    assert [call.kwargs["step_attempt"] for call in runner._observability.funnel_step.call_args_list] == [
        1,
        1,
        2,
        2,
    ]
    assert [call.kwargs["step_attempt"] for call in runner._observability.step_span.call_args_list] == [
        1,
        1,
        2,
        2,
    ]


def test_runner_emits_parent_hard_interrupt_telemetry(runner):
    runner._observability.hard_interrupt = MagicMock()
    runner.state_machine.advance()

    result = runner.apply_hard_interrupt(
        InterruptVerdict(action="hard_interrupt", reason="changed", rollback_target="a")
    )

    assert result is True
    runner._observability.hard_interrupt.assert_called_once_with(
        rollback_scope="parent",
        from_step="b",
        to_step="a",
        candidate_scope=None,
        rollback_reason="changed",
    )


def test_runner_emits_invalid_target_fallback_hard_interrupt_telemetry(runner):
    runner._observability.hard_interrupt = MagicMock()
    runner.state_machine.advance()

    result = runner.apply_hard_interrupt(
        InterruptVerdict(action="hard_interrupt", reason="changed", rollback_target="missing")
    )

    fallback_reason = "invalid rollback target 'missing'; falling back to current step 'b': changed"
    assert result is True
    runner._observability.hard_interrupt.assert_called_once_with(
        rollback_scope="parent",
        from_step="b",
        to_step="b",
        original_target="missing",
        fallback_target="b",
        validation_error="unknown_step",
        candidate_scope=None,
        rollback_reason=fallback_reason,
    )


def test_runner_emits_candidate_hard_interrupt_telemetry(runner):
    runner._observability.hard_interrupt = MagicMock()
    runner.state_machine.advance()
    runner._parallel_candidates_total = 1
    runner.state_machine.current_step.step_type = "parallel_sub_pipeline"
    runner.state_machine.current_step.sub_pipeline_name = "evaluate_candidate"
    runner._loaded.sub_pipelines["evaluate_candidate"] = SubPipelineSpec(
        name="evaluate_candidate",
        steps=[
            StepSpec(
                step_id="template_gen",
                conclusion_field="template",
                forward=None,
                prompt_file="prompts/template.md",
            )
        ],
        max_rollbacks=2,
        iterate_over="a_out.candidates",
    )
    task = MagicMock()
    task.done.return_value = False
    runner._active_candidates[0] = {"task": task, "conclusions": {}, "current_sub_step": "template_gen"}

    result = runner.apply_hard_interrupt(
        InterruptVerdict(
            action="hard_interrupt",
            reason="fix candidate",
            rollback_target="template_gen",
            candidate_scope="candidate:0",
        )
    )

    assert result is False
    runner._observability.hard_interrupt.assert_called_once_with(
        rollback_scope="candidate",
        from_step="b",
        to_step="template_gen",
        candidate_scope="candidate:0",
        rollback_reason="fix candidate",
    )


@pytest.mark.asyncio
async def test_parallel_step_emits_candidate_aggregate_telemetry(runner):
    runner.state_machine.current_step.step_type = "parallel_sub_pipeline"
    runner.state_machine.current_step.sub_pipeline_name = "evaluate_candidate"
    runner.state_machine.current_step.conclusion_field = "evaluated"
    runner._loaded.sub_pipelines["evaluate_candidate"] = SubPipelineSpec(
        name="evaluate_candidate",
        steps=[
            StepSpec(
                step_id="template_gen",
                conclusion_field="template",
                forward=None,
                prompt_file="prompts/template.md",
            )
        ],
        max_rollbacks=2,
        iterate_over="a_out.candidates",
    )
    runner.context.set_conclusion("a_out", {"candidates": [{"name": "A"}, {"name": "B"}]})
    runner._observability.candidates_evaluated = MagicMock()

    async def fake_execute_streaming(self, **kwargs):
        idx = kwargs["candidate_index"]
        yield PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1.0,
            data={"sub_pipeline_id": f"sub-{idx}", "candidate_index": idx},
        )
        yield PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_COMPLETED,
            step_id=None,
            timestamp=2.0,
            data={
                "sub_pipeline_id": f"sub-{idx}",
                "candidate_index": idx,
                "failed": idx == 1,
                "conclusions": {"template": {"ok": True}},
            },
        )

    with patch(
        "iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor.execute_streaming",
        fake_execute_streaming,
    ):
        async for _event in runner._execute_parallel_sub_pipeline(runner.state_machine.current_step):
            pass

    runner._observability.candidates_evaluated.assert_called_once_with(
        parent_step_id="a",
        sub_pipeline_name="evaluate_candidate",
        candidate_count=2,
        candidate_success_count=1,
        candidate_failed_count=1,
    )


@pytest.mark.asyncio
async def test_sub_pipeline_executor_emits_sub_pipeline_and_sub_step_telemetry(tmp_path):
    from iac_code.pipeline.engine.context import PipelineContext
    from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor

    step = StepSpec(step_id="sub_a", conclusion_field="sub_out", forward=None, prompt_file="")
    sub_spec = SubPipelineSpec(
        name="sub",
        steps=[step],
        context_fields_from_parent=[],
        iterate_over="items",
        max_rollbacks=1,
    )
    pipeline = LoadedPipeline(
        name="test",
        steps=[],
        context_dependencies={},
        max_rollbacks=1,
        skills={},
        sub_pipelines={"sub": sub_spec},
    )
    executor = SubPipelineExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        pipeline=pipeline,
        pipeline_dir=tmp_path,
        session_storage=MagicMock(),
        cwd=str(tmp_path),
    )
    order: list[str] = []

    class RecordingSpan:
        def __enter__(self):
            order.append("sub_pipeline_span_enter")

        def __exit__(self, exc_type, exc, traceback):
            order.append("sub_pipeline_span_exit")
            return False

    executor._observability.now = MagicMock(return_value=100.0)
    executor._observability.duration_ms = MagicMock(return_value=12.5)
    executor._observability.sub_pipeline_span = MagicMock(return_value=RecordingSpan())
    executor._observability.sub_pipeline_started = MagicMock()
    executor._observability.sub_pipeline_completed = MagicMock()
    executor._observability.sub_step_started = MagicMock()
    executor._observability.sub_step_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        conclusion = {"ok": True}
        context.set_conclusion(step.conclusion_field, conclusion)
        yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

    with patch.object(executor, "_make_step_executor") as make_step_executor:
        step_executor = MagicMock()
        step_executor.execute = fake_execute
        make_step_executor.return_value = step_executor

        async for event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "c1"},
            candidate_index=0,
            parent_context=PipelineContext({"items": []}),
            session_id="sid",
            parent_step_id="parent_eval",
        ):
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED:
                order.append("terminal_event")

    assert executor._observability.session_id == "sid"
    executor._observability.sub_pipeline_started.assert_called_once()
    executor._observability.sub_pipeline_completed.assert_called_once()
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["duration_ms"] == 12.5
    assert executor._observability.sub_pipeline_started.call_args.kwargs["parent_step_id"] == "parent_eval"
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["parent_step_id"] == "parent_eval"
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["candidate_index"] == 0
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["candidate_name"] == "c1"
    executor._observability.sub_step_started.assert_called_once()
    executor._observability.sub_step_completed.assert_called_once()
    assert executor._observability.sub_step_started.call_args.kwargs["parent_step_id"] == "parent_eval"
    assert executor._observability.sub_step_completed.call_args.kwargs["parent_step_id"] == "parent_eval"
    assert executor._observability.sub_step_completed.call_args.kwargs["duration_ms"] == 12.5
    assert order == ["sub_pipeline_span_enter", "sub_pipeline_span_exit", "terminal_event"]


@pytest.mark.asyncio
async def test_parallel_sub_pipeline_events_inherit_a2a_telemetry_correlation(tmp_path):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "parent.md").write_text("parent", encoding="utf-8")
    (tmp_path / "prompts" / "sub.md").write_text("sub", encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        dedent(
            """\
            name: test
            context_dependencies:
              candidates: []
              evaluated: [candidates]
            max_rollbacks: 1
            sub_pipelines:
              sub:
                iterate_over: candidates
                max_rollbacks: 1
                context_fields_from_parent: []
                steps:
                  - id: sub_a
                    conclusion_field: sub_out
                    forward: null
                    prompt: prompts/sub.md
            steps:
              - id: evaluate
                type: parallel_sub_pipeline
                sub_pipeline: sub
                conclusion_field: evaluated
                forward: null
                prompt: prompts/parent.md
            """
        ),
        encoding="utf-8",
    )
    from iac_code.pipeline.engine.step_executor import StepExecutor

    with (
        patch.object(StepExecutor, "set_telemetry_correlation", autospec=True) as set_step_correlation,
        patch("iac_code.pipeline.engine.observability.log_event") as log_event,
        patch("iac_code.pipeline.engine.observability.add_metric") as add_metric,
    ):
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=_Storage(tmp_path / "sessions"),
            session_id="sid",
            cwd=str(tmp_path),
        )
        runner.context.set_conclusion("candidates", [{"name": "Plan A"}])
        runner.set_telemetry_correlation(task_id="task-1", context_id="ctx-1", pipeline_run_id="run-1")
        step = runner.state_machine.current_step

        async def fake_execute(self, step, context, session_id, **kwargs):
            yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"ok": True})

        with patch.object(StepExecutor, "execute", fake_execute):
            async for _event in runner._execute_parallel_sub_pipeline(step):
                pass

    assert set_step_correlation.call_args_list[-1].kwargs == {
        "task_id": "task-1",
        "context_id": "ctx-1",
        "pipeline_run_id": "run-1",
    }
    sub_event_attrs = [
        call.args[1] for call in log_event.call_args_list if call.args[0] == Events.PIPELINE_SUB_PIPELINE_STARTED
    ][0]
    assert sub_event_attrs["task_id"] == "task-1"
    assert sub_event_attrs["context_id"] == "ctx-1"
    assert sub_event_attrs["pipeline_run_id"] == "run-1"
    for metric_call in add_metric.call_args_list:
        attrs = metric_call.args[2]
        assert "task_id" not in attrs
        assert "context_id" not in attrs
        assert "pipeline_run_id" not in attrs


@pytest.mark.asyncio
async def test_sub_pipeline_executor_emits_failed_telemetry_and_structured_log(tmp_path, caplog):
    from iac_code.pipeline.engine.context import PipelineContext
    from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor

    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.sub_pipeline_executor")

    step = StepSpec(step_id="sub_a", conclusion_field="sub_out", forward=None, prompt_file="")
    sub_spec = SubPipelineSpec(
        name="sub",
        steps=[step],
        context_fields_from_parent=[],
        iterate_over="items",
        max_rollbacks=1,
    )
    pipeline = LoadedPipeline(
        name="test",
        steps=[],
        context_dependencies={},
        max_rollbacks=1,
        skills={},
        sub_pipelines={"sub": sub_spec},
    )
    executor = SubPipelineExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=MagicMock(),
        pipeline=pipeline,
        pipeline_dir=tmp_path,
        session_storage=MagicMock(),
        cwd=str(tmp_path),
    )
    executor._observability.now = MagicMock(return_value=100.0)
    executor._observability.duration_ms = MagicMock(return_value=8.0)
    executor._observability.sub_pipeline_started = MagicMock()
    executor._observability.sub_pipeline_completed = MagicMock()
    executor._observability.sub_step_started = MagicMock()
    executor._observability.sub_step_completed = MagicMock()

    async def fake_execute(step, context, session_id, user_message=None, **kwargs):
        if False:
            yield

    with patch.object(executor, "_make_step_executor") as make_step_executor:
        step_executor = MagicMock()
        step_executor.execute = fake_execute
        make_step_executor.return_value = step_executor

        async for _event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "c1"},
            candidate_index=0,
            parent_context=PipelineContext({"items": []}),
            session_id="sid",
            parent_step_id="parent_eval",
        ):
            pass

    executor._observability.sub_step_completed.assert_called_once()
    assert executor._observability.sub_step_completed.call_args.kwargs["parent_step_id"] == "parent_eval"
    assert executor._observability.sub_step_completed.call_args.kwargs["failed"] is True
    assert executor._observability.sub_step_completed.call_args.kwargs["error_summary"] == "No result"
    assert executor._observability.sub_step_completed.call_args.kwargs["error_type"] == "StepFailed"
    error_id = executor._observability.sub_step_completed.call_args.kwargs["error_id"]
    assert error_id
    executor._observability.sub_pipeline_completed.assert_called_once()
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["parent_step_id"] == "parent_eval"
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["failed"] is True
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["error_summary"] == "No result"
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["error_type"] == "StepFailed"
    assert executor._observability.sub_pipeline_completed.call_args.kwargs["error_id"] == error_id

    record = next(record for record in caplog.records if record.message.startswith("Sub-pipeline failed:"))
    assert "pipeline=test" in record.message
    assert "session_id=sid" in record.message
    assert "parent_step_id=parent_eval" in record.message
    assert "sub_pipeline_name=sub" in record.message
    assert "candidate_index=0" in record.message
    assert "candidate_name=c1" in record.message
    assert "error_type=StepFailed" in record.message
    assert "error_summary=No result" in record.message
    assert record.pipeline == "test"
    assert record.session_id == "sid"
    assert record.parent_step_id == "parent_eval"
    assert record.sub_pipeline_name == "sub"
    assert record.candidate_index == 0
    assert record.candidate_name == "c1"
    assert record.error_summary == "No result"
    assert record.error_type == "StepFailed"


def test_cancel_active_candidates_emits_cancelled_telemetry_and_log(runner, caplog):
    caplog.set_level(logging.INFO, logger="iac_code.pipeline.engine.pipeline_runner")
    task = MagicMock()
    task.done.return_value = False
    task.cancel = MagicMock()
    runner._active_candidates = {
        0: {"task": task, "name": "c1"},
    }
    runner._observability.candidate_cancelled = MagicMock()

    cancelled = runner._cancel_active_candidates(reason="hard_interrupt_parent_rollback")

    assert cancelled == [task]
    task.cancel.assert_called_once_with()
    runner._observability.candidate_cancelled.assert_called_once_with(
        parent_step_id="a",
        candidate_index=0,
        candidate_name="c1",
        reason="hard_interrupt_parent_rollback",
    )
    record = next(record for record in caplog.records if record.message.startswith("Pipeline candidate cancelled:"))
    assert "pipeline=test" in record.message
    assert "session_id=sid" in record.message
    assert "parent_step_id=a" in record.message
    assert "candidate_index=0" in record.message
    assert "candidate_name=c1" in record.message
    assert "reason=hard_interrupt_parent_rollback" in record.message
    assert record.pipeline == "test"
    assert record.session_id == "sid"
    assert record.parent_step_id == "a"
    assert record.candidate_index == 0
    assert record.candidate_name == "c1"
    assert record.reason == "hard_interrupt_parent_rollback"


def test_parent_hard_interrupt_cancels_candidates_with_reason(runner):
    runner.state_machine.advance()
    runner._cancel_active_candidates = MagicMock(return_value=[])

    result = runner.apply_hard_interrupt(
        InterruptVerdict(action="hard_interrupt", reason="changed", rollback_target="a")
    )

    assert result is True
    runner._cancel_active_candidates.assert_called_once_with(reason="hard_interrupt_parent_rollback")


def test_candidate_restart_cancellation_emits_cancelled_telemetry_and_log(runner, caplog):
    caplog.set_level(logging.INFO, logger="iac_code.pipeline.engine.pipeline_runner")
    runner.state_machine.advance()
    runner._parallel_candidates_total = 1
    runner.state_machine.current_step.step_type = "parallel_sub_pipeline"
    runner.state_machine.current_step.sub_pipeline_name = "evaluate_candidate"
    runner._loaded.sub_pipelines["evaluate_candidate"] = SubPipelineSpec(
        name="evaluate_candidate",
        steps=[
            StepSpec(
                step_id="template_gen",
                conclusion_field="template",
                forward=None,
                prompt_file="prompts/template.md",
            )
        ],
        max_rollbacks=2,
        iterate_over="a_out.candidates",
    )
    task = MagicMock()
    task.done.return_value = False
    task.cancel = MagicMock()
    runner._active_candidates[0] = {
        "task": task,
        "conclusions": {},
        "name": "Plan A",
        "current_sub_step": "template_gen",
    }
    runner._observability.candidate_cancelled = MagicMock()

    result = runner.apply_hard_interrupt(
        InterruptVerdict(
            action="hard_interrupt",
            reason="fix candidate",
            rollback_target="template_gen",
            candidate_scope="candidate:0",
        )
    )

    assert result is False
    task.cancel.assert_called_once_with()
    runner._observability.candidate_cancelled.assert_called_once_with(
        parent_step_id="b",
        candidate_index=0,
        candidate_name="Plan A",
        reason="candidate_restart",
    )
    record = next(record for record in caplog.records if record.message.startswith("Pipeline candidate cancelled:"))
    assert "pipeline=test" in record.message
    assert "session_id=sid" in record.message
    assert "parent_step_id=b" in record.message
    assert "candidate_index=0" in record.message
    assert "candidate_name=Plan A" in record.message
    assert "reason=candidate_restart" in record.message
    assert record.pipeline == "test"
    assert record.session_id == "sid"
    assert record.parent_step_id == "b"
    assert record.candidate_index == 0
    assert record.candidate_name == "Plan A"
    assert record.reason == "candidate_restart"
