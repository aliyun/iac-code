from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    AggregationTemporality,
    Histogram,
    HistogramDataPoint,
    Metric,
    NumberDataPoint,
    Sum,
    Summary,
    SummaryDataPoint,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from scripts.observability.local_observe.otlp_decode import decode_logs, decode_metrics, decode_traces


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def test_decode_trace_span_preserves_ids_and_attributes():
    req = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=[_kv("service.name", "iac-code")]),
                scope_spans=[
                    ScopeSpans(
                        spans=[
                            Span(
                                trace_id=bytes.fromhex("01" * 16),
                                span_id=bytes.fromhex("02" * 8),
                                parent_span_id=bytes.fromhex("03" * 8),
                                name="iac.pipeline.step",
                                start_time_unix_nano=100,
                                end_time_unix_nano=250,
                                attributes=[_kv("step_id", "template_generating"), _kv("step_attempt", "2")],
                            )
                        ]
                    )
                ],
            )
        ]
    )

    records = decode_traces(req.SerializeToString())

    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "span"
    assert record["name"] == "iac.pipeline.step"
    assert record["trace_id"] == "01010101010101010101010101010101"
    assert record["span_id"] == "0202020202020202"
    assert record["parent_span_id"] == "0303030303030303"
    assert record["resource"]["service.name"] == "iac-code"
    assert record["attributes"]["step_id"] == "template_generating"
    assert record["attributes"]["step_attempt"] == "2"
    assert record["duration_ms"] == 0.00015
    assert "resourceSpans" in record["raw"]


def test_decode_log_uses_string_body_as_name():
    req = ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(
                resource=Resource(attributes=[_kv("service.name", "iac-code")]),
                scope_logs=[
                    ScopeLogs(
                        log_records=[
                            LogRecord(
                                time_unix_nano=300,
                                body=AnyValue(string_value="iac.pipeline.selection.made"),
                                attributes=[_kv("session_id", "sess_1"), _kv("selected_index", "1")],
                            )
                        ]
                    )
                ],
            )
        ]
    )

    records = decode_logs(req.SerializeToString())

    assert len(records) == 1
    assert records[0]["kind"] == "log"
    assert records[0]["name"] == "iac.pipeline.selection.made"
    assert records[0]["attributes"]["session_id"] == "sess_1"
    assert records[0]["attributes"]["selected_index"] == "1"


def test_decode_metric_number_point():
    req = ExportMetricsServiceRequest()
    metric = Metric(
        name="iac.pipeline.step.duration",
        sum=Sum(
            aggregation_temporality=AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE,
            data_points=[
                NumberDataPoint(
                    time_unix_nano=500,
                    as_double=2410.5,
                    attributes=[_kv("step_id", "template_generating"), _kv("step_attempt", "2")],
                )
            ],
        ),
    )
    req.resource_metrics.add().scope_metrics.add().metrics.append(metric)

    records = decode_metrics(req.SerializeToString())

    assert len(records) == 1
    assert records[0]["kind"] == "metric"
    assert records[0]["name"] == "iac.pipeline.step.duration"
    assert records[0]["value"] == 2410.5
    assert records[0]["aggregation_temporality"] == AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
    assert records[0]["attributes"]["step_attempt"] == "2"


def test_decode_metric_histogram_point_uses_sum_value():
    req = ExportMetricsServiceRequest()
    metric = Metric(
        name="iac.pipeline.step.duration",
        histogram=Histogram(
            aggregation_temporality=AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE,
            data_points=[
                HistogramDataPoint(
                    time_unix_nano=700,
                    count=3,
                    sum=2410.5,
                    bucket_counts=[1, 2],
                    explicit_bounds=[1000.0],
                    attributes=[_kv("step_id", "template_generating"), _kv("step_attempt", "2")],
                )
            ],
        ),
    )
    req.resource_metrics.add().scope_metrics.add().metrics.append(metric)

    records = decode_metrics(req.SerializeToString())

    assert len(records) == 1
    assert records[0]["kind"] == "metric"
    assert records[0]["metric_type"] == "histogram"
    assert records[0]["name"] == "iac.pipeline.step.duration"
    assert records[0]["value"] == 2410.5
    assert records[0]["attributes"]["step_attempt"] == "2"


def test_decode_metric_summary_point_uses_sum_value():
    req = ExportMetricsServiceRequest()
    metric = Metric(
        name="iac.pipeline.step.duration.summary",
        summary=Summary(
            data_points=[
                SummaryDataPoint(
                    time_unix_nano=900,
                    count=3,
                    sum=2410.5,
                    attributes=[_kv("step_id", "template_generating"), _kv("step_attempt", "2")],
                )
            ]
        ),
    )
    req.resource_metrics.add().scope_metrics.add().metrics.append(metric)

    records = decode_metrics(req.SerializeToString())

    assert len(records) == 1
    assert records[0]["kind"] == "metric"
    assert records[0]["metric_type"] == "summary"
    assert records[0]["name"] == "iac.pipeline.step.duration.summary"
    assert records[0]["value"] == 2410.5
