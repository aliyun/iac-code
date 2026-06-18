from __future__ import annotations

from typing import Any

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from scripts.observability.local_observe.records import Record, new_record


class DecodeError(ValueError):
    """Raised when an OTLP request body cannot be decoded."""


def _message_to_dict(message: Any) -> dict[str, Any]:
    return MessageToDict(message, preserving_proto_field_name=False)


def _hex(value: bytes) -> str:
    return value.hex() if value else ""


def _any_value(value: AnyValue) -> Any:
    field = value.WhichOneof("value")
    if field is None:
        return None
    if field == "string_value":
        return value.string_value
    if field == "bool_value":
        return value.bool_value
    if field == "int_value":
        return value.int_value
    if field == "double_value":
        return value.double_value
    if field == "bytes_value":
        return value.bytes_value.hex()
    if field == "array_value":
        return [_any_value(item) for item in value.array_value.values]
    if field == "kvlist_value":
        return {item.key: _any_value(item.value) for item in value.kvlist_value.values}
    return str(getattr(value, field))


def _attrs(values: list[KeyValue]) -> dict[str, Any]:
    return {item.key: _any_value(item.value) for item in values}


def _resource(resource: Resource) -> dict[str, Any]:
    return _attrs(list(resource.attributes))


def _scope(scope: InstrumentationScope) -> dict[str, Any]:
    return {"name": scope.name, "version": scope.version}


def _parse(message: Any, payload: bytes) -> Any:
    try:
        message.ParseFromString(payload)
    except Exception as exc:
        raise DecodeError(str(exc)) from exc
    return message


def decode_traces(payload: bytes) -> list[Record]:
    request = _parse(ExportTraceServiceRequest(), payload)
    raw = _message_to_dict(request)
    records: list[Record] = []
    for resource_spans in request.resource_spans:
        resource = _resource(resource_spans.resource)
        for scope_spans in resource_spans.scope_spans:
            scope = _scope(scope_spans.scope)
            for span in scope_spans.spans:
                duration_ms = None
                if span.start_time_unix_nano and span.end_time_unix_nano:
                    duration_ms = (span.end_time_unix_nano - span.start_time_unix_nano) / 1_000_000
                records.append(
                    new_record(
                        "span",
                        resource=resource,
                        scope=scope,
                        name=span.name,
                        timestamp_unix_nano=span.start_time_unix_nano,
                        attributes=_attrs(list(span.attributes)),
                        trace_id=_hex(span.trace_id),
                        span_id=_hex(span.span_id),
                        parent_span_id=_hex(span.parent_span_id),
                        duration_ms=duration_ms,
                        raw=raw,
                    )
                )
    return records


def decode_logs(payload: bytes) -> list[Record]:
    request = _parse(ExportLogsServiceRequest(), payload)
    raw = _message_to_dict(request)
    records: list[Record] = []
    for resource_logs in request.resource_logs:
        resource = _resource(resource_logs.resource)
        for scope_logs in resource_logs.scope_logs:
            scope = _scope(scope_logs.scope)
            for log_record in scope_logs.log_records:
                body = _any_value(log_record.body)
                name = body if isinstance(body, str) else "otlp.log"
                records.append(
                    new_record(
                        "log",
                        resource=resource,
                        scope=scope,
                        name=name,
                        timestamp_unix_nano=log_record.time_unix_nano,
                        attributes=_attrs(list(log_record.attributes)),
                        trace_id=_hex(log_record.trace_id),
                        span_id=_hex(log_record.span_id),
                        raw=raw,
                    )
                )
    return records


def _point_value(point: Any, metric_type: str) -> Any:
    if metric_type in {"sum", "gauge"}:
        if point.HasField("as_double"):
            return point.as_double
        if point.HasField("as_int"):
            return point.as_int
        return None
    if metric_type == "summary":
        return point.sum
    if metric_type in {"histogram", "exponential_histogram"}:
        return point.sum if point.HasField("sum") else None
    return None


def _metric_points(metric: Any) -> list[tuple[int, Any, dict[str, Any], str, int | None]]:
    field = metric.WhichOneof("data")
    if field is None:
        return []
    data = getattr(metric, field)
    aggregation_temporality = getattr(data, "aggregation_temporality", None)
    points = []
    for point in getattr(data, "data_points", []):
        value = _point_value(point, field)
        points.append((point.time_unix_nano, value, _attrs(list(point.attributes)), field, aggregation_temporality))
    return points


def decode_metrics(payload: bytes) -> list[Record]:
    request = _parse(ExportMetricsServiceRequest(), payload)
    raw = _message_to_dict(request)
    records: list[Record] = []
    for resource_metrics in request.resource_metrics:
        resource = _resource(resource_metrics.resource)
        for scope_metrics in resource_metrics.scope_metrics:
            scope = _scope(scope_metrics.scope)
            for metric in scope_metrics.metrics:
                for timestamp, value, attributes, data_type, aggregation_temporality in _metric_points(metric):
                    records.append(
                        new_record(
                            "metric",
                            resource=resource,
                            scope=scope,
                            name=metric.name,
                            timestamp_unix_nano=timestamp,
                            attributes=attributes,
                            value=value,
                            metric_type=data_type,
                            aggregation_temporality=aggregation_temporality,
                            raw=raw,
                        )
                    )
    return records
