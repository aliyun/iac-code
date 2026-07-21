from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from iac_code.services.permissions.audit import _sanitize_operation_metadata
from iac_code.services.telemetry import set_client
from iac_code.services.telemetry.client import TelemetryClient
from iac_code.services.telemetry.content_serializer import serialize_tool_arguments, serialize_tool_result
from iac_code.services.telemetry.events import EventEmitter
from iac_code.services.telemetry.metrics import METRIC_NAMES
from iac_code.services.telemetry.names import Events, GenAiAttr, Metrics
from iac_code.services.telemetry.sink import AnalyticsSink
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.tools.cloud.aliyun.openmeta import ProductMetadata
from iac_code.tools.cloud.aliyun.product_resolver import ProductResolution
from iac_code.tools.cloud.aliyun.runtime import (
    create_aliyun_runtime_services,
    emit_aliyun_api_called,
    emit_aliyun_api_contract_error,
    emit_aliyun_api_doc,
    emit_aliyun_endpoint_resolution,
    emit_aliyun_openmeta_cache,
    emit_aliyun_openmeta_request,
    emit_aliyun_product_resolution,
)
from iac_code.tools.cloud.registry import ALIYUN_TOOL_NAMES
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor


class _CaptureClient:
    def __init__(self) -> None:
        self.events = []
        self.metrics = []

    def log_event(self, name, metadata=None) -> None:
        self.events.append((name, metadata or {}))

    def add_metric(self, name, value, attributes=None) -> None:
        self.metrics.append((name, value, attributes or {}))


@pytest.fixture
def capture_client():
    client = _CaptureClient()
    set_client(client)
    try:
        yield client
    finally:
        set_client(None)


def test_aliyun_metric_names_are_registered_counters() -> None:
    assert {
        Metrics.ALIYUN_OPENMETA_REQUEST_COUNT,
        Metrics.ALIYUN_OPENMETA_CACHE_COUNT,
        Metrics.ALIYUN_API_DOC_COUNT,
        Metrics.ALIYUN_ENDPOINT_RESOLUTION_COUNT,
        Metrics.ALIYUN_API_CONTRACT_ERROR_COUNT,
    }.issubset(METRIC_NAMES)


def test_aliyun_metric_emitters_use_only_finite_labels(capture_client) -> None:
    emit_aliyun_openmeta_request("success")
    emit_aliyun_openmeta_cache("memory_fresh")
    emit_aliyun_api_doc("summary", "not_found")
    emit_aliyun_endpoint_resolution("catalog_global")
    emit_aliyun_api_contract_error("security")

    assert capture_client.metrics == [
        (Metrics.ALIYUN_OPENMETA_REQUEST_COUNT, 1, {"outcome": "success"}),
        (Metrics.ALIYUN_OPENMETA_CACHE_COUNT, 1, {"status": "memory_fresh"}),
        (Metrics.ALIYUN_API_DOC_COUNT, 1, {"detail": "summary", "outcome": "not_found"}),
        (Metrics.ALIYUN_ENDPOINT_RESOLUTION_COUNT, 1, {"source": "catalog_global"}),
        (Metrics.ALIYUN_API_CONTRACT_ERROR_COUNT, 1, {"stage": "security"}),
    ]

    for call in (
        lambda: emit_aliyun_openmeta_request("bucket-secret"),
        lambda: emit_aliyun_openmeta_cache("host.example.com"),
        lambda: emit_aliyun_api_doc("business-value", "success"),
        lambda: emit_aliyun_endpoint_resolution("cn-hangzhou"),
        lambda: emit_aliyun_api_contract_error("AccessKeyId"),
    ):
        with pytest.raises(ValueError, match="invalid_aliyun_telemetry_label"):
            call()
    assert len(capture_client.metrics) == 5


@pytest.mark.parametrize("source", ["explicit", "override_pattern"])
def test_aliyun_endpoint_metric_accepts_new_controlled_sources(capture_client, source: str) -> None:
    emit_aliyun_endpoint_resolution(source)

    assert capture_client.metrics == [(Metrics.ALIYUN_ENDPOINT_RESOLUTION_COUNT, 1, {"source": source})]


