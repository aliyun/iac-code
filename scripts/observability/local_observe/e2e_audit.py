from __future__ import annotations

import json
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from scripts.observability.local_observe.records import Record
from scripts.observability.local_observe.server import LocalObserveServer
from scripts.observability.local_observe.store import ObserveStore

_STARTED = "iac.api.request.started"
_TERMINALS = frozenset({"iac.api.request.succeeded", "iac.api.request.failed"})
_REQUEST_COUNT = "iac.api.request.count"
_REQUEST_DURATION = "iac.api.request.duration"


class ObserveCapture:
    """Run the existing loopback OTLP receiver for one isolated E2E scenario."""

    def __init__(self, output_dir: Path, *, memory_limit: int = 20_000) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.store = ObserveStore(data_dir=self.output_dir / "receiver", memory_limit=memory_limit)
        self.server = LocalObserveServer(("127.0.0.1", 0), store=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, name="e2e-otlp-receiver", daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def env(self) -> dict[str, str]:
        return {
            "IAC_CODE_ENABLE_LOCAL_TELEMETRY": "1",
            "IAC_CODE_TELEMETRY_ENDPOINT": self.endpoint,
        }

    def start(self) -> "ObserveCapture":
        self.thread.start()
        return self

    def wait_for(self, predicate: Callable[[list[Record]], bool], *, timeout: float = 10.0) -> list[Record]:
        deadline = time.monotonic() + timeout
        while True:
            records = self.store.records()
            if predicate(records):
                return records
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for expected OTLP records")
            time.sleep(0.05)

    def stop(self) -> list[Record]:
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        records = self.store.records()
        write_signal_artifacts(records, self.output_dir)
        return records

    def __enter__(self) -> "ObserveCapture":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()


def write_signal_artifacts(records: Iterable[Record], output_dir: Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "log": output_dir / "logs.jsonl",
        "span": output_dir / "spans.jsonl",
        "metric": output_dir / "metrics.jsonl",
    }
    handles = {kind: path.open("w", encoding="utf-8") for kind, path in paths.items()}
    try:
        for record in records:
            handle = handles.get(str(record.get("kind")))
            if handle is None:
                continue
            stable_record = {key: value for key, value in record.items() if key != "raw"}
            handle.write(json.dumps(stable_record, ensure_ascii=False, default=str) + "\n")
    finally:
        for handle in handles.values():
            handle.close()


def audit_provider_attempts(
    records: Iterable[Record],
    *,
    expected_attempts: int | None = None,
    expected_provider: str | None = None,
    expected_model: str | None = None,
    expected_span_attributes: Mapping[str, Any] | None = None,
    ignored_incomplete_span_ids: Iterable[str] = (),
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Audit provider attempts by request span instead of aggregate counts."""

    materialized = list(records)
    ignored = set(ignored_incomplete_span_ids)
    logs = [record for record in materialized if record.get("kind") == "log"]
    spans = [record for record in materialized if record.get("kind") == "span"]
    metrics = [record for record in materialized if record.get("kind") == "metric"]

    attempt_logs: dict[str, list[Record]] = defaultdict(list)
    uncorrelated_logs: list[str] = []
    for record in logs:
        if record.get("name") not in {_STARTED, *_TERMINALS}:
            continue
        span_id = str(record.get("span_id") or "")
        if not span_id:
            uncorrelated_logs.append(str(record.get("id") or ""))
            continue
        attempt_logs[span_id].append(record)

    span_records: dict[str, list[Record]] = defaultdict(list)
    for record in spans:
        span_id = str(record.get("span_id") or "")
        if span_id:
            span_records[span_id].append(record)

    failures: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    terminal_counts: Counter[tuple[str, str, str, str]] = Counter()
    started_span_ids = {
        span_id for span_id, grouped in attempt_logs.items() if any(item.get("name") == _STARTED for item in grouped)
    }

    for span_id in sorted(started_span_ids):
        grouped = attempt_logs[span_id]
        started = [item for item in grouped if item.get("name") == _STARTED]
        terminals = [item for item in grouped if item.get("name") in _TERMINALS]
        matching_spans = [
            item for item in span_records.get(span_id, []) if str(item.get("name", "")).startswith("chat ")
        ]
        ignored_incomplete = span_id in ignored and not terminals
        attempt = {
            "span_id": span_id,
            "started_count": len(started),
            "terminal_count": len(terminals),
            "request_span_count": len(matching_spans),
            "ignored_incomplete": ignored_incomplete,
            "terminal": terminals[0].get("name") if len(terminals) == 1 else None,
            "status": (terminals[0].get("attributes") or {}).get("status") if len(terminals) == 1 else None,
        }
        attempts.append(attempt)
        if len(started) != 1:
            failures.append({"span_id": span_id, "check": "one_started", "actual": len(started)})
        if ignored_incomplete:
            continue
        if len(terminals) != 1:
            failures.append({"span_id": span_id, "check": "one_terminal", "actual": len(terminals)})
        if len(matching_spans) != 1:
            failures.append({"span_id": span_id, "check": "one_ended_request_span", "actual": len(matching_spans)})

        for record in [*started, *terminals]:
            attrs = record.get("attributes") or {}
            if expected_provider is not None and attrs.get("provider") != expected_provider:
                failures.append(
                    {
                        "span_id": span_id,
                        "check": "provider",
                        "expected": expected_provider,
                        "actual": attrs.get("provider"),
                    }
                )
            if expected_model is not None and attrs.get("model") != expected_model:
                failures.append(
                    {"span_id": span_id, "check": "model", "expected": expected_model, "actual": attrs.get("model")}
                )
        for record in matching_spans:
            attrs = record.get("attributes") or {}
            for key, expected in (expected_span_attributes or {}).items():
                if attrs.get(key) != expected:
                    failures.append(
                        {
                            "span_id": span_id,
                            "check": f"span_attribute:{key}",
                            "expected": expected,
                            "actual": attrs.get(key),
                        }
                    )
        if len(terminals) == 1:
            attrs = terminals[0].get("attributes") or {}
            terminal_counts[
                (
                    str(attrs.get("provider") or ""),
                    str(attrs.get("model") or ""),
                    str(attrs.get("status") or ""),
                    str(attrs.get("error_type") or ""),
                )
            ] += 1

    orphan_terminals = sorted(
        span_id
        for span_id, grouped in attempt_logs.items()
        if any(item.get("name") in _TERMINALS for item in grouped) and span_id not in started_span_ids
    )
    if orphan_terminals:
        failures.append({"check": "terminal_without_started", "span_ids": orphan_terminals})
    if uncorrelated_logs:
        failures.append({"check": "logs_have_span_id", "record_ids": uncorrelated_logs})
    completed_attempt_count = sum(not item["ignored_incomplete"] for item in attempts)
    if expected_attempts is not None and completed_attempt_count != expected_attempts:
        failures.append(
            {"check": "expected_attempt_count", "expected": expected_attempts, "actual": completed_attempt_count}
        )
    if expected_attempts is None and completed_attempt_count == 0:
        failures.append({"check": "at_least_one_complete_attempt", "actual": 0})

    for (provider, model, status, error_type), expected_count in terminal_counts.items():
        count_attrs: dict[str, Any] = {"provider": provider, "model": model, "status": status}
        if error_type:
            count_attrs["error_type"] = error_type
        count_value = _cumulative_metric_value_across_resources(
            metrics,
            name=_REQUEST_COUNT,
            required_attributes=count_attrs,
        )
        if not isinstance(count_value, int | float) or count_value < expected_count:
            failures.append(
                {
                    "check": "request_count_metric",
                    "attributes": count_attrs,
                    "expected_at_least": expected_count,
                    "actual": count_value,
                }
            )
        duration_attrs = {"provider": provider, "model": model}
        duration_value = _latest_metric_value(
            metrics,
            name=_REQUEST_DURATION,
            required_attributes=duration_attrs,
        )
        if not isinstance(duration_value, int | float) or duration_value < 0:
            failures.append(
                {"check": "request_duration_metric", "attributes": duration_attrs, "actual": duration_value}
            )

    result = {
        "passed": not failures,
        "attempt_count": len(attempts),
        "completed_attempt_count": completed_attempt_count,
        "ignored_incomplete_span_ids": sorted(ignored),
        "attempts": attempts,
        "failures": failures,
    }
    if output_path is not None:
        Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _latest_metric_value(
    metrics: Iterable[Record],
    *,
    name: str,
    required_attributes: Mapping[str, Any],
) -> Any:
    latest: tuple[int, Any] | None = None
    for record in metrics:
        if record.get("name") != name:
            continue
        attributes = record.get("attributes") or {}
        if any(attributes.get(key) != value for key, value in required_attributes.items()):
            continue
        timestamp = int(record.get("timestamp_unix_nano") or 0)
        if latest is None or timestamp >= latest[0]:
            latest = (timestamp, record.get("value"))
    return latest[1] if latest is not None else None


def _cumulative_metric_value_across_resources(
    metrics: Iterable[Record],
    *,
    name: str,
    required_attributes: Mapping[str, Any],
) -> Any:
    """Sum the latest cumulative counter from each telemetry resource.

    A Web recovery scenario restarts the server several times. Each process has
    its own cumulative counter starting at zero, while all processes export to
    the same E2E receiver. Taking one globally latest sample undercounts every
    completed request from earlier server epochs.
    """

    latest_by_resource: dict[str, tuple[int, Any]] = {}
    for record in metrics:
        if record.get("name") != name:
            continue
        attributes = record.get("attributes") or {}
        if any(attributes.get(key) != value for key, value in required_attributes.items()):
            continue
        resource = record.get("resource") or {}
        resource_key = json.dumps(resource, sort_keys=True, ensure_ascii=False, default=str)
        timestamp = int(record.get("timestamp_unix_nano") or 0)
        previous = latest_by_resource.get(resource_key)
        if previous is None or timestamp >= previous[0]:
            latest_by_resource[resource_key] = (timestamp, record.get("value"))
    values = [value for _, value in latest_by_resource.values()]
    if not values or not all(isinstance(value, int | float) for value in values):
        return None
    return sum(values)
