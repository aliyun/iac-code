from __future__ import annotations

from scripts.observability.local_observe.e2e_audit import audit_provider_attempts
from scripts.observability.local_observe.records import new_record


def _attempt(span_id: str, *, status: str = "ok", terminal_count: int = 1) -> list[dict]:
    terminal_name = "iac.api.request.succeeded" if status == "ok" else "iac.api.request.failed"
    records = [
        new_record(
            "log",
            name="iac.api.request.started",
            span_id=span_id,
            attributes={"provider": "openai", "model": "fixture-model"},
        ),
        new_record(
            "span",
            name="chat fixture-model",
            span_id=span_id,
            attributes={"gen_ai.provider.name": "openai", "iac_code.mode": "normal"},
        ),
    ]
    for _ in range(terminal_count):
        records.append(
            new_record(
                "log",
                name=terminal_name,
                span_id=span_id,
                attributes={"provider": "openai", "model": "fixture-model", "status": status},
            )
        )
    return records


def _metrics(*, count: int = 1, timestamp: int = 1) -> list[dict]:
    return [
        new_record(
            "metric",
            name="iac.api.request.count",
            timestamp_unix_nano=timestamp,
            attributes={"provider": "openai", "model": "fixture-model", "status": "ok"},
            value=count,
        ),
        new_record(
            "metric",
            name="iac.api.request.duration",
            timestamp_unix_nano=timestamp,
            attributes={"provider": "openai", "model": "fixture-model"},
            value=10,
        ),
    ]


def test_audit_provider_attempts_accepts_one_closed_attempt() -> None:
    result = audit_provider_attempts(
        [*_attempt("aa"), *_metrics()],
        expected_attempts=1,
        expected_provider="openai",
        expected_model="fixture-model",
        expected_span_attributes={"iac_code.mode": "normal"},
    )

    assert result["passed"] is True


def test_audit_provider_attempts_does_not_let_duplicate_cancel_missing_terminal() -> None:
    records = [*_attempt("duplicate", terminal_count=2), *_attempt("missing", terminal_count=0), *_metrics(count=2)]

    result = audit_provider_attempts(records, expected_attempts=2)

    assert result["passed"] is False
    assert {(failure.get("span_id"), failure["check"]) for failure in result["failures"]} >= {
        ("duplicate", "one_terminal"),
        ("missing", "one_terminal"),
    }


def test_audit_provider_attempts_uses_latest_cumulative_metric_snapshot() -> None:
    records = [*_attempt("aa"), *_metrics(count=7, timestamp=1), *_metrics(count=1, timestamp=2)]

    result = audit_provider_attempts(records, expected_attempts=1)

    assert result["passed"] is True


def test_audit_provider_attempts_allows_existing_extra_metric_attributes() -> None:
    metrics = _metrics()
    for metric in metrics:
        metric["attributes"]["iac_code.channel"] = "unknown"

    result = audit_provider_attempts([*_attempt("aa"), *metrics], expected_attempts=1)

    assert result["passed"] is True


def test_audit_provider_attempts_can_exclude_sigkill_incomplete_attempt() -> None:
    records = [*_attempt("killed", terminal_count=0), *_attempt("restarted"), *_metrics()]

    result = audit_provider_attempts(
        records,
        expected_attempts=1,
        ignored_incomplete_span_ids={"killed"},
    )

    assert result["passed"] is True
    assert result["ignored_incomplete_span_ids"] == ["killed"]