def test_aliyun_api_event_has_exact_detailed_private_fields(capture_client) -> None:
    emit_aliyun_api_called(
        metadata_source="cache",
        api_style="ROA",
        http_method="GET",
        transport="acs3_streaming",
        signature_scheme="acs3",
        endpoint_source="location",
        host_template_applied=True,
        contract_override_used=False,
        openmeta_cache_status="disk_fresh",
        outcome="success",
    )

    assert capture_client.events == [
        (
            Events.ALIYUN_API_CALLED,
            {
                "metadata_source": "cache",
                "api_style": "ROA",
                "http_method": "GET",
                "transport": "acs3_streaming",
                "signature_scheme": "acs3",
                "endpoint_source": "location",
                "host_template_applied": True,
                "contract_override_used": False,
                "openmeta_cache_status": "disk_fresh",
                "outcome": "success",
            },
        )
    ]
    serialized = json.dumps(capture_client.events)
    assert "bucket-secret" not in serialized
    assert "host.example.com" not in serialized
    assert "business-value" not in serialized


def test_aliyun_api_event_accepts_reviewed_acs1_labels(capture_client) -> None:
    emit_aliyun_api_called(
        metadata_source="fresh",
        api_style="RPC",
        http_method="POST",
        transport="acs1",
        signature_scheme="acs1",
        endpoint_source="catalog_global",
        host_template_applied=False,
        contract_override_used=True,
        openmeta_cache_status="remote",
        outcome="success",
    )

    assert capture_client.events[0][1]["transport"] == "acs1"
    assert capture_client.events[0][1]["signature_scheme"] == "acs1"


def test_product_resolution_event_has_exact_sanitized_fields(capture_client) -> None:
    emit_aliyun_product_resolution(
        ProductResolution(
            requested_product=" Chatbo ",
            normalized_product="Chatbo",
            metadata=ProductMetadata("Chatbot", "2022-04-08", ("2022-04-08",), None),
            strategy="single_edit",
            confidence="medium",
            source="fresh",
            cache_status="memory_fresh",
        )
    )

    assert capture_client.events == [
        (
            Events.ALIYUN_PRODUCT_RESOLVED,
            {
                "requested_product": " Chatbo ",
                "canonical_product": "Chatbot",
                "match_strategy": "single_edit",
                "confidence": "medium",
                "outcome": "matched",
            },
        )
    ]


def test_product_resolution_event_contract_rejects_unknown_fields_types_and_sensitive_values(monkeypatch) -> None:
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
    emitter = MagicMock(spec=EventEmitter)
    sink = AnalyticsSink(emitter)
    sink.activate()
    valid = {
        "requested_product": "dy0msapi",
        "canonical_product": "",
        "match_strategy": "single_edit_ambiguous",
        "confidence": "none",
        "outcome": "not_found",
    }

    sink.log_event(Events.ALIYUN_PRODUCT_RESOLVED, valid)
    assert emitter.emit.call_count == 1

    malformed = [
        {**valid, "business_parameter": "secret"},
        {**valid, "requested_product": "unsafe/value"},
        {**valid, "canonical_product": "unsafe/value"},
        {**valid, "match_strategy": []},
        {**valid, "confidence": {}},
        {**valid, "outcome": []},
    ]
    for metadata in malformed:
        with pytest.raises(ValueError, match="invalid_aliyun_telemetry_event"):
            sink.log_event(Events.ALIYUN_PRODUCT_RESOLVED, metadata)
    assert emitter.emit.call_count == 1


def test_product_resolution_event_bounds_pathological_ascii_whitespace(capture_client) -> None:
    emit_aliyun_product_resolution(
        ProductResolution(
            requested_product=" " * 200 + "Chatbot",
            normalized_product="Chatbot",
            metadata=ProductMetadata("Chatbot", "2022-04-08", ("2022-04-08",), None),
            strategy="trimmed_exact",
            confidence="high",
        )
    )

    assert capture_client.events[0][1]["requested_product"] == "Chatbot"

    emit_aliyun_product_resolution(
        ProductResolution(
            requested_product="unsafe/value",
            normalized_product="unsafe/value",
            metadata=None,
            strategy="unavailable",
            confidence="none",
            error="protocol_error",
        )
    )
    assert capture_client.events[1][1]["requested_product"] == "invalid"


