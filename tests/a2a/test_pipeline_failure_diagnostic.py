from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from iac_code.a2a.pipeline_continuation import PipelineContinuationUnsafeError
from iac_code.a2a.pipeline_failure_diagnostic import PipelineFailureDiagnostic

_TASK_ID = "task-9f73b3e97d29"
_CONTEXT_ID = "ae022c2ddc714cbcbda444e41fa6670f"
_SECRET = "fake-token-never-log-this"


@pytest.mark.parametrize(
    "reason",
    [
        "active_attempt_outcome_unverified",
        "successor_execution_outcome_unverified",
        "successor_checkpoint_fence_changed",
    ],
)
def test_pipeline_failure_diagnostic_projects_only_exact_guard_reason(reason: str) -> None:
    try:
        raise PipelineContinuationUnsafeError(reason)
    except PipelineContinuationUnsafeError as caught:
        diagnostic = PipelineFailureDiagnostic.from_exception(caught, task_id=_TASK_ID, context_id=_CONTEXT_ID)
    assert diagnostic.exception_type == "PipelineContinuationUnsafeError"
    assert diagnostic.guard_reason == reason
    assert diagnostic.source_module == "UNKNOWN"
    assert diagnostic.source_line == 0


@pytest.mark.parametrize("reason", [_SECRET, f"active_attempt_outcome_unverified {_SECRET}", [], {}])
def test_pipeline_failure_diagnostic_rejects_arbitrary_guard_text_or_wrong_type(reason: object) -> None:
    exception = PipelineContinuationUnsafeError("active_attempt_outcome_unverified")
    exception.args = (reason,)
    diagnostic = PipelineFailureDiagnostic.from_exception(exception, task_id=_TASK_ID, context_id=_CONTEXT_ID)
    assert diagnostic.guard_reason == "UNKNOWN"
    assert _SECRET not in json.dumps(asdict(diagnostic))


def test_pipeline_failure_diagnostic_does_not_coerce_unknown_exception_or_ids() -> None:
    class UntrustedError(ValueError):
        def __str__(self) -> str:
            pytest.fail("diagnostics coerced an arbitrary exception")

    class UntrustedText(str):
        def __str__(self) -> str:
            pytest.fail("diagnostics coerced an arbitrary ID")

    exception = UntrustedError(_SECRET)
    diagnostic = PipelineFailureDiagnostic.from_exception(
        exception, task_id=UntrustedText(_TASK_ID), context_id=UntrustedText(_CONTEXT_ID)
    )
    assert asdict(diagnostic) == {
        "exception_type": "UNKNOWN",
        "guard_reason": "UNKNOWN",
        "source_module": "UNKNOWN",
        "source_function": "UNKNOWN",
        "source_line": 0,
        "task_id": "UNKNOWN",
        "context_id": "UNKNOWN",
    }


@pytest.mark.parametrize(
    "value",
    [_SECRET, f"{_TASK_ID}\n{_SECRET}", "a" * 10000, None, [], {}],
    ids=["secret", "newline", "overlong", "none", "list", "dict"],
)
def test_pipeline_failure_diagnostic_rejects_unsafe_identifiers(value: object) -> None:
    diagnostic = PipelineFailureDiagnostic.from_exception(ValueError(_SECRET), task_id=value, context_id=value)
    assert diagnostic.task_id == "UNKNOWN"
    assert diagnostic.context_id == "UNKNOWN"
    assert _SECRET not in json.dumps(asdict(diagnostic))


def test_pipeline_failure_diagnostic_identifies_actual_factory_raise_without_pipeline_name(monkeypatch) -> None:
    import iac_code.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "discover_pipelines", lambda: {})
    with pytest.raises(ValueError) as caught:
        pipeline_module.create_pipeline(
            _SECRET, provider_manager=None, base_tool_registry=None, session_storage=None, session_id=_CONTEXT_ID
        )
    diagnostic = PipelineFailureDiagnostic.from_exception(caught.value, task_id=_TASK_ID, context_id=_CONTEXT_ID)
    assert diagnostic.exception_type == "ValueError"
    assert diagnostic.source_module == "pipeline_factory"
    assert diagnostic.source_function == "create_pipeline"
    assert diagnostic.source_line > 0
    assert _SECRET not in json.dumps(asdict(diagnostic))


@pytest.mark.parametrize("spoof_module", ["private.fake-token-never-log-this", "iac_code.a2a.pipeline_continuation"])
def test_pipeline_failure_diagnostic_rejects_dynamic_frame_names_and_copied_module(spoof_module: str) -> None:
    function = "secret_fake_token_never_log_this" if spoof_module.startswith("private.") else "_load_unlocked"
    namespace = {"__name__": spoof_module}
    code = compile(
        f"def {function}():\n    raise ValueError('fake-token-never-log-this')\n{function}()",
        f"/private/{_SECRET}/credentials.py",
        "exec",
    )
    with pytest.raises(ValueError) as caught:
        exec(code, namespace)
    diagnostic = PipelineFailureDiagnostic.from_exception(caught.value, task_id=_TASK_ID, context_id=_CONTEXT_ID)
    assert diagnostic.source_module == "UNKNOWN"
    assert diagnostic.source_function == "UNKNOWN"
    assert diagnostic.source_line == 0
    assert _SECRET not in json.dumps(asdict(diagnostic))


def test_pipeline_failure_diagnostic_does_not_inspect_or_retain_secret_cause_chain() -> None:
    cause = ValueError(_SECRET)
    cause.__cause__ = cause
    exception = RuntimeError(_SECRET)
    exception.__cause__ = cause
    diagnostic = PipelineFailureDiagnostic.from_exception(exception, task_id=_TASK_ID, context_id=_CONTEXT_ID)
    assert diagnostic.exception_type == "RuntimeError"
    assert _SECRET not in json.dumps(asdict(diagnostic))


def test_pipeline_failure_diagnostic_keeps_known_caller_for_unknown_provider_failure(monkeypatch) -> None:
    import iac_code.pipeline as pipeline_module

    class UnknownProviderError(Exception):
        def __str__(self) -> str:
            pytest.fail("diagnostics formatted unknown provider failure")

        @property
        def __traceback__(self):
            pytest.fail("diagnostics invoked unknown exception property")

    def unavailable_registry():
        raise UnknownProviderError(_SECRET)

    monkeypatch.setattr(pipeline_module, "discover_pipelines", unavailable_registry)
    with pytest.raises(UnknownProviderError) as caught:
        pipeline_module.create_pipeline(
            _SECRET, provider_manager=None, base_tool_registry=None, session_storage=None, session_id=_CONTEXT_ID
        )
    diagnostic = PipelineFailureDiagnostic.from_exception(caught.value, task_id=_TASK_ID, context_id=_CONTEXT_ID)
    assert diagnostic.exception_type == "UNKNOWN"
    assert diagnostic.source_module == "pipeline_factory"
    assert diagnostic.source_function == "create_pipeline"
    assert diagnostic.source_line > 0
    assert _SECRET not in json.dumps(asdict(diagnostic))


def test_pipeline_failure_diagnostic_recognizes_loaded_engine_persistence_failure() -> None:
    from iac_code.pipeline.engine.pipeline_runner import PipelineStatePersistenceError

    diagnostic = PipelineFailureDiagnostic.from_exception(
        PipelineStatePersistenceError(_SECRET, step_id=_SECRET), task_id=_TASK_ID, context_id=_CONTEXT_ID
    )
    assert diagnostic.exception_type == "PipelineStatePersistenceError"
    assert _SECRET not in json.dumps(asdict(diagnostic))