def test_real_event_sink_reserves_aliyun_api_called_for_exact_finite_contract(monkeypatch) -> None:
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
    emitter = MagicMock(spec=EventEmitter)
    sink = AnalyticsSink(emitter)
    sink.activate()
    set_client(TelemetryClient(sink=sink))
    try:
        emit_aliyun_api_called(
            metadata_source="fresh",
            api_style="RPC",
            http_method="POST",
            transport="tea",
            signature_scheme="acs3",
            endpoint_source="catalog_region",
            host_template_applied=False,
            contract_override_used=False,
            openmeta_cache_status="memory_fresh",
            outcome="pre_connect_failure",
        )
        _, valid = emitter.emit.call_args.args
        assert set(valid) == {
            "metadata_source",
            "api_style",
            "http_method",
            "transport",
            "signature_scheme",
            "endpoint_source",
            "host_template_applied",
            "contract_override_used",
            "openmeta_cache_status",
            "outcome",
        }

        with pytest.raises(ValueError, match="invalid_aliyun_telemetry_event"):
            sink.log_event(Events.ALIYUN_API_CALLED, {"api_service": "bucket-secret"})
        invalid = dict(valid)
        invalid["metadata_source"] = "bucket-secret"
        with pytest.raises(ValueError, match="invalid_aliyun_telemetry_event"):
            sink.log_event(Events.ALIYUN_API_CALLED, invalid)
        invalid["metadata_source"] = []
        with pytest.raises(ValueError, match="invalid_aliyun_telemetry_event"):
            sink.log_event(Events.ALIYUN_API_CALLED, invalid)
        invalid = dict(valid)
        invalid["outcome"] = "bucket-secret"
        with pytest.raises(ValueError, match="invalid_aliyun_telemetry_event"):
            sink.log_event(Events.ALIYUN_API_CALLED, invalid)

        sink.log_event(Events.ALIYUN_API_LEGACY_CALLED, {"outcome": "success"})
        for malformed_outcome in ([], {}):
            with pytest.raises(ValueError, match="invalid_aliyun_telemetry_event"):
                sink.log_event(Events.ALIYUN_API_LEGACY_CALLED, {"outcome": malformed_outcome})
        with pytest.raises(ValueError, match="invalid_aliyun_telemetry_event"):
            sink.log_event(
                Events.ALIYUN_API_LEGACY_CALLED,
                {"outcome": "success", "api_service": "bucket-secret"},
            )
        assert emitter.emit.call_count == 2
    finally:
        set_client(None)


@pytest.mark.asyncio
async def test_isolated_runtime_emits_real_openmeta_request_cache_and_api_doc_outcomes(
    tmp_path: Path,
    capture_client,
) -> None:
    fixture_path = (
        Path(__file__).parents[2]
        / "tools"
        / "cloud"
        / "aliyun"
        / "fixtures"
        / "openmeta"
        / "ecs_describe_instances.json"
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    services = create_aliyun_runtime_services(
        cache_dir=tmp_path,
        openmeta_transport=httpx.MockTransport(handler),
    )
    try:

        async def load_api_doc_metadata():
            return await services.openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")

        remote = await services.run_api_doc_operation(detail="summary", operation=load_api_doc_metadata)
        memory = await services.openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances")
    finally:
        await services.aclose()

    assert remote.value is not None
    assert memory.value is not None
    assert len(requests) == 1
    assert capture_client.metrics == [
        (Metrics.ALIYUN_OPENMETA_REQUEST_COUNT, 1, {"outcome": "success"}),
        (Metrics.ALIYUN_OPENMETA_CACHE_COUNT, 1, {"status": "remote"}),
        (Metrics.ALIYUN_API_DOC_COUNT, 1, {"detail": "summary", "outcome": "success"}),
        (Metrics.ALIYUN_OPENMETA_CACHE_COUNT, 1, {"status": "memory_fresh"}),
    ]


@pytest.mark.asyncio
async def test_isolated_runtime_classifies_normalization_failure_as_openmeta_protocol_error(
    tmp_path: Path,
    capture_client,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"product": "Ecs"})

    services = create_aliyun_runtime_services(
        cache_dir=tmp_path,
        openmeta_transport=httpx.MockTransport(handler),
    )
    try:
        result = await services.run_api_doc_operation(
            detail="full",
            operation=lambda: services.openmeta.get_api("Ecs", "2014-05-26", "DescribeInstances"),
        )
    finally:
        await services.aclose()

    assert result.value is None
    assert result.error == "protocol_error"
    assert capture_client.metrics == [
        (Metrics.ALIYUN_OPENMETA_REQUEST_COUNT, 1, {"outcome": "protocol_error"}),
        (Metrics.ALIYUN_OPENMETA_CACHE_COUNT, 1, {"status": "miss"}),
        (Metrics.ALIYUN_API_DOC_COUNT, 1, {"detail": "full", "outcome": "protocol_error"}),
    ]


@pytest.mark.asyncio
async def test_api_doc_runtime_boundary_rejects_invalid_detail_before_operation(
    tmp_path: Path,
    capture_client,
) -> None:
    called = False

    async def forbidden_operation():
        nonlocal called
        called = True

    services = create_aliyun_runtime_services(cache_dir=tmp_path)
    try:
        with pytest.raises(ValueError, match="invalid_api_doc_detail"):
            await services.run_api_doc_operation(
                detail="business-value",
                operation=forbidden_operation,
            )
    finally:
        await services.aclose()

    assert called is False
    assert capture_client.metrics == [
        (Metrics.ALIYUN_API_DOC_COUNT, 1, {"detail": "summary", "outcome": "invalid_input"})
    ]


def test_aliyun_content_serializers_keep_only_protocol_and_presence() -> None:
    arguments = json.loads(
        serialize_tool_arguments(
            {
                "product": "oss",
                "version": "2019-05-17",
                "action": "GetObject",
                "region_id": "cn-secret-region",
                "style": "ROA",
                "method": "GET",
                "pathname": "/private/business/path",
                "params": {"bucket": "bucket-secret", "Key": "business-value"},
                "body_file": "/private/payload.bin",
            },
            tool_name="aliyun_api",
        )
    )
    assert arguments == {
        "style": "ROA",
        "method": "GET",
        "body_source": "body_file",
        "product_present": True,
        "version_present": True,
        "action_present": True,
        "region_id_present": True,
        "pathname_present": True,
        "params_present": True,
        "body_present": False,
        "body_file_present": True,
    }

    result = ToolResult(
        content=json.dumps(
            {
                "status": 200,
                "headers": {"host": "host.example.com"},
                "body": {"bucket": "bucket-secret", "value": "business-value"},
                "content_type": "application/json",
                "content_encoding": None,
                "size": 99,
                "artifact_path": "/private/artifact.bin",
            }
        ),
        metadata={"artifacts": [{"path": "/private/artifact.bin"}]},
    )
    captured_result = json.loads(serialize_tool_result(result, tool_name="aliyun_api"))
    assert captured_result == {
        "is_error": False,
        "status": 200,
        "status_class": "2xx",
        "headers_present": True,
        "body_present": True,
        "content_type_present": True,
        "content_encoding_present": False,
        "size_present": True,
        "artifact_present": True,
    }
    serialized = json.dumps([arguments, captured_result])
    for forbidden in (
        "bucket-secret",
        "host.example.com",
        "business-value",
        "/private",
        "artifact_path",
        "artifacts",
    ):
        assert forbidden not in serialized


class _TelemetryAliyunTool(Tool):
    def __init__(self, name: str = "aliyun_api") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "test"

    @property
    def input_schema(self) -> dict:
        return {"type": "object"}

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        return ToolResult(
            content=json.dumps(
                {
                    "status": 200,
                    "body": {"bucket": "bucket-secret"},
                    "artifact_path": "/private/artifact.bin",
                }
            ),
            metadata={"artifacts": [{"path": "/private/artifact.bin"}]},
        )


@pytest.mark.asyncio
async def test_real_tool_executor_uses_aliyun_private_content_capture(monkeypatch) -> None:
    spans = []

    class Span:
        def __init__(self, attributes):
            self.attributes = attributes

        def set_attribute(self, key, value) -> None:
            self.attributes[key] = value

    @contextmanager
    def start_span(name, attributes):
        del name
        span = Span(dict(attributes))
        spans.append(span)
        yield span

    monkeypatch.setattr("iac_code.tools.tool_executor.should_capture_content_on_span", lambda: True)
    monkeypatch.setattr("iac_code.tools.tool_executor.start_span", start_span)
    monkeypatch.setattr("iac_code.tools.tool_executor.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.tools.tool_executor.add_metric", lambda *args, **kwargs: None)
    registry = ToolRegistry()
    registry.register(_TelemetryAliyunTool())
    executor = ToolExecutor(registry)
    tool_input = {
        "product": "oss",
        "action": "GetObject",
        "region_id": "cn-secret-region",
        "params": {"bucket": "bucket-secret"},
    }

    result = await executor._validate_and_execute(
        ToolCallRequest("call", "aliyun_api", tool_input),
        ToolContext(),
    )

    assert result.is_error is False
    captured = spans[0].attributes
    serialized = captured[GenAiAttr.TOOL_CALL_ARGUMENTS] + captured[GenAiAttr.TOOL_CALL_RESULT]
    assert "bucket-secret" not in serialized
    assert "cn-secret-region" not in serialized
    assert "/private/artifact.bin" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ALIYUN_TOOL_NAMES)
async def test_real_tool_executor_sanitizes_every_registered_aliyun_tool_group(
    monkeypatch,
    tool_name: str,
) -> None:
    spans = []

    class Span:
        def __init__(self, attributes):
            self.attributes = attributes

        def set_attribute(self, key, value) -> None:
            self.attributes[key] = value

    @contextmanager
    def start_span(name, attributes):
        del name
        span = Span(dict(attributes))
        spans.append(span)
        yield span

    monkeypatch.setattr("iac_code.tools.tool_executor.should_capture_content_on_span", lambda: True)
    monkeypatch.setattr("iac_code.tools.tool_executor.start_span", start_span)
    monkeypatch.setattr("iac_code.tools.tool_executor.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.tools.tool_executor.add_metric", lambda *args, **kwargs: None)
    registry = ToolRegistry()
    registry.register(_TelemetryAliyunTool(tool_name))
    executor = ToolExecutor(registry)
    tool_input = {
        "product": "oss-private-product",
        "action": "PrivateBusinessAction",
        "version": "private-version",
        "keywords": "private search words",
        "detail": "full",
        "region_id": "cn-private-region",
        "bucket": "private-bucket",
        "pathname": "/private/business/path",
        "template_url": "/private/template.yaml",
        "body_file": "/private/payload.bin",
        "params": {
            "bucket": "private-bucket",
            "Key": "business-value",
            "Metadata": {"owner": "private-owner"},
        },
        "metadata": {"business": "private-metadata"},
    }

    await executor._validate_and_execute(
        ToolCallRequest("call", tool_name, tool_input),
        ToolContext(),
    )

    captured = spans[0].attributes
    serialized = captured[GenAiAttr.TOOL_CALL_ARGUMENTS] + captured[GenAiAttr.TOOL_CALL_RESULT]
    for forbidden in (
        "oss-private-product",
        "PrivateBusinessAction",
        "private-version",
        "private search words",
        "cn-private-region",
        "private-bucket",
        "/private",
        "business-value",
        "private-owner",
        "private-metadata",
        "artifact_path",
        '"artifacts"',
    ):
        assert forbidden not in serialized, (tool_name, forbidden, serialized)


def test_permission_audit_preserves_only_the_five_approved_aliyun_fields() -> None:
    sanitized = _sanitize_operation_metadata(
        {
            "product": "ecs",
            "action": "DescribeInstances",
            "region": "cn-hangzhou",
            "api_version": "2014-05-26",
            "api_style": "RPC",
            "http_method": "POST",
            "operation_type": "read",
            "metadata_source": "stale_cache",
            "hostname": "host.example.com",
            "bucket": "bucket-secret",
        }
    )

    assert sanitized == {
        "product": "ecs",
        "action": "DescribeInstances",
        "region": "cn-hangzhou",
        "api_version": "2014-05-26",
        "api_style": "RPC",
        "http_method": "POST",
        "operation_type": "read",
        "metadata_source": "stale_cache",
    }
